import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from notestpilot.runner import TestCancelled, finish_lab, observe, run_steps, write_report


class CancellationTests(unittest.TestCase):
    def test_cancelled_observation_retains_partial_measurements(self):
        class Peer:
            def status(self): return {"running": True, "remotePlayers": 2}
        peer = Peer()
        specification = {"seconds": 600, "expect": [{"path": "running", "equals": True}]}
        with patch("notestpilot.runner.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(TestCancelled) as failure:
                observe(peer, {"server": peer}, specification)
        evidence = failure.exception.evidence
        self.assertTrue(evidence["incomplete"])
        self.assertEqual(600, evidence["seconds"])
        self.assertLess(evidence["completedSeconds"], 600)
        self.assertEqual(2, evidence["samples"][0]["server"]["remotePlayers"])

    def test_cancelled_action_keeps_prior_pass_and_valid_failure_report(self):
        class Peer:
            def call(self, command, *args, **kwargs):
                if command == "connect": raise KeyboardInterrupt()
                return {"accepted": True}
        report = {"scenario": "cancelled", "steps": []}
        scenario = {"name": "cancelled", "steps": [
            {"target": "client", "command": "status"},
            {"target": "client", "command": "connect"}]}
        with self.assertRaises(TestCancelled):
            run_steps(scenario, {"client": Peer()}, report)
        self.assertTrue(report["steps"][0]["passed"])
        self.assertFalse(report["steps"][1]["passed"])
        self.assertTrue(report["steps"][1]["cancelled"])
        with tempfile.TemporaryDirectory() as temporary:
            write_report(Path(temporary), report)
            saved = json.loads((Path(temporary) / "result.json").read_text())
            self.assertFalse(saved["steps"][1]["passed"])
            suite = ET.parse(Path(temporary) / "junit.xml").getroot()
            self.assertEqual("1", suite.get("failures"))
            self.assertEqual(2, len(suite.findall("testcase")))

    def test_stop_during_cleanup_cannot_leave_a_passing_report(self):
        class Child:
            alive = True
            stopped = False
            def poll(self): return None if self.alive else 0
            def terminate(self):
                if not self.stopped:
                    self.stopped = True
                    raise TestCancelled("Cancelled by SIGTERM during cleanup")
                self.alive = False
            def wait(self, timeout): return 0
        child = Child()
        report = {"runId": "cleanup-stop", "scenario": "finished actions", "passed": True,
                  "steps": [{"name": "flight", "target": "server", "passed": True, "seconds": 10}]}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.assertTrue(finish_lab(directory, report, {"server": child}))
            saved = json.loads((directory / "result.json").read_text())
            self.assertFalse(saved["passed"])
            self.assertTrue(saved["cancelled"])
            self.assertTrue(saved["steps"][0]["passed"])
            self.assertEqual("cancelled", json.loads((directory / "progress.json").read_text())["state"])
            self.assertEqual("1", ET.parse(directory / "junit.xml").getroot().get("failures"))
            self.assertFalse(child.alive)

    @unittest.skipIf(os.name == "nt", "Windows SIGTERM terminates Python without invoking a handler")
    def test_real_sigterm_saves_cancelled_result_during_preparation(self):
        # A real Python process/signal, with lab preparation held BEFORE any game
        # launch. No game executable or server is involved in this tooling test.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario = root / "scenario.json"
            scenario.write_text(json.dumps({"name": "signal", "steps": [
                {"target": "client", "command": "connect"}]}))
            ready, output = root / "ready", root / "result"
            script = root / "probe.py"
            script.write_text('''import sys,time
from pathlib import Path
from notestpilot import runner
ready=Path(sys.argv.pop(1))
def prepare(*args):
    ready.write_text("waiting before game launch")
    time.sleep(60)
    raise AssertionError("must be cancelled before game launch")
runner.prepare_lab=prepare
raise SystemExit(runner.main())
''')
            process = subprocess.Popen([sys.executable, str(script), str(ready), str(scenario),
                "--game", str(root / "unused-game"), "--lab", str(root / "unused-lab"),
                "--output", str(output), "--execute"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "cancellation probe never reached preparation")
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=10)
                self.assertEqual(1, process.returncode)
                report = json.loads((output / "result.json").read_text())
                progress = json.loads((output / "progress.json").read_text())
                self.assertTrue(report["cancelled"])
                self.assertFalse(report["passed"])
                self.assertEqual("cancelled", progress["state"])
                self.assertEqual(report["runId"], progress["runId"])
                self.assertIn("SIGTERM", report["error"])
                self.assertEqual("1", ET.parse(output / "junit.xml").getroot().get("failures"))
                self.assertFalse((root / "unused-lab").exists())
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()