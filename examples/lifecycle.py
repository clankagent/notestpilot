"""Bounded lifecycle/navigation assertions for an already running private lab.

Run `uv run python examples/lifecycle.py`. Runtime port/token come from the
environment. This script owns only its two uniquely named server mock players.
It does not start/quit the server or establish realistic multiplayer coverage.
"""

import argparse
import json
import math
import sys
import time
import uuid

from notestpilot import CommandError, Session, StaleActor
from notestpilot.client import CreationUncertain
from explicit_target import TEXTURE_ERROR, SmokeFailure, actor_row, choose_fixture, cleanup_owned, clean_state


def air_diagnostics(handle, air):
    return {"actor": handle.name, "playerId": handle.player_id, "expectedAircraftId": handle.aircraft_id,
            "aircraft": {key: air.get(key) for key in ("id", "linkedPlayerId", "initialized", "localSim",
                         "remoteSim", "disabled", "health", "task", "position", "forward")}}


def linked_aircraft(state, handle):
    air = actor_row(state, handle)["aircraft"]
    if air.get("initialized") is not True:
        return False
    if air.get("linkedPlayerId") != handle.player_id or air.get("localSim") is not True or air.get("remoteSim") is not False:
        raise SmokeFailure("Native player link or server simulation invariant failed", air_diagnostics(handle, air))
    health = air.get("health")
    if (not isinstance(health, list) or not health or any(not isinstance(p, dict)
            or type(p.get("seat")) is not int or p["seat"] < 0
            or p.get("npcBrainInitialized") is not False for p in health)):
        raise SmokeFailure("Native NPC brain appeared or crew telemetry is incomplete", air_diagnostics(handle, air))
    if len({p["seat"] for p in health}) != len(health) or not any(p["seat"] == 0 for p in health):
        raise SmokeFailure("Controlling seat identity is missing or ambiguous", air_diagnostics(handle, air))
    return True


def require_directed_state(handle, air):
    # The runtime controls native pilots[0]. Other crew can remain idle; explicit
    # seat identity must come from telemetry, not a filtered array's position.
    controlling = [p for p in air.get("health", []) if p.get("seat") == 0]
    if len(controlling) != 1 or controlling[0].get("state") != "DirectedPilot":
        raise SmokeFailure("Controlling pilot escaped its authored state", air_diagnostics(handle, air))


def reject(operation, exception, description):
    try:
        operation()
    except exception:
        return
    raise SmokeFailure("Expected stale identity rejection: " + description)


def create_owned(session, owned, name, faction):
    try:
        handle = session.create(name, faction)
    except CreationUncertain as failure:
        owned.append(session.recover_creation(failure))
        raise
    owned.append(handle)
    return handle


def spawn_owned(session, owned, handle, position):
    index = owned.index(handle)
    try:
        spawned = handle.spawn(type="COIN", position=position, velocity=(0, 0, 110))
    except Exception:
        current = session.actor(handle.name)
        if (current.instance, current.player_id) != (handle.instance, handle.player_id):
            raise StaleActor("Spawn cleanup recovery refused a changed runtime/player")
        owned[index] = current
        raise
    owned[index] = spawned
    return spawned


