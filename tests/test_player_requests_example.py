"""Consumer request scenario contract fixtures; not native runtime coverage."""

import base64
import copy
from dataclasses import dataclass
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import player_requests as example
from notestpilot import RequestJoinUncertain, StaleActor, TransportError
from test_lifecycle_example import Clock


@dataclass(frozen=True)
class Handle:
    session: object
    name: str
    connection_id: str
    player_id: int | None
    aircraft_id: int | None
    instance: str = "native-fixture"

    def checked(self):
        row = self.session.rows[self.name]
        aircraft = row["aircraft"]
        if (row["connectionId"], row["playerId"], None if aircraft is None else aircraft["id"]) != (
                self.connection_id, self.player_id, self.aircraft_id):
            raise StaleActor("Fixture stale request handle")
        return row

    def join_faction(self, faction):
        row = self.checked()
        row.update(faction=faction, allocation=1000)
        self.session.commands.append((self.name, "join_faction"))

    def purchase_airframe(self, aircraft):
        row = self.checked()
        row["allocation"] -= 100
        row["inventory"].append({"aircraft": aircraft, "reserved": False})
        self.session.commands.append((self.name, "purchase"))
        if self.session.cross_purchase:
            other = next(r for r in self.session.rows.values() if r["name"] != self.name and r["name"] != "unrelated")
            other["allocation"] -= 1

    def request_spawn(self, airbase, aircraft):
        row = self.checked()
        reply = self.session.reply
        self.session.reply += 1
        row["lastSpawnReplyId"] = reply
        row["spawnRequests"].append({"replyId": reply, "state": "pending", "uncertain": False, "outcome": None})
        row["aircraftSpawnPending"] = True
        self.session.pending[self.name] = [0, aircraft]
        self.session.commands.append((self.name, "spawn"))
        return self

    def packets(self):
        row = self.checked()
        self.session.packet_reads += 1
        sequence = self.session.packet_sequences.get(self.name, 0) + 1
        self.session.packet_sequences[self.name] = sequence
        row["journalPackets"] = 0
        return [{"sequence": sequence, "channel": "reliable", "bytes": base64.b64encode(b"private native packet").decode()}]

    def disconnect(self):
        row = self.checked()
        row.update(state="disconnected", playerId=None, ready=False, aircraft=None, registered=False,
                   authenticatedMember=False, nativeOwnedIdentityCount=0,
                   nativeVisibleIdentityCount=1 if self.session.cleanup_incomplete else 0,
                   cleanupAttempted=True, cleanupIncomplete=self.session.cleanup_incomplete)
        self.session.commands.append((self.name, "disconnect"))


