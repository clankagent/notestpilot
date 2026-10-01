# Development

## Tool checks, without the game

```sh
uv run --locked python -m unittest discover -s tests -v
```

The tests cover real loopback request/reply matching, rejected actions, missing
state, game errors, crashes, restarts, peer ID checks, motion checks, preservation
of existing folders and JUnit failures. CI runs only these tool tests; it does not
claim game coverage or launch a private lab for public pull requests.

## Clean Linux inputs

Use a disposable Ubuntu 26.04 lab, with enough space for a full game copy per
process. Three processes consume several GB of disk and RAM. Other distributions
need their own package/runtime checks.

```sh
bash scripts/bootstrap-linux.sh /path/to/new-input
```

The script downloads the server anonymously through SteamCMD, installs BepInEx
5.4.23.3, .NET SDK 8, uv and system dependencies. It refuses existing input folders
and starts no game. It needs sudo for package installation. Game updates may
produce a newer, unreviewed fingerprint that the bridge intentionally rejects.

The official [dedicated-server guide](https://github.com/Shockfront-Studios/Nuclear-Option-Server-Tools/blob/main/DedicatedServerGuide.md)
documents the server installation and configuration.

## Build and install the bridge

```sh
/path/to/new-input/tools/dotnet/dotnet build src/NOTestPilot.Bridge -c Release \
  -p:GamePath=/path/to/new-input/game \
  -p:BepInExPath=/path/to/new-input/game

mkdir -p /path/to/new-input/game/BepInEx/plugins/NOTestPilot
cp src/NOTestPilot.Bridge/bin/Release/net48/NOTestPilot.Bridge.dll \
   src/NOTestPilot.Bridge/bin/Release/net48/Newtonsoft.Json.dll \
   /path/to/new-input/game/BepInEx/plugins/NOTestPilot/
```

Game assemblies are build references and are never redistributed. The guard
recognizes reviewed Windows and Linux dedicated-server assemblies for 0.34.1 /
Steam build 24724541. Linux runtime passes are recorded; a retail client needs
separate inspection, build approval and runtime tests.

## Execute in the lab

```sh
/path/to/new-input/tools/uv/uv run notestpilot scenarios/two-player-motion.json \
  --game /path/to/new-input/game \
  --lab /path/to/disposable-lab \
  --output /path/to/private-results \
  --execute
```

No local game runs are permitted on this project's Windows agent workspace.

The runner copies clean inputs, excluding other plugins and existing game/BepInEx
configs. It preserves the Mono runtime configuration. Each copy has its own control
port and native Steam API ports, a hidden-server config and a per-run random token.
The game UDP port in these scenarios is 17777; run one lab at a time on the VM.
The game transport uses its normal bind behaviour; use an isolated VM/network.
The control interface itself is loopback only.

BepInEx `HideManagerGameObject = true` is required for lifecycle callbacks in this
build; the runner writes that setting. Game startup is controlled by the bridge,
so the native headless auto-host path cannot race the scenario.

The bridge is disabled unless `NOTESTPILOT_ENABLE=1`, with a 32+ character token,
role `server`/`client`, valid TCP port and an approved assembly fingerprint. The
runner supplies these only to its child processes and stops them in `finally`.

A lab may be reused when its marker matches the source and game/bridge fingerprints
match. Changed inputs require a new lab. Current scenarios verify one or two clients;
the host currently has a four-connection cap. Do not infer larger-player coverage
from the runner's configurable client count.

## Extending coverage

Prefer commands that request normal game actions, followed by server assertions.
Do not create players on the server, fabricate Steam identities, force ownership,
teleport aircraft or suppress failures to get a green test.

Add flight/combat, disconnect cleanup, death/respawn, long sessions and the native
dedicated-server-manager lifecycle before treating this as a complete server suite.
A mod-under-test input manifest and separate server/client measurements are still
needed for repeatable performance comparisons.
