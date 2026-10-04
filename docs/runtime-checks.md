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

The Python suite passes **47 tests**, covering real loopback transport, identity
checks, no automatic command retries, deadlines, incomplete evidence and example
cleanup. A reflection check validates **24 Harmony callback bindings** against
the supplied game assemblies. These checks supplement the game runs; they do
not substitute for them.

## Resource observations

Across the fuller short runs, the whole game process averaged about **0.65–0.68
busy CPU cores** and its sampled RSS was roughly **986–1082 MiB**. This includes
the Escalation world, native AI, physics, mock actors and test adapter. It is not
the cost of a mock actor alone. The 30-second public lifecycle run averaged
0.684 busy cores, with sampled RSS of 997–1063 MiB.

Measurements use Linux game-process CPU tick deltas and RSS during observed
mission-running intervals, roughly 17–51 seconds for the fuller runs. Startup, later mission
aging and a prolonged human multiplayer workload are not represented. There is
no before/after comparison establishing resource savings or player capacity.

[Machine-readable summaries](runtime-checks.json) include check names, exact
runtime hashes, evidence archive hashes and measured intervals. Private raw game
logs and game files are excluded from this repository.

## What remains unproven

- Sustained missions, collision avoidance, takeoff, landing and waypoint arrival.
- Other aircraft, missiles, turrets, ground targets and kill/score scenarios.
- Many simultaneous actors, mission reloads and complete emitted-object cleanup.
- Retail Steam clients, authentication, client ownership and network packet paths.
- Steady-state capacity or a performance improvement over the previous approach.

The fixture deliberately starts two aircraft airborne, nearby and aligned. It
checks native mechanics and precise script ownership; it is not a realistic
combat encounter or a claim of full multiplayer coverage. Known headless texture
upload errors are explicitly allowed by the lab fixture; unexpected errors,
truncated error journals and lost event evidence fail the run.
