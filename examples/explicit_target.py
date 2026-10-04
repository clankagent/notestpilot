"""An explicit-target gun smoke test against an already running private lab.

Run with `uv run python examples/explicit_target.py`. Supply the private runtime
port/token through NOTESTPILOT_PORT/NOTESTPILOT_TOKEN. This never starts a game or
mission, installs NPC AI, applies artificial damage, or quits the runtime.

Two server mock players in an airborne fixture do not establish realistic
multiplayer, client authentication, kill/score semantics or full combat coverage.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import sys
import time
import uuid

from notestpilot import Session, StaleActor
from notestpilot.client import CreationUncertain


TEXTURE_ERROR = (("Error", "There is no texture data available to upload."),)


class SmokeFailure(RuntimeError):
    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def choose_fixture(state):
    """Use live faction/base/type metadata; spatial offsets define this fixture."""
    if state.get("missionRunning") is not True or state.get("serverActive") is not True:
        raise SmokeFailure("An already running disposable lab mission is required")
    if "COIN" not in state.get("aircraftTypes", []):
        raise SmokeFailure("The native COIN aircraft key is unavailable")
    factions = state.get("factions", [])
    bases = [b for b in state.get("airbases", []) if b.get("faction") in factions]
    for base in bases:
        origin = base.get("position")
        if not isinstance(origin, list) or len(origin) != 3 or any(
                type(v) not in (float, int) or not math.isfinite(v) for v in origin):
            continue
        opposing = next((b for b in bases if b["faction"] != base["faction"]), None)
        if opposing:
            x, y, z = origin
            return {
                "base": base["name"], "shooter_faction": base["faction"], "victim_faction": opposing["faction"],
                "shooter_position": (x, y + 2200, z + 5000),
                "victim_position": (x, y + 2200, z + 5400),
                "victim_destination": (x, y + 2200, z + 20000),
            }
    raise SmokeFailure("Two observed airbases belonging to opposing native factions are required")


def actor_row(state, handle):
    rows = [r for r in state["actors"] if r.get("actor") == handle.name]
    if len(rows) != 1 or rows[0].get("playerId") != handle.player_id:
        raise StaleActor("Owned mock player identity changed")
    row = rows[0]
    aircraft = row.get("aircraft")
    if not isinstance(aircraft, dict) or aircraft.get("id") != handle.aircraft_id:
        raise StaleActor("Owned aircraft generation changed")
    return row


@dataclass
class Evidence:
    shooter: object
    victim: object
    bullet: dict | None = None
    hit: dict | None = None
    applied: dict | None = None
    observed: int = 0

    def consume(self, events):
        for event in events:
            self.observed += 1
            if event.get("mockPlayer") != self.shooter.name:
                continue
            kind = event.get("type")
            if kind == "gun_bullet_created" and event.get("attackerPersistentId") == self.shooter.aircraft_id:
                self.bullet = self.bullet or event
            elif (kind == "unit_hit_registered" and event.get("attackerPersistentId") == self.shooter.aircraft_id
                  and event.get("victimPersistentId") == self.victim.aircraft_id):
                self.hit = self.hit or event
            elif (kind == "part_damage_applied" and event.get("dealerPersistentId") == self.shooter.aircraft_id
                  and event.get("victimPersistentId") == self.victim.aircraft_id
                  and event.get("attribution") == "synchronous native TakeDamage scope"):
                amounts = [event.get(key) for key in ("pierceDamage", "blastDamage", "fireDamage", "impactDamage")]
                if any(type(v) not in (float, int) or not math.isfinite(v) for v in amounts):
                    raise SmokeFailure("Applied damage amounts are malformed")
                if any(v > 0 for v in amounts):
                    self.applied = self.applied or event

    @property
    def complete(self):
        return self.bullet is not None and self.hit is not None and self.applied is not None

    def summary(self):
        return {"nativeBullet": self.bullet is not None, "exactVictimHit": self.hit is not None,
                "attributedPositiveAppliedDamage": self.applied is not None,
                "proofSequences": {name: row["seq"] if row else None for name, row in
                                   (("bullet", self.bullet), ("hit", self.hit), ("applied", self.applied))},
                "observedEvents": self.observed}


def bounded_read(session, deadline, operation):
    """Reserve total time for public helpers that perform up to three RPC reads."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Read stage deadline expired")
    original = session.timeout
    session.timeout = min(original, remaining / 4)
    try:
        return operation()
    finally:
        session.timeout = original


def clean_state(session, timeout, allowed_errors):
    return session.wait_for(lambda state: True, "a clean runtime snapshot", timeout=timeout,
                            allowed_errors=allowed_errors)


def cleanup_owned(session, owned):
    """Rebind only the same player and recorded aircraft generation, then remove."""
    failures = []
    for original in reversed(owned):
        try:
            current = session.actor(original.name)
            if (current.instance, current.player_id, current.aircraft_id) != (
                    original.instance, original.player_id, original.aircraft_id):
                raise StaleActor("Cleanup refused a changed actor/aircraft generation")
            current.remove()  # Also accepts the bound aircraft when disabled.
        except Exception as failure:
            failures.append({"actor": original.name, "error": str(failure)})
    return failures


