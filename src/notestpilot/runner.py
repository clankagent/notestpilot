from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

from .inputs import apply_server_inputs, describe_server_inputs, load_server_inputs

MAX_RESPONSE = 256 * 1024
# Exact, documented rendering-only startup error from the headless build. Nothing
# involving networking, Steam, assets, mission logic or exceptions is allowed.
HEADLESS_RENDER_ERRORS = {"There is no texture data available to upload."}


class TestFailure(RuntimeError):
    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence


class TestCancelled(TestFailure):
    """An interrupted run is incomplete, never a successful observation."""


class Bridge:
    def __init__(self, port: int, token: str, process=None):
        self.port, self.token, self.process = port, token, process
        self.instance = None
        self.baseline_errors = None

    def call(self, command: str, args=None, timeout=125):
        if self.process is not None and self.process.poll() is not None:
            raise TestFailure(f"Game process exited ({self.process.returncode})")
        request_id = uuid.uuid4().hex
        payload = {"id": request_id, "token": self.token, "command": command, "args": args or {}}
        with socket.create_connection(("127.0.0.1", self.port), timeout=min(3, timeout)) as connection:
            connection.settimeout(timeout)
            connection.sendall(json.dumps(payload, allow_nan=False).encode() + b"\n")
            response = bytearray()
            while b"\n" not in response:
                part = connection.recv(4096)
                if not part:
                    raise TestFailure("Bridge closed without a complete response")
                response.extend(part)
                if len(response) > MAX_RESPONSE:
                    raise TestFailure("Bridge response too large")
        result = json.loads(bytes(response).split(b"\n", 1)[0])
        if result.get("id") != request_id:
            raise TestFailure("Response belongs to a different request")
        if result.get("ok") is not True:
            raise TestFailure(result.get("error", "Bridge command failed"))
        return result["result"]

    def status(self):
        result = self.call("status", timeout=5)
        if result.get("protocol") != 1:
            raise TestFailure("Unsupported bridge protocol")
        if self.instance is None:
            self.instance = result["instance"]
        elif result["instance"] != self.instance:
            raise TestFailure("Game restarted during the test")
        if self.baseline_errors is not None and result["errorCount"] > self.baseline_errors:
            events = [e for e in result["errors"] if e["seq"] > self.baseline_errors]
            if len(events) != result["errorCount"] - self.baseline_errors:
                raise TestFailure("Game error journal overflowed; cannot establish a clean run")
            unexpected = [e["message"] for e in events if e["type"] != "Error" or e["message"] not in HEADLESS_RENDER_ERRORS]
            if unexpected:
                raise TestFailure("Game reported new errors: " + "; ".join(unexpected))
            self.baseline_errors = result["errorCount"]
        return result


def value_at(data, path):
    for key in path.split("."):
        if isinstance(data, list):
            data = data[int(key)]
        else:
            data = data[key]
    return data


def matches(data, assertion):
    try:
        value = value_at(data, assertion["path"])
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    if "equals" in assertion:
        return value == assertion["equals"]
    if "atLeast" in assertion:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= assertion["atLeast"]
    if "atMost" in assertion:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value <= assertion["atMost"]
    if "notNull" in assertion:
        return (value is not None) == assertion["notNull"]
    raise ValueError("Assertion needs equals, atLeast, atMost or notNull")


def matches_with_peers(data, assertion, bridges, captures=None):
    saved = next((key for key in ("equalsSaved", "notEqualsSaved") if key in assertion), None)
    if saved:
        reference = assertion[saved]
        try:
            expected = value_at((captures or {})[reference["capture"]], reference["path"])
            actual = value_at(data, assertion["path"])
        except (KeyError, IndexError, TypeError, ValueError):
            return False
        # Missing identities cannot prove a respawn or stable player identity.
        if expected is None or actual is None:
            return False
        return actual == expected if saved == "equalsSaved" else actual != expected
    peer_operator = next((key for key in ("equalsFrom", "containsFrom") if key in assertion), None)
    if peer_operator is None:
        return matches(data, assertion)
    reference = assertion[peer_operator]
    try:
        expected = value_at(bridges[reference["target"]].status(), reference["path"])
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    if expected is None:  # Two absent identities must never count as a matched player.
        return False
    if peer_operator == "containsFrom":
        try:
            actual = value_at(data, assertion["path"])
        except (KeyError, IndexError, TypeError, ValueError):
            return False
        return isinstance(actual, list) and expected in actual
    return matches(data, {"path": assertion["path"], "equals": expected})


