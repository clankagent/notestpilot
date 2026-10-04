# What has actually passed

These are real dedicated-build game runs in disposable Linux labs, using built-in
Escalation, BepInEx 5 and the exact game assembly hash documented in the README.
There is one game process with server-side mock players. There are no separate
retail clients in these tests.

## Verified foundation

| Check | Observed result |
|---|---|
| Two mock players | Distinct native player IDs, separate associated aircraft, server simulation |
| Control ownership | Directed task state; ordinary NPC brain not initialized |
| Independent navigation | One aircraft physically turned east while the other continued north |
| Cancel isolation | Cancelling one task left the other ticking |
| Exact-target gunfire | Native bullets, hit on the designated opposing aircraft, positive applied damage attributed to the shooter |
| Ejection and replacement | Native ejection retired an airframe; replacement retained the player and received a new aircraft ID |
| Stale commands | Retired player, aircraft and runtime identities rejected before mutation |
| Cleanup | Mock player/aircraft registry emptied; disposable game quit natively with exit code zero |
| Public example | `examples/explicit_target.py` passed against the real runtime and cleaned its actors |
| Public lifecycle script | `examples/lifecycle.py` passed with 30 seconds of navigation, ejection/replacement and owned cleanup |

The fuller scenario passed repeatedly as checks were added: navigation first,
then explicit combat, then replacement and negative identity tests. The final
full scenario passed **49 checks**, including deliberately lost replies after
native create and spawn commands: the example failed, recovered only its own
actors, and cleaned them without repeating the mutations. A separate run executed the public combat
example itself, rather than relying on a private wrapper's equivalent logic.

The Python suite passes **65 tests**, covering real loopback transport, identity
checks, no automatic command retries, deadlines, incomplete evidence and example
cleanup. A reflection check validates **24 Harmony callback bindings** against
the supplied game assemblies. These checks supplement the game runs; they do
not substitute for them.

The sustained-task orchestration tests also reject aircraft that keep moving and
ticking while ignoring waypoint turns. Simulated telemetry in these tests does
not establish sustained native flight; that needs a separate real game run.

## Five-minute navigation check

Two native mock players completed **301.19 seconds** of scripted navigation in
one game process. Both responded physically to ten alternating waypoint turns;
the smallest observed turn response was **19.9°** for a requested 20° turn.
Every 30-second phase covered at least **2.86 km**. Native identities, server
simulation, controlling task state and absence of autonomous NPC brains were
checked throughout. Cancelling one actor left the other progressing, and both
actors were removed before a clean native game exit.

A second scenario with **four actors** and requested **15°** turns completed
**302.28 seconds**. All forty actor/phase observations passed; the minimum
heading response was **14.83°** and minimum phase displacement **2.91 km**.
Cancelling the first actor left every other actor progressing. Exact-owned cleanup
and a clean native game exit passed again.

These prove the measured two-actor and four-actor airborne navigation scenarios.
A separate sustained session checked the combat transitions below.

## Navigation, combat, cancellation and navigation again

The same native player and airframe completed **60.01 seconds** of navigation,
attacked an exact opposing aircraft with native bullets, a registered hit and
positive attributed applied damage, then stopped firing. A fresh event cursor
after cancellation began a **3.001-second** window with no new shooter bullets.
The aircraft then flew for **30.02 seconds**, covering **3.09 km** and turning
**19.9°** toward its new destination. The friendly pilot continued independently;
all owned actors were cleaned up and the game exited natively with code zero.

The same public script passed again with **300.04 seconds of initial navigation**,
covering about **33 km** per pilot, followed by native gun effects and
cancellation. The same player and aircraft then resumed navigation for
**30.06 seconds**, covering **3.13 km** with a **19.8°** turn.
The independent friendly kept progressing. The
post-cancellation event cursor remained unchanged throughout the observed
**3.115-second** window, and cleanup and native game exit passed again.

The target was a newly created airborne fixture approximately 386–391 m ahead of the
observed shooter. Existing aircraft were not repositioned. This checks task
transitions and native weapon effects, rather than a realistic combat encounter.
Hit registration can be observed after damage application because its observer
runs after the native method returns; sequence numbers describe observation order.

## Resource observations

Across the fuller short runs, the whole game process averaged about **0.65–0.68
busy CPU cores** and its sampled RSS was roughly **986–1082 MiB**. This includes
the Escalation world, native AI, physics, mock actors and test adapter. It is not
the cost of a mock actor alone. The 30-second public lifecycle run averaged
0.684 busy cores, with sampled RSS of 997–1063 MiB.

The five-minute two-actor run averaged **0.663 busy cores** over its measured
mission interval, with RSS of **986–1127 MiB**. These are whole-game observations,
including the stock Escalation world and test adapter.
The four-actor follow-up averaged **0.763 busy cores**, with RSS of **996–1152 MiB**.
The two runs used different turn schedules; their difference is not an isolated
measurement of the cost of adding two actors.

The five-minute navigation-to-combat session averaged **0.683 busy cores** over
345.17 seconds of observed mission time, with sampled RSS of **984–1136 MiB**.
These are whole-game measurements, including the mission and adapter; they do
not isolate the scripting overhead or compare against an unmodified server.

Measurements use Linux game-process CPU tick deltas and RSS during observed
mission-running intervals, roughly 17–51 seconds for the fuller short runs and
about five minutes for the navigation follow-up. Startup, later mission aging
and a prolonged human multiplayer workload are not represented. There is
no before/after comparison establishing resource savings or player capacity.

[Machine-readable summaries](runtime-checks.json) include check names, exact
runtime hashes, evidence archive hashes and measured intervals. Private raw game
logs and game files are excluded from this repository.

## What remains unproven

- Longer missions, deliberate collision avoidance, takeoff, landing and waypoint arrival.
- Other aircraft, missiles, turrets, ground targets and kill/score scenarios.
- Many simultaneous actors, mission reloads and complete emitted-object cleanup.
- Retail Steam clients, authentication, client ownership and network packet paths.
- Steady-state capacity or a performance improvement over the previous approach.

The fixture deliberately starts two aircraft airborne, nearby and aligned. It
checks native mechanics and precise script ownership; it is not a realistic
combat encounter or a claim of full multiplayer coverage. Known headless texture
upload errors are explicitly allowed by the lab fixture; unexpected errors,
truncated error journals and lost event evidence fail the run.
