# Stock-server coverage

Four automated pilots flew and fired guns in the normal Linux server for five minutes. All 41 checks passed: each joined, spawned its own aircraft, took off, stayed airborne and used ammunition. The server had no BepInEx or test bridge installed. The clients used the dedicated-server build with a headless UDP adapter; retail Steam clients still need separate testing. The tested game was Nuclear Option 0.34.1 / Steam build 24724541.

During flight, the server averaged about three quarters of one CPU core and reached about 1.07 GiB of resident memory. Each test client used about 0.68 cores and 1.12 GiB, measured separately. All five processes shared one VM. These figures describe one short workload; they do not establish maximum player capacity or a performance-mod benefit.

The pilots performed the same client actions as the earlier instrumented test: three minutes of takeoff work followed by five minutes of flight and gun bursts. This path checks the clients' view of their aircraft and the server's external process counters. It cannot provide authoritative server state or server frame timing, so the complete tests are not equivalent. A repeated stock-versus-mod comparison remains to be done.

The experimental [stock-server foundation](../src/notestpilot/stock_server.py) prepares and launches a reviewed game build, checks process identity and samples CPU and memory. Its [unit tests](../tests/test_stock_server.py) protect those setup guards; the flight result above comes from real game processes. This is currently a developer API for a separate harness. The normal `notestpilot` CLI still uses a server bridge; see [development setup](development.md).
