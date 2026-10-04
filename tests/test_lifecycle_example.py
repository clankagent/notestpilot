"""Developer-script state/identity gates using simulated native snapshots.

These tests validate orchestration and failure handling, never game physics.
"""

from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
import unittest

from notestpilot import CommandError, StaleActor


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
sys.path.insert(0, str(EXAMPLES))
spec = importlib.util.spec_from_file_location("lifecycle_example", EXAMPLES / "lifecycle.py")
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


class Clock:
    time = 0
    def __call__(self):
        return self.time
    def sleep(self, seconds):
        self.time += seconds


@dataclass(frozen=True)
class Handle:
    session: object
    name: str
    player_id: int
    aircraft_id: int | None
    instance: str = "fixture-runtime"

    def _check(self):
        current = self.session.actor(self.name)
        if (current.player_id, current.aircraft_id) != (self.player_id, self.aircraft_id):
            raise StaleActor("Fixture stale identity")
    def spawn(self, **args):
        self._check()
        record = self.session.registry[self.name]
        generation = self.session.next_aircraft
        self.session.next_aircraft += 1
        record["aircraft"] = {"id": generation, "linkedPlayerId": self.player_id, "initialized": True,
            "localSim": True, "remoteSim": False, "disabled": False, "position": list(args["position"]),
            "forward": [0, 0, 1], "health": [
                {"seat": 0, "dead": False, "ejected": False, "state": None, "npcBrainInitialized": False},
                {"seat": 1, "dead": False, "ejected": False, "state": None, "npcBrainInitialized": False}],
            "task": {"active": False, "outcome": "idle", "ticks": 0}}
        return Handle(self.session, self.name, self.player_id, generation)
    def goto(self, destination, speed):
        self._check()
        air = self.session.registry[self.name]["aircraft"]
        air["task"] = {"active": True, "outcome": "running", "ticks": 0}
        air["health"][0]["state"] = self.session.controlling_state
        air["east"] = destination[0] > air["position"][0] + 1000
        self.session.commands.append((self.name, "goto"))
    def cancel(self):
        self._check()
        self.session.registry[self.name]["aircraft"]["task"]["active"] = False
        self.session.commands.append((self.name, "cancel"))
    def eject(self):
        self._check()
        air = self.session.registry[self.name]["aircraft"]
        air["disabled"] = True
        air["task"]["active"] = False
        air["health"][0]["ejected"] = True
        self.session.commands.append((self.name, "eject"))
    def remove(self):
        self._check()
        if self.session.fail_cleanup and self.session.removal_count >= 1:
            raise RuntimeError("Fixture cleanup transport failed")
        self.session.removal_count += 1
        self.session.commands.append((self.name, "remove"))
        del self.session.registry[self.name]


class NativeSnapshots:
    timeout = 1
    def __init__(self, *, turn_observed=True, fail_cleanup=False, controlling_state="DirectedPilot"):
        self.registry = {"unrelated": {"actor": "unrelated", "playerId": 900, "aircraft": None}}
        self.next_player, self.next_aircraft = 10, 100
        self.commands, self.creation_counts, self.rejected = [], {}, []
        self.turn_observed, self.fail_cleanup, self.removal_count = turn_observed, fail_cleanup, 0
        self.controlling_state = controlling_state
    def state(self):
        for record in self.registry.values():
            air = record["aircraft"]
            if air and air["task"]["active"]:
                air["task"]["ticks"] += 40
                if air.get("east"):
                    air["forward"] = [0.7, 0, 0.7] if self.turn_observed else [0, 0, 1]
                    air["position"][0 if self.turn_observed else 2] += 20
                else:
                    air["forward"] = [0, 0, 1]
                    air["position"][2] += 30
        # Deep-copy to keep first position snapshots distinct from later samples.
        import copy
        return {"missionRunning": True, "serverActive": True, "aircraftTypes": ["COIN"],
                "factions": ["native-a", "native-b"], "airbases": [
                    {"name": "observed-a", "faction": "native-a", "position": [10, 20, 30]},
                    {"name": "observed-b", "faction": "native-b", "position": [40, 50, 60]}],
                "actors": copy.deepcopy(list(self.registry.values()))}
    def wait_for(self, predicate, description, **kwargs):
        for _ in range(4):
            state = self.state()
            if predicate(state):
                return state
        raise TimeoutError(description)
    def create(self, name, faction):
        self.creation_counts[name] = self.creation_counts.get(name, 0) + 1
        player = self.next_player
        self.next_player += 1
        self.registry[name] = {"actor": name, "playerId": player, "aircraft": None}
        return Handle(self, name, player, None)
    def actor(self, name):
        if name not in self.registry:
            raise StaleActor("Fixture actor absent")
        record = self.registry[name]
        return Handle(self, name, record["playerId"], None if record["aircraft"] is None else record["aircraft"]["id"])
    def call(self, command, args):
        current = self.actor(args["actor"])
        if current.player_id != args["playerId"] or current.aircraft_id != args["aircraftId"]:
            self.rejected.append(command)
            raise CommandError("Fixture native identity guard rejected mutation")
        raise AssertionError("Stale command unexpectedly matched current identity")


