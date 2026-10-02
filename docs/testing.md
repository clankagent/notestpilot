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
| `native-aging-sorties.json` | Five planned takeoff/flight/gun/rocket sorties with ordinary aircraft replacement | The strict-survival fixture remains unchanged; earlier runs failed during the second and third sorties. Not a completed aging or combat soak |
| `native-combat-recovery.json` | Five sorties with a two-minute taxi/takeoff phase followed by a separate one-minute continuously checked airborne gate; verified missile loss branches to ordinary ejection and replacement | One native 142-check run completed four full sorties and the fifth taxi, then verified missile loss at 58.03/60 seconds airborne. Normal ejection and replacement passed; fifth-sortie gun-flight and rocket steps were omitted. One recovery pass, not five completed sorties or a long-session pass |
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

The strict-survival five-sortie fixture keeps its original behavior. Earlier runs
failed during the second and third sorties; one later trace found missile
fragment damage before a pilot slowed, while the cause of an earlier breakup
remains separate. A separate recovery scenario changes the takeoff window to
120 seconds of taxi plus a continuously checked 60-second airborne gate. Its
first native run completed four full sorties and the fifth taxi, then reached
the evidence-gated branch after 58.03 of 60 airborne seconds. Both clients
ejected and completed ordinary replacement checks, but the fifth gun-flight
and rocket steps were omitted. This verifies one recovery path, not five full
sorties or an uninterrupted 32.5-minute soak.

The bridge now retains the first incoming damage, applied damage and joint-break
events separately from its rolling event queue. That helps distinguish a weapon
hit from later collisions between broken aircraft parts. It records original
game actions; it does not prevent damage or make an uninterrupted survival check
pass. The separate recovery scenario now has one native run in which the
verified missile-loss branch led through ordinary ejection and replacement;
repeat recovery and longer combat coverage remain unverified.

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


The latest four-pilot sequence completed one matched baseline/Spatial pair: each
passed all 50 checks through 3 minutes of takeoff and 10 minutes of flight with
gun bursts. Game, script and test-bridge hashes matched, and the server's Spatial
patch activation was verified. Average flight CPU was 0.757 baseline versus
0.761 Spatial cores. That small difference is within the variation of passing
baselines (0.757–0.771); no whole-server gain is established.

The following baseline failed while its fourth client loaded Escalation, with
the same scene-loading exception previously recorded in a candidate run.
The error therefore also occurs without the performance plugin. Its cause and
applicability to retail clients remain unresolved. Failed runs are retained,
and one successful pair does not waive them or establish long-session stability.


Read-only loading diagnostics caught a destroyed aircraft entry in a failing
client's mission lookup immediately before the original loader accessed a
destroyed network identity. The bridge records that state; it does not remove
objects or replace the loader. The original exception still fails the run.
The exact lifetime sequence and retail-client applicability remain unverified.
Snapshots keep at most eight scene events and sixteen missing lookup entries.

Both clients also completed another two-sortie lifecycle run with these
diagnostics: flight, rockets, airborne ejection, replacement and flight again
(51 checks). Physics mode context was observed. At that point the physical joint-break callback had not fired. A later failed
profiling flight exercised that callback and first-airborne-contact retention: joint
breaks at about 935 m preceded own-part contacts. Saved part hitpoints were already
negative, so the initiating damage remains unresolved. Callback coverage is now
observed; the failure remains a failure.


The two-sortie lifecycle also passed a consecutive repeat (51 checks).
`native-late-join.json` passed a narrower 10-check case: one client starts
Escalation, the mission advances for 60 seconds, then a second client joins.
It checks both identities against the server and requests no aircraft.
That successful delayed join does not resolve the intermittent loading error.
