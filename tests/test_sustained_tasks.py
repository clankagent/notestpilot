"""Sustained-script orchestration fixtures; these do not prove game flight."""

import sys
import math
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import sustained_tasks as scenario
from test_lifecycle_example import Clock, Handle, NativeSnapshots
from notestpilot.client import CreationUncertain, TransportError


class TaskHandle(Handle):
    def spawn(self, **args):
        result = super().spawn(**args)
        current = TaskHandle(result.session, result.name, result.player_id, result.aircraft_id)
        if self.session.lost_spawn:
            self.session.lost_spawn = False
            raise TransportError("spawn reply lost after applying")
        return current

    def goto(self, destination, speed):
        super().goto(destination, speed)
        self.session.registry[self.name]["aircraft"]["task"]["destination"] = list(destination)
        self.session.waypoints.setdefault(self.name, []).append(tuple(destination))

    def cancel(self):
        super().cancel()
        if self.session.cancel_both:
            for row in self.session.registry.values():
                if row["aircraft"]:
                    row["aircraft"]["task"]["active"] = False
        elif self.session.cancel_last:
            aircraft = [row["aircraft"] for row in self.session.registry.values() if row["aircraft"]]
            aircraft[-1]["task"]["active"] = False


class Snapshots(NativeSnapshots):
    def __init__(self, *, stall=False, ignore_heading=False, cancel_both=False, cancel_last=False,
                 lost_create=False, lost_spawn=False, fail_cleanup=False):
        super().__init__(fail_cleanup=fail_cleanup)
        self.stall, self.cancel_both = stall, cancel_both
        self.cancel_last = cancel_last
        self.ignore_heading = ignore_heading
        self.lost_create, self.lost_spawn = lost_create, lost_spawn
        self.waypoints = {}
        self.recoveries = 0

    def actor(self, name):
        result = super().actor(name)
        return TaskHandle(self, name, result.player_id, result.aircraft_id)

    def create(self, name, faction):
        super().create(name, faction)
        if self.lost_create:
            self.lost_create = False
            raise CreationUncertain(name, "a" * 32, "fixture-runtime", TransportError("reply lost after applying"))
        return self.actor(name)

    def recover_creation(self, failure):
        self.recoveries += 1
        return self.actor(failure.name)

    def state(self):
        if self.stall:
            positions = {name: list(row["aircraft"]["position"]) for name, row in self.registry.items() if row["aircraft"]}
        state = super().state()
        for row in state["actors"]:
            air = row["aircraft"]
            if air:
                if air["task"]["active"] and not self.ignore_heading:
                    target = air["task"]["destination"]
                    dx, dz = target[0] - air["position"][0], target[2] - air["position"][2]
                    length = math.hypot(dx, dz)
                    air["forward"] = [dx / length, 0, dz / length]
                elif self.ignore_heading:
                    air["forward"] = [0, 0, 1]
                air["velocity"] = [v * 110 for v in air["forward"]]
                self.registry[row["actor"]]["aircraft"]["forward"] = air["forward"][:]
                self.registry[row["actor"]]["aircraft"]["velocity"] = air["velocity"][:]
        if self.stall:
            for row in state["actors"]:
                if row["aircraft"]:
                    row["aircraft"]["position"] = positions[row["actor"]]
                    self.registry[row["actor"]]["aircraft"]["position"] = positions[row["actor"]]
        return state


