"""Server simulation tests with native mocks; no connected-player coverage."""

import argparse
import json
import time
import uuid

from .client import Session


def two_actor_example(session: Session, aircraft_type: str, seconds: float = 5):
    """Task two owned actors using observed airbases, then remove both.

    Run: uv run notestpilot two-actors --type <observed-aircraft-key>
    This submits navigation tasks; it does not assert arrival or combat success.
    """
    if not 0 < seconds <= 60:
        raise ValueError("Example duration must be in (0, 60] seconds")
    state = session.status()
    if state.get("missionRunning") is not True:
        raise ValueError("Start the disposable lab mission before running this example")
    if aircraft_type not in state.get("aircraftTypes", []):
        raise ValueError("Choose an aircraft key listed by status.aircraftTypes")
    bases = [b for b in state.get("airbases", []) if b.get("faction") in state.get("factions", [])
             and isinstance(b.get("position"), list) and len(b["position"]) == 3]
    if len(bases) < 2:
        raise ValueError("Example requires two observed faction-owned airbases")
    prefix = "script-" + uuid.uuid4().hex[:10]
    owned = []
    cleanup_errors = []
    try:
        for index, base in enumerate(bases[:2]):
            actor = session.create(f"{prefix}-{index + 1}", base["faction"])
            owned.append(actor)
            x, y, z = base["position"]
            # This deliberate airborne fixture uses offsets from observed map
            # positions; it neither guesses named bases nor changes other units.
            actor = actor.spawn(type=aircraft_type, position=(x, y + 1000, z), velocity=(0, 0, 100))
            owned[-1] = actor
            destination = (x + 2000, y + 1000, z) if index == 0 else (x, y + 1000, z + 2000)
            actor.goto(destination, speed=100)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            yield {"actors": [actor.status() for actor in owned], "events": session.events()}
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    finally:
        for actor in reversed(owned):
            try:
                # Explicit cleanup rebinding handles a spawn that was applied
                # before its response failed; the player identity stays pinned.
                current = session.actor(actor.name)
                if current.player_id != actor.player_id:
                    raise RuntimeError("Owned actor identity changed during cleanup")
                current.remove()
            except Exception as failure:
                cleanup_errors.append(f"{actor.name}: {failure}")
        if cleanup_errors:
            raise RuntimeError("Example cleanup incomplete: " + "; ".join(cleanup_errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "events", "two-actors"), nargs="?", default="status")
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--type", dest="aircraft_type", help="Observed aircraft key for two-actors")
    parser.add_argument("--seconds", type=float, default=5)
    args = parser.parse_args()
    session = Session()
    if args.command == "two-actors":
        if not args.aircraft_type:
            parser.error("two-actors requires --type from status.aircraftTypes")
        for sample in two_actor_example(session, args.aircraft_type, args.seconds):
            print(json.dumps(sample, allow_nan=False), flush=True)
        return
    result = session.events(after=args.after) if args.command == "events" else session.status()
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