class Fixture:
    def __init__(self, *, rejected_spawn=False, rpc_error=False, wrong_reply=False, cross_purchase=False,
                 cleanup_incomplete=False, uncertain_join=False, timeout_spawn=False):
        self.rows, self.commands, self.pending = {}, [], {}
        self.next_player, self.reply = 10, 1
        self.packet_reads, self.packet_sequences = 0, {}
        self.rejected_spawn, self.wrong_reply, self.cross_purchase = rejected_spawn, wrong_reply, cross_purchase
        self.rpc_error = rpc_error
        self.cleanup_incomplete, self.uncertain_join, self.timeout_spawn = cleanup_incomplete, uncertain_join, timeout_spawn
        self.allowlists = []
        self.rows["unrelated"] = {"name": "unrelated", "connectionId": "unrelated", "playerId": 999,
                                   "aircraft": None, "state": "ready", "allocation": 5432}

    def status(self):
        for name, pending in list(self.pending.items()):
            row = self.rows[name]
            if row["state"] == "disconnected":
                continue
            pending[0] += 1
            if self.timeout_spawn:
                continue
            receipt = row["spawnRequests"][-1]
            if pending[0] >= 2:
                receipt.update(state="replied", outcome={"matched": True, "replyId": receipt["replyId"] + int(self.wrong_reply),
                    "success": not self.rpc_error, "allowed": None if self.rpc_error else not self.rejected_spawn, "delayedSpawn": True})
            if pending[0] >= 4 and not self.rejected_spawn and not self.rpc_error:
                row["aircraft"] = {"id": row["playerId"] * 100, "localSim": False, "remoteSim": True,
                                    "ownedByConnection": True, "linkedPlayerId": row["playerId"]}
                row["aircraftSpawnPending"] = False
                row["inventory"].remove({"aircraft": pending[1], "reserved": False})
                row["airframeInUse"] = {"aircraft": pending[1], "reserved": False}
                del self.pending[name]
        return copy.deepcopy({"protocol": 2, "instance": "native-fixture", "serverActive": True, "missionRunning": True,
            "factions": ["observed-faction"], "factionEconomy": [{"faction": "observed-faction", "joinAllowance": 1000}],
            "aircraftCatalog": [{"key": "native-airframe", "cost": 100, "rank": 0}],
            "airbases": [{"name": name, "faction": "observed-faction", "disabled": False, "availableAircraft": ["native-airframe"]}
                         for name in ("observed-base-a", "observed-base-b")], "requestPlayers": list(self.rows.values()), "actors": []})

    def wait_for(self, predicate, description, **kwargs):
        self.allowlists.append(kwargs.get("allowed_errors"))
        snapshot = self.status()
        if not predicate(snapshot):
            raise AssertionError("Unexpected fixture wait")
        return snapshot

    def request_player(self, name):
        row = self.rows[name]
        return Handle(self, name, row["connectionId"], row["playerId"], None if row["aircraft"] is None else row["aircraft"]["id"])

    def join_request_player(self, name, password, **kwargs):
        player = self.next_player
        self.next_player += 1
        self.rows[name] = {"name": name, "mode": "player-requests", "connectionId": "native-" + str(player),
            "creationId": str(player) * 16, "playerId": player, "aircraft": None, "state": "ready", "ready": True,
            "host": False, "authenticated": True, "registered": True, "authenticatedMember": True, "error": None,
            "nativeOwnedIdentityCount": 1, "nativeVisibleIdentityCount": 100, "allocation": 0, "rank": 0,
            "inventory": [], "airframeInUse": None, "spawnRequests": [], "lastSpawnReplyId": None,
            "aircraftSpawnPending": False, "journalPackets": 1, "queuedPackets": 0, "nativeErrorBits": 0}
        self.commands.append((name, "join"))
        self.allowlists.append(kwargs.get("allowed_errors"))
        if self.uncertain_join:
            self.uncertain_join = False
            raise RequestJoinUncertain(name, self.rows[name]["creationId"], "native-fixture", TransportError("Join applied; reply lost"))
        return self.request_player(name)

    def recover_request_player(self, uncertainty):
        if self.rows[uncertainty.name]["creationId"] != uncertainty.creation_id:
            raise StaleActor("Wrong fixture receipt")
        return self.request_player(uncertainty.name)