def run(session, *, tracking_seconds=20, attack_seconds=20, allowed_errors=()):
    if not all(type(v) in (float, int) and math.isfinite(v) and 0 < v <= 60 for v in (tracking_seconds, attack_seconds)):
        raise ValueError("Tracking and attack deadlines must be finite and in (0, 60]")
    owned = []
    report = {"coverage": "server mock-player explicit-target gun smoke; no real multiplayer coverage"}
    try:
        initial = clean_state(session, 10, allowed_errors)
        fixture = choose_fixture(initial)
        report["fixture"] = fixture
        prefix = "explicit-" + uuid.uuid4().hex[:12]
        for role in ("shooter", "victim"):
            name = f"{prefix}-{role}"
            try:
                handle = session.create(name, fixture[f"{role}_faction"])
            except CreationUncertain as failure:
                try:
                    # Read-only recovery requires the server's exact creation
                    # correlation. The recovered handle is used ONLY to clean up.
                    owned.append(session.recover_creation(failure))
                except Exception as recovery:
                    raise SmokeFailure("Uncertain creation could not be safely recovered for cleanup",
                                       {"actor": name, "recovery": str(recovery)}) from failure
                raise
            except Exception as failure:
                raise SmokeFailure("Actor creation failed for " + name) from failure
            owned.append(handle)
            try:
                handle = handle.spawn(type="COIN", position=fixture[f"{role}_position"], velocity=(0, 0, 110))
            except Exception as failure:
                try:
                    recovered = session.actor(handle.name)
                    if (recovered.instance, recovered.player_id) != (handle.instance, handle.player_id):
                        raise StaleActor("Spawn recovery refused changed runtime/player identity")
                    # The created player's native record identifies ownership.
                    # Adopt its new aircraft only for cleanup; never retry spawn.
                    owned[-1] = recovered
                except Exception as recovery:
                    raise SmokeFailure("Spawn outcome could not be safely recovered for cleanup",
                                       {"actor": name, "recovery": str(recovery)}) from failure
                raise
            owned[-1] = handle
        shooter, victim = owned
        state = session.wait_for(lambda s: all(actor_row(s, h)["aircraft"].get("initialized") is True for h in owned),
                                 "both owned aircraft to initialize", timeout=20, allowed_errors=allowed_errors)
        stations = actor_row(state, shooter)["aircraft"].get("stations", [])
        selected = next((s["index"] for s in stations if s.get("fixedGun") is True and s.get("ammo", 0) > 0), None)
        if selected is None:
            raise SmokeFailure("Shooter has no armed native fixed-gun station")
        report.update(shooterId=shooter.aircraft_id, victimId=victim.aircraft_id, station=selected)
        victim.goto(fixture["victim_destination"], speed=110)
        deadline = time.monotonic() + tracking_seconds
        observation = None
        while time.monotonic() < deadline:
            bounded_read(session, deadline, lambda: clean_state(session, min(5, deadline-time.monotonic()), allowed_errors))
            observation = bounded_read(session, deadline, lambda: shooter.inspect_target(victim.aircraft_id))
            if observation["disabled"] or not observation["opposing"]:
                raise SmokeFailure("Designated native target became ineligible", observation)
            if observation["known"] and observation["accurate"]:
                break
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        else:
            raise SmokeFailure("Native HQ tracking deadline expired", {"lastInspection": observation})
        # Establish a fresh cursor BEFORE the one attack mutation. Old events
        # are never accepted as proof for this task. Overflow still fails.
        session.events()
        shooter.attack(victim.aircraft_id, station=selected, seconds=attack_seconds)
        evidence = Evidence(shooter, victim)
        deadline = time.monotonic() + attack_seconds
        while time.monotonic() < deadline:
            state = bounded_read(session, deadline, lambda: clean_state(session, min(5, deadline-time.monotonic()), allowed_errors))
            batch = bounded_read(session, deadline, session.events)
            evidence.consume(batch["events"])
            if evidence.complete:
                report["evidence"] = evidence.summary()
                report["passed"] = True
                return report
            task = actor_row(state, shooter)["aircraft"].get("task", {})
            if task.get("outcome") in ("deadline", "out-of-ammunition", "actor-unavailable", "interrupted"):
                raise SmokeFailure("Attack ended before all required evidence", {"task": task, **evidence.summary()})
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        raise SmokeFailure("Native proof deadline expired; required damage application remains unobserved", evidence.summary())
    finally:
        primary = sys.exc_info()[1]
        failures = cleanup_owned(session, owned)
        if failures:
            raise SmokeFailure("Owned actor cleanup incomplete; no other actors were removed",
                               {"cleanup": failures, "primaryFailure": str(primary) if primary else None})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracking-seconds", type=float, default=20)
    parser.add_argument("--attack-seconds", type=float, default=20)
    parser.add_argument("--allow-headless-texture-error", action="store_true",
                        help="Allow only the exact historical headless Error kind/message with a complete journal")
    args = parser.parse_args()
    if not all(math.isfinite(v) and 0 < v <= 60 for v in (args.tracking_seconds, args.attack_seconds)):
        parser.error("Tracking and attack deadlines must be finite and in (0, 60]")
    try:
        result = run(Session(timeout=5), tracking_seconds=args.tracking_seconds, attack_seconds=args.attack_seconds,
                     allowed_errors=TEXTURE_ERROR if args.allow_headless_texture_error else ())
        print(json.dumps(result, indent=2, allow_nan=False))
    except Exception as failure:
        print(json.dumps({"passed": False, "error": str(failure), "diagnostics": getattr(failure, "diagnostics", {})},
                         indent=2, allow_nan=False))
        raise SystemExit(1) from failure


if __name__ == "__main__":
    main()
