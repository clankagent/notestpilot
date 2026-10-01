"""Build equivalent flight workloads for one to four independent clients.

This creates a scenario, not a game process. The current dedicated fixture caps
connections at four; generation does not establish that any player count passed.
"""
import argparse
import json
from pathlib import Path

from .runner import client_names, validate_scenario


def flight_workload(players=2, seconds=600, fire=True):
    if isinstance(players, bool) or not isinstance(players, int) or not 1 <= players <= 4:
        raise ValueError("The current native fixture supports one to four clients")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 30 <= seconds <= 3600:
        raise ValueError("Airborne observation must last 30 to 3600 seconds")
    if not isinstance(fire, bool):
        raise ValueError("fire must be a boolean")
    actors = client_names(players)
    # Separate stock airbases reduce pilot-to-pilot interference without changing
    # mission content or moving any aircraft directly.
    starts = [("Primeva", "Sandrift Airbase"), ("Boscali", "Maris Airport"),
              ("Primeva", "Agrapol Airbase"), ("Boscali", "North Boscali Airbase")]
    steps = []

    def action(actor, command, args=None):
        steps.append({"name": f"{actor}: {command}", "target": actor,
                      "command": command, "args": args or {}})

    def check(actor, name, expectations, timeout=120, capture=None):
        step = {"name": name, "target": actor, "expect": expectations, "timeout": timeout}
        if capture:
            step["capture"] = capture
        steps.append(step)

    def same_peer(index, actor, field, client_field):
        return {"path": f"players.{index}.{field}",
                "equalsFrom": {"target": actor, "path": client_field}}

    action("server", "dedicated", {"missions": ["Escalation"], "port": 17777})
    check("server", "Native server waits for players", [
        {"path": "serverActive", "equals": True},
        {"path": "dedicated.running", "equals": True},
        {"path": "dedicated.hidden", "equals": True}])
    for actor in actors:
        action(actor, "connect", {"port": 17777})
        check(actor, f"{actor}: joins stock Escalation", [
            {"path": "localPlayerNetId", "notNull": True},
            {"path": "mission", "equals": "Escalation"},
            {"path": "missionRunning", "equals": True}])
    check("server", "Native server confirms every joined player", [
        {"path": "remotePlayers", "equals": players},
        {"path": "dedicated.currentMission", "equals": "Escalation"},
        {"path": "units", "atLeast": 800},
        *[same_peer(i, actor, "netId", "localPlayerNetId") for i, actor in enumerate(actors)]])
    for i, actor in enumerate(actors):
        faction, airbase = starts[i]
        action(actor, "faction", {"name": faction})
        check("server", f"Server confirms {actor}'s faction", [
            {"path": f"players.{i}.faction", "equals": faction}])
        action(actor, "reserve", {"aircraft": "COIN"})
        check(actor, f"{actor}: receives reserved airframe", [
            {"path": "localPlayerOwnedCount", "atLeast": 1}])
        action(actor, "spawn", {"airbase": airbase})
        check(actor, f"{actor}: receives aircraft", [
            {"path": "localPlayerAircraftNetId", "notNull": True}])
    check("server", "Server matches every owned aircraft", [
        same_peer(i, actor, "aircraftNetId", "localPlayerAircraftNetId")
        for i, actor in enumerate(actors)], capture="spawned")
    for actor in actors:
        action(actor, "engine", {"on": True})
    check("server", "Server confirms every engine is running", [
        {"path": f"players.{i}.ignition", "equals": True} for i in range(players)])

    alive = [{"path": "remotePlayers", "equals": players},
             {"path": "mission", "equals": "Escalation"},
             {"path": "missionRunning", "equals": True}]
    for i in range(players):
        alive.extend([{"path": f"players.{i}.aircraftNetId", "notNull": True},
                      {"path": f"players.{i}.disabled", "equals": False}])
    movement = [{"path": f"players.{i}", "minimumMetres": 100} for i in range(players)]

    def observation(name, duration, expectations, firing=False):
        steps.append({"name": name, "target": "server", "observe": {
            "seconds": duration, "expect": expectations, "motion": movement,
            "actions": [{"target": actor, "command": "fly",
                         "args": {"seconds": 45, "fire": firing,
                                  **({"fireSeconds": 0.5} if firing else {})}, "everySeconds": 20}
                        for actor in actors]}})

    observation("Every pilot taxis, takes off and climbs", 180, alive)
    airborne = [*alive]
    for i in range(players):
        airborne.extend([{"path": f"players.{i}.radarAltitude", "atLeast": 80},
                         {"path": f"players.{i}.speed", "atLeast": 50}])
    check("server", "Every pilot is airborne", airborne)
    observation("Sustained multiplayer flight" + (" and gun firing" if fire else ""),
                seconds, airborne, fire)
    final = []
    for i, actor in enumerate(actors):
        final.extend([
            same_peer(i, actor, "netId", "localPlayerNetId"),
            same_peer(i, actor, "aircraftNetId", "localPlayerAircraftNetId"),
            {"path": f"players.{i}.aircraftNetId", "equalsSaved": {
                "capture": "spawned", "path": f"players.{i}.aircraftNetId"}}])
        if fire:
            final.append({"path": f"players.{i}.weapons.0.ammo", "atMost": 999})
            final.append({"path": f"players.{i}.weapons.0.ammo", "atLeast": 1})
    check("server", "Server confirms original aircraft and weapon activity", final)
    scenario = {"name": f"Native Escalation: {players} pilots, {seconds}s airborne",
                "clients": players, "steps": steps}
    validate_scenario(scenario)
    return scenario


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--players", type=int, default=2)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--no-fire", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    scenario = flight_workload(args.players, args.seconds, not args.no_fire)
    # Do not overwrite a previous experiment's scenario fingerprint.
    with Path(args.output).open("x", encoding="utf-8") as output:
        json.dump(scenario, output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
