# Stock-server coverage

Four automated pilots flew and fired guns in the normal Linux server for five minutes. All 41 checks passed: each joined, spawned its own aircraft, took off, stayed airborne and used ammunition. The server had no BepInEx or test bridge installed. The clients used the dedicated-server build with a headless UDP adapter; retail Steam clients still need separate testing. The tested game was Nuclear Option 0.34.1 / Steam build 24724541.

In a later passing five-minute flight, the server averaged 0.74 CPU cores and reached 1.05 GiB of resident memory. Each test client averaged 0.64–0.65 cores and reached about 1.1 GiB, measured separately. All five processes shared one four-vCPU, eight-GiB Ryzen VM. These figures describe one short workload; they do not establish maximum player capacity or a performance-mod benefit.

A separate four-player replacement test passed all 62 checks:

| Stage | What was verified |
|---|---|
| First sortie | Three minutes of takeoff work, then one minute of continuously checked healthy flight with gun bursts |
| Ejection | Each player ejected once; old controls cleared and the game's normal spawn prerequisite became true |
| Replacement | Each received a different aircraft; all four player identities stayed the same |
| Second sortie | Engines started, all four took off again and completed another minute of healthy flight |

This tests planned airborne replacement. It does not establish recovery after combat damage. The aircraft associations did not need to become null: the old pilots had ejected, which is another normal spawn prerequisite. The harness did not repair aircraft or ignore unexpected errors. The replacement harness is currently separate from the public CLI.

A longer replacement test also passed all 62 checks. It kept each healthy flight phase running for two and a half minutes, before and after ejection, with the same four players and normal gun bursts. This extends the planned-replacement evidence; it does not extend combat-damage or retail-client coverage.

Profiling that longer run exposed a measurement limit. The server-and-clients group reached 1.9 GiB of swap use over the run. The separate profiling group reached 3.4 GiB of memory use, including work after the game processes stopped. Client frames slowed and combined resident memory fell around the pre-record verification interval. The peaks have no timestamps, and individual verification stages were not timed, so the evidence does not establish what caused the swapping. These profiled measurements cannot establish capacity or a mod benefit. Further comparisons need pressure telemetry and a separate run without the profiler.

The successful profile contained 4,600 CPU samples. Identifiable physics functions accounted for about 20% of sampled work; particles, audio and other engine functions also appeared. About 45% of sample weight had empty caller stacks, and some managed game code remained unidentified. This is an incomplete view of where CPU time goes, not proof that those costs can be removed safely. In that same interval the main thread accounted for about 65% of observed process CPU time, with two job workers accounting for about 17% and 13%. The server uses multiple threads, but the shared, swapping lab does not establish how it scales with more cores.

Failures remain part of the evidence. One repeat failed during the fourth client's loading. Connecting all four ready clients before waiting for loading then passed, but that timing workaround does not fix late joining. Another flight failed after a destructive blast broke an aircraft's joints and it descended. The attacker and weapon are unknown; the failed survival test was not converted into a recovery pass. Longer sessions, reconnects and rotation remain unfinished.

The pilots performed the same client actions as the earlier instrumented test: three minutes of takeoff work followed by five minutes of flight and gun bursts. This path checks the clients' view of their aircraft and the server's external process counters. It cannot provide authoritative server state or server frame timing, so the complete tests are not equivalent. A repeated stock-versus-mod comparison remains to be done.

The experimental [stock-server foundation](../src/notestpilot/stock_server.py) prepares and launches a reviewed game build, checks process identity and samples CPU and memory. Its [unit tests](../tests/test_stock_server.py) protect those setup guards; the flight result above comes from real game processes. This is currently a developer API for a separate harness. The normal `notestpilot` CLI still uses a server bridge; see [development setup](development.md).