def run(session, *, navigation_seconds=12, allowed_errors=(), clock=time.monotonic, sleep=time.sleep):
    if type(navigation_seconds) not in (float, int) or not math.isfinite(navigation_seconds) or not 12 <= navigation_seconds <= 30:
        raise ValueError("Navigation observation must be 12..30 seconds")
    owned = []
    report = {"passed": False, "coverage": "server mock lifecycle/navigation; no network-client coverage", "checks": {}}
    stage = "fixture metadata"
    state = None
    try:
        initial = clean_state(session, 10, allowed_errors)
        state = initial
        fixture = choose_fixture(initial)
        x, height, z = fixture["shooter_position"]
        prefix = "lifecycle-" + uuid.uuid4().hex[:12]
        stage = "native actor initialization"
        turner = create_owned(session, owned, prefix + "-turner", fixture["shooter_faction"])
        straight = create_owned(session, owned, prefix + "-straight", fixture["shooter_faction"])
        turner = spawn_owned(session, owned, turner, (x, height, z))
        straight = spawn_owned(session, owned, straight, (x + 1000, height + 100, z))
        state = session.wait_for(lambda s: all(linked_aircraft(s, h) for h in owned),
                                 "both native links/server simulation without NPC brains", timeout=20, allowed_errors=allowed_errors)
        if turner.player_id == straight.player_id or turner.aircraft_id == straight.aircraft_id:
            raise SmokeFailure("Two independent native identities are required")
        report["checks"]["independentNativeLinksServerSimulationNoNPC"] = True
        start = {h.name: actor_row(state, h)["aircraft"]["position"] for h in owned}
        for h in owned:
            heading = actor_row(state, h)["aircraft"]["forward"]
            if heading[2] <= 0.8 or abs(heading[0]) >= 0.2:
                raise SmokeFailure("Airborne fixture did not start with observed north heading", {"actor": h.name, "forward": heading})
        turner.goto((x + 15000, height, z), 110)
        straight.goto((x + 1000, height + 100, z + 16000), 110)
        stage = "physical navigation observation"
        deadline = clock() + navigation_seconds
        while clock() < deadline:
            sleep(min(0.5, max(0, deadline - clock())))
            state = clean_state(session, 5, allowed_errors)
            for h in owned:
                linked_aircraft(state, h)
                air = actor_row(state, h)["aircraft"]
                if air.get("disabled") is not False or any(p.get("dead") or p.get("ejected") for p in air["health"]):
                    raise SmokeFailure("Navigation aircraft/pilot became unavailable", air_diagnostics(h, air))
        for h in owned:
            air = actor_row(state, h)["aircraft"]
            task = air["task"]
            if task.get("active") is not True or task.get("outcome") != "running" or task.get("ticks", 0) < 60:
                raise SmokeFailure("Directed navigation did not remain active", air_diagnostics(h, air))
            if math.dist(start[h.name], air["position"]) <= 200:
                raise SmokeFailure("Navigation produced insufficient measured displacement", air_diagnostics(h, air))
            require_directed_state(h, air)
        turned = actor_row(state, turner)["aircraft"]
        held = actor_row(state, straight)["aircraft"]
        if turned["forward"][0] <= 0.2 or turned["position"][0] - start[turner.name][0] <= 50:
            raise SmokeFailure("Authored turn lacked measured east heading/displacement")
        if held["forward"][2] <= 0.8:
            raise SmokeFailure("Separate straight pilot did not retain north heading")
        report["checks"]["independentPhysicalTurnAndStraightNavigation"] = True
        stage = "cancel isolation"
        before_ticks = held["task"]["ticks"]
        turner.cancel()
        session.wait_for(lambda s: actor_row(s, turner)["aircraft"]["task"]["active"] is False
                         and actor_row(s, straight)["aircraft"]["task"]["active"] is True
                         and actor_row(s, straight)["aircraft"]["task"]["ticks"] > before_ticks,
                         "cancel one actor while the other keeps navigating", timeout=8, allowed_errors=allowed_errors)
        report["checks"]["cancelIsolation"] = True
        stage = "native ejection and replacement"
        old = turner
        old.eject()
        def retired(s):
            rows = [r for r in s["actors"] if r.get("actor") == old.name and r.get("playerId") == old.player_id]
            if len(rows) != 1:
                raise StaleActor("Ejection changed player identity")
            air = rows[0].get("aircraft")
            return air is None or (air.get("id") == old.aircraft_id and air.get("disabled") is True)
        session.wait_for(retired, "native ejection to retire the old aircraft", timeout=25, allowed_errors=allowed_errors)
        eligible = session.actor(old.name)
        if (eligible.instance, eligible.player_id) != (old.instance, old.player_id):
            raise StaleActor("Ejection changed runtime/player identity")
        owned[owned.index(old)] = eligible
        turner = spawn_owned(session, owned, eligible, (x + 2000, height, z + 3000))
        if turner.player_id != old.player_id or turner.aircraft_id == old.aircraft_id:
            raise SmokeFailure("Replacement did not preserve player with a new aircraft generation")
        session.wait_for(lambda s: linked_aircraft(s, turner), "replacement initialization", timeout=20, allowed_errors=allowed_errors)
        turner.goto((x + 2000, height, z + 18000), 110)
        report["checks"]["ejectionReplacementPreservesPlayerNewGeneration"] = True
        stage = "stale aircraft isolation"
        reject(old.cancel, StaleActor, "old aircraft handle")
        reject(lambda: session.call("actor.cancel", {"actor": old.name, "instance": old.instance,
                 "playerId": old.player_id, "aircraftId": old.aircraft_id}), CommandError, "runtime old aircraft guard")
        session.wait_for(lambda s: actor_row(s, turner)["aircraft"]["task"]["active"] is True
                         and actor_row(s, turner)["aircraft"]["task"]["ticks"] > 30,
                         "new task continues after stale aircraft rejection", timeout=8, allowed_errors=allowed_errors)
        report["checks"]["staleAircraftCannotCancelReplacement"] = True
        stage = "name reuse identity isolation"
        turner.remove()
        owned.remove(turner)
        session.wait_for(lambda s: all(r.get("actor") != turner.name for r in s["actors"]),
                         "owned removed name to retire", timeout=10, allowed_errors=allowed_errors)
        recreated = create_owned(session, owned, turner.name, fixture["shooter_faction"])
        if recreated.player_id == turner.player_id:
            raise SmokeFailure("Recreated name reused retired native player identity")
        reject(turner.remove, StaleActor, "retired player handle")
        reject(lambda: session.call("actor.remove", {"actor": turner.name, "instance": turner.instance,
                 "playerId": turner.player_id, "aircraftId": None}), CommandError, "runtime old player guard")
        if session.actor(recreated.name).player_id != recreated.player_id:
            raise SmokeFailure("Stale removal affected recreated player")
        report["checks"]["recreatedNameRejectsRetiredNativePlayer"] = True
        report["passed"] = True
        return report
    except Exception as failure:
        observed = []
        if isinstance(state, dict):
            for handle in owned:
                rows = [r for r in state.get("actors", []) if r.get("actor") == handle.name]
                if rows and isinstance(rows[0].get("aircraft"), dict):
                    observed.append(air_diagnostics(handle, rows[0]["aircraft"]))
        raise SmokeFailure("Lifecycle assertion failed at " + stage,
                           {"stage": stage, "cause": str(failure), "failureType": type(failure).__name__,
                            "details": getattr(failure, "diagnostics", {}), "lastObservedActors": observed,
                            "completedChecks": report["checks"]}) from failure
    finally:
        primary = sys.exc_info()[1]
        failures = cleanup_owned(session, owned)
        if failures:
            raise SmokeFailure("Lifecycle owned cleanup incomplete",
                               {"cleanup": failures, "primaryFailure": str(primary) if primary else None,
                                "primaryDiagnostics": getattr(primary, "diagnostics", {}),
                                "completedChecks": report["checks"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--navigation-seconds", type=float, default=12)
    parser.add_argument("--allow-headless-texture-error", action="store_true")
    args = parser.parse_args()
    try:
        result = run(Session(timeout=5), navigation_seconds=args.navigation_seconds,
                     allowed_errors=TEXTURE_ERROR if args.allow_headless_texture_error else ())
    except Exception as failure:
        result = {"passed": False, "error": str(failure), "diagnostics": getattr(failure, "diagnostics", {})}
    print(json.dumps(result, indent=2, allow_nan=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
