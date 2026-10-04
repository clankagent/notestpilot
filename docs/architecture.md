# How the programmable pilots work

A test is a Python program. It creates named mock players, gives each one a task,
reads what the server did, and makes assertions. Tasks can change in response to
observations: this is a two-way control API, not a recording of button presses.

All mock players share the running server's world and game process. They each
have a native `Player`, `PlayerRef`, faction membership and associated aircraft.
They have no network connection or Steam identity. This allows server behavior
tests without starting another complete game process for every actor.

## Who makes the decisions?

| Decision | Owner |
|---|---|
| Which mock player acts | Your script |
| Destination and requested speed | Your script |
| Exact opposing target and weapon station | Your script |
| Steering towards the assigned point | Native `Autopilot.AutoAim` |
| Estimated bullet travel time | Native `TargetCalc.TargetLeadTime` |
| Aim compensation and firing gates | The directed task adapter |
| Ammunition, gun cadence, bullets, collisions and damage | Normal game routines |
| Whether the test passed | Your script's observations and assertions |

The adapter assigns a small `DirectedPilot` state to the aircraft's pilot. It
never initializes the stock autonomous NPC brain. It borrows particular native
routines without giving the NPC mission planner or target selector control of
the test. Native HQ tracking and line of sight still constrain an attack; the
adapter does not invent visibility.

Navigation currently steers toward one explicit point. It is not a route planner,
taxi controller, landing system or collision avoidance guarantee. Attack currently
supports an armed fixed boresight gun, not every weapon available in the game.

## A task can react

```python
target = other_pilot.aircraft_id  # one specific native aircraft generation
inspection = pilot.inspect_target(target)
if inspection["opposing"] and inspection["known"] and inspection["accurate"]:
    pilot.attack(target, station=chosen_station, seconds=20)

# Read session.events(), assert the exact attacker/victim IDs,
# then cancel, replace the task or fail with the observed state.
```

The control listener accepts authenticated newline-delimited JSON on loopback.
It queues requests for Unity's main thread. Python waits and branches outside
the game; the directed task runs with the game's normal pilot update callbacks.
No simulation frequency is changed. An attack has an explicit deadline, and
the script's waits also have deadlines and retain their last observed state.

## Handles protect the intended actor

Every handle contains three identities: runtime instance, native player ID and
aircraft persistent ID. `spawn()` returns a new immutable handle; keep that
returned value. Ejection and replacement retire the old aircraft generation.
Reusing a name does not transfer an old handle to the new player.

Both Python and the runtime check these identities. A stale request fails before
mutating a replacement. A transport failure can happen after a command was
applied, so mutations are never automatically retried. Inspect fresh state before
deciding how to recover. Creation requests carry a unique receipt ID, retained by
the runtime even after removal. `CreationUncertain` carries that ID;
`session.recover_creation(error)` reads state and binds only the matching creation
in the same runtime. It does not repeat the command or turn a failed test into a
pass. The runtime rejects reused receipt IDs, with a limit of 4,096 creations per
disposable process to keep the ledger bounded.

The combat example uses such recovery only for cleanup. After an uncertain spawn
it can inspect the same native player and adopt the resulting aircraft for
cleanup; a changed runtime or player is refused. Outside that explicit recovery,
cleanup still requires the recorded aircraft generation and reports mismatches.

## Evidence is separate from commands

`fireRequests` tells you that the adapter asked the pilot to fire. It proves
neither a bullet nor a hit. The observer records distinct native events:

1. `gun_bullet_created`: the normal bullet creation path completed.
2. `unit_hit_registered`: a native hit was registered against a particular unit.
3. `part_damage_applied`: the native part damage application completed.

Damage source attribution is included only when the application occurs in the
matching synchronous native `TakeDamage` context. `part_damage_call` alone is
an attempted damage call, and is not proof of applied damage. A damage network
message alone also lacks a reliable attacker field.

Events have sequence numbers in a bounded buffer. Overflow or observer errors
invalidate evidence. Postfix observations need not appear in causal order, so
assert matching identities and event meanings rather than an assumed ordering.

## Cleanup and coverage

Cancellation restores the prior pilot state, targeting, gun linking and flight
assist, and stops the directed controls. Removal retires the mock player's
linked aircraft and registry entries. Native ejected crew and other emitted
world objects follow their normal world lifecycle; actor removal is not a promise
to erase every effect. Quitting the disposable server ends the entire fixture.

These mocks test authoritative server game behavior. They do not test retail
client login, ownership transfer over transport, packet serialization, rendering
or a human multiplayer session. Keep those as separate tests.