def wait_for(bridge: Bridge, assertions: list, timeout: float, bridges=None, captures=None):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        # Game failures are fatal; only absence of a starting bridge is retryable.
        try:
            latest = bridge.status()
        except (ConnectionRefusedError, TimeoutError, ConnectionResetError, OSError) as error:
            latest = {"connectionError": str(error)}
        else:
            if all(matches_with_peers(latest, assertion, bridges or {}, captures) for assertion in assertions):
                return latest
        time.sleep(0.2)
    raise TestFailure(f"Timed out after {timeout:g}s waiting for {assertions}; last state: {latest}")


def validate_scenario(data):
    if not isinstance(data.get("name"), str) or not isinstance(data.get("steps"), list) or not data["steps"]:
        raise ValueError("Scenario needs a name and nonempty steps")
    count = data.get("clients", 1)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 16:
        raise ValueError("Scenario clients must be between 1 and 16")
    targets = {"server", *client_names(count)}
    captures = set()
    allowed = {"status", "catalog", "host", "dedicated", "rotate", "connect", "disconnect", "faction", "purchase", "reserve", "spawn", "controls", "release-controls", "engine", "eject", "gear", "next-weapon", "fly"}
    names = [step.get("name") for step in data["steps"]]
    for step_index, step in enumerate(data["steps"]):
        if step.get("target") not in targets:
            raise ValueError("Step target does not name a configured server/client")
        if sum(k in step for k in ("command", "expect", "observe")) != 1:
            raise ValueError("Each step needs exactly one command, expect or observe")
        if "command" in step and step["command"] not in allowed:
            raise ValueError("Unknown scenario command")
        if "expect" in step:
            if not step["expect"]:
                raise ValueError("An expectation must contain assertions")
            for assertion in step["expect"]:
                if not isinstance(assertion.get("path"), str) or sum(k in assertion for k in ("equals", "atLeast", "atMost", "notNull", "equalsFrom", "containsFrom", "equalsSaved", "notEqualsSaved")) != 1:
                    raise ValueError("Invalid assertion")
                for key in ("equalsFrom", "containsFrom"):
                    if key in assertion:
                        reference = assertion[key]
                        if not isinstance(reference, dict) or reference.get("target") not in targets or not isinstance(reference.get("path"), str):
                            raise ValueError("Invalid peer reference")
                for key in ("equalsSaved", "notEqualsSaved"):
                    if key in assertion:
                        reference = assertion[key]
                        if not isinstance(reference, dict) or reference.get("capture") not in captures or not isinstance(reference.get("path"), str):
                            raise ValueError("Saved assertion requires an earlier named capture")
        if "observe" in step:
            observe = step["observe"]
            if not isinstance(observe, dict) or not 0 < observe.get("seconds", 0) <= 10800:
                raise ValueError("Observation duration must be between 0 and 10800 seconds")
            if not observe.get("expect"):
                raise ValueError("Observation needs continuously checked expectations")
            for assertion in observe["expect"]:
                if not isinstance(assertion.get("path"), str) or sum(k in assertion for k in ("equals", "atLeast", "atMost", "notNull")) != 1:
                    raise ValueError("Invalid observation assertion")
            for tracking in observe.get("motion", []):
                minimum = tracking.get("minimumMetres")
                if not isinstance(tracking.get("path"), str) or isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not math.isfinite(minimum) or minimum <= 0:
                    raise ValueError("Motion needs a player path and positive minimumMetres")
            for action in observe.get("actions", []):
                if action.get("target") not in targets or action.get("command") not in {"controls", "fly"}:
                    raise ValueError("Repeated observation actions need a configured actor and controls/fly command")
                interval = action.get("everySeconds", 20)
                if isinstance(interval, bool) or not isinstance(interval, (float, int)) or not 1 <= interval <= 30:
                    raise ValueError("Repeated action interval must be between 1 and 30 seconds")
        if "onCombatLoss" in step:
            policy = step["onCombatLoss"]
            if "observe" not in step or step["target"] != "server" or not isinstance(policy, dict):
                raise ValueError("Combat-loss branching requires a server observation")
            destination = policy.get("resumeAt")
            if not isinstance(destination, str) or names.count(destination) != 1 or names.index(destination) <= step_index:
                raise ValueError("Combat-loss recovery needs a unique later named step")
            actors = policy.get("actors")
            if not isinstance(actors, list) or not actors:
                raise ValueError("Combat-loss recovery needs explicit client/player mappings")
            paths, clients = set(), set()
            for actor in actors:
                if not isinstance(actor, dict):
                    raise ValueError("Invalid combat-loss client/player mapping")
                path, client = actor.get("playerPath"), actor.get("target")
                if client not in targets - {"server"} or not isinstance(path, str) or not path.startswith("players.") or not path[8:].isdigit() or path in paths or client in clients:
                    raise ValueError("Invalid combat-loss client/player mapping")
                paths.add(path); clients.add(client)
            resume_index = names.index(destination)
            omitted_captures = {s["capture"] for s in data["steps"][step_index + 1:resume_index] if "capture" in s}
            for subsequent in data["steps"][resume_index:]:
                for assertion in subsequent.get("expect", []):
                    for key in ("equalsSaved", "notEqualsSaved"):
                        if key in assertion and assertion[key].get("capture") in omitted_captures:
                            raise ValueError("Combat-loss branch would omit a required capture")
        timeout = step.get("timeout", 60)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 180:
            raise ValueError("Timeout must be between 0 and 180 seconds")
        if "capture" in step:
            name = step["capture"]
            if "expect" not in step or not isinstance(name, str) or not name or name in captures:
                raise ValueError("Capture needs a unique name on an expectation step")
            captures.add(name)


