"""Two to four script-owned airborne mock players with measured task phases.

Requires an enabled disposable runtime with a mission already running. Uses
NOTESTPILOT_PORT/TOKEN. Five minutes is the default; shorter runs are smoke only.
The initial airborne fixture is artificial and establishes no retail-client coverage.
"""

import argparse
import json
import math
import sys
import time
import uuid

from notestpilot import Session
from explicit_target import TEXTURE_ERROR, SmokeFailure, actor_row, choose_fixture, clean_state, cleanup_owned
from lifecycle import air_diagnostics, linked_aircraft, require_directed_state, create_owned, spawn_owned


def checked_air(state, handle, *, active=True, destination=None):
    if not linked_aircraft(state, handle):
        raise SmokeFailure("Native aircraft lost initialization", air_diagnostics(handle, actor_row(state, handle)["aircraft"]))
    air = actor_row(state, handle)["aircraft"]
    if air.get("disabled") is not False or any(p.get("dead") is not False or p.get("ejected") is not False for p in air["health"]):
        raise SmokeFailure("Owned aircraft/crew became unavailable", air_diagnostics(handle, air))
    if active:
        require_directed_state(handle, air)
        task = air.get("task", {})
        if task.get("active") is not True or task.get("outcome") != "running":
            raise SmokeFailure("Authored navigation ended unexpectedly", air_diagnostics(handle, air))
        actual = task.get("destination")
        if destination is not None and (not isinstance(actual, list) or len(actual) != 3
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in actual)
                or math.dist(actual, destination) > 1):
            raise SmokeFailure("Authored destination changed", air_diagnostics(handle, air))
    for key in ("position", "forward", "velocity"):
        vector = air.get(key)
        if not isinstance(vector, list) or len(vector) != 3 or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector):
            raise SmokeFailure("Invalid physical telemetry", air_diagnostics(handle, air))
    return air


