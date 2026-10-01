import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
from notestpilot.runner import Bridge, TestFailure, matches, matches_with_peers, observe, prepare_lab, run_steps, validate_scenario, wait_for, write_report


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
