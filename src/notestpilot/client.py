"""Synchronous scripting API for the opt-in protocol-two runtime.

Commands are never retried automatically: a transport failure can happen after
the game applies a command. Query fresh state before deciding what to do next.

For an already running disposable server, a user script can use::

    session = Session()  # reads port/token from environment
    pilot = session.create("alpha", "Primeva")
    pilot = pilot.spawn(position=(1000, 1000, 1000), velocity=(0, 0, 100))
    pilot.goto((2000, 1000, 1000), speed=100)
    # Inspect pilot.status(), branch on session.events(), issue a new task.
    pilot.cancel()

Coordinates and valid target IDs must come from the chosen mission's state.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import binascii
import json
import math
import os
import socket
import time
import uuid
from collections.abc import Sequence
from typing import Any


class ProtocolError(RuntimeError):
    """Malformed or mismatched runtime response."""


class CommandError(RuntimeError):
    """The runtime rejected a command."""


class TransportError(RuntimeError):
    """Transport failed; a mutation's outcome can be unknown."""


class StaleActor(RuntimeError):
    """A handle belongs to a replaced player, aircraft or server instance."""


class CreationUncertain(RuntimeError):
    """Creation response failed; exact correlation permits inspection, not replay."""

    def __init__(self, name: str, creation_id: str, instance: str, cause: Exception):
        super().__init__(f"Creation outcome uncertain for {name!r}; inspect its exact creation correlation before cleanup")
        self.name = name
        self.creation_id = creation_id
        self.instance = instance
        self.cause = cause


class RequestJoinUncertain(RuntimeError):
    """A single native request-player join has an uncertain outcome, never replay."""

    def __init__(self, name: str, creation_id: str, instance: str, cause: Exception):
        super().__init__(f"Request-player join uncertain for {name!r}; inspect its exact correlation for cleanup")
        self.name, self.creation_id, self.instance, self.cause = name, creation_id, instance, cause


class EventOverflow(RuntimeError):
    """Events were lost before the requested cursor; assertions lack evidence."""


class WaitTimeout(TimeoutError):
    """A read-only condition missed its deadline; last_state preserves evidence."""

    def __init__(self, description: str, last_state: dict | None):
        super().__init__(f"Timed out waiting for {description}")
        self.description = description
        self.last_state = last_state


class RuntimeFailure(RuntimeError):
    """Observed runtime/task failure, with the snapshot that established it."""

    def __init__(self, message: str, state: dict):
        super().__init__(message)
        self.state = state


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ProtocolError(f"{name} must be an integer >= {minimum}")
    return value


