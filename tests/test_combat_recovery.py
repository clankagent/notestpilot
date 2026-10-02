import copy
import unittest
import tempfile
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from unittest.mock import patch

from notestpilot.runner import TestFailure, observe, run_steps, validate_scenario, verified_missile_loss, write_report


def damage_fixture():
    incoming = {"action": "PartTakeDamage", "seconds": 100, "radarAltitude": 885,
                "playerNetId": 4, "aircraftNetId": 7,
                "callers": ["DamageEffects.ArmorPenetrate", "Missile.ServerFixedUpdate"],
                "damage": {"dealerValid": True, "dealerIsSelf": False}}
    destructive = {"action": "DestructivePartApplyDamage", "seconds": 100.1, "radarAltitude": 884,
                   "playerNetId": 4, "aircraftNetId": 7,
                   "damage": {"hitPointsBefore": 100, "predictedHitPointsAfter": -752.5, "part": {"partID": 19}}}
    return {"server": {"missionRunning": True, "players": [{"netId": 4, "aircraftNetId": 7, "speed": 47}],
                       "observation": {"realtimeSeconds": 113, "firstDamageEvents": [incoming, destructive]}},
            "client": {"localPlayerNetId": 4, "localPlayerAircraftNetId": 7,
                       "observation": {"firstDamageEvents": [copy.deepcopy(destructive)]}}}


ASSERTIONS = [{"path": "missionRunning", "equals": True}, {"path": "players.0.speed", "atLeast": 50}]
POLICY = {"resumeAt": "Eject", "actors": [{"target": "client", "playerPath": "players.0"}]}


