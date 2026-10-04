"""Navigation -> native gun proof -> cancel -> navigation, on the same airframe.

Requires an already running disposable mission and NOTESTPILOT_PORT/TOKEN.
Three server mocks and an artificial airborne target do not prove client or
realistic multiplayer coverage. No existing aircraft is moved by this script.
"""

import argparse
import json
import math
import sys
import time
import uuid

from notestpilot import Session, StaleActor
from explicit_target import TEXTURE_ERROR, SmokeFailure, Evidence, actor_row, choose_fixture, clean_state, cleanup_owned
from lifecycle import air_diagnostics, linked_aircraft, require_directed_state, create_owned, spawn_owned


def live_air(state, handle, task=None):
    if not linked_aircraft(state, handle):
        raise SmokeFailure("Native initialization was lost")
    air = actor_row(state, handle)["aircraft"]
    if air.get("disabled") is not False or any(p.get("dead") is not False or p.get("ejected") is not False for p in air["health"]):
        raise SmokeFailure("Owned flight crew/aircraft unavailable", air_diagnostics(handle, air))
    for key in ("position", "forward", "velocity"):
        values = air.get(key)
        if not isinstance(values, list) or len(values) != 3 or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise SmokeFailure("Physical telemetry incomplete", air_diagnostics(handle, air))
    if task:
        require_directed_state(handle, air)
        current = air.get("task", {})
        if current.get("active") is not True or current.get("outcome") != "running" or current.get("task") != task:
            raise SmokeFailure("Requested task transition was not retained", air_diagnostics(handle, air))
    return air


def spawn_target(session, owned, handle, position, rotation, velocity):
    index = owned.index(handle)
    try:
        result = handle.spawn(type="COIN", position=position, rotation=rotation, velocity=velocity)
    except Exception:
        current = session.actor(handle.name)
        if (current.instance, current.player_id) != (handle.instance, handle.player_id):
            raise StaleActor("Target spawn recovery refused changed runtime/player identity")
        owned[index] = current  # Cleanup only; never replay an uncertain mutation.
        raise
    owned[index] = result
    return result