def run(session, *, duration_seconds=300, actor_count=2, turn_degrees=20, allowed_errors=(), clock=time.monotonic, sleep=time.sleep):
    if type(duration_seconds) not in (int, float) or not math.isfinite(duration_seconds) or not 12 <= duration_seconds <= 3600:
        raise ValueError("duration_seconds must be 12..3600")
    if type(actor_count) is not int or not 2 <= actor_count <= 4:
        raise ValueError("actor_count must be an integer in 2..4")
    if type(turn_degrees) not in (int, float) or not math.isfinite(turn_degrees) or not 10 <= turn_degrees <= 35:
        raise ValueError("turn_degrees must be finite and in 10..35")
    owned, state, stage = [], None, "fixture metadata"
    report = {"passed": False, "requestedSeconds": duration_seconds, "actorCount": actor_count, "turnDegrees": turn_degrees,
              "coverage": "airborne server mocks; no network-client coverage",
              "sustained": False, "checks": {}, "phases": []}
    try:
        state = clean_state(session, 10, allowed_errors)
        fixture = choose_fixture(state)
        x, height, z = fixture["shooter_position"]
        report["fixture"] = {"base": fixture["base"], "faction": fixture["shooter_faction"], "airborne": True}
        prefix = "sustained-" + uuid.uuid4().hex[:12]
        stage = "native actor initialization"
        for index in range(actor_count):
            handle = create_owned(session, owned, prefix + "-" + str(index), fixture["shooter_faction"])
            spawn_owned(session, owned, handle, (x + index * 5000, height + index * 300, z))
        state = session.wait_for(lambda s: all(linked_aircraft(s, h) for h in owned),
                                 "all native server-simulated mock aircraft", timeout=20, allowed_errors=allowed_errors)
        if len({h.player_id for h in owned}) != actor_count or len({h.aircraft_id for h in owned}) != actor_count:
            raise SmokeFailure("Independent native player and aircraft identities required")
        report["checks"]["independentNativeIdentity"] = True
        started, phase = clock(), 0
        phase_count = max(2, math.ceil(duration_seconds / 30))
        phase_seconds = duration_seconds / phase_count
        # Long relative legs avoid arrival shutdown; gentle alternating turns give
        # native aim/terrain avoidance time to act. No monotonic distance claim.
        while phase < phase_count:
            stage = "navigation phase " + str(phase)
            baselines, destinations = {}, {}
            for index, handle in enumerate(owned):
                air = checked_air(state, handle, active=phase > 0)
                position = list(air["position"])
                forward = air["forward"]
                length = math.hypot(forward[0], forward[2])
                if not math.isfinite(length) or length < 0.1:
                    raise SmokeFailure("Observed heading cannot define a waypoint", air_diagnostics(handle, air))
                angle = math.radians((turn_degrees if phase % 2 == 0 else -turn_degrees) * (1 if index % 2 == 0 else -1))
                fx, fz = forward[0] / length, forward[2] / length
                dx, dz = fx * math.cos(angle) + fz * math.sin(angle), fz * math.cos(angle) - fx * math.sin(angle)
                destination = (position[0] + dx * 20000, height + index * 300, position[2] + dz * 20000)
                destinations[handle.name] = destination
                baselines[handle.name] = {"position": position, "forward": list(forward), "requestedTurnDegrees": math.degrees(angle)}
                handle.goto(destination, 110)
            state = clean_state(session, 5, allowed_errors)
            for handle in owned:
                baselines[handle.name]["ticks"] = checked_air(state, handle, destination=destinations[handle.name])["task"]["ticks"]
            phase_start = clock()
            phase_end = phase_start + phase_seconds
            samples = 0
            ranges = {h.name: {"speed": [], "altitude": []} for h in owned}
            while clock() < phase_end:
                sleep(min(1, max(0, phase_end - clock())))
                state = clean_state(session, 5, allowed_errors)
                for handle in owned:
                    air = checked_air(state, handle, destination=destinations[handle.name])
                    ranges[handle.name]["speed"].append(math.sqrt(sum(v * v for v in air["velocity"])))
                    ranges[handle.name]["altitude"].append(air["position"][1])
                samples += 1
            actors = []
            for handle in owned:
                air = checked_air(state, handle, destination=destinations[handle.name])
                baseline = baselines[handle.name]
                displacement = math.dist(baseline["position"], air["position"])
                leg = [destinations[handle.name][i] - baseline["position"][i] for i in range(3)]
                projected = sum((air["position"][i] - baseline["position"][i]) * leg[i] for i in range(3)) / math.sqrt(sum(v * v for v in leg))
                tick_delta = air["task"]["ticks"] - baseline["ticks"]
                original_heading = math.atan2(baseline["forward"][0], baseline["forward"][2])
                heading = math.atan2(air["forward"][0], air["forward"][2])
                turn = math.degrees(math.atan2(math.sin(heading - original_heading), math.cos(heading - original_heading)))
                signed_turn = turn * (1 if baseline["requestedTurnDegrees"] > 0 else -1)
                response = {"displacement": displacement, "minimumDisplacement": phase_seconds * 10,
                            "projectedDisplacement": projected, "tickDelta": tick_delta,
                            "requestedTurnDegrees": baseline["requestedTurnDegrees"], "observedTurnDegrees": turn,
                            "signedTurnDegrees": signed_turn, "initialForward": baseline["forward"], "forward": air["forward"],
                            "actualPhaseSeconds": clock() - phase_start}
                # Each phase must show native physical motion as well as policy ticks.
                if displacement < phase_seconds * 10 or projected <= 0 or tick_delta <= 0:
                    raise SmokeFailure("Independent phase produced insufficient measured progress",
                                       {**air_diagnostics(handle, air), "response": response})
                if signed_turn < 3:
                    raise SmokeFailure("Authored waypoint lacked a measured signed heading response",
                                       {**air_diagnostics(handle, air), "response": response})
                actors.append({"actor": handle.name, "playerId": handle.player_id, "aircraftId": handle.aircraft_id,
                               "destination": list(destinations[handle.name]), "position": air["position"],
                               **response, "speedRange": [min(ranges[handle.name]["speed"]), max(ranges[handle.name]["speed"])],
                               "altitudeRange": [min(ranges[handle.name]["altitude"]), max(ranges[handle.name]["altitude"])]})
            report["phases"].append({"phase": phase, "elapsedSeconds": clock() - started,
                                     "actualPhaseSeconds": clock() - phase_start, "samples": samples, "actors": actors})
            if phase == 0:
                stage = "cancel isolation"
                before = {h.name: actor_row(state, h)["aircraft"]["task"]["ticks"] for h in owned[1:]}
                owned[0].cancel()
                state = session.wait_for(lambda s: actor_row(s, owned[0])["aircraft"]["task"]["active"] is False
                                         and all(checked_air(s, h, destination=destinations[h.name])["task"]["ticks"] > before[h.name]
                                                 for h in owned[1:]),
                                         "cancel one mock while every other actor progresses", timeout=8, allowed_errors=allowed_errors)
                checked_air(state, owned[0], active=False)
                report["checks"]["cancelIsolation"] = True
                # Explicitly resume the cancelled task once, preserving handle identity.
                owned[0].goto(destinations[owned[0].name], 110)
                state = clean_state(session, 5, allowed_errors)
            phase += 1
        report["observedSeconds"] = clock() - started
        report["sustained"] = duration_seconds >= 300 and report["observedSeconds"] >= 300
        report["checks"]["repeatedWaypoints"] = len(report["phases"]) >= 2
        report["checks"]["physicalMotionEveryPhase"] = True
        report["checks"]["signedHeadingResponseEveryPhase"] = True
        report["passed"] = True
        return report
    except Exception as failure:
        observed = []
        diagnostic_state = getattr(failure, "last_state", None) or getattr(failure, "state", None) or state
        if isinstance(diagnostic_state, dict):
            for handle in owned:
                rows = [r for r in diagnostic_state.get("actors", []) if r.get("actor") == handle.name]
                if rows and isinstance(rows[0].get("aircraft"), dict):
                    observed.append(air_diagnostics(handle, rows[0]["aircraft"]))
        raise SmokeFailure("Sustained scenario failed at " + stage,
                           {"stage": stage, "cause": str(failure), "failureType": type(failure).__name__,
                            "details": getattr(failure, "diagnostics", {}), "lastObservedActors": observed,
                            "phases": report["phases"], "checks": report["checks"]}) from failure
    finally:
        primary = sys.exc_info()[1]
        failures = cleanup_owned(session, owned)
        if failures:
            raise SmokeFailure("Sustained owned cleanup incomplete", {"cleanup": failures,
                               "primaryFailure": str(primary) if primary else None,
                               "primaryDiagnostics": getattr(primary, "diagnostics", {}), "phases": report["phases"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=300)
    parser.add_argument("--actors", type=int, default=2)
    parser.add_argument("--turn-degrees", type=float, default=20)
    parser.add_argument("--allow-headless-texture-error", action="store_true")
    args = parser.parse_args()
    try:
        result = run(Session(timeout=5), duration_seconds=args.duration_seconds,
                     actor_count=args.actors, turn_degrees=args.turn_degrees,
                     allowed_errors=TEXTURE_ERROR if args.allow_headless_texture_error else ())
    except Exception as failure:
        result = {"passed": False, "error": str(failure), "diagnostics": getattr(failure, "diagnostics", {})}
    print(json.dumps(result, indent=2, allow_nan=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