class LifecycleTests(unittest.TestCase):
    def execute(self, session):
        clock = Clock()
        return example.run(session, clock=clock, sleep=clock.sleep)

    def test_full_lifecycle_assertions_preserve_unrelated_actor(self):
        session = NativeSnapshots()
        report = self.execute(session)
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["checks"]), 6)
        self.assertEqual(session.registry.keys(), {"unrelated"})
        self.assertEqual(session.rejected, ["actor.cancel", "actor.remove"])
        self.assertEqual(sorted(session.creation_counts.values()), [1, 2])
        self.assertEqual(sum(command == "eject" for _, command in session.commands), 1)

    def test_ticks_and_displacement_do_not_substitute_for_observed_turn(self):
        session = NativeSnapshots(turn_observed=False)
        with self.assertRaises(example.SmokeFailure) as caught:
            self.execute(session)
        self.assertIn("physical navigation", str(caught.exception))
        self.assertIn("turn lacked", caught.exception.diagnostics["cause"])
        self.assertEqual(session.registry.keys(), {"unrelated"})
        self.assertFalse(any(command == "eject" for _, command in session.commands))

    def test_cleanup_failure_prevents_success_report_and_preserves_diagnostics(self):
        session = NativeSnapshots(fail_cleanup=True)
        with self.assertRaises(example.SmokeFailure) as caught:
            self.execute(session)
        self.assertIn("cleanup incomplete", str(caught.exception))
        self.assertTrue(caught.exception.diagnostics["cleanup"])
        self.assertIn("unrelated", session.registry)

    def test_native_link_and_npc_state_gates_fail_closed(self):
        session = NativeSnapshots()
        handle = session.create("owned", "native-a").spawn(position=(1, 2, 3))
        air = session.registry["owned"]["aircraft"]
        air["linkedPlayerId"] = 12345
        with self.assertRaises(example.SmokeFailure):
            example.linked_aircraft(session.state(), handle)
        air["linkedPlayerId"] = handle.player_id
        air["health"][0]["npcBrainInitialized"] = True
        with self.assertRaises(example.SmokeFailure):
            example.linked_aircraft(session.state(), handle)

    def test_two_crew_explicit_controlling_seat_allows_idle_passenger(self):
        session = NativeSnapshots()
        handle = session.create("owned", "native-a").spawn(position=(1, 2, 3))
        handle.goto((1, 2, 3000), 100)
        air = session.registry["owned"]["aircraft"]
        self.assertIsNone(air["health"][1]["state"])
        # Reverse serialized ordering to prove selection uses native seat ID.
        air["health"].reverse()
        state = session.state()
        self.assertTrue(example.linked_aircraft(state, handle))
        example.require_directed_state(handle, state["actors"][-1]["aircraft"])

    def test_wrong_controlling_state_reports_actual_two_crew_health_and_task(self):
        session = NativeSnapshots()
        handle = session.create("owned", "native-a").spawn(position=(1, 2, 3))
        handle.goto((1, 2, 3000), 100)
        air = session.registry["owned"]["aircraft"]
        air["health"][0]["state"] = "PilotPlayerState"
        with self.assertRaises(example.SmokeFailure) as caught:
            example.require_directed_state(handle, air)
        diagnostic = caught.exception.diagnostics
        self.assertEqual(diagnostic["actor"], "owned")
        self.assertEqual(diagnostic["aircraft"]["health"][0]["state"], "PilotPlayerState")
        self.assertTrue(diagnostic["aircraft"]["task"]["active"])

    def test_passenger_native_brain_still_fails(self):
        session = NativeSnapshots()
        handle = session.create("owned", "native-a").spawn(position=(1, 2, 3))
        session.registry["owned"]["aircraft"]["health"][1]["npcBrainInitialized"] = True
        with self.assertRaises(example.SmokeFailure):
            example.linked_aircraft(session.state(), handle)

    def test_workflow_failure_preserves_nested_crew_and_task_diagnostics(self):
        session = NativeSnapshots(controlling_state="PilotPlayerState")
        with self.assertRaises(example.SmokeFailure) as caught:
            self.execute(session)
        details = caught.exception.diagnostics["details"]
        self.assertTrue(details["actor"].endswith("turner"))
        self.assertEqual(details["aircraft"]["health"][0]["state"], "PilotPlayerState")
        self.assertIsNone(details["aircraft"]["health"][1]["state"])
        self.assertTrue(details["aircraft"]["task"]["active"])
        self.assertTrue(caught.exception.diagnostics["lastObservedActors"])
        self.assertEqual(session.registry.keys(), {"unrelated"})


if __name__ == "__main__":
    unittest.main()
