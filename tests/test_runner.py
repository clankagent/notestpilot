import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
from notestpilot.runner import Bridge, TestFailure, matches, matches_with_peers, observe, prepare_lab, run_steps, validate_scenario, wait_for, window_telemetry, write_report


def reply_once(handler):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    def serve():
        try:
            with listener.accept()[0] as connection:
                data = bytearray()
                while b"\n" not in data:
                    data.extend(connection.recv(4096))
                request = json.loads(data)
                connection.sendall(json.dumps(handler(request)).encode() + b"\n")
        finally:
            listener.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, thread


class RunnerTests(unittest.TestCase):
    def test_ammunition_upper_bound_rejects_missing_and_non_numeric_state(self):
        assertion = {"path": "ammo", "atMost": 999}
        self.assertTrue(matches({"ammo": 998}, assertion))
        for state in ({"ammo": 1000}, {}, {"ammo": True}, {"ammo": float("nan")}, {"ammo": "998"}):
            self.assertFalse(matches(state, assertion))
        validate_scenario({"name": "ammo", "steps": [{"target": "server", "expect": [assertion]}]})

    def test_repeated_actions_drive_each_client_during_observation(self):
        class Peer:
            def __init__(self): self.calls = []
            def call(self, command, args):
                self.calls.append((command, args))
                return {"accepted": True}
            def status(self): return {"running": True}
        server, client1, client2 = Peer(), Peer(), Peer()
        peers = {"server": server, "client1": client1, "client2": client2}
        spec = {"seconds": 10, "expect": [{"path": "running", "equals": True}], "actions": [
            {"target": "client1", "command": "fly", "args": {"seconds": 45}, "everySeconds": 2},
            {"target": "client2", "command": "fly", "args": {"seconds": 45}, "everySeconds": 2}]}
        with patch("notestpilot.runner.time.monotonic", side_effect=[0, 0, 0, 0, 3, 3, 3, 11]), patch("notestpilot.runner.time.sleep"):
            result = observe(server, peers, spec)
        self.assertEqual(2, len(client1.calls))
        self.assertEqual(2, len(client2.calls))
        self.assertEqual(4, len(result["actions"]))

    def test_rejected_repeated_action_is_fatal(self):
        class Peer:
            def call(self, *args): return {"accepted": False}
        peer = Peer()
        spec = {"seconds": 10, "expect": [{"path": "running", "equals": True}],
                "actions": [{"target": "client", "command": "fly"}]}
        with self.assertRaisesRegex(TestFailure, "action rejected"):
            observe(peer, {"client": peer}, spec)

    def test_window_telemetry_excludes_loading_and_separates_process_cpu(self):
        first = {"realtimeSeconds": 100, "gameSeconds": 70, "processCpuSeconds": 200,
                 "frames": 5000, "fixedSteps": 3000, "frameBuckets": [1, 2, 3, 4, 5, 99]}
        last = {"realtimeSeconds": 110, "gameSeconds": 80, "processCpuSeconds": 215,
                "frames": 5500, "fixedSteps": 3500, "frameBuckets": [201, 202, 103, 4, 5, 99]}
        result = window_telemetry(first, last)
        self.assertEqual(150, result["cpuPercentOneCore"])
        self.assertEqual(50, result["framesPerSecond"])
        self.assertEqual([200, 200, 100, 0, 0, 0], result["frameBucketCounts"])
        self.assertEqual(10, result["gameSeconds"])

    def test_failed_observation_preserves_last_state_and_other_clients(self):
        class Peer:
            def status(self): return {"running": False, "observation": {"events": ["returned"]}}
        peer = Peer()
        with self.assertRaises(TestFailure) as failure:
            observe(peer, {"server": peer, "client": peer},
                    {"seconds": 1, "expect": [{"path": "running", "equals": True}]})
        self.assertEqual(["returned"], failure.exception.evidence["samples"][0]["server"]["observation"]["events"])
        self.assertIn("client", failure.exception.evidence["samples"][0])

    def test_socket_command_correlates_reply(self):
        port, thread = reply_once(lambda r: {"id": r["id"], "ok": True, "result": {"accepted": True}})
        self.assertTrue(Bridge(port, "token").call("connect")["accepted"])
        thread.join(2)

    def test_wrong_request_reply_fails(self):
        port, thread = reply_once(lambda r: {"id": "old-request", "ok": True, "result": {}})
        with self.assertRaisesRegex(TestFailure, "different request"):
            Bridge(port, "token").call("connect")
        thread.join(2)

    def test_bridge_action_failure_is_not_transport_success(self):
        port, thread = reply_once(lambda r: {"id": r["id"], "ok": False, "error": "No local game player yet"})
        with self.assertRaisesRegex(TestFailure, "No local"):
            Bridge(port, "token").call("spawn")
        thread.join(2)

    def test_missing_state_does_not_pass_null_assertion(self):
        self.assertFalse(matches({}, {"path": "localPlayerNetId", "equals": None}))
        self.assertFalse(matches({}, {"path": "localPlayerNetId", "notNull": False}))

    def test_array_and_numeric_assertions(self):
        self.assertTrue(matches({"players": [{"netId": 42}]}, {"path": "players.0.netId", "equals": 42}))
        self.assertFalse(matches({"units": True}, {"path": "units", "atLeast": 1}))

    def test_peer_identity_must_exist_and_match(self):
        class Peer:
            def status(self): return {"id": 42}
        assertion = {"path": "player", "equalsFrom": {"target": "client", "path": "id"}}
        self.assertTrue(matches_with_peers({"player": 42}, assertion, {"client": Peer()}))
        self.assertFalse(matches_with_peers({"player": 99}, assertion, {"client": Peer()}))
        self.assertFalse(matches_with_peers({}, assertion, {"client": Peer()}))
        Peer.status = lambda self: {"id": None}
        self.assertFalse(matches_with_peers({"player": None}, assertion, {"client": Peer()}))

    def test_observation_checks_errors_on_unselected_peer(self):
        class Healthy:
            def status(self): return {"running": True}
        class Broken:
            def status(self): raise TestFailure("Other client crashed")
        selected = Healthy()
        with self.assertRaisesRegex(TestFailure, "Other client crashed"):
            observe(selected, {"server": selected, "client": Broken()},
                    {"seconds": 1, "expect": [{"path": "running", "equals": True}]})

    def test_motion_cannot_pass_without_displacement(self):
        class Peer:
            def status(self): return {"running": True, "players": [{"aircraftNetId": 7, "position": [1, 2, 3]}]}
        peer = Peer()
        specification = {"seconds": 1, "expect": [{"path": "running", "equals": True}],
                         "motion": [{"path": "players.0", "minimumMetres": 2}]}
        with patch("notestpilot.runner.time.monotonic", side_effect=[0, 0, 2]), patch("notestpilot.runner.time.sleep"):
            with self.assertRaisesRegex(TestFailure, "moved only"):
                observe(peer, {"server": peer}, specification)

    def test_motion_requires_the_same_aircraft(self):
        class Peer:
            identity = 7
            def status(self):
                self.identity += 1
                return {"running": True, "players": [{"aircraftNetId": self.identity, "position": [self.identity * 10, 2, 3]}]}
        peer = Peer()
        specification = {"seconds": 1, "expect": [{"path": "running", "equals": True}],
                         "motion": [{"path": "players.0", "minimumMetres": 2}]}
        with patch("notestpilot.runner.time.monotonic", side_effect=[0, 0, 0.5]), patch("notestpilot.runner.time.sleep"):
            with self.assertRaisesRegex(TestFailure, "aircraft changed"):
                observe(peer, {"server": peer}, specification)

    def test_motion_passes_for_measured_global_displacement(self):
        class Peer:
            x = 0
            def status(self):
                self.x += 3
                return {"running": True, "players": [{"aircraftNetId": 7, "position": [self.x, 2, 3]}]}
        peer = Peer()
        specification = {"seconds": 1, "expect": [{"path": "running", "equals": True}],
                         "motion": [{"path": "players.0", "minimumMetres": 2}]}
        with patch("notestpilot.runner.time.monotonic", side_effect=[0, 0, 0.5, 2]), patch("notestpilot.runner.time.sleep"):
            result = observe(peer, {"server": peer}, specification)
        self.assertEqual(3, result["motion"]["players.0"]["maximumMetres"])

    def test_process_exit_is_immediately_fatal(self):
        class Process:
            returncode = 9
            def poll(self): return 9
        with self.assertRaisesRegex(TestFailure, "exited"):
            wait_for(Bridge(1, "token", Process()), [{"path": "menuReady", "equals": True}], 5)

    def test_new_game_error_fails_observation(self):
        bridge = Bridge(1, "token")
        bridge.baseline_errors = 2
        bridge.call = lambda *a, **kw: {"protocol": 1, "instance": "a", "errorCount": 3,
                                      "errors": [{"seq": 3, "type": "Exception", "message": "Player spawn broke"}]}
        with self.assertRaisesRegex(TestFailure, "Player spawn broke"):
            bridge.status()

    def test_render_message_does_not_hide_an_exception(self):
        bridge = Bridge(1, "token")
        bridge.baseline_errors = 0
        bridge.call = lambda *a, **kw: {"protocol": 1, "instance": "a", "errorCount": 1,
            "errors": [{"seq": 1, "type": "Exception", "message": "There is no texture data available to upload."}]}
        with self.assertRaisesRegex(TestFailure, "new errors"):
            bridge.status()

    def test_missing_error_history_is_fatal(self):
        bridge = Bridge(1, "token")
        bridge.baseline_errors = 0
        bridge.call = lambda *a, **kw: {"protocol": 1, "instance": "a", "errorCount": 40, "errors": []}
        with self.assertRaisesRegex(TestFailure, "overflowed"):
            bridge.status()

    def test_multiple_clients_have_explicit_targets(self):
        validate_scenario({"name": "two", "clients": 2, "steps": [{"target": "client2", "command": "connect"}]})
        with self.assertRaises(ValueError):
            validate_scenario({"name": "two", "clients": 2, "steps": [{"target": "client3", "command": "connect"}]})

    def test_restart_fails_even_when_state_looks_good(self):
        bridge = Bridge(1, "token")
        bridge.instance = "previous"
        bridge.call = lambda *a, **kw: {"protocol": 1, "instance": "new", "errorCount": 0}
        with self.assertRaisesRegex(TestFailure, "restarted"):
            bridge.status()

    def test_rejected_action_creates_failed_junit_case(self):
        class Fake:
            def call(self, *a): return {"accepted": False}
        report = {"scenario": "spawn", "steps": []}
        scenario = {"name": "spawn", "steps": [{"target": "client", "command": "spawn"}]}
        with self.assertRaises(TestFailure):
            run_steps(scenario, {"client": Fake()}, report)
        with tempfile.TemporaryDirectory() as temporary:
            write_report(Path(temporary), report)
            xml = ET.parse(Path(temporary) / "junit.xml").getroot()
            self.assertEqual("1", xml.get("failures"))
            self.assertIsNotNone(xml.find("testcase/failure"))

    def test_invalid_scenario_cannot_launch(self):
        for step in ({"target": "remote", "command": "connect"}, {"target": "client", "expect": []},
                     {"target": "client", "command": "connect", "timeout": -1},
                     {"target": "client", "command": "shell"}):
            with self.subTest(step=step), self.assertRaises(ValueError):
                validate_scenario({"name": "bad", "steps": [step]})

    def test_lab_refuses_unmarked_existing_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, lab = Path(temporary) / "game", Path(temporary) / "existing"
            source.mkdir(); lab.mkdir()
            (lab / "valuable-file").write_text("preserve")
            with self.assertRaisesRegex(ValueError, "unmarked"):
                prepare_lab(source, lab)
            self.assertEqual("preserve", (lab / "valuable-file").read_text())

    def test_lab_cannot_be_inside_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "separate"):
                prepare_lab(Path(temporary), Path(temporary) / "lab")

    def test_lab_preserves_runtime_config_but_excludes_other_plugins(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, lab = Path(temporary) / "game", Path(temporary) / "lab"
            runtime = source / "NuclearOptionServer_Data/MonoBleedingEdge/etc/mono/config"
            runtime.parent.mkdir(parents=True)
            runtime.write_text("native DLL mapping")
            bridge = source / "BepInEx/plugins/NOTestPilot/NOTestPilot.Bridge.dll"
            bridge.parent.mkdir(parents=True)
            bridge.write_bytes(b"fixture bridge")
            (source / "BepInEx/plugins/production.dll").write_bytes(b"must not inherit")
            prepare_lab(source, lab)
            for role in ("server", "client"):
                self.assertEqual("native DLL mapping", (lab / role / runtime.relative_to(source)).read_text())
                self.assertFalse((lab / role / "BepInEx/plugins/production.dll").exists())


if __name__ == "__main__":
    unittest.main()