def _vector(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3 or any(type(v) not in (float, int) or not math.isfinite(v) for v in value):
        raise ValueError("Expected three finite numbers")
    return list(value)


def _number(value: Any, name: str, maximum: float) -> float:
    if type(value) not in (float, int) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError(f"{name} must be finite and in (0, {maximum}]")
    return value


def _invalid_constant(value):
    raise ValueError(f"Nonfinite JSON constant {value}")


class Session:
    """One loopback runtime session, with server-issued identity binding."""

    def __init__(self, port: int | None = None, token: str | None = None, *,
                 timeout: float = 30, max_response_bytes: int = 4 * 1024 * 1024):
        self.port = int(os.environ["NOTESTPILOT_PORT"]) if port is None else port
        self._token = os.environ["NOTESTPILOT_TOKEN"] if token is None else token
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Invalid loopback port")
        if not isinstance(self._token, str) or len(self._token) < 32:
            raise ValueError("A private runtime token of at least 32 characters is required")
        if type(timeout) not in (float, int) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be finite and positive")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("Response limit must be a positive integer")
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.instance: str | None = None
        self.event_cursor = 0
        self._request_packet_cursors: dict[tuple[str, str], int | None] = {}

    def call(self, command: str, args: dict | None = None, *, timeout: float | None = None) -> Any:
        """Issue exactly one request, with finite total transport time."""
        request_id = uuid.uuid4().hex
        wire = json.dumps({"id": request_id, "token": self._token,
                           "command": command, "args": args or {}}, allow_nan=False).encode("utf-8") + b"\n"
        if len(wire) > 65536:
            raise ValueError("Request exceeds the runtime's 64 KiB limit")
        budget = self.timeout if timeout is None else min(self.timeout, _number(timeout, "timeout", float("inf")))
        deadline = time.monotonic() + budget
        data = bytearray()
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=budget) as peer:
                peer.settimeout(max(0.001, deadline - time.monotonic()))
                peer.sendall(wire)
                while b"\n" not in data:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Response deadline exceeded")
                    peer.settimeout(remaining)
                    chunk = peer.recv(min(65536, self.max_response_bytes + 1 - len(data)))
                    if not chunk:
                        raise ProtocolError("Connection closed before a complete response")
                    data.extend(chunk)
                    if len(data) > self.max_response_bytes:
                        raise ProtocolError("Response exceeds configured limit")
        except OSError as failure:
            raise TransportError(f"{command}: transport failed; query state before retry") from failure
        line, trailing = bytes(data).split(b"\n", 1)
        if trailing.strip():
            raise ProtocolError("Unexpected data after response")
        try:
            result = json.loads(line.decode("utf-8"), parse_constant=_invalid_constant)
        except (UnicodeError, ValueError) as failure:
            raise ProtocolError("Response is not strict UTF-8 JSON") from failure
        if not isinstance(result, dict) or result.get("id") != request_id:
            raise ProtocolError("Response does not match request identity")
        if type(result.get("ok")) is not bool:
            raise ProtocolError("Response requires a boolean ok field")
        if result["ok"] is False:
            if not isinstance(result.get("error"), str):
                raise ProtocolError("Rejected response lacks an error message")
            raise CommandError(result["error"])
        if "result" not in result:
            raise ProtocolError("Successful response lacks result")
        return result["result"]

    def _state(self, state: Any) -> dict:
        if not isinstance(state, dict) or type(state.get("protocol")) is not int or state["protocol"] != 2:
            raise ProtocolError("Expected protocol-two state")
        instance = state.get("instance")
        if not isinstance(instance, str) or not instance:
            raise ProtocolError("Runtime instance identity missing")
        if self.instance is None:
            self.instance = instance
        elif self.instance != instance:
            raise StaleActor("Runtime restarted; open a new Session")
        if not isinstance(state.get("actors"), list):
            raise ProtocolError("Actor registry missing")
        return state

    def status(self, *, timeout: float | None = None) -> dict:
        return self._state(self.call("status", timeout=timeout))

    def wait_for(self, predicate, description: str, *, timeout: float = 30, interval: float = 0.25,
                 allowed_errors: tuple[tuple[str, str], ...] = ()) -> dict:
        """Poll read-only status until predicate(state) is true.

        Any unallowed runtime error or directed task with outcome='failed' raises
        RuntimeFailure before evaluating the predicate. This is a clean-lab
        assertion helper; ordinary status() remains available for inspecting
        expected failures. It never retries commands or resets failed tasks.
        allowed_errors explicitly lists exact (kind, message) pairs. Allowing
        errors requires the complete recent journal (count == len(recent)); a
        truncated journal never establishes clean evidence. The default is strict.
        Predicate exceptions propagate. The predicate should be fast/read-only;
        arbitrary user code cannot be forcibly interrupted by this helper.
        """
        _number(timeout, "timeout", float("inf"))
        _number(interval, "interval", float("inf"))
        if not callable(predicate) or not isinstance(description, str) or not description.strip():
            raise ValueError("wait_for requires a predicate and a description")
        if not isinstance(allowed_errors, tuple) or any(not isinstance(pair, tuple) or len(pair) != 2
                or any(not isinstance(part, str) for part in pair) for pair in allowed_errors):
            raise ValueError("allowed_errors must be a tuple of exact (kind, message) string pairs")
        allowed = set(allowed_errors)
        deadline = time.monotonic() + timeout
        last = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WaitTimeout(description, last)
            try:
                last = self.status(timeout=remaining)
            except TransportError as failure:
                if time.monotonic() >= deadline:
                    raise WaitTimeout(description, last) from failure
                raise
            errors = last.get("errors")
            if not isinstance(errors, dict):
                raise ProtocolError("wait_for requires runtime error telemetry")
            count = _integer(errors.get("count"), "errors.count")
            recent = errors.get("recent")
            if allowed and (not isinstance(recent, list) or count != len(recent)):
                raise RuntimeFailure("Runtime error journal is incomplete", last)
            if count > 0:
                if not allowed:
                    raise RuntimeFailure("Runtime reported errors while waiting for " + description, last)
                if any(not isinstance(row, dict) or not isinstance(row.get("kind"), str)
                       or not isinstance(row.get("message"), str)
                       or (row["kind"], row["message"]) not in allowed for row in recent):
                    raise RuntimeFailure("Runtime reported an unallowed error while waiting for " + description, last)
            for actor in last["actors"]:
                aircraft = actor.get("aircraft") if isinstance(actor, dict) else None
                task = aircraft.get("task") if isinstance(aircraft, dict) else None
                if isinstance(task, dict) and task.get("outcome") == "failed":
                    raise RuntimeFailure(f"Directed task failed for {actor.get('actor')!r}", last)
            satisfied = predicate(last)
            if time.monotonic() >= deadline:
                raise WaitTimeout(description, last)
            if satisfied:
                return last
            time.sleep(min(interval, max(0, deadline - time.monotonic())))

    def units(self, *, faction: str | None = None, name_contains: str | None = None,
              include_disabled: bool = False, limit: int = 256) -> list[dict]:
        """Query native units for explicit inspection before choosing a target.

        Unit metadata does not prove weapon compatibility, native tracking, a hit
        or a kill. attack() still asks the server to validate the selected target.
        """
        if type(limit) is not int or not 1 <= limit <= 256 or type(include_disabled) is not bool:
            raise ValueError("units requires limit 1..256 and boolean include_disabled")
        if faction is not None and not isinstance(faction, str):
            raise ValueError("faction must be a string")
        if name_contains is not None and not isinstance(name_contains, str):
            raise ValueError("name_contains must be a string")
        self.status()
        args = {"instance": self.instance, "includeDisabled": include_disabled, "limit": limit}
        if faction is not None:
            args["faction"] = faction
        if name_contains is not None:
            args["nameContains"] = name_contains
        rows = self.call("units", args)
        if not isinstance(rows, list) or len(rows) > limit:
            raise ProtocolError("Malformed or oversized units response")
        ids = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ProtocolError("Malformed unit row")
            identity = _integer(row.get("id"), "unit.id", 1)
            _integer(row.get("netId"), "unit.netId")
            if identity in ids or type(row.get("disabled")) is not bool:
                raise ProtocolError("Duplicate unit identity or invalid disabled state")
            ids.add(identity)
            if not isinstance(row.get("name"), str) or not isinstance(row.get("kind"), str):
                raise ProtocolError("Unit name/kind missing")
            try:
                _vector(row.get("position"))
            except ValueError as failure:
                raise ProtocolError("Unit position invalid") from failure
        return rows

    def host(self, mission: str = "Escalation") -> dict:
        self.status()
        return self._state(self.call("host", {"mission": mission, "instance": self.instance}))

    def create(self, name: str, faction: str) -> Actor:
        self.status()
        creation_id = uuid.uuid4().hex
        try:
            state = self._state(self.call("actor.create", {"actor": name, "faction": faction,
                                "instance": self.instance, "creationId": creation_id}))
            return self._bind(name, state)
        except (TransportError, ProtocolError) as failure:
            raise CreationUncertain(name, creation_id, self.instance, failure) from failure

    def recover_creation(self, uncertainty: CreationUncertain) -> Actor:
        """Inspect an uncertain creation; only its exact existing record can bind.

        This does not repeat creation or turn the failed operation into a pass.
        Callers can use the returned handle for narrowly owned cleanup.
        """
        if not isinstance(uncertainty, CreationUncertain):
            raise ValueError("Expected a CreationUncertain correlation record")
        state = self.status()
        if state["instance"] != uncertainty.instance:
            raise StaleActor("Creation recovery refused a different runtime instance")
        rows = [row for row in state["actors"] if isinstance(row, dict) and row.get("actor") == uncertainty.name]
        if len(rows) != 1 or rows[0].get("creationId") != uncertainty.creation_id:
            raise StaleActor("Creation recovery found no exact name/correlation match")
        return self._bind(uncertainty.name, state)

    def join_request_player(self, name: str, password: str, *,
                            allowed_errors: tuple[tuple[str, str], ...] = ()) -> RequestPlayer:
        """Join once through native virtual-connection requests, then poll readiness.

        This adapter does not establish socket delivery or client-owned flight.
        An uncertain result permits exact-correlation inspection for cleanup only.
        """
        if not isinstance(name, str) or not name.strip() or len(name) > 64:
            raise ValueError("Request-player name must contain 1..64 characters")
        if not isinstance(password, str):
            raise ValueError("password must be a string")
        if not isinstance(allowed_errors, tuple) or any(not isinstance(pair, tuple) or len(pair) != 2
                or any(not isinstance(part, str) for part in pair) for pair in allowed_errors):
            raise ValueError("allowed_errors must be a tuple of exact (kind, message) string pairs")
        self.status()
        creation_id = uuid.uuid4().hex
        try:
            state = self._state(self.call("request_join", {"name": name, "password": password,
                                "creationId": creation_id, "instance": self.instance, "mode": "player-requests"}))
            def ready(snapshot):
                row = self._request_row(name, snapshot)
                if row.get("creationId") != creation_id:
                    raise ProtocolError("Join correlation changed while awaiting readiness")
                if row.get("state") in ("failed", "disconnected"):
                    raise CommandError("Native request-player join failed")
                return row.get("ready") is True and type(row.get("playerId")) is int and row["playerId"] > 0
            if not ready(state):
                state = self.wait_for(ready, "native request-player readiness", timeout=self.timeout,
                                      allowed_errors=allowed_errors)
            return self._bind_request_player(name, state)
        except (TransportError, ProtocolError, WaitTimeout, RuntimeFailure, StaleActor) as failure:
            raise RequestJoinUncertain(name, creation_id, self.instance, failure) from failure

    def recover_request_player(self, uncertainty: RequestJoinUncertain) -> RequestPlayer:
        """Bind only an exact join receipt, including pending rows, for cleanup."""
        if not isinstance(uncertainty, RequestJoinUncertain):
            raise ValueError("Expected RequestJoinUncertain")
        state = self.status()
        if state["instance"] != uncertainty.instance:
            raise StaleActor("Request join recovery refused a changed runtime")
        row = self._request_row(uncertainty.name, state)
        if row.get("creationId") != uncertainty.creation_id:
            raise StaleActor("Request join recovery found no exact name/correlation match")
        bound = self._bind_request_player(uncertainty.name, state)
        return RequestPlayer(self, bound.name, bound.connection_id, bound.player_id, bound.aircraft_id,
                             bound.instance, tuple(c for c in bound.capabilities if c in ("status", "disconnect")))

    def request_player(self, name: str) -> RequestPlayer:
        """Explicitly bind the current request-player generation, never a mock."""
        return self._bind_request_player(name, self.status())

    def _request_row(self, name: str, state: dict) -> dict:
        registry = state.get("requestPlayers")
        if not isinstance(registry, list):
            raise ProtocolError("Request-player registry missing")
        rows = [row for row in registry if isinstance(row, dict) and row.get("name") == name]
        if len(rows) != 1:
            raise StaleActor("Request player missing or ambiguous")
        row = rows[0]
        if row.get("mode") != "player-requests":
            raise ProtocolError("Request-player mode mismatch")
        return row

    def _bind_request_player(self, name: str, state: dict) -> RequestPlayer:
        row = self._request_row(name, state)
        connection = row.get("connectionId")
        if not isinstance(connection, str) or not connection:
            raise ProtocolError("Request connection identity missing")
        player = row.get("playerId")
        if player is not None:
            _integer(player, "request playerId", 1)
        aircraft = row.get("aircraft")
        if aircraft is not None and not isinstance(aircraft, dict):
            raise ProtocolError("Malformed request aircraft")
        generation = None if aircraft is None else _integer(aircraft.get("id"), "request aircraft.id", 1)
        capabilities = row.get("capabilities")
        ready = row.get("ready")
        if type(ready) is not bool:
            raise ProtocolError("Request-player readiness missing")
        allowed = {"status", "join_faction", "purchase_airframe", "request_spawn", "disconnect"}
        if (not isinstance(capabilities, list) or any(not isinstance(c, str) or c not in allowed for c in capabilities)
                or len(set(capabilities)) != len(capabilities) or "status" not in capabilities):
            raise ProtocolError("Malformed request-player capabilities")
        if (player is None or not ready) and any(c not in ("status", "disconnect") for c in capabilities):
            raise ProtocolError("Pending join has unsafe capabilities")
        return RequestPlayer(self, name, connection, player, generation, state["instance"], tuple(capabilities))

    def quit(self) -> dict:
        """Quit the bound disposable runtime once; never retry uncertain results."""
        self.status()
        result = self.call("quit", {"instance": self.instance})
        if not isinstance(result, dict) or result.get("accepted") is not True:
            raise ProtocolError("Quit did not return accepted:true")
        return result

    def actor(self, name: str) -> Actor:
        """Explicitly bind to the current generation; old handles stay stale."""
        return self._bind(name, self.status())

    def _bind(self, name: str, state: dict) -> Actor:
        rows = [row for row in state["actors"] if isinstance(row, dict) and row.get("actor") == name]
        if len(rows) != 1:
            raise StaleActor(f"Actor {name!r} missing or ambiguous")
        row = rows[0]
        player = _integer(row.get("playerId"), "playerId", 1)
        aircraft = row.get("aircraft")
        if aircraft is not None and not isinstance(aircraft, dict):
            raise ProtocolError("Malformed actor aircraft")
        generation = None if aircraft is None else _integer(aircraft.get("id"), "aircraft.id", 1)
        return Actor(self, name, player, generation, state["instance"])

    def events(self, *, after: int | None = None) -> dict:
        """Read sequenced evidence; lost events raise rather than implying success."""
        self.status()  # Cursor identity is scoped to this server instance.
        cursor = self.event_cursor if after is None else _integer(after, "after")
        batch = self.call("events", {"after": cursor, "instance": self.instance})
        if not isinstance(batch, dict) or not isinstance(batch.get("events"), list):
            raise ProtocolError("Malformed event batch")
        if type(batch.get("overflow")) is not bool:
            raise ProtocolError("Event batch requires overflow indicator")
        if batch["overflow"]:
            raise EventOverflow(f"Runtime lost events after cursor {cursor}")
        if _integer(batch.get("observerErrors"), "observerErrors") != 0:
            raise ProtocolError("Runtime event observer reported errors; evidence is incomplete")
        next_cursor = _integer(batch.get("cursor"), "cursor")
        if next_cursor < cursor:
            raise ProtocolError("Event cursor moved backwards")
        previous = cursor
        for event in batch["events"]:
            if not isinstance(event, dict):
                raise ProtocolError("Malformed event")
            seq = _integer(event.get("seq"), "event.seq", 1)
            if not previous < seq <= next_cursor:
                raise ProtocolError("Event sequence is unordered or outside cursor bounds")
            previous = seq
        if after is None:
            self.event_cursor = next_cursor
        return batch