def client_names(count):
    return ["client"] if count == 1 else [f"client{i + 1}" for i in range(count)]


def verified_missile_loss(states, assertions, policy):
    """Narrow evidence gate, not an exception handler for arbitrary flight failures."""
    server = states["server"]
    failed = [a for a in assertions if not matches(server, a)]
    if not failed:
        return None
    evidence = []
    remaining = list(failed)
    now = server.get("observation", {}).get("realtimeSeconds")
    if not isinstance(now, (int, float)) or not math.isfinite(now):
        return None
    events = server.get("observation", {}).get("firstDamageEvents", [])
    for mapping in policy["actors"]:
        path, actor = mapping["playerPath"], mapping["target"]
        losses = [a for a in failed if
                  (a["path"] in {path + ".speed", path + ".radarAltitude"} and "atLeast" in a)
                  or (a["path"] == path + ".disabled" and a.get("equals") is False)
                  or (a["path"] == path + ".aircraftNetId" and a.get("notNull") is True)]
        if not losses:
            continue
        try:
            player = value_at(server, path)
            client = states[actor]
            player_id, aircraft_id = player["netId"], player["aircraftNetId"]
        except (KeyError, IndexError, ValueError, TypeError):
            return None
        if player_id is None or aircraft_id is None or client.get("localPlayerNetId") != player_id or client.get("localPlayerAircraftNetId") != aircraft_id:
            return None
        matching = [e for e in events if e.get("aircraftNetId") == aircraft_id and e.get("playerNetId") == player_id
                    and isinstance(e.get("seconds"), (int, float)) and 0 <= now - e["seconds"] <= 30
                    and isinstance(e.get("radarAltitude"), (int, float)) and e["radarAltitude"] >= 80]
        incoming = next((e for e in matching if e.get("action") == "PartTakeDamage"
                        and e.get("damage", {}).get("dealerValid") is True
                        and e.get("damage", {}).get("dealerIsSelf") is False
                        and ("Missile.ServerFixedUpdate" in e.get("callers", [])
                             or (server.get("assemblySha256") == "df5bed594dd84912efb3e57faa75b37d7e327bf4c8f5418f50411ad0ff46e24a"
                                 and "Missile+<ExplosionForceOnPhysicsFrame>d__141.MoveNext" in e.get("callers", [])))), None)
        destructive = next((e for e in matching if e.get("action") == "DestructivePartApplyDamage"
                           and isinstance(e.get("damage", {}).get("predictedHitPointsAfter"), (int, float))
                           and e["damage"]["predictedHitPointsAfter"] <= 0
                           and isinstance(e.get("damage", {}).get("hitPointsBefore"), (int, float))
                           and e["damage"]["hitPointsBefore"] > 0), None)
        if incoming is None or destructive is None or abs(incoming["seconds"] - destructive["seconds"]) > 2:
            return None
        # A server-only trace is insufficient: require the same destructive part
        # damage on the controlling client, within the same short interval.
        client_events = client.get("observation", {}).get("firstDamageEvents", [])
        replicated = next((e for e in client_events if e.get("action") == "DestructivePartApplyDamage"
                          and e.get("aircraftNetId") == aircraft_id and e.get("playerNetId") == player_id
                          and e.get("damage", {}).get("part", {}).get("partID") == destructive.get("damage", {}).get("part", {}).get("partID")
                          and e.get("damage", {}).get("predictedHitPointsAfter") == destructive["damage"]["predictedHitPointsAfter"]), None)
        if replicated is None:
            return None
        evidence.append({"target": actor, "playerPath": path, "aircraftNetId": aircraft_id,
                         "playerNetId": player_id, "failedAssertions": losses,
                         "incoming": incoming, "destructive": destructive, "clientDamage": replicated})
        remaining = [a for a in remaining if a not in losses]
    return evidence if evidence and not remaining else None