class CombatRecoveryTests(unittest.TestCase):
    def test_status_error_retains_prior_samples_without_allowing_the_error(self):
        state = damage_fixture()["server"]
        state["players"][0]["speed"] = 100
        state.pop("observation")
        class Peer:
            def __init__(self): self.calls = 0
            def status(self):
                self.calls += 1
                if self.calls == 2:
                    raise TestFailure("Game error: Airbrake.Update", {"gameErrors": ["rotation"]})
                return state
        peer = Peer()
        with patch("notestpilot.runner.time.sleep"), self.assertRaises(TestFailure) as failure:
            observe(peer, {"server": peer}, {"seconds": 180, "expect": ASSERTIONS})
        self.assertEqual("Game error: Airbrake.Update", str(failure.exception))
        self.assertEqual(["rotation"], failure.exception.evidence["gameErrors"])
        self.assertEqual(1, len(failure.exception.evidence["samples"]))
        self.assertTrue(failure.exception.evidence["incomplete"])

    def test_requires_current_external_missile_damage_and_replication(self):
        original = damage_fixture()
        self.assertEqual(7, verified_missile_loss(original, ASSERTIONS, POLICY)[0]["aircraftNetId"])
        variants = []
        for field, value in [("dealerValid", False), ("dealerIsSelf", True)]:
            state = copy.deepcopy(original)
            state["server"]["observation"]["firstDamageEvents"][0]["damage"][field] = value
            variants.append(state)
        state = copy.deepcopy(original); state["server"]["observation"]["realtimeSeconds"] = 140; variants.append(state)
        state = copy.deepcopy(original); state["server"]["observation"]["firstDamageEvents"][0]["callers"] = ["AeroPart.OnCollisionEnter"]; variants.append(state)
        state = copy.deepcopy(original); state["client"]["observation"]["firstDamageEvents"] = []; variants.append(state)
        state = copy.deepcopy(original); state["client"]["localPlayerAircraftNetId"] = 8; variants.append(state)
        state = copy.deepcopy(original); state["server"]["observation"]["firstDamageEvents"][1]["aircraftNetId"] = 8; variants.append(state)
        state = copy.deepcopy(original); state["server"]["observation"]["firstDamageEvents"][1]["damage"]["hitPointsBefore"] = -10; variants.append(state)
        for state in variants:
            with self.subTest(state=state):
                self.assertIsNone(verified_missile_loss(state, ASSERTIONS, POLICY))

    def test_mission_failure_and_other_player_loss_cannot_be_hidden(self):
        states = damage_fixture()
        states["server"]["missionRunning"] = False
        self.assertIsNone(verified_missile_loss(states, ASSERTIONS, POLICY))
        states = damage_fixture()
        states["server"]["players"].append({"netId": 5, "aircraftNetId": 8, "speed": 1})
        self.assertIsNone(verified_missile_loss(states, [*ASSERTIONS, {"path": "players.1.speed", "atLeast": 50}], POLICY))

    def test_damage_interrupt_records_partial_flight_and_omitted_weapon_steps(self):
        states = damage_fixture()
        class Peer:
            def __init__(self, role): self.role, self.calls = role, []
            def status(self): return states[self.role]
            def call(self, command, args=None): self.calls.append(command); return {"accepted": True}
        server, client = Peer("server"), Peer("client")
        scenario = {"name": "recover", "steps": [
            {"name": "Flight", "target": "server", "observe": {"seconds": 180, "expect": ASSERTIONS}, "onCombatLoss": POLICY},
            {"name": "Rocket claim", "target": "client", "command": "next-weapon"},
            {"name": "Eject", "target": "client", "command": "eject"},
            {"name": "Reserve", "target": "client", "command": "reserve"}]}
        report = {"steps": []}
        with patch("notestpilot.runner.time.monotonic", return_value=0):
            run_steps(scenario, {"server": server, "client": client}, report)
        flight = report["steps"][0]
        self.assertEqual("verified-missile-loss", flight["result"]["outcome"])
        self.assertFalse(flight["result"]["survivalCompleted"])
        self.assertEqual(["Rocket claim"], flight["omittedSteps"])
        self.assertEqual(["eject", "reserve"], client.calls)
        self.assertEqual(3, len(report["steps"]))
        report.update(scenario="recover", passed=True)
        with tempfile.TemporaryDirectory() as temporary:
            write_report(Path(temporary), report)
            saved = json.loads((Path(temporary) / "result.json").read_text())
            self.assertFalse(saved["combatLossInterruptions"][0]["survivalCompleted"])
            output = ET.parse(Path(temporary) / "junit.xml").find("testcase/system-out")
            self.assertEqual(["Rocket claim"], json.loads(output.text)["omittedSteps"])

    def test_unknown_loss_stays_fatal_with_the_policy_enabled(self):
        states = damage_fixture()
        states["server"]["observation"]["firstDamageEvents"] = []
        class Peer:
            def __init__(self, role): self.role = role
            def status(self): return states[self.role]
        server = Peer("server")
        with self.assertRaisesRegex(TestFailure, "State changed"):
            observe(server, {"server": server, "client": Peer("client")}, {"seconds": 180, "expect": ASSERTIONS}, combat_policy=POLICY)

    def test_recovery_cannot_jump_back_or_skip_required_identity_capture(self):
        first = {"name": "Flight", "target": "server", "observe": {"seconds": 180, "expect": ASSERTIONS}, "onCombatLoss": copy.deepcopy(POLICY)}
        end = {"name": "Eject", "target": "client", "command": "eject"}
        validate_scenario({"name": "ok", "steps": [first, end]})
        with self.assertRaisesRegex(ValueError, "later"):
            validate_scenario({"name": "bad", "steps": [end, first]})
        capture = {"name": "Saved", "target": "server", "expect": [{"path": "id", "notNull": True}], "capture": "omitted"}
        check = {"name": "Check", "target": "server", "expect": [{"path": "id", "notEqualsSaved": {"capture": "omitted", "path": "id"}}]}
        with self.assertRaisesRegex(ValueError, "omit a required capture"):
            validate_scenario({"name": "bad", "steps": [first, capture, end, check]})


if __name__ == "__main__":
    unittest.main()
