import unittest

from notestpilot.runner import matches_with_peers
from notestpilot.scenarios import flight_workload


class WorkloadTests(unittest.TestCase):
    def test_fourth_actor_cannot_be_missing_or_mismatched(self):
        scenario = flight_workload(players=4, seconds=600)
        check = next(s for s in scenario["steps"] if s["name"] == "Native server confirms every joined player")
        class Peer:
            def __init__(self, identity): self.identity = identity
            def status(self): return {"localPlayerNetId": self.identity}
        peers = {f"client{i + 1}": Peer(100 + i) for i in range(4)}
        server = {"remotePlayers": 4, "dedicated": {"currentMission": "Escalation"},
                  "units": 1000, "players": [{"netId": 100 + i} for i in range(4)]}
        valid = lambda: all(matches_with_peers(server, a, peers) for a in check["expect"])
        self.assertTrue(valid())
        server["players"][3]["netId"] = 999
        self.assertFalse(valid())
        server["players"].pop()
        self.assertFalse(valid())

    def test_replacement_cannot_satisfy_final_aircraft_check(self):
        scenario = flight_workload(players=1, seconds=30, fire=False)
        final = scenario["steps"][-1]
        aircraft_check = next(a for a in final["expect"] if "equalsSaved" in a)
        original = {"players": [{"aircraftNetId": 22}]}
        self.assertTrue(matches_with_peers(original, aircraft_check, {}, {"spawned": original}))
        self.assertFalse(matches_with_peers({"players": [{"aircraftNetId": 23}]}, aircraft_check, {}, {"spawned": original}))
        self.assertFalse(matches_with_peers({"players": [{"aircraftNetId": None}]}, aircraft_check, {}, {"spawned": original}))

    def test_generation_respects_fixture_limit_and_bounded_run(self):
        for players in (0, 5, True):
            with self.assertRaises(ValueError): flight_workload(players=players)
        for seconds in (0, 3601, True):
            with self.assertRaises(ValueError): flight_workload(seconds=seconds)

    def test_dry_firing_and_no_firing_cannot_pass_weapon_activity(self):
        scenario = flight_workload(players=1, seconds=600)
        ammunition = [a for a in scenario["steps"][-1]["expect"] if a["path"].endswith("ammo")]
        def valid(ammo):
            state = {"players": [{"weapons": [{"ammo": ammo}]}]}
            return all(matches_with_peers(state, a, {}) for a in ammunition)
        self.assertTrue(valid(500))
        for ammo in (0, 1000, None):
            self.assertFalse(valid(ammo))


if __name__ == "__main__":
    unittest.main()