def observe(bridge, bridges, specification, on_sample=None, combat_policy=None):
    started = time.monotonic()
    deadline = started + specification["seconds"]
    samples = []
    tracked = {}
    first_observation = {}
    telemetry = {}
    peak_resident = {}
    actions = specification.get("actions", [])
    next_actions = [started] * len(actions)
    action_records = []
    try:
        while time.monotonic() < deadline:
            for index, action in enumerate(actions):
                now = time.monotonic()
                if now >= next_actions[index]:
                    result = bridges[action["target"]].call(action["command"], action.get("args"))
                    if result.get("accepted") is False:
                        raise TestFailure("Repeated player action rejected", {"samples": samples, "actions": action_records})
                    action_records.append({"seconds": now - started, "target": action["target"],
                                           "command": action["command"], "result": result})
                    next_actions[index] = now + action.get("everySeconds", 20)
            states = {name: peer.status() for name, peer in bridges.items()}
            selected = next(state for name, state in states.items() if bridges[name] is bridge)
            # Retain the failing sample too, so a dropped aircraft is diagnosable.
            samples.append(states)
            if on_sample is not None:
                on_sample({"seconds": time.monotonic() - started, "states": states})
            if not all(matches(selected, assertion) for assertion in specification["expect"]):
                combat = verified_missile_loss(states, specification["expect"], combat_policy) if combat_policy else None
                if combat:
                    return {"samples": samples, "seconds": specification["seconds"],
                            "completedSeconds": time.monotonic() - started,
                            "outcome": "verified-missile-loss", "survivalCompleted": False,
                            "combatLoss": combat, "motion": tracked, "actions": action_records,
                            "telemetry": telemetry, "activity": "Interrupted flight; ordinary recovery is required"}
                raise TestFailure("State changed during sustained observation", {"samples": samples, "motion": tracked, "actions": action_records})
            for name, state in states.items():
                counters = state.get("observation")
                if counters is not None:
                    if name not in first_observation:
                        first_observation[name] = counters
                    telemetry[name] = window_telemetry(first_observation[name], counters)
                    if "residentBytes" in counters:
                        # The peak belongs to this observation, not startup/menu time.
                        peak_resident[name] = max(peak_resident.get(name, 0), counters["residentBytes"])
                        telemetry[name]["peakResidentBytes"] = peak_resident[name]
            for tracking in specification.get("motion", []):
                path = tracking["path"]
                try:
                    player = value_at(selected, path)
                    identity, position = player["aircraftNetId"], player["position"]
                    if identity is None or not isinstance(position, list) or len(position) != 3 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in position):
                        raise ValueError("Missing/invalid aircraft position")
                except (KeyError, IndexError, TypeError, ValueError) as error:
                    raise TestFailure(f"Cannot track motion at {path}: {error}", {"samples": samples, "motion": tracked}) from error
                if path not in tracked:
                    tracked[path] = {"aircraftNetId": identity, "initial": position, "maximumMetres": 0}
                track = tracked[path]
                if identity != track["aircraftNetId"]:
                    raise TestFailure("Tracked aircraft changed during motion observation", {"samples": samples, "motion": tracked})
                track["maximumMetres"] = max(track["maximumMetres"], math.dist(track["initial"], position))
            time.sleep(1)
        for tracking in specification.get("motion", []):
            distance = tracked.get(tracking["path"], {}).get("maximumMetres", 0)
            if distance < tracking["minimumMetres"]:
                raise TestFailure(f"Aircraft at {tracking['path']} moved only {distance:.2f}m; required {tracking['minimumMetres']}m", {"samples": samples, "motion": tracked})
        return {"samples": samples, "seconds": specification["seconds"], "motion": tracked,
                "telemetry": telemetry, "actions": action_records,
                "activity": "aircraft movement observed; not a flight/combat soak" if tracked else "observation only; not evidence of playing"}
    except (TestCancelled, KeyboardInterrupt) as error:
        raise TestCancelled(str(error) or "Cancelled by keyboard interrupt", {
            "samples": samples, "seconds": specification["seconds"],
            "completedSeconds": time.monotonic() - started, "incomplete": True,
            "telemetry": telemetry, "motion": tracked, "actions": action_records}) from error
    except TestFailure as error:
        # Preserve earlier observations when a status call itself reports a
        # game exception. Keep the original failure and bridge evidence fatal.
        details = dict(error.evidence or {})
        details.setdefault("samples", samples)
        details.setdefault("motion", tracked)
        details.setdefault("actions", action_records)
        details.setdefault("seconds", specification["seconds"])
        details["incomplete"] = True
        raise TestFailure(str(error), details) from error