class SustainedTests(unittest.TestCase):
    def execute(self, session, duration=12, **options):
        clock = Clock()
        return scenario.run(session, duration_seconds=duration, clock=clock, sleep=clock.sleep, **options)

    def test_four_actors_have_independent_identity_motion_and_alternating_turns(self):
        session = Snapshots()
        report = self.execute(session, actor_count=4, turn_degrees=15)
        self.assertTrue(report["passed"])
        first = report["phases"][0]["actors"]
        self.assertEqual(len({a["playerId"] for a in first}), 4)
        self.assertEqual(len({a["aircraftId"] for a in first}), 4)
        self.assertEqual([round(a["requestedTurnDegrees"]) for a in first], [15, -15, 15, -15])
        self.assertTrue(all(a["tickDelta"] > 0 and a["signedTurnDegrees"] >= 3 for phase in report["phases"] for a in phase["actors"]))
        self.assertEqual(len(session.waypoints), 4)
        self.assertEqual(session.registry.keys(), {"unrelated"})

    def test_four_actor_cancel_must_preserve_last_actor_not_only_second(self):
        session = Snapshots(cancel_last=True)
        with self.assertRaises(scenario.SmokeFailure) as caught:
            self.execute(session, actor_count=4, turn_degrees=15)
        self.assertEqual(caught.exception.diagnostics["stage"], "cancel isolation")
        self.assertIn("ended unexpectedly", caught.exception.diagnostics["cause"])
        self.assertTrue(caught.exception.diagnostics["details"]["actor"].endswith("-3"))
        self.assertEqual(session.registry.keys(), {"unrelated"})

    def test_invalid_actor_counts_and_turn_angles_reject_before_mutations(self):
        for count in (True, 1, 5, 2.0, "4"):
            with self.subTest(actor_count=count):
                session = Snapshots()
                with self.assertRaises(ValueError):
                    self.execute(session, actor_count=count)
                self.assertFalse(session.creation_counts)
        for angle in (True, 9.9, 35.1, float("nan"), float("inf"), "15"):
            with self.subTest(turn_degrees=angle):
                session = Snapshots()
                with self.assertRaises(ValueError):
                    self.execute(session, turn_degrees=angle)
                self.assertFalse(session.creation_counts)

    def test_short_run_changes_two_waypoints_and_is_never_sustained(self):
        session = Snapshots()
        report = self.execute(session)
        self.assertTrue(report["passed"])
        self.assertFalse(report["sustained"])
        self.assertTrue(report["checks"]["repeatedWaypoints"])
        self.assertTrue(report["checks"]["cancelIsolation"])
        self.assertEqual(len(report["phases"]), 2)
        self.assertTrue(all(len(set(points)) >= 2 for points in session.waypoints.values()))
        self.assertEqual(session.registry.keys(), {"unrelated"})
        self.assertEqual(sum(command == "cancel" for _, command in session.commands), 1)

    def test_five_minutes_requires_ten_measured_phases(self):
        report = self.execute(Snapshots(), 300)
        self.assertTrue(report["sustained"])
        self.assertGreaterEqual(report["observedSeconds"], 300)
        self.assertEqual(len(report["phases"]), 10)

    def test_policy_ticks_without_motion_fail_with_actual_actor_diagnostics(self):
        session = Snapshots(stall=True)
        with self.assertRaises(scenario.SmokeFailure) as caught:
            self.execute(session)
        self.assertIn("insufficient measured progress", caught.exception.diagnostics["cause"])
        self.assertTrue(caught.exception.diagnostics["details"]["aircraft"]["task"]["active"])
        self.assertTrue(caught.exception.diagnostics["lastObservedActors"])
        self.assertEqual(session.registry.keys(), {"unrelated"})

    def test_straight_motion_and_ticks_cannot_substitute_for_heading_response(self):
        session = Snapshots(ignore_heading=True)
        with self.assertRaises(scenario.SmokeFailure) as caught:
            self.execute(session)
        details = caught.exception.diagnostics["details"]
        self.assertGreaterEqual(details["response"]["displacement"], details["response"]["minimumDisplacement"])
        self.assertGreater(details["response"]["tickDelta"], 0)
        self.assertEqual(details["response"]["signedTurnDegrees"], 0)
        self.assertIn("signed heading", caught.exception.diagnostics["cause"])
        self.assertEqual(session.registry.keys(), {"unrelated"})

    def test_cancel_affecting_other_actor_fails_and_cleans_only_owned(self):
        session = Snapshots(cancel_both=True)
        with self.assertRaises(scenario.SmokeFailure) as caught:
            self.execute(session)
        self.assertEqual(caught.exception.diagnostics["stage"], "cancel isolation")
        self.assertEqual(session.registry.keys(), {"unrelated"})

    def test_lost_creation_reply_recovers_for_cleanup_without_replay_or_pass(self):
        session = Snapshots(lost_create=True)
        with self.assertRaises(scenario.SmokeFailure):
            self.execute(session)
        self.assertEqual(list(session.creation_counts.values()), [1])
        self.assertEqual(session.recoveries, 1)
        self.assertEqual(session.registry.keys(), {"unrelated"})

    def test_lost_spawn_reply_adopts_generation_only_for_cleanup(self):
        session = Snapshots(lost_spawn=True)
        with self.assertRaises(scenario.SmokeFailure):
            self.execute(session)
        self.assertEqual(session.next_aircraft, 101)
        self.assertEqual(session.registry.keys(), {"unrelated"})
        self.assertFalse(session.waypoints)

    def test_cleanup_failure_cannot_report_success(self):
        with self.assertRaises(scenario.SmokeFailure) as caught:
            self.execute(Snapshots(fail_cleanup=True))
        self.assertIn("cleanup incomplete", str(caught.exception))
        self.assertTrue(caught.exception.diagnostics["cleanup"])


if __name__ == "__main__":
    unittest.main()
