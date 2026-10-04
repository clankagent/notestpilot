"""Consumer transition-script tests with simulated snapshots, not game physics."""

import copy
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import task_transitions as scenario
from test_lifecycle_example import Clock, Handle, NativeSnapshots
from test_explicit_target_example import proof_events
from notestpilot.client import TransportError


class TransitionActor(Handle):
    def spawn(self, **args):
        result = super().spawn(**args)
        self.session.spawn_args.append(copy.deepcopy(args))
        if self.session.lost_target_spawn and self.name.endswith("-target"):
            raise TransportError("Target spawned but its reply was lost")
        return TransitionActor(self.session, self.name, self.player_id, result.aircraft_id)

    def goto(self, destination, speed):
        super().goto(destination, speed)
        air = self.session.registry[self.name]["aircraft"]
        air["task"].update(task="navigate", destination=list(destination))

    def inspect_target(self, target):
        return {"targetId": target, "disabled": False, "opposing": True, "known": True, "accurate": True}

    def attack(self, target, station, seconds):
        self.session.attacks.append((self.player_id, self.aircraft_id, target, station))
        self.session.shooter = self
        self.session.target_id = target
        if not self.session.ignore_attack:
            air = self.session.registry[self.name]["aircraft"]
            air["task"] = {"active": True, "outcome": "running", "task": "attack", "ticks": 0,
                           "targetNetId": target, "station": station}

    def cancel(self):
        super().cancel()
        self.session.cancelled = True
        if self.session.cancel_ally:
            for row in self.session.registry.values():
                if row["actor"].endswith("-ally"):
                    row["aircraft"]["task"]["active"] = False


class TransitionSession(NativeSnapshots):
    def __init__(self, *, ignore_attack=False, missing_damage=False, cancel_ally=False, stale_fire=False,
                 late_damage=False, lost_target_spawn=False):
        super().__init__()
        self.ignore_attack, self.missing_damage = ignore_attack, missing_damage
        self.cancel_ally, self.stale_fire = cancel_ally, stale_fire
        self.lost_target_spawn = lost_target_spawn
        self.late_damage = late_damage
        self.spawn_args, self.attacks, self.event_seq = [], [], 0
        self.shooter, self.target_id, self.cancelled = None, None, False
        self.after_cancel_reads = 0

    def actor(self, name):
        result = super().actor(name)
        return TransitionActor(self, name, result.player_id, result.aircraft_id)

    def create(self, name, faction):
        super().create(name, faction)
        return self.actor(name)

    def state(self):
        headings = {name: list(row["aircraft"]["forward"]) for name, row in self.registry.items() if row["aircraft"]}
        state = super().state()
        for row in state["actors"]:
            air = row["aircraft"]
            if not air:
                continue
            air["netId"] = air["id"]
            air["stations"] = [{"index": 2, "fixedGun": True, "ammo": 100}]
            if air["task"].get("task") == "navigate" and air["task"]["active"]:
                target = air["task"]["destination"]
                desired = math.atan2(target[0] - air["position"][0], target[2] - air["position"][2])
                original = math.atan2(headings[row["actor"]][0], headings[row["actor"]][2])
                difference = math.atan2(math.sin(desired - original), math.cos(desired - original))
                angle = original + max(-0.04, min(0.04, difference))
                air["forward"] = [math.sin(angle), 0, math.cos(angle)]
            else:
                air["forward"] = headings[row["actor"]]
            air["velocity"] = [v * 110 for v in air["forward"]]
            self.registry[row["actor"]]["aircraft"].update(forward=air["forward"][:], velocity=air["velocity"][:])
        return state

    def events(self):
        rows = []
        if self.shooter and not self.cancelled:
            rows = proof_events(self.shooter.name, self.shooter.aircraft_id, self.target_id)
            if self.missing_damage:
                rows = rows[:2]
        elif self.cancelled:
            self.after_cancel_reads += 1
            if self.stale_fire and self.after_cancel_reads > 1:
                rows = proof_events(self.shooter.name, self.shooter.aircraft_id, self.target_id)[:1]
            elif self.late_damage and self.after_cancel_reads > 1:
                rows = proof_events(self.shooter.name, self.shooter.aircraft_id, self.target_id)[1:]
        for row in rows:
            self.event_seq += 1
            row["seq"] = self.event_seq
        return {"cursor": self.event_seq, "events": rows, "overflow": False, "observerErrors": 0}


