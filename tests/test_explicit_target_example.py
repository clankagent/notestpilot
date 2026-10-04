import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "explicit_target.py"
spec = importlib.util.spec_from_file_location("explicit_target_example", EXAMPLE)
example = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = example
spec.loader.exec_module(example)


def fixture_state():
    return {"missionRunning": True, "serverActive": True, "aircraftTypes": ["COIN"],
            "factions": ["native-a", "native-b"], "airbases": [
                {"name": "observed-base-a", "faction": "native-a", "position": [10, 20, 30]},
                {"name": "observed-base-b", "faction": "native-b", "position": [90, 80, 70]}]}


def handles():
    return (SimpleNamespace(name="shooter", aircraft_id=10), SimpleNamespace(name="victim", aircraft_id=20))


def proof_events(shooter="shooter", attacker=10, victim=20):
    return [
        {"seq": 1, "type": "gun_bullet_created", "mockPlayer": shooter, "attackerPersistentId": attacker},
        {"seq": 2, "type": "unit_hit_registered", "mockPlayer": shooter, "attackerPersistentId": attacker,
         "victimPersistentId": victim},
        {"seq": 3, "type": "part_damage_applied", "mockPlayer": shooter, "dealerPersistentId": attacker,
         "victimPersistentId": victim, "attribution": "synchronous native TakeDamage scope",
         "pierceDamage": 1, "blastDamage": 0, "fireDamage": 0, "impactDamage": 0},
    ]


