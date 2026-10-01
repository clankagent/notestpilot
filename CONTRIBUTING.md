# Contributing

Start with the README's coverage table. A new command needs a check of the resulting
server state; compilation or a successful request reply is not runtime verification.

Run the runner tests before submitting a change. Changes touching game adapters need
manual verification against the approved real build in a disposable lab. Record the
scenario, build and result. Public CI runs tooling tests only. Never put game binaries,
decompiled source, credentials, private lab details or raw game logs in a contribution.

Open a pull request for changes. An issue is optional for discussing larger designs.
Keep claims precise: an idle connection check is not a multiplayer playtest, and a
short aircraft action check is not a sustained flight/combat or capacity benchmark.
