"""Request-player client contract over real loopback TCP; no native proof."""

import unittest
import base64
from unittest.mock import patch

from notestpilot import CommandError, ProtocolError, RequestJoinUncertain, RuntimeFailure, Session, StaleActor
from test_client import Peer, state


CAPABILITIES = ["status", "join_faction", "purchase_airframe", "request_spawn", "disconnect"]


def requests_state(*, player=17, aircraft=None, connection="native-connection-a", mode="player-requests", ready=True):
    snapshot = state()
    snapshot["requestPlayers"] = [{"name": "alpha", "mode": mode, "creationId": "a" * 32,
        "connectionId": connection, "playerId": player, "ready": ready, "state": "ready" if ready else "authenticating",
        "capabilities": CAPABILITIES[:] if player is not None else ["status", "disconnect"],
        "aircraft": None if aircraft is None else {"id": aircraft}, "allocation": 123}]
    return snapshot


def packets_state(**kwargs):
    snapshot = requests_state(**kwargs)
    snapshot["requestPlayers"][0]["packetGap"] = False
    return snapshot


def packet(sequence, channel="reliable", raw=b"native-packet"):
    return {"sequence": sequence, "channel": channel, "bytes": base64.b64encode(raw).decode("ascii")}


class RequestPlayerTests(unittest.TestCase):
    def test_packet_drain_contiguous_across_batches_and_independent_of_event_cursor(self):
        snapshot = packets_state()
        with Peer([snapshot, snapshot, snapshot, [packet(1), packet(2, "notify")],
                   snapshot, snapshot, [packet(3, "unreliable")]]) as peer:
            session = peer.client()
            session.event_cursor = 1000
            actor = session.request_player("alpha")
            self.assertEqual([p["sequence"] for p in actor.packets()], [1, 2])
            self.assertEqual(actor.packets(), [packet(3, "unreliable")])
            self.assertEqual(session.event_cursor, 1000)
        drains = [r for r in peer.requests if r["command"] == "request_packets"]
        self.assertEqual(len(drains), 2)
        self.assertEqual(drains[0]["args"], {"name": "alpha", "mode": "player-requests", "instance": "session-one",
                                             "connectionId": "native-connection-a", "playerId": 17, "aircraftId": None})

    def test_malformed_packet_records_poison_evidence_without_repeating_drain(self):
        batches = ([packet(2)], [packet(1), packet(1)], [packet(1), packet(3)],
                   [{**packet(1), "sequence": True}], [{**packet(1), "channel": "other"}],
                   [{**packet(1), "bytes": "%%%"}], [{**packet(1), "bytes": "YQ"}],
                   [{**packet(1), "bytes": "YR=="}], [{**packet(1), "bytes": ""}], "not-an-array")
        for batch in batches:
            snapshot = packets_state()
            with self.subTest(batch=batch), Peer([snapshot, snapshot, snapshot, batch]) as peer:
                actor = peer.client().request_player("alpha")
                with self.assertRaises(ProtocolError):
                    actor.packets()
                with self.assertRaisesRegex(ProtocolError, "cannot be drained again"):
                    actor.packets()
            self.assertEqual(sum(r["command"] == "request_packets" for r in peer.requests), 1)

    def test_lost_packet_drain_reply_is_not_retried_even_if_next_batch_would_be_empty(self):
        snapshot = packets_state()
        with Peer([snapshot, snapshot, snapshot, b""]) as peer:
            actor = peer.client().request_player("alpha")
            with self.assertRaises(ProtocolError):
                actor.packets()
            with self.assertRaisesRegex(ProtocolError, "evidence was lost"):
                actor.packets()
        self.assertEqual(sum(r["command"] == "request_packets" for r in peer.requests), 1)

    def test_native_gap_and_stale_generation_block_drain_before_consuming_journal(self):
        first, gap, changed = packets_state(), packets_state(), packets_state(aircraft=100)
        gap["requestPlayers"][0]["packetGap"] = True
        with Peer([first, first, gap]) as peer:
            actor = peer.client().request_player("alpha")
            with self.assertRaisesRegex(ProtocolError, "journal reports a gap"):
                actor.packets()
        self.assertFalse(any(r["command"] == "request_packets" for r in peer.requests))

        with Peer([first, changed]) as peer:
            actor = peer.client().request_player("alpha")
            with self.assertRaises(StaleActor):
                actor.packets()
        self.assertFalse(any(r["command"] == "request_packets" for r in peer.requests))

    def test_wrong_mode_cannot_drain_and_pending_tombstone_can(self):
        with Peer([packets_state(), packets_state(mode="server-simulation")]) as peer:
            actor = peer.client().request_player("alpha")
            with self.assertRaises(ProtocolError):
                actor.packets()
        self.assertFalse(any(r["command"] == "request_packets" for r in peer.requests))
        pending = packets_state(player=None, ready=False)
        with Peer([pending, pending, pending, [packet(1)]]) as peer:
            actor = peer.client().request_player("alpha")
            self.assertEqual(actor.packets(), [packet(1)])
        self.assertIsNone(peer.requests[-1]["args"]["playerId"])

    def test_packet_batch_count_per_packet_and_total_byte_limits(self):
        snapshot = packets_state()
        megabyte = base64.b64encode(b"x" * (1024 * 1024)).decode("ascii")
        batches = ([packet(i + 1) for i in range(4097)], [packet(1, raw=b"x" * (1024 * 1024 + 1))],
                   [{"sequence": i + 1, "channel": "reliable", "bytes": megabyte} for i in range(17)])
        for batch in batches:
            with self.subTest(count=len(batch)):
                session = Session(1, "fixture-token-" + "x" * 32)
                # Exercise production byte accounting without serializing 24MiB
                # over TCP; framing/caps are independently exercised by Peer.
                with patch.object(session, "call", side_effect=[snapshot, snapshot, snapshot, batch]) as call:
                    actor = session.request_player("alpha")
                    with self.assertRaisesRegex(ProtocolError, "limit|oversized"):
                        actor.packets()
                    self.assertEqual(sum(c.args[0] == "request_packets" for c in call.call_args_list), 1)

    def test_join_allowlist_accepts_only_exact_texture_error_with_complete_journal(self):
        message = "There is no texture data available to upload."
        allowed = (("Error", message),)
        for kind, text, count, succeeds, whitelist in (("Error", message, 1, True, allowed),
                                           ("Exception", message, 1, False, allowed),
                                           ("Error", "Unexpected native failure", 1, False, allowed),
                                           ("Error", message, 2, False, allowed),
                                           ("Error", message, 1, False, ())):
            creation = None
            def joined(request):
                nonlocal creation
                creation = request["args"]["creationId"]
                pending = requests_state(player=None, ready=False)
                pending["requestPlayers"][0]["creationId"] = creation
                return {"id": request["id"], "ok": True, "result": pending}
            def ready(request):
                snapshot = requests_state()
                snapshot["requestPlayers"][0]["creationId"] = creation
                snapshot["errors"] = {"count": count, "recent": [{"kind": kind, "message": text}]}
                return {"id": request["id"], "ok": True, "result": snapshot}
            with self.subTest(kind=kind, count=count, succeeds=succeeds), Peer([state(), joined, ready]) as peer:
                session = peer.client()
                if succeeds:
                    self.assertEqual(session.join_request_player("alpha", "fixture-password", allowed_errors=whitelist).player_id, 17)
                else:
                    with self.assertRaises(RequestJoinUncertain) as caught:
                        session.join_request_player("alpha", "fixture-password", allowed_errors=whitelist)
                    self.assertIsInstance(caught.exception.cause, RuntimeFailure)
            self.assertEqual(sum(r["command"] == "request_join" for r in peer.requests), 1)

    def test_one_join_then_read_only_native_readiness_poll(self):
        creation = None
        def joined(request):
            nonlocal creation
            creation = request["args"]["creationId"]
            pending = requests_state(player=None, ready=False)
            pending["requestPlayers"][0]["creationId"] = creation
            return {"id": request["id"], "ok": True, "result": pending}
        def ready(request):
            snapshot = requests_state()
            snapshot["requestPlayers"][0]["creationId"] = creation
            return {"id": request["id"], "ok": True, "result": snapshot}
        with Peer([state(), joined, ready]) as peer:
            handle = peer.client().join_request_player("alpha", "fixture-password")
            self.assertEqual((handle.player_id, handle.connection_id, handle.mode), (17, "native-connection-a", "player-requests"))
        self.assertEqual([r["command"] for r in peer.requests], ["status", "request_join", "status"])
        self.assertEqual(peer.requests[1]["args"]["instance"], "session-one")
        self.assertEqual(len(creation), 32)
        self.assertFalse(any(hasattr(handle, operation) for operation in ("goto", "attack", "spawn", "cancel", "eject")))

    def test_requests_carry_every_bound_identity_and_spawn_returns_new_generation(self):
        original, spawned, disconnected = requests_state(), requests_state(aircraft=99), state()
        disconnected["requestPlayers"] = []
        with Peer([original, original, original, original, original, original, spawned, spawned, disconnected]) as peer:
            session = peer.client()
            actor = session.request_player("alpha")
            actor.join_faction("native-faction")
            actor.purchase_airframe("COIN")
            actor = actor.request_spawn("native-base", "COIN", loadout=["native-mount", None], fuel=0.5, livery=0)
            self.assertEqual(actor.aircraft_id, 99)
            actor.disconnect()
        mutations = [r for r in peer.requests if r["command"] != "status"]
        self.assertEqual([r["command"] for r in mutations], ["request_join_faction", "request_purchase_airframe", "request_spawn", "request_disconnect"])
        for command in mutations:
            args = command["args"]
            self.assertEqual((args["name"], args["mode"], args["instance"], args["connectionId"], args["playerId"]),
                             ("alpha", "player-requests", "session-one", "native-connection-a", 17))
            self.assertIn("aircraftId", args)
            self.assertNotIn("position", args)
        self.assertEqual(mutations[2]["args"]["loadout"], ["native-mount", None])
        self.assertEqual(mutations[3]["args"]["aircraftId"], 99)

    def test_status_is_request_status_not_server_mock_binding(self):
        snapshot = requests_state()
        with Peer([snapshot, snapshot, snapshot]) as peer:
            session = peer.client()
            requester = session.request_player("alpha")
            self.assertEqual(requester.status()["allocation"], 123)
        self.assertEqual(peer.requests[-1]["command"], "request_status")
        self.assertEqual(requester.player_id, 17)  # same named server mock has player7

    def test_stale_runtime_connection_player_or_airframe_never_issues_mutation(self):
        for field, value in (("connectionId", "other"), ("playerId", 18), ("aircraft", {"id": 99}), ("instance", "restarted")):
            changed = requests_state()
            if field == "instance":
                changed[field] = value
            else:
                changed["requestPlayers"][0][field] = value
            with self.subTest(field=field), Peer([requests_state(), changed]) as peer:
                handle = peer.client().request_player("alpha")
                with self.assertRaises(StaleActor):
                    handle.purchase_airframe("COIN")
            self.assertEqual([r["command"] for r in peer.requests], ["status", "status"])

    def test_wrong_mode_or_revoked_capability_is_not_a_fallback_to_actor(self):
        wrong_mode, revoked = requests_state(mode="server-simulation"), requests_state()
        revoked["requestPlayers"][0]["capabilities"] = ["status", "disconnect"]
        for changed, error in ((wrong_mode, ProtocolError), (revoked, CommandError)):
            with self.subTest(error=error), Peer([requests_state(), changed]) as peer:
                handle = peer.client().request_player("alpha")
                with self.assertRaises(error):
                    handle.request_spawn("base", "COIN")
            self.assertFalse(any(r["command"] == "request_spawn" for r in peer.requests))

    def test_lost_join_response_recovers_pending_receipt_only_for_disconnect(self):
        pending = requests_state(player=None, ready=False)
        def lost(request):
            pending["requestPlayers"][0]["creationId"] = request["args"]["creationId"]
            return b""  # Applied connection creation, reply lost.
        disconnected = state()
        disconnected["requestPlayers"] = []
        with Peer([state(), lost, pending, pending, disconnected]) as peer:
            session = peer.client()
            with self.assertRaises(RequestJoinUncertain) as caught:
                session.join_request_player("alpha", "fixture-password")
            recovered = session.recover_request_player(caught.exception)
            self.assertIsNone(recovered.player_id)
            recovered.disconnect()
        self.assertEqual(sum(r["command"] == "request_join" for r in peer.requests), 1)
        self.assertEqual(peer.requests[-1]["args"]["playerId"], None)

    def test_recovery_refuses_an_unrelated_creation_tag_or_runtime(self):
        uncertainty = RequestJoinUncertain("alpha", "b" * 32, "session-one", ProtocolError("lost"))
        with Peer([requests_state()]) as peer:
            with self.assertRaises(StaleActor):
                peer.client().recover_request_player(uncertainty)
        changed = requests_state()
        changed["instance"] = "other-runtime"
        with Peer([changed]) as peer:
            with self.assertRaises(StaleActor):
                peer.client().recover_request_player(uncertainty)

    def test_completed_uncertain_join_recovery_still_grants_cleanup_only(self):
        uncertainty = RequestJoinUncertain("alpha", "a" * 32, "session-one", ProtocolError("lost"))
        with Peer([requests_state(), requests_state()]) as peer:
            recovered = peer.client().recover_request_player(uncertainty)
            self.assertEqual(recovered.capabilities, ("status", "disconnect"))
            with self.assertRaises(CommandError):
                recovered.purchase_airframe("COIN")
        self.assertFalse(any(r["command"] == "request_purchase_airframe" for r in peer.requests))

    def test_native_rejection_never_retries_or_direct_spawns(self):
        def rejected(request):
            return {"id": request["id"], "ok": False, "error": "Native insufficient allocation"}
        with Peer([requests_state(), requests_state(), rejected]) as peer:
            actor = peer.client().request_player("alpha")
            with self.assertRaises(CommandError):
                actor.purchase_airframe("COIN")
        self.assertEqual([r["command"] for r in peer.requests], ["status", "status", "request_purchase_airframe"])

    def test_pending_join_cannot_purchase(self):
        pending = requests_state(player=None, ready=False)
        with Peer([pending, pending]) as peer:
            actor = peer.client().request_player("alpha")
            with self.assertRaises(CommandError):
                actor.purchase_airframe("COIN")

    def test_old_handle_cannot_disconnect_a_spawned_generation(self):
        original, spawned = requests_state(), requests_state(aircraft=99)
        with Peer([original, original, spawned, spawned]) as peer:
            actor = peer.client().request_player("alpha")
            current = actor.request_spawn("base", "COIN")
            self.assertEqual(current.aircraft_id, 99)
            with self.assertRaises(StaleActor):
                actor.disconnect()
        self.assertEqual(sum(r["command"] == "request_spawn" for r in peer.requests), 1)
        self.assertFalse(any(r["command"] == "request_disconnect" for r in peer.requests))

    def test_invalid_native_selectors_reject_before_any_request(self):
        with Peer([requests_state()]) as peer:
            actor = peer.client().request_player("alpha")
            for extra in ({"fuel": float("nan")}, {"fuel": 1.1}, {"fuel": True}, {"livery": "0"},
                          {"loadout": "mount"}, {"loadout": [{"name": "gun", "ammo": 100}]}, {"loadout": [""]}):
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    actor.request_spawn("base", "COIN", **extra)
        self.assertEqual(len(peer.requests), 1)

    def test_loadout_over_native_sixteen_stations_is_rejected_before_request(self):
        with Peer([requests_state()]) as peer:
            actor = peer.client().request_player("alpha")
            with self.assertRaises(ValueError):
                actor.request_spawn("base", "COIN", loadout=[None] * 17)
        self.assertEqual(len(peer.requests), 1)


if __name__ == "__main__":
    unittest.main()