def window_telemetry(first, last):
    elapsed = last["realtimeSeconds"] - first["realtimeSeconds"]
    cpu = last["processCpuSeconds"] - first["processCpuSeconds"]
    frames = last["frames"] - first["frames"]
    buckets = [b - a for a, b in zip(first["frameBuckets"], last["frameBuckets"])]
    return {"seconds": elapsed, "cpuSeconds": cpu,
            "cpuPercentOneCore": cpu / elapsed * 100 if elapsed > 0 else None,
            "frames": frames, "framesPerSecond": frames / elapsed if elapsed > 0 else None,
            "fixedSteps": last["fixedSteps"] - first["fixedSteps"],
            "frameBucketBoundsMs": [16.7, 25, 33.4, 50, 100, None], "frameBucketCounts": buckets,
            "gameSeconds": last["gameSeconds"] - first["gameSeconds"]}


def run_steps(scenario, bridges, report, output_dir=None):
    validate_scenario(scenario)
    captures = {}
    resume_at = None
    for index, step in enumerate(scenario["steps"]):
        if resume_at is not None:
            if step.get("name") != resume_at:
                report["steps"][-1].setdefault("omittedSteps", []).append(step.get("name", f"Step {index + 1}"))
                continue
            resume_at = None
        record = {"name": step.get("name", f"Step {index + 1}"), "target": step["target"]}
        start = time.monotonic()
        if output_dir is not None:
            checkpoint(output_dir, {"runId": report.get("runId"), "scenario": scenario["name"], "step": record["name"],
                                    "completedSteps": len(report["steps"]), "state": "running"})
        try:
            bridge = bridges[step["target"]]
            if "command" in step:
                record["result"] = bridge.call(step["command"], step.get("args"))
                # A rejected game action is a failure, even if the bridge handled the command.
                if record["result"].get("accepted") is False:
                    raise TestFailure("Game rejected the action")
                if "errorCount" in record["result"]:
                    bridge.status()  # Also reject game errors during a command returning a snapshot.
            elif "expect" in step:
                record["result"] = wait_for(bridge, step["expect"], step.get("timeout", 60), bridges, captures)
            else:
                def sample(evidence):
                    if output_dir is not None:
                        with (output_dir / "timeline.ndjson").open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps({"runId": report.get("runId"), "step": record["name"], **evidence}, allow_nan=False) + "\n")
                        checkpoint(output_dir, {"runId": report.get("runId"), "scenario": scenario["name"], "step": record["name"],
                            "completedSteps": len(report["steps"]), "state": "running", **evidence})
                record["result"] = observe(bridge, bridges, step["observe"], sample if output_dir is not None else None,
                                           step.get("onCombatLoss"))
                if record["result"].get("outcome") == "verified-missile-loss":
                    resume_at = step["onCombatLoss"]["resumeAt"]
                    record["resumeAt"] = resume_at
            if "capture" in step:
                # Keep a value snapshot, independent of later mutable fixture state.
                captures[step["capture"]] = json.loads(json.dumps(record["result"], allow_nan=False))
                record["capture"] = step["capture"]
            record["passed"] = True
            print(f"PASS {record['name']}", flush=True)
        except (Exception, KeyboardInterrupt) as error:
            if isinstance(error, KeyboardInterrupt):
                error = TestCancelled("Cancelled by keyboard interrupt")
            record.update(passed=False, error=str(error))
            if isinstance(error, TestCancelled):
                record["cancelled"] = True
            if isinstance(error, TestFailure) and error.evidence is not None:
                record["result"] = error.evidence
            record["failureSnapshots"] = {}
            for name, peer in bridges.items():
                try:
                    record["failureSnapshots"][name] = peer.call("status", timeout=5)
                except Exception as snapshot_error:
                    record["failureSnapshots"][name] = {"unavailable": str(snapshot_error)}
            raise error
        finally:
            record["seconds"] = round(time.monotonic() - start, 3)
            report["steps"].append(record)


