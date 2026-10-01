# What a passing test establishes

**Check the server's answer, not just the client's request.**

An aircraft count rising could be an AI spawn. NOTestPilot compares player and
aircraft network IDs across processes so the test follows the correct player.

| Scenario | Checks | Coverage limit |
|---|---|---|
| `lifecycle.json` | Terminal Control, join, leave, rejoin, remote counts | One client; no aircraft |
| `two-player-escalation.json` | Two clients, matching IDs, factions, 30 seconds connected | Connected idle observation |
| `two-player-aircraft.json` | Reservations, ownership, spawning, engine commands, replicated throttle | Short control check without firing |
| `two-player-motion.json` | Adds movement and checks displacement of the same aircraft IDs | Ground movement check, not a flight/combat soak |
| `two-player-flight.json` | Repeated steering, runway takeoff and server altitude checks | Three-minute script passed; long-session stability unproven |
| `two-player-flight-soak.json` | Fifteen minutes airborne, renewed steering/firing, ammo checks | Failed after about ten minutes: physical collision disabled one player aircraft |
| `two-player-recovery.json` | Parked ejection, inventory recovery and spawning again | Passed with two clients; does not establish airborne combat death |
| `native-startup-flight.json` | Native waiting server, player-triggered Escalation loading, two-client flight, weapon cycling and rockets | Passed 37 checks; three-minute flight plus 30 seconds of firing |
| `native-dedicated-flight.json` | Native Terminal Control startup and time-limit rotation to Escalation | Failed during ship particle-effect cleanup; subsequent flight steps were not reached |
| `native-two-sorties.json` | Airborne ejection, normal reserve replacement, changed aircraft IDs with original player IDs, second takeoff | Passed 51 checks; both replacement aircraft finished airborne |
| `native-three-sorties-reconnect.json` | Three sorties, airborne replacement, rockets, six minutes of gun firing, sequential reconnects | Repeat passed flight/replacement/firing and the first reconnect, then failed while the second client disconnected: missile-warning display exception |
| `native-flight-reconnect.json` | Occupied-aircraft disconnect and reconnect after native flight/rockets | Failed on first disconnect in radar-warning display cleanup |
| `native-gun-bursts.json` | Three brief gun bursts per client; trigger expires while flight continues; ammunition stops decreasing | Passed 54 checks on the native server |
| `native-playing-rotation.json` | Fly/fire in Escalation, rotate to Terminal Control and back, verify both peers | Scenario prepared; runtime unverified |

The `host` command starts UDP multiplayer hosting. The separate `dedicated`
command uses the original dedicated-server manager and its player-triggered
mission loading. Both modes remain hidden; retail Steam joining/listing is not
tested. The native tests revealed an actual rotation failure: unloading Terminal
Control calls `ShipPropulsion.DisablePropulsion`, then throws inside
`ParticleSystem.Stop`. The suite keeps this failure rather than ignoring it.

The three-sortie repeat caught a separate `ThreatItem.AnimateItem` exception
while the second client unloaded its mission. This is a client-side failure in
the tested headless setup; retail reproduction is unverified. The disconnect
command now resumes the gameplay UI and sets the disconnect reason as the normal
Quit button does. A separate occupied-flight reconnect repeat still failed in
`RadarWarning.Update` while unloading. The quit-path alignment did not resolve
warning-display cleanup. Both failures remain recorded.

Another three-sortie repeat failed after roughly 143 seconds of its final
six-minute flight window. The diagnostics trace missile blast/fragment damage to
a pilot, then collisions between parts of that aircraft and loss of altitude.
This establishes combat damage before the descent; the attacker is unidentified.
The strict survival test failed. It does not establish a server crash or a
performance-mod regression, and the earlier collision failure remains separate.

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

Each run writes `result.json` and `junit.xml`. Observations also write
`timeline.ndjson` and update `progress.json` as they run. Failures retain the last
sample and attempt a snapshot from every peer, so a lost aircraft can be separated
from a lost connection. The JSON includes per-step states,
startup build fingerprints, errors and observation samples. Logs stay beside each
disposable game copy. Keep raw reports/logs private; publish reviewed summaries.
Use a new output directory for every attempt. The runner refuses an existing
directory so an old passing report cannot masquerade as a new result. New reports,
progress and timeline entries share a run ID. Final JSON/XML files are replaced
atomically; even a setup failure gets a failed report without launching a game.

The input guard recognizes two exact 0.34.1 dedicated-server assembly fingerprints.
Updating the game requires reviewing the touched APIs and verifying the new build.
An old-build pass does not establish support for the next update.

## Needed before realistic capacity claims

Use several independently controlled clients flying and fighting for sustained
sessions. Include death/respawn, reconnects and mission aging. Use large missions
without reducing physics/AI cadence. Measure server and clients separately. Repeat
baseline and mod runs in alternating order with the same scripts and mission inputs.
Combat diverges over time; one run per variant is insufficient.

Run server variants one at a time, with multiple clients active inside each run.
Two variants running together would compete for the same resources. Use the same
scenario goals and checks; the flight driver should respond to the state in its
own session rather than blindly replaying another session's control inputs.
Re-establish both variants on each new machine. Historical CPU times and speedup
ratios do not establish the new machine's capacity.

The current baseline retains BepInEx and the same NOTestPilot bridge used by the
candidate server. It measures the effect of adding the candidate mod to that
instrumented environment. A genuinely stock server without either loader or
bridge is a different comparison and is not implemented by the current runner.

Retail clients, Steam authentication, deliberate target engagement and long-session
stability remain unverified coverage. Mission rotation was exercised and failed.

The dedicated control-expiry.json scenario verifies that a one-second lease ends
without a release command and the server receives cleared throttle/brake inputs.

Movement was observed once (178 m / 44 m), but a repeat lost the second player's
aircraft during the observation and failed. A later diagnostic repeat recorded pilot damage/death, ejection and inventory
recovery while both clients stayed connected. The initiating damage source is
still uncertain. The fixture needs route/obstacle handling; it is not a stable
regression test yet.

CPU seconds and frame buckets are collected separately for the server and each
client. Reports subtract the counters at the observation's start from its end,
excluding startup/loading. CPU percentages use one logical core as 100%; they are
not a percentage of the whole host. Frame buckets are wall-clock Unity Update
intervals, not isolated physics/AI cost or latency measured by a retail player.
Observation windows also record the peak resident memory seen in their samples.
These are separate process measurements on a shared lab VM, not a production
capacity estimate. Native Escalation's three-minute window averaged 58.9 server
updates/s and 104.7% of one CPU core; the failed longer flight window averaged
54.7 updates/s and 115.7% of one core. No performance mods were loaded for either.


CLI runs handle Linux SIGTERM and Ctrl+C as cancellation. Interrupted observations
retain the completed samples and are marked incomplete. Reports retain earlier
passed steps and include a failed cancellation step, with overall passed:false
and progress state cancelled. The runner closes its own child processes. A real
Linux signal tooling test verifies preparation without launching a game. A
separate remote game check interrupted a 180-second observation after 10.616 seconds,
retained 11 samples and archived logs, and verified all its game processes stopped. A cancelled run
is never equivalent to completing the requested workload.