class TransitionTests(unittest.TestCase):
    def execute(self, session):
        clock = Clock()
        return scenario.run(session, navigation_seconds=6, resumed_seconds=6, attack_seconds=1,
                            quiet_seconds=1, clock=clock, sleep=clock.sleep)

    def test_native_identity_survives_all_transitions_with_exact_target_proof(self):
        session = TransitionSession()
        report = self.execute(session)
        self.assertTrue(report["passed"])
        player, airframe, target, station = session.attacks[0]
        self.assertEqual((player, airframe), (report["identity"]["playerId"], report["identity"]["aircraftId"]))
        self.assertEqual(station, 2)
        self.assertTrue(report["evidence"]["attributedPositiveAppliedDamage"])
        self.assertEqual(len(session.spawn_args), 3)
        self.assertIn("rotation", session.spawn_args[-1])
        self.assertEqual(session.registry.keys(), {"unrelated"})
        self.assertEqual(sum(command == "cancel" for _, command in session.commands), 1)

    def test_ignored_attack_transition_fails_despite_available_native_proof(self):
        session = TransitionSession(ignore_attack=True)
        with self.assertRaises(scenario.SmokeFailure) as caught:
            self.execute(session)
        self.assertIn("transition was not retained", caught.exception.diagnostics["cause"])
        self.assertEqual(len(session.attacks), 1)
        self.assertEqual(session.registry.keys(), {"unrelated"})

    def test_hit_without_applied_damage_fails_honestly_without_retry(self):
        session = TransitionSession(missing_damage=True)
        with self.assertRaises(scenario.SmokeFailure) as caught:
            self.execute(session)
        self.assertIn("application remains unobserved", caught.exception.diagnostics["cause"])
        self.assertTrue(caught.exception.diagnostics["evidence"]["exactVictimHit"])
        self.assertFalse(caught.exception.diagnostics["evidence"]["attributedPositiveAppliedDamage"])
        self.assertEqual(len(session.attacks), 1)
        self.assertEqual(session.registry.keys(), {"unrelated"})

    def test_cancel_must_preserve_friendly_task(self):
        session = TransitionSession(cancel_ally=True)
        with self.assertRaises(scenario.SmokeFailure) as caught:
            self.execute(session)
        self.assertIn("cancel isolation", caught.exception.diagnostics["stage"])
        self.assertEqual(session.registry.keys(), {"unrelated"})

    def test_new_shooter_bullet_in_quiet_window_fails(self):
        session = TransitionSession(stale_fire=True)
        with self.assertRaises(scenario.SmokeFailure) as caught:
            self.execute(session)
        self.assertIn("bullet appeared after cancellation", caught.exception.diagnostics["cause"])
        self.assertEqual(session.registry.keys(), {"unrelated"})

    def test_lost_target_spawn_reply_rebinds_only_for_cleanup_without_retry(self):
        session = TransitionSession(lost_target_spawn=True)
        with self.assertRaises(scenario.SmokeFailure) as caught:
            self.execute(session)
        self.assertIn("reply was lost", caught.exception.diagnostics["cause"])
        self.assertEqual(len(session.spawn_args), 3)
        self.assertFalse(session.attacks)
        self.assertEqual(session.registry.keys(), {"unrelated"})

    def test_late_inflight_hit_and_damage_do_not_violate_quiet_firing_window(self):
        session = TransitionSession(late_damage=True)
        report = self.execute(session)
        self.assertTrue(report["passed"])
        self.assertGreater(report["quietCursor"], report["attackCursor"])
        self.assertGreaterEqual(report["quietObservedSeconds"], 1)
        self.assertEqual(report["quietWindow"]["cursorStart"], report["quietCursor"])
        self.assertGreater(report["quietWindow"]["cursorEnd"], report["quietWindow"]["cursorStart"])
        self.assertGreaterEqual(report["quietWindow"]["observedSeconds"], report["quietWindow"]["requestedSeconds"])
        self.assertGreater(session.after_cancel_reads, 1)
        self.assertEqual(session.registry.keys(), {"unrelated"})


if __name__ == "__main__":
    unittest.main()