def checkpoint(directory, status):
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "progress.tmp"
    temporary.write_text(json.dumps(status, allow_nan=False), encoding="utf-8")
    temporary.replace(directory / "progress.json")


def write_report(directory: Path, report: dict):
    directory.mkdir(parents=True, exist_ok=True)
    report["combatLossInterruptions"] = [
        {"step": s["name"], "requestedSeconds": s["result"]["seconds"],
         "completedSeconds": s["result"]["completedSeconds"], "survivalCompleted": False,
         "resumeAt": s["resumeAt"], "omittedSteps": s.get("omittedSteps", [])}
        for s in report["steps"] if s.get("result", {}).get("outcome") == "verified-missile-loss"]
    temporary = directory / "result.tmp"
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(directory / "result.json")
    suite = ET.Element("testsuite", name=report["scenario"], tests=str(len(report["steps"])),
                       failures=str(sum(not s["passed"] for s in report["steps"])))
    for step in report["steps"]:
        case = ET.SubElement(suite, "testcase", name=step["name"], classname="NOTestPilot", time=str(step["seconds"]))
        if not step["passed"]:
            ET.SubElement(case, "failure", message=step.get("error", "failed")).text = step.get("error")
        if step.get("result", {}).get("outcome") == "verified-missile-loss":
            ET.SubElement(case, "system-out").text = json.dumps({
                "outcome": "verified-missile-loss", "survivalCompleted": False,
                "completedSeconds": step["result"]["completedSeconds"],
                "requestedSeconds": step["result"]["seconds"],
                "resumeAt": step["resumeAt"], "omittedSteps": step.get("omittedSteps", [])})
    ET.ElementTree(suite).write(directory / "junit.tmp", encoding="utf-8", xml_declaration=True)
    (directory / "junit.tmp").replace(directory / "junit.xml")


def finish_lab(directory, report, processes):
    # A first stop can arrive during teardown or an atomic report write too.
    # Retry after recording it; CLI stop signals are raised only once.
    while True:
        try:
            for process in processes.values():
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=8)
            write_report(directory, report)
            checkpoint(directory, {"runId": report["runId"], "scenario": report["scenario"],
                "state": "passed" if report["passed"] else ("cancelled" if report.get("cancelled") else "failed"),
                "completedSteps": len(report["steps"]), "error": report.get("error")})
            return bool(report.get("cancelled"))
        except (TestCancelled, KeyboardInterrupt) as error:
            report.update(passed=False, cancelled=True)
            reason = str(error) or "Cancelled by keyboard interrupt during cleanup"
            report["error"] = reason if not report.get("error") else report["error"] + "; " + reason
            report["steps"].append({"name": "lab cleanup interrupted", "target": "lab",
                "passed": False, "cancelled": True, "error": reason, "seconds": 0})