class ExampleTests(unittest.TestCase):
    def execute(self, session, **kwargs):
        clock = Clock()
        return example.run(session, "explicit-fixture-password", timeout=2, clock=clock, sleep=clock.sleep, **kwargs)

    def test_two_native_request_players_have_independent_economy_spawn_and_cleanup(self):
        session, private_packets = Fixture(), []
        report = self.execute(session, packet_sink=lambda name, packets: private_packets.extend(packets))
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["checks"]), 5)
        self.assertEqual(report["cleanup"], {"connections": 2, "complete": True})
        self.assertEqual([r["replyId"] for r in report["spawnReceipts"]], [1, 2])
        self.assertTrue(all(r["delayedSpawn"] for r in report["spawnReceipts"]))
        self.assertGreater(report["packetRecords"], 0)
        self.assertTrue(private_packets)
        self.assertNotIn("bytes", str(report))
        self.assertEqual(session.rows["unrelated"]["allocation"], 5432)
        for name, row in session.rows.items():
            if name != "unrelated":
                self.assertEqual(row["state"], "disconnected")
        self.assertEqual([command for _, command in session.commands].count("spawn"), 2)
        self.assertTrue(all(allowlist == () for allowlist in session.allowlists))

    def test_allowed_false_and_wrong_correlation_never_count_as_completed_spawn(self):
        for option, message in (("rejected_spawn", "Allowed=false"), ("wrong_reply", "correlation mismatch"),
                                ("rpc_error", "without a TrySpawnResult")):
            session = Fixture(**{option: True})
            with self.subTest(option=option), self.assertRaises(example.SmokeFailure) as caught:
                self.execute(session)
            self.assertIn(message, caught.exception.diagnostics["cause"])
            self.assertEqual(sum(command == "spawn" for _, command in session.commands), 1)
            self.assertEqual(sum(command == "disconnect" for _, command in session.commands), 2)

    def test_purchase_touching_other_native_player_fails_isolation(self):
        session = Fixture(cross_purchase=True)
        with self.assertRaises(example.SmokeFailure) as caught:
            self.execute(session)
        self.assertIn("independent player's state", caught.exception.diagnostics["cause"])
        self.assertFalse(any(command == "spawn" for _, command in session.commands))

    def test_delayed_native_spawn_timeout_does_not_repeat_request(self):
        session = Fixture(timeout_spawn=True)
        with self.assertRaises(example.SmokeFailure) as caught:
            self.execute(session)
        self.assertIn("Deadline", caught.exception.diagnostics["cause"])
        self.assertEqual(sum(command == "spawn" for _, command in session.commands), 1)
        self.assertTrue(caught.exception.diagnostics["players"])

    def test_uncertain_join_recovers_for_cleanup_and_never_replays_or_passes(self):
        session = Fixture(uncertain_join=True)
        with self.assertRaises(example.SmokeFailure) as caught:
            self.execute(session)
        self.assertIn("join uncertain", caught.exception.diagnostics["cause"])
        self.assertEqual([command for _, command in session.commands], ["join", "disconnect"])

    def test_pending_uncertain_auth_can_finish_before_ownership_read_or_finally(self):
        class PendingAuth(Fixture):
            def __init__(self, reads_before_completion):
                super().__init__(uncertain_join=True)
                self.reads_before_completion = reads_before_completion
                self.finish_auth = False
                self.native_id = None
                self.name = None

            def join_request_player(self, name, password, **kwargs):
                try:
                    return super().join_request_player(name, password, **kwargs)
                except RequestJoinUncertain:
                    self.name = name
                    row = self.rows[name]
                    self.native_id = row["playerId"]
                    row.update(playerId=None, ready=False, state="authenticating")
                    raise

            def recover_request_player(self, uncertainty):
                handle = super().recover_request_player(uncertainty)
                if handle.player_id is None:
                    self.finish_auth = True
                return handle

            def status(self):
                if self.finish_auth:
                    if self.reads_before_completion == 0:
                        self.rows[self.name].update(playerId=self.native_id, ready=True, state="ready")
                        self.finish_auth = False
                    else:
                        self.reads_before_completion -= 1
                return super().status()

        for reads in (0, 1):
            session = PendingAuth(reads)
            with self.subTest(reads=reads), self.assertRaises(example.SmokeFailure) as caught:
                self.execute(session)
            self.assertIn("join uncertain", caught.exception.diagnostics["cause"])
            self.assertEqual([command for _, command in session.commands], ["join", "disconnect"])
            self.assertEqual(session.rows[session.name]["state"], "disconnected")
            self.assertIsNone(session.rows[session.name]["playerId"])

    def test_exact_uncertain_receipt_never_adopts_changed_nonnull_native_id(self):
        record = {"name": "owned", "instance": "native-fixture", "connectionId": "same-connection",
                  "creationId": "a" * 32, "playerId": 10,
                  "uncertainty": RequestJoinUncertain("owned", "a" * 32, "native-fixture", TransportError("lost"))}
        snapshot = {"instance": "native-fixture", "requestPlayers": [{"name": "owned", "mode": "player-requests",
                    "connectionId": "same-connection", "creationId": "a" * 32, "playerId": 11, "state": "ready"}]}
        with self.assertRaises(StaleActor):
            example.row_for(snapshot, record)
        self.assertEqual(record["playerId"], 10)

    def test_cleanup_incomplete_blocks_success_and_retains_primary_failure(self):
        session = Fixture(rejected_spawn=True, cleanup_incomplete=True)
        with self.assertRaises(example.SmokeFailure) as caught:
            self.execute(session)
        self.assertIn("cleanup incomplete", str(caught.exception))
        self.assertIn("correlated owned spawn", caught.exception.diagnostics["primaryDiagnostics"]["stage"])
        self.assertTrue(caught.exception.diagnostics["cleanup"])


if __name__ == "__main__":
    unittest.main()
