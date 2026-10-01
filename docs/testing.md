# What a passing test establishes

**Check the server's answer, not just the client's request.**

An aircraft count rising could be an AI spawn. NOTestPilot compares player and
aircraft network IDs across processes so the test follows the correct player.

| Scenario | Checks | Coverage limit |
|---|---|---|
| `lifecycle.json` | Terminal Control, join, leave, rejoin, remote counts | One client; no aircraft |
| `two-player-escalation.json` | Two clients, matching IDs, factions, 30 seconds connected | Connected idle observation |
| `two-player-aircraft.json` | Reservations, ownership, spawning, engine commands, replicated throttle | Short unarmed control check |
| `two-player-motion.json` | Adds movement and checks displacement of the same aircraft IDs | Ground movement check, not a flight/combat soak |

The host command starts the game's UDP multiplayer host. It does not exercise
the dedicated-server manager's mission scheduling, rotation or Steam listing.

## Failure handling

Wrong request IDs, missing state, rejected actions, process exits, restarts and
deadlines fail. Unexpected game errors fail, including errors on other clients
during observation. An incomplete error journal fails. Switching aircraft cannot
satisfy a movement check.

One exact headless rendering `Error` is allowed:
`There is no texture data available to upload.` It occurs during menu/map loading
and remains visible in reports. An exception with the same message still fails.
Networking, simulation and authentication errors are not waived.

`accepted: true` means a request was issued. Add a server expectation for the
intended outcome. Commands are not automatically retried after a timeout because
a purchase or spawn might already have happened.

## Reports

Each run writes `result.json` and `junit.xml`. The JSON includes per-step states,
startup build fingerprints, errors and observation samples. Logs stay beside each
disposable game copy. Keep raw reports/logs private; publish reviewed summaries.

The input guard recognizes two exact 0.34.1 dedicated-server assembly fingerprints.
Updating the game requires reviewing the touched APIs and verifying the new build.
An old-build pass does not establish support for the next update.

## Needed before realistic capacity claims

Use several independently controlled clients flying and fighting for sustained
sessions. Include death/respawn, reconnects and mission aging. Use large missions
without reducing physics/AI cadence. Measure server and clients separately. Repeat
baseline and mod runs in alternating order with the same scripts and mission inputs.
Combat diverges over time; one run per variant is insufficient.

Retail clients, Steam authentication, mission rotation, combat and long-session
stability remain unverified coverage.

The dedicated control-expiry.json scenario verifies that a one-second lease ends
without a release command and the server receives cleared throttle/brake inputs.

Movement was observed once (178 m / 44 m), but a repeat lost the second player's
aircraft during the observation and failed. Its cause is not established. The
fixture needs route/obstacle handling; it is not a stable regression test yet.