def run(session, *, navigation_seconds=60, resumed_seconds=30, attack_seconds=20, quiet_seconds=3,
        allowed_errors=(), clock=time.monotonic, sleep=time.sleep):
    for name, value, minimum, maximum in (("navigation_seconds", navigation_seconds, 6, 300),
            ("resumed_seconds", resumed_seconds, 6, 120), ("attack_seconds", attack_seconds, 1, 30),
            ("quiet_seconds", quiet_seconds, 1, 10)):
        if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be finite and in {minimum}..{maximum}")
    owned, state, evidence, inspection, stage = [], None, None, None, "fixture metadata"
    report = {"passed": False, "coverage": "server mock task transitions; artificial airborne target; no network-client coverage", "checks": {}}

    def read(deadline, operation):
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("Transition read deadline expired")
        original = session.timeout
        session.timeout = min(original, remaining / 4)
        try:
            return operation()
        finally:
            session.timeout = original

    def observe(seconds, handles, *, turning=None):
        nonlocal state
        start_air = {h.name: live_air(state, h, "navigate") for h in handles}
        baseline = {h.name: {"position": list(start_air[h.name]["position"]), "forward": list(start_air[h.name]["forward"]),
                            "ticks": start_air[h.name]["task"]["ticks"]} for h in handles}
        started, deadline = clock(), clock() + seconds
        while clock() < deadline:
            sleep(min(0.5, max(0, deadline - clock())))
            state = clean_state(session, 5, allowed_errors)
            for h in handles:
                live_air(state, h, "navigate")
        result = []
        for h in handles:
            air, first = live_air(state, h, "navigate"), baseline[h.name]
            distance = math.dist(first["position"], air["position"])
            delta = air["task"]["ticks"] - first["ticks"]
            angle = math.atan2(air["forward"][0], air["forward"][2]) - math.atan2(first["forward"][0], first["forward"][2])
            turn = math.degrees(math.atan2(math.sin(angle), math.cos(angle)))
            metrics = {"actor": h.name, "displacement": distance, "tickDelta": delta, "turnDegrees": turn,
                       "position": air["position"], "forward": air["forward"], "observedSeconds": clock() - started}
            if distance < seconds * 10 or delta <= 0 or (h == turning and turn < 3):
                raise SmokeFailure("Navigation lacks required physical/task response", {**air_diagnostics(h, air), "response": metrics})
            result.append(metrics)
        return result

    try:
        state = clean_state(session, 10, allowed_errors)
        fixture = choose_fixture(state)
        prefix = "transitions-" + uuid.uuid4().hex[:12]
        x, y, z = fixture["shooter_position"]
        stage = "initial native navigation"
        shooter = create_owned(session, owned, prefix + "-shooter", fixture["shooter_faction"])
        shooter = spawn_owned(session, owned, shooter, (x, y, z))
        ally = create_owned(session, owned, prefix + "-ally", fixture["shooter_faction"])
        ally = spawn_owned(session, owned, ally, (x + 5000, y + 300, z))
        state = session.wait_for(lambda s: all(linked_aircraft(s, h) for h in owned), "native friendly aircraft initialization",
                                 timeout=20, allowed_errors=allowed_errors)
        shooter.goto((x, y, z + 40000), 110)
        ally.goto((x + 5000, y + 300, z + 100000), 110)
        state = clean_state(session, 5, allowed_errors)
        report["initialNavigation"] = observe(navigation_seconds, [shooter, ally])
        report["identity"] = {"playerId": shooter.player_id, "aircraftId": shooter.aircraft_id, "instance": shooter.instance}
        stage = "observed-heading target fixture"
        air = live_air(state, shooter, "navigate")
        forward = air["forward"]
        magnitude = math.sqrt(sum(v * v for v in forward))
        if magnitude < 0.1 or math.hypot(forward[0], forward[2]) < 0.1:
            raise SmokeFailure("Observed shooter heading cannot define the target fixture", air_diagnostics(shooter, air))
        direction = [v / magnitude for v in forward]
        position = [air["position"][i] + direction[i] * 400 for i in range(3)]
        rotation = (-math.degrees(math.atan2(direction[1], math.hypot(direction[0], direction[2]))),
                    math.degrees(math.atan2(direction[0], direction[2])), 0)
        victim = create_owned(session, owned, prefix + "-target", fixture["victim_faction"])
        victim = spawn_target(session, owned, victim, position, rotation, tuple(air["velocity"]))
        state = session.wait_for(lambda s: linked_aircraft(s, victim), "new opposing fixture initialization", timeout=20, allowed_errors=allowed_errors)
        if len({h.player_id for h in owned}) != 3 or len({h.aircraft_id for h in owned}) != 3:
            raise SmokeFailure("Independent native identities required for all three fixture actors")
        observed_shooter = live_air(state, shooter, "navigate")
        observed_target = live_air(state, victim)
        separation = [observed_target["position"][i] - observed_shooter["position"][i] for i in range(3)]
        distance = math.sqrt(sum(v * v for v in separation))
        observed_length = math.sqrt(sum(v * v for v in observed_shooter["forward"]))
        report["targetFixture"] = {"playerId": victim.player_id, "aircraftId": victim.aircraft_id,
                                   "requestedPosition": position, "rotation": list(rotation), "initialVelocity": list(air["velocity"]),
                                   "observedDistance": distance,
                                   "observedForwardAlignment": sum(separation[i] * observed_shooter["forward"][i] for i in range(3)) / (distance * observed_length)
                                   if distance > 0 and observed_length > 0 else None}
        alignment = report["targetFixture"]["observedForwardAlignment"]
        if not 200 <= distance <= 600 or alignment is None or alignment < 0.9:
            raise SmokeFailure("Observed target fixture is no longer near and ahead of the shooter", report["targetFixture"])
        victim.goto([position[i] + direction[i] * 20000 for i in range(3)], 110)
        stations = live_air(state, shooter, "navigate").get("stations", [])
        station = next((s["index"] for s in stations if s.get("fixedGun") is True and s.get("ammo", 0) > 0), None)
        if station is None:
            raise SmokeFailure("Shooter lacks an armed native fixed-gun station")
        deadline = clock() + 20
        stage = "native target tracking"
        while clock() < deadline:
            state = read(deadline, lambda: clean_state(session, 5, allowed_errors))
            live_air(state, shooter, "navigate")
            live_air(state, ally, "navigate")
            inspection = read(deadline, lambda: shooter.inspect_target(victim.aircraft_id))
            if inspection["disabled"] or not inspection["opposing"]:
                raise SmokeFailure("Native target became ineligible", inspection)
            if inspection["known"] and inspection["accurate"]:
                break
            sleep(min(0.25, max(0, deadline - clock())))
        else:
            raise SmokeFailure("Native accurate target tracking unobserved", inspection)
        stage = "navigation to exact native gun attack"
        report["attackCursor"] = session.events()["cursor"]  # Fresh cursor before the single attack mutation.
        evidence = Evidence(shooter, victim)
        friendly_before_attack = live_air(state, ally, "navigate")["task"]["ticks"]
        shooter.attack(victim.aircraft_id, station=station, seconds=attack_seconds)
        deadline = clock() + attack_seconds
        while clock() < deadline:
            state = read(deadline, lambda: clean_state(session, 5, allowed_errors))
            live_air(state, ally, "navigate")
            attack = live_air(state, shooter, "attack")["task"]
            if attack.get("station") != station or attack.get("targetNetId") != actor_row(state, victim)["aircraft"].get("netId"):
                raise SmokeFailure("Exact target/station transition changed", attack)
            evidence.consume(read(deadline, session.events)["events"])
            if evidence.complete:
                break
            sleep(min(0.25, max(0, deadline - clock())))
        else:
            raise SmokeFailure("Native damage application remains unobserved", evidence.summary())
        report["evidence"] = evidence.summary()
        state = session.wait_for(lambda s: live_air(s, ally, "navigate")["task"]["ticks"] > friendly_before_attack,
                                 "independent friendly task progresses during attack", timeout=8, allowed_errors=allowed_errors)
        report["checks"]["nativeBulletExactHitAttributedDamage"] = True
        stage = "attack cancel isolation and quiet window"
        before = live_air(state, ally, "navigate")["task"]["ticks"]
        shooter.cancel()
        state = session.wait_for(lambda s: live_air(s, shooter)["task"]["active"] is False
                                 and live_air(s, ally, "navigate")["task"]["ticks"] > before,
                                 "cancel attack while friendly navigation advances", timeout=8, allowed_errors=allowed_errors)
        # The quiet claim begins only after this post-ACK cursor. Events between
        # cancel and this drain are ambiguous and excluded from that claim.
        report["quietCursor"] = session.events()["cursor"]
        quiet_started = clock()
        quiet_end_cursor = report["quietCursor"]
        deadline = clock() + quiet_seconds
        while clock() < deadline:
            sleep(min(0.25, max(0, deadline - clock())))
            state = clean_state(session, 5, allowed_errors)
            if live_air(state, shooter)["task"]["active"] is not False:
                raise SmokeFailure("Cancelled attack became active again", air_diagnostics(shooter, actor_row(state, shooter)["aircraft"]))
            live_air(state, ally, "navigate")
            batch = session.events()
            quiet_end_cursor = batch["cursor"]
            for event in batch["events"]:
                if event.get("type") == "gun_bullet_created" and event.get("attackerPersistentId") == shooter.aircraft_id:
                    raise SmokeFailure("New native shooter gun bullet appeared after cancellation", {"event": event})
        report["checks"]["cancelIsolationNoNewShooterBullets"] = True
        report["quietObservedSeconds"] = clock() - quiet_started
        report["quietWindow"] = {"cursorStart": report["quietCursor"], "cursorEnd": quiet_end_cursor,
                                 "requestedSeconds": quiet_seconds, "observedSeconds": report["quietObservedSeconds"]}
        if report["quietObservedSeconds"] < quiet_seconds:
            raise SmokeFailure("Quiet firing observation was shorter than requested", report["quietWindow"])
        stage = "same-airframe resumed physical turn"
        air = live_air(state, shooter)
        fx, fz = air["forward"][0], air["forward"][2]
        length, angle = math.hypot(fx, fz), math.radians(20)
        if length < 0.1:
            raise SmokeFailure("Resumed heading unavailable", air_diagnostics(shooter, air))
        destination = (air["position"][0] + (fx * math.cos(angle) + fz * math.sin(angle)) / length * 20000,
                       air["position"][1], air["position"][2] + (fz * math.cos(angle) - fx * math.sin(angle)) / length * 20000)
        shooter.goto(destination, 110)
        state = clean_state(session, 5, allowed_errors)
        report["resumedNavigation"] = observe(resumed_seconds, [shooter, ally], turning=shooter)
        report["checks"]["sameNativePlayerAirframeResumedTurn"] = True
        report["passed"] = True
        return report
    except Exception as failure:
        observed = []
        diagnostic_state = getattr(failure, "last_state", None) or getattr(failure, "state", None) or state
        if isinstance(diagnostic_state, dict):
            for h in owned:
                rows = [r for r in diagnostic_state.get("actors", []) if r.get("actor") == h.name]
                if rows and isinstance(rows[0].get("aircraft"), dict):
                    observed.append(air_diagnostics(h, rows[0]["aircraft"]))
        raise SmokeFailure("Task transitions failed at " + stage, {"stage": stage, "cause": str(failure),
                           "details": getattr(failure, "diagnostics", {}), "inspection": inspection,
                           "evidence": evidence.summary() if evidence else None, "lastObservedActors": observed,
                           "completedChecks": report["checks"]}) from failure
    finally:
        primary = sys.exc_info()[1]
        failures = cleanup_owned(session, owned)
        if failures:
            raise SmokeFailure("Task transition owned cleanup incomplete", {"cleanup": failures,
                               "primaryFailure": str(primary) if primary else None,
                               "primaryDiagnostics": getattr(primary, "diagnostics", {})})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--navigation-seconds", type=float, default=60)
    parser.add_argument("--resumed-seconds", type=float, default=30)
    parser.add_argument("--allow-headless-texture-error", action="store_true")
    args = parser.parse_args()
    try:
        result = run(Session(timeout=5), navigation_seconds=args.navigation_seconds, resumed_seconds=args.resumed_seconds,
                     allowed_errors=TEXTURE_ERROR if args.allow_headless_texture_error else ())
    except Exception as failure:
        result = {"passed": False, "error": str(failure), "diagnostics": getattr(failure, "diagnostics", {})}
    print(json.dumps(result, indent=2, allow_nan=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