@dataclass(frozen=True)
class RequestPlayer:
    """Native request capability; no direct spawn pose, server control or firing."""

    session: Session
    name: str
    connection_id: str
    player_id: int | None
    aircraft_id: int | None
    instance: str
    capabilities: tuple[str, ...]
    mode: str = "player-requests"

    def _checked(self, capability: str) -> dict:
        current = self.session.request_player(self.name)
        if self.mode != "player-requests" or (current.mode, current.instance, current.connection_id, current.player_id, current.aircraft_id) != (
                self.mode, self.instance, self.connection_id, self.player_id, self.aircraft_id):
            raise StaleActor("Request-player mode, connection or native generation changed")
        if capability not in self.capabilities or capability not in current.capabilities:
            raise CommandError("Request-player capability unavailable: " + capability)
        return {"name": self.name, "mode": self.mode, "instance": self.instance, "connectionId": self.connection_id,
                "playerId": self.player_id, "aircraftId": self.aircraft_id}

    def _command(self, command: str, capability: str, extra: dict | None = None) -> dict:
        args = self._checked(capability)
        args.update(extra or {})
        return self.session._state(self.session.call(command, args))

    def status(self) -> dict:
        state = self._command("request_status", "status")
        current = self.session._bind_request_player(self.name, state)
        if (current.connection_id, current.player_id, current.aircraft_id) != (self.connection_id, self.player_id, self.aircraft_id):
            raise StaleActor("Request status changed native identity; rebind explicitly")
        return self.session._request_row(self.name, state)

    def packets(self) -> list[dict]:
        """Drain copied native outgoing records once; this consumes diagnostics.

        Sequences start at one per connection, independently of event cursors.
        A gap, malformed batch or lost drain response permanently invalidates
        this Session's packet evidence for that connection. No request is retried.
        Raw packet contents should remain in private diagnostic artifacts.
        """
        key = (self.instance, self.connection_id)
        previous = self.session._request_packet_cursors.get(key, 0)
        if previous is None:
            raise ProtocolError("Request packet journal evidence was lost; this connection cannot be drained again")
        row = self.status()  # Includes exact mode/runtime/player/generation guards.
        if type(row.get("packetGap")) is not bool or row["packetGap"]:
            self.session._request_packet_cursors[key] = None
            raise ProtocolError("Native packet journal reports a gap or lacks complete evidence")
        args = {"name": self.name, "mode": self.mode, "instance": self.instance, "connectionId": self.connection_id,
                "playerId": self.player_id, "aircraftId": self.aircraft_id}
        try:
            batch = self.session.call("request_packets", args)
            if not isinstance(batch, list) or len(batch) > 4096:
                raise ProtocolError("Malformed or oversized request packet batch")
            copied, total = [], 0
            for packet in batch:
                if not isinstance(packet, dict):
                    raise ProtocolError("Malformed request packet record")
                sequence = _integer(packet.get("sequence"), "packet.sequence", 1)
                if sequence != previous + 1:
                    raise ProtocolError("Request packet sequence is duplicated, truncated or nonconsecutive")
                channel, encoded = packet.get("channel"), packet.get("bytes")
                if channel not in ("reliable", "unreliable", "notify") or not isinstance(encoded, str):
                    raise ProtocolError("Request packet channel or bytes malformed")
                if len(encoded) > 4 * ((1024 * 1024 + 2) // 3):
                    raise ProtocolError("Request packet exceeds per-packet limit")
                try:
                    decoded = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as failure:
                    raise ProtocolError("Request packet bytes are not strict base64") from failure
                if not decoded or len(decoded) > 1024 * 1024 or base64.b64encode(decoded).decode("ascii") != encoded:
                    raise ProtocolError("Request packet bytes are empty, oversized or noncanonical")
                total += len(decoded)
                if total > 16 * 1024 * 1024:
                    raise ProtocolError("Request packet batch exceeds total byte limit")
                copied.append({"sequence": sequence, "channel": channel, "bytes": encoded})
                previous = sequence
        except (TransportError, ProtocolError):
            self.session._request_packet_cursors[key] = None
            raise
        self.session._request_packet_cursors[key] = previous
        return copied

    def join_faction(self, faction: str) -> dict:
        if not isinstance(faction, str) or not faction:
            raise ValueError("faction must be a native faction name")
        return self._command("request_join_faction", "join_faction", {"faction": faction})

    def purchase_airframe(self, aircraft: str) -> dict:
        if not isinstance(aircraft, str) or not aircraft:
            raise ValueError("aircraft must be a native aircraft key")
        return self._command("request_purchase_airframe", "purchase_airframe", {"aircraft": aircraft})

    def request_spawn(self, airbase: str, aircraft: str, *, loadout: Sequence[str | None] | None = None,
                      fuel: float = 1, livery: int | None = None) -> RequestPlayer:
        """One native airbase spawn request; inspect native outcome, never fallback.

        Returns a new generation handle. A native rejection can leave it without
        an aircraft; inspect status/receipts to assert the game's actual outcome.
        """
        if not isinstance(airbase, str) or not airbase or not isinstance(aircraft, str) or not aircraft:
            raise ValueError("airbase and aircraft must be native catalog names")
        if type(fuel) not in (int, float) or not math.isfinite(fuel) or not 0 <= fuel <= 1:
            raise ValueError("fuel must be finite and in 0..1")
        if livery is not None and (type(livery) is not int or livery < 0):
            raise ValueError("livery must be a nonnegative builtin index or None")
        mounts = None
        if loadout is not None:
            if not isinstance(loadout, Sequence) or isinstance(loadout, (str, bytes)) or len(loadout) > 16:
                raise ValueError("loadout must be a sequence of native weapon mounts")
            mounts = []
            for mount in loadout:
                if mount is not None and (not isinstance(mount, str) or not mount):
                    raise ValueError("Each loadout station requires a native weapon-mount key or None")
                mounts.append(mount)
        state = self._command("request_spawn", "request_spawn", {"airbase": airbase, "aircraft": aircraft,
                               "loadout": mounts, "fuel": fuel, "livery": livery})
        current = self.session._bind_request_player(self.name, state)
        if (current.connection_id, current.player_id) != (self.connection_id, self.player_id):
            raise ProtocolError("Spawn request changed native player/connection")
        return current

    def disconnect(self) -> dict:
        return self._command("request_disconnect", "disconnect")


@dataclass(frozen=True)
class Actor:
    """Immutable handle. Spawn returns a new aircraft-generation handle."""

    session: Session
    name: str
    player_id: int
    aircraft_id: int | None
    instance: str

    def _checked(self, *, aircraft: bool = True) -> dict:
        current = self.session.actor(self.name)
        if current.instance != self.instance or current.player_id != self.player_id or current.aircraft_id != self.aircraft_id:
            raise StaleActor(f"Actor {self.name!r} generation changed; bind explicitly to continue")
        if aircraft and self.aircraft_id is None:
            raise StaleActor("Handle has no aircraft; use the handle returned by spawn")
        return {"actor": self.name, "playerId": self.player_id, "aircraftId": self.aircraft_id, "instance": self.instance}

    def spawn(self, *, position, rotation=(0, 0, 0), velocity=(0, 0, 0), type: str = "COIN") -> Actor:
        args = self._checked(aircraft=False)
        args.update(position=_vector(position), rotation=_vector(rotation), velocity=_vector(velocity), type=type)
        state = self.session._state(self.session.call("actor.spawn", args))
        current = self.session._bind(self.name, state)
        if current.player_id != self.player_id or current.aircraft_id is None:
            raise ProtocolError("Spawn did not return the expected player's aircraft")
        return current

    def _command(self, command: str, extra: dict | None = None, *, aircraft: bool = True) -> dict:
        args = self._checked(aircraft=aircraft)
        args.update(extra or {})
        return self.session._state(self.session.call(command, args))

    def goto(self, destination, speed: float = 100) -> dict:
        _number(speed, "speed", 1000)
        return self._command("actor.navigate", {"destination": _vector(destination), "speed": speed})

    def attack(self, target_id: int, station: int = 0, seconds: float = 20) -> dict:
        _integer(target_id, "target_id", 1)
        _integer(station, "station")
        _number(seconds, "seconds", 600)
        return self._command("actor.attack", {"targetId": target_id, "station": station, "seconds": seconds})

    def inspect_target(self, target_id: int) -> dict:
        """Read this actor's native HQ tracking without issuing an attack task."""
        _integer(target_id, "target_id", 1)
        args = self._checked()
        args["targetId"] = target_id
        result = self.session.call("actor.inspect_target", args)
        if not isinstance(result, dict) or type(result.get("targetId")) is not int or result["targetId"] != target_id:
            raise ProtocolError("Target inspection identity mismatch")
        for flag in ("disabled", "opposing", "known", "accurate"):
            if type(result.get(flag)) is not bool:
                raise ProtocolError(f"Target inspection requires boolean {flag}")
        distance = result.get("distance")
        if type(distance) not in (float, int) or not math.isfinite(distance) or distance < 0:
            raise ProtocolError("Target inspection distance must be finite and nonnegative")
        position = result.get("knownPosition")
        if position is not None:
            try:
                _vector(position)
            except ValueError as failure:
                raise ProtocolError("Target inspection known position is invalid") from failure
        if result["known"] and position is None:
            raise ProtocolError("Known target lacks native known position")
        if result["accurate"] and not result["known"]:
            raise ProtocolError("Accurate target lacks native tracking")
        return result

    def cancel(self) -> dict:
        return self._command("actor.cancel")

    def eject(self) -> dict:
        return self._command("actor.eject")

    def remove(self) -> dict:
        return self._command("actor.remove", aircraft=False)

    def status(self) -> dict:
        state = self.session.status()
        current = self.session._bind(self.name, state)
        if current.instance != self.instance or current.player_id != self.player_id or current.aircraft_id != self.aircraft_id:
            raise StaleActor(f"Actor {self.name!r} generation changed")
        return next(row for row in state["actors"] if row["actor"] == self.name)
