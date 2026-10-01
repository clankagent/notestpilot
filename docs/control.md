# How to control a test player

**Tell the client what to do. Ask the server whether it happened.**

Each client is a separate Nuclear Option process. A BepInEx 5 plugin provides a
local control interface. Python sends commands there and polls the server's bridge
for the resulting state.

```mermaid
sequenceDiagram
  participant R as Python runner
  participant B as Client bridge
  participant C as Game client
  participant S as Game server
  R->>B: faction / reserve / spawn
  B->>C: Execute on Unity main thread
  C->>S: Normal game request / RPC
  S-->>C: Replicated player / aircraft state
  R->>S: Read server bridge state
  S-->>R: IDs, faction, controls
  Note over R,S: Compare with the client's owned IDs
```

The socket worker parses requests; game methods run on Unity's main thread. Each
request has an ID, token and execution deadline. The runner creates a fresh random
token for each run. This token is separate from the game server password.

Two scoped adapters allow headless UDP connections without retail Steam callbacks
and provide a fallback display name. The server's original authenticator still
checks the join. This does not test Steam authentication or retail-client compatibility.

## Commands

| Command | Target | Arguments / effect |
|---|---|---|
| `status` | Either | Read mission, IDs, aircraft, controls and game errors |
| `host` | Server | `mission`, `port`, optional `password`; UDP multiplayer host |
| `connect` | Client | `port`, optional `password`; join loopback host |
| `disconnect` | Client | Normal game network stop |
| `faction` | Client | `name`; request faction selection |
| `reserve` | Client | Optional `aircraft` key; request eligible reserve airframe |
| `purchase` | Client | `aircraft` key; request purchase (**unverified at runtime**) |
| `spawn` | Client | Optional `aircraft`, `airbase`, `fuel`; owned aircraft; game default loadout unless explicitly specified |
| `engine` | Client | `on`; request ignition toggle if needed |
| `controls` | Client | `pitch`, `roll`, `yaw`, `throttle`, `brake`, `seconds`, optional `fire` |
| `release-controls` | Client | End input override |
| `fly` | Client | `seconds`, optional `fire`; experimental taxi, takeoff, climb and square circuit |
| `catalog` | Either | Aircraft keys, compatible hardpoints and available standard loadouts |
| `eject` | Client | Normal ejection; parked recovery/respawn passed for two clients |
| `gear` | Client | `down`; normal gear operation |
| `next-weapon` | Client | Cycle the normal weapon selection; runtime test pending |

Default reserve selection is deterministic: eligible rank, then aircraft key.
Default spawn chooses an owned airframe and a friendly compatible base. Game
availability and ownership checks still apply. A command reply is not proof of success.

## Aircraft controls

During an active control lease, the bridge replaces the local pilot state's
keyboard/joystick reading with supplied raw values. Input filtering, pilot
simulation, physics and network snapshots keep running. The runner never directly
moves or teleports the aircraft.

Pitch/roll/yaw range from −1 to +1. Throttle/brake range from 0 to 1. Leases last
0.1–60 seconds and target one owned aircraft. Omitted controls are zero: every
command supplies a complete set. Long scripts must renew leases deliberately.

Expiry/release clears the supplied controls and returns to normal input sampling.
If the aircraft disappears, the override stops applying. Lease status reports the
aircraft ID, remaining time and how many times the pilot applied the values.

Replacing input reading also bypasses that layer's UI and pilot-strength gates.
This is a test driver, not a keyboard/menu test or an exact simulation of human
input limitations. Flight and combat need their own runtime scenarios.

## Check the resulting ownership

```json
{
  "target": "server",
  "expect": [
    { "path": "remotePlayers", "equals": 2 },
    {
      "path": "players.1.aircraftNetId",
      "equalsFrom": { "target": "client1", "path": "localPlayerAircraftNetId" }
    }
  ],
  "timeout": 60
}
```

Snapshots sort players by player index. Included scenarios join in a known order.
A selector by network ID will be needed for arbitrary concurrent join patterns.

## Python control

A custom controller can use the runner's interface:

```python
from notestpilot.runner import Bridge

client = Bridge(port=bridge_port, token=run_token)
client.call("controls", {"throttle": 0.7, "brake": 0, "seconds": 10})
state = client.status()
client.call("release-controls")
```

The port/token belong to an explicitly enabled disposable game process. The current
CLI executes whole scenarios and closes its processes afterward. The experimental flight script uses this same interface. An interactive dashboard
is not implemented.

## A longer action script

An observation can renew a flight lease while checking the server every second:

```json
{
  "target": "server",
  "observe": {
    "seconds": 900,
    "expect": [{"path": "remotePlayers", "equals": 2}],
    "actions": [
      {"target": "client1", "command": "fly",
       "args": {"seconds": 45, "fire": true}, "everySeconds": 20}
    ]
  }
}
```

The included soak scenario renews both clients and additionally requires both owned
aircraft to stay alive, moving and at least 80 metres above terrain. This is a
**running test, not a passed soak**. Firing calls the game's normal weapon method;
weapon safety, ammunition and firing cadence still apply.

`fly` uses client-local taxi pathfinding and the game's steering helpers. It does
not enable server AI for the player or assign aircraft position/velocity. Its
steering replaces human input, so passing this script would not prove human skill,
menu behaviour or sophisticated combat AI.

Spawn accepts either a named `loadout` or `weapons` with one mount key/null per
hardpoint set. Read `catalog` first. These options still go through normal spawn
validation and need targeted runtime coverage. An empty request can receive the
game's default weapons: inspect the actual aircraft's `weapons` snapshot rather
than assuming an empty request means an unarmed plane.