class ExampleTests(unittest.TestCase):
    def test_fixture_uses_observed_native_keys_and_relative_coordinates(self):
        result = example.choose_fixture(fixture_state())
        self.assertEqual(result["shooter_faction"], "native-a")
        self.assertEqual(result["victim_faction"], "native-b")
        self.assertEqual(result["shooter_position"], (10, 2220, 5030))
        self.assertEqual(result["victim_position"], (10, 2220, 5430))
        bad = fixture_state()
        bad["aircraftTypes"] = []
        with self.assertRaises(example.SmokeFailure):
            example.choose_fixture(bad)

    def test_proof_requires_native_bullet_hit_and_positive_attributed_application(self):
        evidence = example.Evidence(*handles())
        rows = proof_events()
        evidence.consume(rows[:2])
        self.assertFalse(evidence.complete)
        zero = {**rows[2], "pierceDamage": 0}
        evidence.consume([zero])
        self.assertFalse(evidence.complete)
        evidence.consume(rows[2:])
        self.assertTrue(evidence.complete)
        self.assertEqual(evidence.summary()["proofSequences"], {"bullet": 1, "hit": 2, "applied": 3})

    def test_unrelated_shooter_attacker_victim_or_no_attribution_cannot_pass(self):
        for rows in (proof_events(shooter="other"), proof_events(attacker=11), proof_events(victim=21),
                     [*proof_events()[:2], {**proof_events()[2], "attribution": None}]):
            evidence = example.Evidence(*handles())
            evidence.consume(rows)
            self.assertFalse(evidence.complete)

    def test_nonfinite_applied_damage_is_not_proof(self):
        evidence = example.Evidence(*handles())
        with self.assertRaises(example.SmokeFailure):
            evidence.consume([{**proof_events()[2], "pierceDamage": float("nan")}])

    def test_cleanup_refuses_changed_generations_and_preserves_unrelated_actors(self):
        removed = []
        original = SimpleNamespace(name="owned", instance="same", player_id=7, aircraft_id=10)
        current = SimpleNamespace(name="owned", instance="same", player_id=7, aircraft_id=11,
                                  remove=lambda: removed.append("owned"))
        session = SimpleNamespace(actor=lambda name: current)
        failures = example.cleanup_owned(session, [original])
        self.assertEqual(len(failures), 1)
        self.assertEqual(removed, [])
        current.aircraft_id = 10
        self.assertEqual(example.cleanup_owned(session, [original]), [])
        self.assertEqual(removed, ["owned"])

    def test_run_uses_one_attack_then_cleans_only_its_two_owned_players(self):
        class Actor:
            def __init__(self, session, name, faction):
                self.session, self.name = session, name
                self.instance, self.player_id, self.aircraft_id = "bound-instance", len(session.actors) + 1, None
                self.faction = faction
            def spawn(self, **args):
                self.session.spawns.append(args)
                spawned = Actor(self.session, self.name, self.faction)
                spawned.player_id = self.player_id
                spawned.aircraft_id = self.player_id * 10
                self.session.actors[self.name] = spawned
                if self.session.fail_spawn:
                    if self.session.changed_spawn_player:
                        spawned.player_id = 999
                    raise RuntimeError("Spawn applied but reply lost")
                return spawned
            def goto(self, destination, speed):
                self.session.navigation.append((self.name, destination, speed))
            def inspect_target(self, identity):
                self.session.inspections.append(identity)
                return {"targetId": identity, "disabled": False, "opposing": True, "known": True,
                        "accurate": True, "knownPosition": [10, 2220, 5430], "distance": 400}
            def attack(self, identity, station, seconds):
                self.session.attacks.append((self.name, identity, station, seconds))
                if self.session.fail_attack:
                    raise RuntimeError("Attack response failed; no retry")
            def remove(self):
                self.session.removed.append(self.name)
        class Session:
            timeout = 1
            def __init__(self):
                self.actors, self.spawns, self.navigation, self.inspections, self.attacks, self.removed = {}, [], [], [], [], []
                self.event_reads = 0
                self.fail_attack = False
                self.fail_creation = False
                self.fail_spawn = False
                self.changed_spawn_player = False
            def state(self):
                state = fixture_state()
                state["actors"] = [{"actor": actor.name, "playerId": actor.player_id, "aircraft": {
                    "id": actor.aircraft_id, "initialized": True, "stations": [{"index": 2, "fixedGun": True, "ammo": 20}],
                    "task": {"outcome": "running"}}} for actor in self.actors.values()]
                return state
            def wait_for(self, predicate, description, **kwargs):
                state = self.state()
                if not predicate(state):
                    raise AssertionError(description)
                return state
            def create(self, name, faction):
                actor = Actor(self, name, faction)
                self.actors[name] = actor
                if self.fail_creation:
                    failure = example.CreationUncertain(name, "a" * 32, actor.instance, RuntimeError("lost response"))
                    raise failure
                return actor
            def recover_creation(self, uncertainty):
                self.recovery_read = uncertainty
                return self.actors[uncertainty.name]
            def actor(self, name):
                return self.actors[name]
            def events(self):
                self.event_reads += 1
                shooter = next(a for a in self.actors.values() if a.name.endswith("shooter"))
                victim = next(a for a in self.actors.values() if a.name.endswith("victim"))
                return {"events": [] if self.event_reads == 1 else proof_events(shooter.name, shooter.aircraft_id, victim.aircraft_id)}
        session = Session()
        report = example.run(session)
        self.assertTrue(report["passed"])
        self.assertEqual(len(session.attacks), 1)
        self.assertEqual(session.attacks[0][1:3], (20, 2))
        self.assertEqual(len(session.navigation), 1)
        self.assertEqual(session.event_reads, 2)
        self.assertEqual(set(session.removed), set(session.actors))
        failed = Session()
        failed.fail_attack = True
        with self.assertRaisesRegex(RuntimeError, "no retry"):
            example.run(failed)
        self.assertEqual(len(failed.attacks), 1)
        self.assertEqual(set(failed.removed), set(failed.actors))
        uncertain_create = Session()
        uncertain_create.fail_creation = True
        with self.assertRaises(example.CreationUncertain):
            example.run(uncertain_create)
        self.assertEqual(len(uncertain_create.actors), 1)
        self.assertEqual(set(uncertain_create.removed), set(uncertain_create.actors))
        self.assertEqual(uncertain_create.attacks, [])
        uncertain_spawn = Session()
        uncertain_spawn.fail_spawn = True
        with self.assertRaisesRegex(RuntimeError, "reply lost"):
            example.run(uncertain_spawn)
        self.assertEqual(len(uncertain_spawn.spawns), 1)
        self.assertEqual(set(uncertain_spawn.removed), set(uncertain_spawn.actors))
        changed_spawn = Session()
        changed_spawn.fail_spawn = changed_spawn.changed_spawn_player = True
        with self.assertRaises(example.SmokeFailure) as caught:
            example.run(changed_spawn)
        self.assertIn("cleanup incomplete", str(caught.exception))
        self.assertEqual(changed_spawn.removed, [])
        self.assertEqual(len(changed_spawn.spawns), 1)


if __name__ == "__main__":
    unittest.main()