def free_tcp_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def prepare_lab(source: Path, lab: Path, clients=1, server_inputs=None):
    source, lab = source.resolve(), lab.resolve()
    if source == lab or source in lab.parents or lab in source.parents:
        raise ValueError("Game source and disposable lab must be separate directories")
    marker = lab / ".notestpilot-lab"
    if lab.exists() and not marker.is_file():
        raise ValueError("Refusing existing unmarked lab directory")
    if marker.exists() and marker.read_text(encoding="utf-8") != str(source):
        raise ValueError("Lab belongs to a different source installation")
    # Copy only a known game build plus this project's bridge. Never inherit somebody's
    # production plugins, configs, caches or credentials into a disposable test.
    if not (source / "BepInEx/plugins/NOTestPilot/NOTestPilot.Bridge.dll").is_file():
        raise ValueError("NOTestPilot bridge is not installed in the game input")
    lab.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(source), encoding="utf-8")
    server_was_present = (lab / "server").exists()
    for role in ["server", *client_names(clients)]:
        destination = lab / role
        if not destination.exists():
            def ignore(path, names):
                location = Path(path)
                exclusions = {n for n in names if n.endswith(".log")}
                if location == source:
                    exclusions.update(n for n in names if n in {"steamapps", "logs", "DedicatedServerConfig.json", "ban_list.txt"})
                if location == source / "BepInEx":
                    exclusions.update(n for n in names if n in {"cache", "config", "data", "patchers"})
                if location == source / "BepInEx/plugins":
                    exclusions.update(n for n in names if n != "NOTestPilot")
                return exclusions
            shutil.copytree(source, destination, ignore=ignore)
        else:
            # Never silently reuse game binaries or a bridge from a different build.
            for relative in ("NuclearOptionServer_Data/Managed/Assembly-CSharp.dll", "BepInEx/plugins/NOTestPilot/NOTestPilot.Bridge.dll"):
                if hashlib.sha256((source / relative).read_bytes()).digest() != hashlib.sha256((destination / relative).read_bytes()).digest():
                    raise ValueError("Lab input changed; choose a new disposable lab directory")
    apply_server_inputs(lab, server_inputs or [], server_was_present)
    return lab


