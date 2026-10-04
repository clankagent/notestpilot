import json
import socket
import threading
import time
import unittest

from notestpilot import CommandError, EventOverflow, ProtocolError, RuntimeFailure, Session, StaleActor, TransportError, WaitTimeout
from notestpilot.__main__ import two_actor_example
from notestpilot.client import CreationUncertain


TOKEN = "private-test-token-" + "x" * 32


def state(aircraft=42, player=7, instance="session-one"):
    return {"protocol": 2, "instance": instance, "errors": {"count": 0}, "actors": [
        {"actor": "alpha", "playerId": player, "aircraft": None if aircraft is None else {"id": aircraft}}]}


class Peer:
    """A real loopback TCP peer implementing only response framing fixtures."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.failures = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(2)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def serve(self):
        try:
            for reply in self.replies:
                with self.listener.accept()[0] as connection:
                    connection.settimeout(2)
                    data = bytearray()
                    while b"\n" not in data:
                        data.extend(connection.recv(4096))
                    request = json.loads(bytes(data).split(b"\n", 1)[0])
                    self.requests.append(request)
                    output = reply(request) if callable(reply) else reply if isinstance(reply, bytes) else {"id": request["id"], "ok": True, "result": reply}
                    wire = output if isinstance(output, bytes) else json.dumps(output).encode() + b"\n"
                    # Fragment even short replies to exercise full-response reads.
                    connection.sendall(wire[:5])
                    connection.sendall(wire[5:])
        except Exception as failure:
            self.failures.append(failure)
        finally:
            self.listener.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *ignored):
        self.thread.join(3)
        if self.thread.is_alive():
            self.listener.close()
            raise AssertionError("Fixture peer did not finish")
        if self.failures:
            raise self.failures[0]

    def client(self, **kwargs):
        return Session(self.port, TOKEN, timeout=2, **kwargs)


class ClientTests(unittest.TestCase):
    def test_fragmented_status_and_request_token(self):
        with Peer([state()]) as peer:
            self.assertEqual(peer.client().status()["instance"], "session-one")
        self.assertEqual(peer.requests[0]["command"], "status")
        self.assertEqual(peer.requests[0]["token"], TOKEN)

    def test_request_identity_mismatch_rejected(self):
        with Peer([lambda request: {"id": "wrong", "ok": True, "result": state()}]) as peer:
            with self.assertRaises(ProtocolError):
                peer.client().status()

    def test_response_size_cap_rejected(self):
        with Peer([b"x" * 200 + b"\n"]) as peer:
            with self.assertRaisesRegex(ProtocolError, "limit"):
                peer.client(max_response_bytes=64).status()

    def test_partial_response_rejected(self):
        with Peer([b'{"id":"unfinished"}']) as peer:
            with self.assertRaisesRegex(ProtocolError, "complete"):
                peer.client().status()

    def test_error_is_not_success(self):
        with Peer([lambda request: {"id": request["id"], "ok": False, "error": "Stale aircraft generation"}]) as peer:
            with self.assertRaisesRegex(CommandError, "Stale aircraft"):
                peer.client().call("actor.cancel")

    def test_transport_deadline_does_not_retry(self):
        def delayed(request):
            time.sleep(0.1)
            return b""
        with Peer([delayed]) as peer:
            client = Session(peer.port, TOKEN, timeout=0.02)
            with self.assertRaises(TransportError):
                client.call("actor.cancel", {"aircraftId": 42})
        self.assertEqual(len(peer.requests), 1)

    def test_wait_for_observes_transition_without_mutation(self):
        first, second = state(), state()
        first["ready"], second["ready"] = False, True
        with Peer([first, second]) as peer:
            result = peer.client().wait_for(lambda s: s["ready"], "ready transition", interval=0.001)
            self.assertTrue(result["ready"])
        self.assertEqual([r["command"] for r in peer.requests], ["status", "status"])

    def test_wait_timeout_preserves_last_state(self):
        with Peer([state()]) as peer:
            with self.assertRaises(WaitTimeout) as caught:
                peer.client().wait_for(lambda s: False, "a specific state", timeout=0.02, interval=1)
            self.assertEqual(caught.exception.last_state, state())
            self.assertEqual(caught.exception.description, "a specific state")
        self.assertEqual(len(peer.requests), 1)

    def test_wait_rejects_errors_and_failed_tasks_even_if_predicate_true(self):
        runtime_error, task_error = state(), state()
        runtime_error["errors"]["count"] = 1
        task_error["actors"][0]["aircraft"]["task"] = {"outcome": "failed", "error": "tracking lost"}
        for snapshot in (runtime_error, task_error):
            with self.subTest(snapshot=snapshot), Peer([snapshot]) as peer:
                with self.assertRaises(RuntimeFailure) as caught:
                    peer.client().wait_for(lambda s: True, "successful condition")
                self.assertEqual(caught.exception.state, snapshot)

    def test_wait_allows_only_explicit_exact_kind_and_message(self):
        message = "There is no texture data available to upload."
        snapshot = state()
        snapshot["errors"] = {"count": 1, "recent": [{"kind": "Error", "message": message}]}
        with Peer([snapshot]) as peer:
            result = peer.client().wait_for(lambda s: True, "ready",
                                           allowed_errors=(("Error", message),))
            self.assertEqual(result, snapshot)

    def test_wait_allowlist_rejects_unknown_kind_message_and_truncation(self):
        message = "There is no texture data available to upload."
        for errors in [
            {"count": 1, "recent": [{"kind": "Exception", "message": message}]},
            {"count": 1, "recent": [{"kind": "Error", "message": message + " extra"}]},
            {"count": 2, "recent": [{"kind": "Error", "message": message}]},
            {"count": 1, "recent": []},
        ]:
            snapshot = state()
            snapshot["errors"] = errors
            with self.subTest(errors=errors), Peer([snapshot]) as peer:
                with self.assertRaises(RuntimeFailure):
                    peer.client().wait_for(lambda s: True, "ready",
                                          allowed_errors=(("Error", message),))

    def test_wait_allowlist_requires_exact_pairs(self):
        client = Session(12345, TOKEN)
        with self.assertRaises(ValueError):
            client.wait_for(lambda s: True, "ready", allowed_errors=("Error",))

    def test_units_filter_arguments_and_native_metadata(self):
        row = {"id": 99, "netId": 101, "name": "observed target", "faction": "observed-opponent",
               "disabled": False, "position": [10, 20, 30], "kind": "GroundVehicle"}
        with Peer([state(), [row]]) as peer:
            result = peer.client().units(faction="observed-opponent", name_contains="target", limit=4)
            self.assertEqual(result, [row])
        self.assertEqual(peer.requests[-1]["command"], "units")
        self.assertEqual(peer.requests[-1]["args"], {"instance": "session-one", "faction": "observed-opponent",
                         "nameContains": "target", "includeDisabled": False, "limit": 4})

    def test_units_rejects_duplicate_identity(self):
        row = {"id": 99, "netId": 101, "name": "target", "disabled": False,
               "position": [10, 20, 30], "kind": "Aircraft"}
        with Peer([state(), [row, row]]) as peer:
            with self.assertRaises(ProtocolError):
                peer.client().units()

    def test_inspect_target_sends_bound_identity_without_mutating_task(self):
        inspection = {"targetId": 99, "disabled": False, "opposing": True, "known": True,
                      "accurate": True, "knownPosition": [10, 20, 30], "distance": 100.5}
        with Peer([state(), state(), inspection]) as peer:
            actor = peer.client().actor("alpha")
            self.assertEqual(actor.inspect_target(99), inspection)
        self.assertEqual([r["command"] for r in peer.requests], ["status", "status", "actor.inspect_target"])
        self.assertEqual(peer.requests[-1]["args"], {"actor": "alpha", "playerId": 7, "aircraftId": 42,
                         "instance": "session-one", "targetId": 99})

    def test_inspect_target_rejects_mismatch_and_malformed_evidence(self):
        valid = {"targetId": 99, "disabled": False, "opposing": True, "known": True,
                 "accurate": True, "knownPosition": [10, 20, 30], "distance": 100.5}
        for change in ({"targetId": 100}, {"known": 1}, {"distance": "100"},
                       {"distance": -1}, {"knownPosition": None}, {"knownPosition": [1, 2]},
                       {"known": False, "accurate": True}):
            with self.subTest(change=change), Peer([state(), state(), {**valid, **change}]) as peer:
                actor = peer.client().actor("alpha")
                with self.assertRaises(ProtocolError):
                    actor.inspect_target(99)

    def test_cancel_sends_exact_identity(self):
        with Peer([state(), state(), state()]) as peer:
            actor = peer.client().actor("alpha")
            actor.cancel()
        self.assertEqual(peer.requests[-1]["command"], "actor.cancel")
        self.assertEqual(peer.requests[-1]["args"], {"actor": "alpha", "playerId": 7, "aircraftId": 42, "instance": "session-one"})

    def test_replaced_aircraft_blocks_mutation(self):
        with Peer([state(), state(43)]) as peer:
            actor = peer.client().actor("alpha")
            with self.assertRaises(StaleActor):
                actor.cancel()
        self.assertEqual([r["command"] for r in peer.requests], ["status", "status"])

    def test_reused_name_player_blocks_mutation(self):
        with Peer([state(), state(player=8)]) as peer:
            actor = peer.client().actor("alpha")
            with self.assertRaises(StaleActor):
                actor.remove()

    def test_restart_invalidates_session(self):
        with Peer([state(), state(instance="new-session")]) as peer:
            client = peer.client()
            client.status()
            with self.assertRaises(StaleActor):
                client.status()

    def test_host_and_create_pin_instance(self):
        with Peer([state(), state(), state(), state()]) as peer:
            client = peer.client()
            client.host("observed-mission")
            actor = client.create("alpha", "observed-faction")
            self.assertEqual(actor.player_id, 7)
        self.assertEqual(peer.requests[1]["args"], {"mission": "observed-mission", "instance": "session-one"})
        creation_args = peer.requests[3]["args"]
        self.assertEqual({key: creation_args[key] for key in ("actor", "faction", "instance")},
                         {"actor": "alpha", "faction": "observed-faction", "instance": "session-one"})
        self.assertRegex(creation_args["creationId"], r"^[0-9a-f]{32}$")

    def test_lost_creation_reply_recovers_only_exact_native_correlation_without_replay(self):
        applied = []
        def lost_reply(request):
            applied.append(request)
            return b""  # Native creation applied; response connection then closes.
        def inspect_record(request):
            native = state(None)
            native["actors"][0]["creationId"] = applied[0]["args"]["creationId"]
            return {"id": request["id"], "ok": True, "result": native}
        with Peer([state(), lost_reply, inspect_record]) as peer:
            client = peer.client()
            with self.assertRaises(CreationUncertain) as caught:
                client.create("alpha", "observed-faction")
            uncertainty = caught.exception
            self.assertIsInstance(uncertainty.cause, ProtocolError)
            self.assertEqual(uncertainty.instance, "session-one")
            recovered = client.recover_creation(uncertainty)
            self.assertEqual((recovered.name, recovered.player_id, recovered.aircraft_id), ("alpha", 7, None))
        self.assertEqual([r["command"] for r in peer.requests], ["status", "actor.create", "status"])
        self.assertEqual(len(applied), 1)

    def test_creation_recovery_refuses_same_name_with_wrong_tag_or_runtime(self):
        for tag, instance in (("other-tag", "session-one"), ("exact-tag", "different-instance")):
            native = state(None, instance=instance)
            native["actors"][0]["creationId"] = tag
            with self.subTest(tag=tag, instance=instance), Peer([native]) as peer:
                uncertainty = CreationUncertain("alpha", "exact-tag", "session-one", ProtocolError("lost"))
                with self.assertRaises(StaleActor):
                    peer.client().recover_creation(uncertainty)

    def test_restart_before_host_or_create_preflight_sends_no_mutation(self):
        for action in (lambda s: s.host(), lambda s: s.create("alpha", "faction")):
            with self.subTest(action=action), Peer([state(), state(instance="replacement")]) as peer:
                client = peer.client()
                client.status()
                with self.assertRaises(StaleActor):
                    action(client)
            self.assertEqual([r["command"] for r in peer.requests], ["status", "status"])

    def test_restart_between_preflight_and_mutation_is_rejected_without_retry(self):
        for action, command in ((lambda s: s.host(), "host"),
                                (lambda s: s.create("alpha", "faction"), "actor.create")):
            mutations = []
            def guarded_reply(request):
                # Model the actual server's current-instance guard. Its instance
                # changed after status; stale requests never reach mutation.
                if request["args"].get("instance") != "replacement":
                    return {"id": request["id"], "ok": False, "error": "Stale runtime instance"}
                mutations.append(request)
                return {"id": request["id"], "ok": True, "result": state(instance="replacement")}
            with self.subTest(command=command), Peer([state(), guarded_reply]) as peer:
                with self.assertRaises(CommandError):
                    action(peer.client())
            self.assertEqual(mutations, [])
            self.assertEqual([r["command"] for r in peer.requests], ["status", command])

    def test_quit_pins_instance_and_accepts_nonstate_receipt(self):
        with Peer([state(), {"accepted": True}]) as peer:
            self.assertEqual(peer.client().quit(), {"accepted": True})
        self.assertEqual(peer.requests[-1]["command"], "quit")
        self.assertEqual(peer.requests[-1]["args"], {"instance": "session-one"})

    def test_spawn_returns_new_immutable_handle(self):
        with Peer([state(None), state(None), state(43)]) as peer:
            original = peer.client().actor("alpha")
            spawned = original.spawn(position=(1, 2, 3))
            self.assertIsNone(original.aircraft_id)
            self.assertEqual(spawned.aircraft_id, 43)
        self.assertEqual(peer.requests[-1]["args"]["playerId"], 7)
        self.assertEqual(peer.requests[-1]["args"]["instance"], "session-one")

    def test_lost_spawn_reply_can_inspect_new_generation_for_owned_cleanup_only(self):
        applied = []
        def lost_spawn(request):
            applied.append(request)
            return b""
        removed = state(None)
        removed["actors"] = []
        with Peer([state(None), state(None), lost_spawn, state(43), state(43), removed]) as peer:
            client = peer.client()
            original = client.actor("alpha")
            with self.assertRaises(ProtocolError):
                original.spawn(position=(1, 2, 3))
            recovered = client.actor("alpha")
            self.assertEqual((recovered.instance, recovered.player_id), (original.instance, original.player_id))
            self.assertIsNone(original.aircraft_id)
            self.assertEqual(recovered.aircraft_id, 43)
            recovered.remove()
        self.assertEqual(len(applied), 1)
        self.assertEqual([r["command"] for r in peer.requests].count("actor.spawn"), 1)
        self.assertEqual(peer.requests[-1]["args"]["aircraftId"], 43)

    def test_event_cursor_advances_without_replaying(self):
        first = {"cursor": 2, "overflow": False, "observerErrors": 0, "events": [{"seq": 1}, {"seq": 2}]}
        second = {"cursor": 2, "overflow": False, "observerErrors": 0, "events": []}
        with Peer([state(), first, state(), second]) as peer:
            client = peer.client()
            self.assertEqual(len(client.events()["events"]), 2)
            self.assertEqual(client.events()["events"], [])
        self.assertEqual(peer.requests[-1]["args"], {"after": 2, "instance": "session-one"})

    def test_bad_actor_arguments_do_not_issue_commands(self):
        with Peer([state()]) as peer:
            actor = peer.client().actor("alpha")
            for destination, speed in [(None, 100), ((1, 2, 3), float("nan")), ((1, 2), 100)]:
                with self.subTest(destination=destination), self.assertRaises(ValueError):
                    actor.goto(destination, speed)
            with self.assertRaises(ValueError):
                actor.attack(1, seconds=0)
        self.assertEqual(len(peer.requests), 1)

    def test_overflow_and_observer_error_reject_evidence(self):
        for batch, expected in [
            ({"cursor": 50, "overflow": True, "observerErrors": 0, "events": []}, EventOverflow),
            ({"cursor": 1, "overflow": False, "observerErrors": 1, "events": []}, ProtocolError),
        ]:
            with self.subTest(batch=batch), Peer([state(), batch]) as peer:
                client = peer.client()
                with self.assertRaises(expected):
                    client.events()
                self.assertEqual(client.event_cursor, 0)

    def test_unsorted_events_rejected(self):
        batch = {"cursor": 2, "overflow": False, "observerErrors": 0, "events": [{"seq": 2}, {"seq": 1}]}
        with Peer([state(), batch]) as peer:
            with self.assertRaises(ProtocolError):
                peer.client().events()

    def test_two_actor_example_uses_observed_bases_and_cleans_owned_actors(self):
        class ExampleActor:
            def __init__(self, owner, name, faction):
                self.owner, self.name, self.faction = owner, name, faction
                self.player_id = len(owner.actors) + 1
            def spawn(self, **args):
                self.owner.spawns.append((self.faction, args))
                return self
            def goto(self, destination, speed):
                self.owner.tasks.append((self.name, destination, speed))
            def status(self):
                return {"actor": self.name}
            def remove(self):
                self.owner.removed.append(self.name)
        class ExampleSession:
            def __init__(self):
                self.actors, self.spawns, self.tasks, self.removed = {}, [], [], []
            def status(self):
                return {"missionRunning": True, "aircraftTypes": ["observed-plane"],
                        "factions": ["observed-one", "observed-two"], "airbases": [
                            {"faction": "observed-one", "position": [10, 20, 30]},
                            {"faction": "observed-two", "position": [40, 50, 60]}]}
            def create(self, name, faction):
                result = ExampleActor(self, name, faction)
                self.actors[name] = result
                return result
            def actor(self, name):
                return self.actors[name]
            def events(self):
                return {"events": []}
        session = ExampleSession()
        list(two_actor_example(session, "observed-plane", seconds=0.001))
        self.assertEqual([item[0] for item in session.spawns], ["observed-one", "observed-two"])
        self.assertEqual([item[1]["position"] for item in session.spawns], [(10, 1020, 30), (40, 1050, 60)])
        self.assertEqual([item[1] for item in session.tasks], [(2010, 1020, 30), (40, 1050, 2060)])
        self.assertEqual(set(session.removed), set(session.actors))


if __name__ == "__main__":
    unittest.main()