def run_lab(args):
    scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    validate_scenario(scenario)
    if not args.execute:
        raise ValueError("Game launch is opt-in: inspect the scenario, then pass --execute in your remote lab")
    directory = Path(args.output).resolve()
    # Claim a fresh output directory atomically. Never append a new attempt to
    # an old timeline or leave an old passing result visible during a new run.
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise ValueError("Results directory already exists; choose a new output for every run") from error
    report = {"runId": uuid.uuid4().hex, "startedAtUtc": datetime.now(timezone.utc).isoformat(),
              "scenario": scenario["name"], "passed": False, "steps": [],
              "mode": "real-process UDP lab", "startup": {}}
    processes, bridges = {}, {}
    result_code = 1
    token = secrets.token_hex(32)
    try:
        checkpoint(directory, {"runId": report["runId"], "scenario": scenario["name"], "state": "preparing"})
        server_inputs = load_server_inputs(Path(args.server_mods) if args.server_mods else None)
        lab = prepare_lab(Path(args.game), Path(args.lab), scenario.get("clients", 1), server_inputs)
        report["inputSha256"] = {
            "scenario": hashlib.sha256(Path(args.scenario).read_bytes()).hexdigest(),
            "gameAssembly": hashlib.sha256((Path(args.game) / "NuclearOptionServer_Data/Managed/Assembly-CSharp.dll").read_bytes()).hexdigest(),
            "bridge": hashlib.sha256((Path(args.game) / "BepInEx/plugins/NOTestPilot/NOTestPilot.Bridge.dll").read_bytes()).hexdigest(),
        }
        report["serverInputs"] = describe_server_inputs(server_inputs)
        for index, name in enumerate(["server", *client_names(scenario.get("clients", 1))]):
            role = "server" if name == "server" else "client"
            folder = lab / name
            executable = folder / ("NuclearOptionServer.exe" if os.name == "nt" else "NuclearOptionServer.x86_64")
            port = free_tcp_port()
            # Defence in depth: even unexpected native auto-start must be hidden.
            config = {"Hidden": True, "ServerName": "NOTestPilot disposable lab", "MaxPlayers": 4,
                      "Port": {"IsOverride": True, "Value": 19777 + 2 * index},
                      "QueryPort": {"IsOverride": True, "Value": 19778 + 2 * index}}
            (folder / "DedicatedServerConfig.json").write_text(json.dumps(config), encoding="utf-8")
            # This game build otherwise prevents later BepInEx lifecycle callbacks.
            # Without Update, a loaded bridge never executes queued commands.
            config_dir = folder / "BepInEx/config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "BepInEx.cfg").write_text("[Chainloader]\nHideManagerGameObject = true\n[Logging.Console]\nEnabled = false\n", encoding="utf-8")
            env = dict(os.environ, NOTESTPILOT_ENABLE="1", NOTESTPILOT_ROLE=role, NOTESTPILOT_TOKEN=token, NOTESTPILOT_PORT=str(port))
            if os.name != "nt":
                env.update(DOORSTOP_ENABLED="1", DOORSTOP_TARGET_ASSEMBLY=str(folder / "BepInEx/core/BepInEx.Preloader.dll"),
                           DOORSTOP_IGNORE_DISABLED_ENV="0", DOORSTOP_MONO_DLL_SEARCH_PATH_OVERRIDE="",
                           LD_LIBRARY_PATH=str(folder) + ":" + str(folder / "linux64") + ":" + env.get("LD_LIBRARY_PATH", ""),
                           LD_PRELOAD=str(folder / "libdoorstop.so"))
            command = [str(executable), "-batchmode", "-nographics", "-logFile", str(folder / "game.log"), "-limitframerate", "60"]
            # The bridge starts at the menu; the scenario explicitly decides when to host/connect.
            processes[name] = subprocess.Popen(command, cwd=folder, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            bridges[name] = Bridge(port, token, processes[name])
            start = time.monotonic()
            snapshot = wait_for(bridges[name], [{"path": "menuReady", "equals": True}], 90)
            report["startup"][name] = snapshot
            unexpected = [e for e in snapshot["errors"] if e["type"] != "Error" or e["message"] not in HEADLESS_RENDER_ERRORS]
            if unexpected:
                raise TestFailure(f"{name} reported startup errors: {unexpected}")
            bridges[name].baseline_errors = snapshot["errorCount"]
            report["steps"].append({"name": f"{name} starts", "target": name, "passed": True,
                                    "seconds": round(time.monotonic() - start, 3)})
        run_steps(scenario, bridges, report, directory)
        report["passed"] = True
        result_code = 0
    except (Exception, KeyboardInterrupt) as error:
        report["cancelled"] = isinstance(error, (TestCancelled, KeyboardInterrupt))
        report["error"] = str(error) or "Cancelled by keyboard interrupt"
        if not report["steps"] or report["steps"][-1]["passed"]:
            report["steps"].append({"name": "lab setup", "target": "lab", "passed": False, "error": report["error"], "seconds": 0})
        print("FAIL " + report["error"], file=sys.stderr)
        result_code = 1
    finally:
        if finish_lab(directory, report, processes):
            result_code = 1
    return result_code


def main():
    parser = argparse.ArgumentParser(description="Run a scenario against a disposable Nuclear Option server and controlled clients")
    parser.add_argument("scenario")
    parser.add_argument("--game", required=True, help="Dedicated server with NOTestPilot bridge installed")
    parser.add_argument("--lab", required=True, help="New/marked disposable lab directory")
    parser.add_argument("--output", required=True, help="Private run artifacts directory")
    parser.add_argument("--server-mods", help="Explicit SHA256 manifest of server-only test plugins/configs; requires its own lab")
    parser.add_argument("--execute", action="store_true", help="Explicitly launch disposable game processes in the remote lab")
    args = parser.parse_args()
    previous = {kind: signal.getsignal(kind) for kind in (signal.SIGINT, signal.SIGTERM)}
    stopping = False
    def cancel(signum, frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            raise TestCancelled("Cancelled by " + signal.Signals(signum).name)
    try:
        for kind in previous:
            signal.signal(kind, cancel)
        return run_lab(args)
    finally:
        for kind, handler in previous.items():
            signal.signal(kind, handler)


if __name__ == "__main__":
    raise SystemExit(main())
