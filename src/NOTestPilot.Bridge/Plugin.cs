using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using HarmonyLib;
using BepInEx;
using Cysharp.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using NuclearOption.Networking;
using NuclearOption.BuildScripts;
using UnityEngine;
using Player = NuclearOption.Networking.Player;
using SocketType = NuclearOption.Networking.SocketType;
using NuclearOption.SavedMission;
using NuclearOption.DedicatedServer;

namespace NOTestPilot;

// No listener, patches or gameplay changes unless explicitly launched as a test process.
[BepInPlugin("clankagent.notestpilot.bridge", "NOTestPilot disposable test bridge", "0.1.0")]
public sealed class Plugin : BaseUnityPlugin
{
    internal const string WindowsBuild = "01e2c543cb5de43a01a996d831ee85f8962d076a6f066d75152d20f0c2a3d162";
    internal const string LinuxBuild = "df5bed594dd84912efb3e57faa75b37d7e327bf4c8f5418f50411ad0ff46e24a";
    private readonly ConcurrentQueue<Pending> commands = new ConcurrentQueue<Pending>();
    private readonly ConcurrentQueue<object> errors = new ConcurrentQueue<object>();
    private TcpListener listener;
    private string token, role, build;
    private volatile bool stopping;
    private long errorCount;
    private Harmony harmony;
    private readonly string instance = Guid.NewGuid().ToString("N");

    private sealed class Pending
    {
        public JObject Request;
        public readonly ManualResetEventSlim Done = new ManualResetEventSlim(false);
        public JObject Response;
        public long Expires = Stopwatch.GetTimestamp() + 10 * Stopwatch.Frequency;
    }

    private void Awake()
    {
        if (Environment.GetEnvironmentVariable("NOTESTPILOT_ENABLE") != "1") return;
        token = Environment.GetEnvironmentVariable("NOTESTPILOT_TOKEN");
        role = Environment.GetEnvironmentVariable("NOTESTPILOT_ROLE");
        if (token == null || token.Length < 32 || (role != "server" && role != "client"))
        { Logger.LogError("Refusing test bridge: set token (32+ characters) and role server/client."); return; }
        using (var file = File.OpenRead(typeof(GameManager).Assembly.Location))
        using (var sha = SHA256.Create())
            build = BitConverter.ToString(sha.ComputeHash(file)).Replace("-", "").ToLowerInvariant();
        if (build != WindowsBuild && build != LinuxBuild)
        { Logger.LogError("Refusing unreviewed game assembly: " + build); return; }
        // A dedicated build otherwise automatically hosts and advertises during menu startup.
        // The disposable lab must let the scenario choose when/how to start networking.
        CommandLineArgParser.IsAutoStart = true;
        // Use the free dedicated build's server API initialization path. This is
        // not retail-client/Steam-login coverage and never fabricates a Steam ID.
        CommandLineArgParser.ForceSteamServerInit = true;
        NuclearOption.DedicatedServer.DedicatedServerManager.AutoRun = false;
        if (!int.TryParse(Environment.GetEnvironmentVariable("NOTESTPILOT_PORT"), out int port) || port < 1024 || port > 65535)
        { Logger.LogError("Refusing invalid test bridge port."); return; }
        listener = new TcpListener(IPAddress.Loopback, port);
        harmony = new Harmony("clankagent.notestpilot.headless-udp");
        harmony.Patch(AccessTools.Method(typeof(NuclearOption.Networking.Authentication.NetworkAuthenticatorNuclearOption), "OnClientConnected"),
            prefix: new HarmonyMethod(typeof(HeadlessUdpAdapter), nameof(HeadlessUdpAdapter.OnConnected)));
        harmony.Patch(AccessTools.Method(typeof(Player), nameof(Player.GetPlayerName)),
            prefix: new HarmonyMethod(typeof(HeadlessUdpAdapter), nameof(HeadlessUdpAdapter.DisplayName)));
        harmony.Patch(AccessTools.Method(typeof(NuclearOption.NetworkTransforms.SendTransformBatcher), "VisualUpdate"),
            prefix: new HarmonyMethod(typeof(HeadlessUdpAdapter), nameof(HeadlessUdpAdapter.BeforeVisualUpdate)));
        foreach (var method in new[] { "PlayerControls", "PlayerAxisControls" })
            harmony.Patch(AccessTools.Method(typeof(PilotPlayerState), method),
                prefix: new HarmonyMethod(typeof(ControlLease), nameof(ControlLease.BeforeControls)));
        harmony.Patch(AccessTools.Method(typeof(Aircraft), nameof(Aircraft.FilterInputs)),
            prefix: new HarmonyMethod(typeof(ControlLease), nameof(ControlLease.BeforeFilter)));
        Observation.Install(harmony);
        listener.Start(4);
        Application.logMessageReceived += RecordError;
        new Thread(Serve) { IsBackground = true, Name = "NOTestPilot loopback" }.Start();
        Logger.LogInfo("Test-only bridge listening on loopback, role=" + role);
    }

    private void RecordError(string message, string trace, LogType type)
    {
        if (type != LogType.Error && type != LogType.Exception && type != LogType.Assert) return;
        long seq = Interlocked.Increment(ref errorCount);
        errors.Enqueue(new { seq, type = type.ToString(), message = message.Length > 1500 ? message.Substring(0, 1500) : message });
        while (errors.Count > 32) errors.TryDequeue(out _);
    }

    private void Serve()
    {
        while (!stopping)
        {
            try
            {
                using (var connection = listener.AcceptTcpClient())
                {
                    connection.ReceiveTimeout = 3000;
                    connection.SendTimeout = 3000;
                    using (var stream = connection.GetStream())
                    {
                        var bytes = new MemoryStream();
                        int next;
                        while ((next = stream.ReadByte()) != -1 && next != '\n')
                        {
                            if (bytes.Length >= 16384) throw new InvalidDataException("Request too large");
                            bytes.WriteByte((byte)next);
                        }
                        if (next != '\n') throw new InvalidDataException("Incomplete request");
                        JObject request = JObject.Parse(Encoding.UTF8.GetString(bytes.ToArray()));
                        JObject response;
                        if ((string)request["token"] != token)
                            response = Failure(request, "Unauthorized");
                        else
                        {
                            var pending = new Pending { Request = request };
                            commands.Enqueue(pending);
                            response = pending.Done.Wait(120000) ? pending.Response : Failure(request, "Main thread timeout");
                        }
                        var output = Encoding.UTF8.GetBytes(response.ToString(Formatting.None) + "\n");
                        stream.Write(output, 0, output.Length);
                    }
                }
            }
            catch (Exception ex) { if (stopping) return; Logger.LogWarning("Local bridge request failed: " + ex.Message); }
        }
    }

    private static JObject Failure(JObject request, string error) => JObject.FromObject(new { id = (string)request["id"], ok = false, error });
    private static JObject Success(JObject request, object result) => JObject.FromObject(new { id = (string)request["id"], ok = true, result });

    private void Update()
    {
        if (listener == null) return;
        Observation.Frame();
        ControlLease.Tick();
        for (int n = 0; n < 4 && commands.TryDequeue(out var pending); n++)
        {
            if (Stopwatch.GetTimestamp() > pending.Expires)
            { pending.Response = Failure(pending.Request, "Expired before execution"); pending.Done.Set(); continue; }
            Execute(pending).Forget();
        }
    }

    private void FixedUpdate() { if (listener != null) Observation.FixedStep(); }

    private async UniTask Execute(Pending pending)
    {
        try { pending.Response = Success(pending.Request, await Dispatch(pending.Request)); }
        catch (Exception ex) { pending.Response = Failure(pending.Request, ex.GetBaseException().ToString()); }
        finally { pending.Done.Set(); }
    }

    private void RequireRole(string expected)
    { if (role != expected) throw new InvalidOperationException("Command requires " + expected + " role"); }
    private Player LocalPlayer()
    {
        RequireRole("client");
        if (!GameManager.GetLocalPlayer<Player>(out var player)) throw new InvalidOperationException("No local game player yet");
        return player;
    }

    private async UniTask<object> Dispatch(JObject request)
    {
        var args = request["args"] as JObject ?? new JObject();
        switch ((string)request["command"])
        {
            case "status": return Snapshot();
            case "catalog":
                return new { aircraft = Encyclopedia.i.aircraft.Select(a => new {
                    key = a.jsonKey, rank = a.aircraftParameters.rankRequired,
                    takeoffSpeed = a.aircraftParameters.takeoffSpeed,
                    takeoffDistance = a.aircraftParameters.takeoffDistance,
                    loadouts = (a.aircraftParameters.StandardLoadouts ?? new StandardLoadout[0])
                        .Where(l => !l.disabled).Select(l => new { name = l.Name, fuel = l.FuelRatio,
                            weapons = l.loadout.weapons.Select(w => w?.jsonKey).ToArray() }).ToArray(),
                    hardpoints = (a.unitPrefab?.GetComponent<Aircraft>()?.weaponManager?.hardpointSets ?? new HardpointSet[0]).Select((h, index) => new {
                        index, h.name, options = h.weaponOptions.Where(w => w != null).Select(w => new {
                            key = w.jsonKey, name = w.mountName, ammo = w.ammo, gun = w.info != null && w.info.gun,
                            nuclear = w.info != null && w.info.nuclear
                        }).ToArray()
                    }).ToArray()
                }).ToArray() };
            case "host":
            {
                RequireRole("server");
                if (MainMenu.State != MainMenu.LoadingState.Loaded) throw new InvalidOperationException("Menu not loaded yet");
                string missionName = (string)args["mission"] ?? "Terminal Control";
                var mission = CommandLineArgParser.LoadMission(missionName, false);
                MissionManager.SetMission(mission, false);
                int port = (int?)args["port"] ?? 17777;
                if (port < 1024 || port > 65535) throw new ArgumentException("Invalid UDP port");
                await NetworkManagerNuclearOption.i.StartHostAsync(new HostOptions(SocketType.UDP, GameState.Multiplayer, mission.MapKey)
                    { UdpPort = port, MaxConnections = 4, Password = (string)args["password"] });
                return Snapshot();
            }
            case "dedicated":
            {
                RequireRole("server");
                if (MainMenu.State != MainMenu.LoadingState.Loaded || NetworkManagerNuclearOption.i.Server.Active)
                    throw new InvalidOperationException("Dedicated manager requires an idle loaded menu");
                var missionNames = args["missions"] as JArray;
                if (missionNames == null || missionNames.Count == 0 || missionNames.Count > 8)
                    throw new ArgumentException("Specify one to eight built-in missions");
                var missions = missionNames.Select(n => (string)n).ToArray();
                // Resolve before starting networking; arbitrary Workshop IDs and
                // filesystem paths are outside this initial native-manager test.
                foreach (string name in missions)
                {
                    if (name != "Escalation" && name != "Terminal Control")
                        throw new ArgumentException("Native probe currently supports Escalation and Terminal Control");
                    CommandLineArgParser.LoadMission(name, false);
                }
                int port = (int?)args["port"] ?? 17777;
                if (port < 1024 || port > 65535) throw new ArgumentException("Invalid UDP port");
                var config = DedicatedServerConfig.CreateDefault();
                config.Hidden = true;
                config.ServerName = "NOTestPilot disposable native server";
                config.MaxPlayers = 4;
                config.Port = new Override<ushort> { IsOverride = true, Value = (ushort)port };
                config.QueryPort = DedicatedServerManager.GetConfig().config.QueryPort;
                config.Password = (string)args["password"];
                config.MissionDirectory = null;
                config.BanListPaths = config.ErrorKickImmuneListPaths = new string[0];
                config.MissionRotation = missions.Select(name => new MissionOptions {
                    Key = new MissionKeySaveable { Group = "BuiltIn", Name = name }, MaxTime = 7200
                }).ToArray();
                // The actual native manager owns startup, player-triggered map
                // loading and rotation. Hidden stays true; transport is UDP.
                CommandLineArgParser.SocketType = SocketType.UDP;
                string path = Path.Combine(Environment.CurrentDirectory, "DedicatedServerConfig.json");
                DedicatedServerConfig.Save(path, config, true);
                DedicatedServerManager.Instance.Run(config, path);
                return new { accepted = true };
            }
            case "rotate":
            {
                RequireRole("server");
                if (!DedicatedServerManager.IsRunning || !MissionManager.IsRunning)
                    throw new InvalidOperationException("No native dedicated mission running");
                // Normal admin operation: expire this mission's time limit. It
                // tests rotation without faking a gameplay victory or winner.
                DedicatedServerManager.Instance.SetTimeRemaining(0);
                return new { accepted = true };
            }
            case "connect":
            {
                RequireRole("client");
                if (MainMenu.State != MainMenu.LoadingState.Loaded) throw new InvalidOperationException("Menu not loaded yet");
                int port = (int?)args["port"] ?? 17777;
                if (port < 1024 || port > 65535) throw new ArgumentException("Invalid UDP port");
                // Initial lab mode is local only. Steam authentication is deliberately untouched.
                NetworkManagerNuclearOption.i.StartClient(new ConnectOptions(SocketType.UDP, "127.0.0.1", port)
                    { Password = (string)args["password"] });
                return new { accepted = true };
            }
            case "disconnect":
            {
                RequireRole("client");
                // Follow the normal quit button's resume/disconnect-reason path.
                // Await completion so the runner can inspect unloading errors.
                if (SceneSingleton<GameplayUI>.i != null)
                    SceneSingleton<GameplayUI>.i.ResumeGame();
                await NetworkManagerNuclearOption.i.StopAsync(true);
                return Snapshot();
            }
            case "faction":
            {
                var player = LocalPlayer();
                var hq = FactionRegistry.HqFromName((string)args["name"]);
                if (hq == null) throw new ArgumentException("Unknown faction");
                player.SetFaction(hq);
                return new { accepted = true }; // Runner must confirm the change from the server.
            }
            case "purchase":
            {
                var player = LocalPlayer();
                var definition = FindAircraft((string)args["aircraft"]);
                player.CmdPurchaseAirframe(definition);
                return new { accepted = true };
            }
            case "reserve":
            {
                var player = LocalPlayer();
                if (player.HQ == null) throw new InvalidOperationException("Choose a faction first");
                var definition = args["aircraft"] != null ? FindAircraft((string)args["aircraft"]) : Encyclopedia.i.aircraft
                    .Where(a => a.aircraftParameters.rankRequired <= player.PlayerRank && !player.HQ.restrictedAircraft.Contains(a.jsonKey))
                    .OrderBy(a => a.aircraftParameters.rankRequired).ThenBy(a => a.jsonKey, StringComparer.Ordinal).FirstOrDefault();
                if (definition == null) throw new InvalidOperationException("No rank-appropriate aircraft found");
                player.CmdRequestReserveAirframe(definition);
                return new { accepted = true, aircraft = definition.jsonKey };
            }
            case "spawn":
            {
                var player = LocalPlayer();
                var previousAircraft = player.Aircraft;
                if (previousAircraft != null && !previousAircraft.disabled && previousAircraft.pilots.Any(p => !p.dead && !p.ejected))
                    throw new InvalidOperationException("Eject or finish the current sortie before spawning another aircraft");
                var definition = args["aircraft"] != null ? FindAircraft((string)args["aircraft"]) : player.OwnedAirframes
                    .Select(a => a.Definition).OrderBy(a => a.jsonKey, StringComparer.Ordinal).FirstOrDefault();
                if (definition == null) throw new InvalidOperationException("Player owns no airframe");
                var airbase = args["airbase"] != null ? FactionRegistry.airbaseLookup.TryGetValue((string)args["airbase"], out var selected) ? selected : null :
                    FactionRegistry.airbaseLookup.OrderBy(a => a.Key, StringComparer.Ordinal)
                    .Select(a => a.Value).FirstOrDefault(a => a.CurrentHQ == player.HQ && a.CanSpawnAircraft(definition));
                if (airbase == null)
                    throw new ArgumentException("Unknown airbase");
                if (player.HQ != airbase.CurrentHQ) throw new InvalidOperationException("Airbase is not in player's faction");
                var loadout = new Loadout();
                string loadoutName = (string)args["loadout"];
                if (loadoutName != null)
                {
                    var selectedLoadout = definition.aircraftParameters.StandardLoadouts?
                        .FirstOrDefault(l => !l.disabled && l.Name == loadoutName);
                    if (selectedLoadout == null) throw new ArgumentException("Unknown standard loadout");
                    loadout = selectedLoadout.loadout;
                }
                if (args["weapons"] is JArray mounts)
                {
                    if (loadoutName != null) throw new ArgumentException("Choose a standard loadout or explicit mounts");
                    var hardpoints = definition.unitPrefab.GetComponent<Aircraft>().weaponManager.hardpointSets;
                    if (mounts.Count != hardpoints.Length) throw new ArgumentException("Specify one mount or null for each hardpoint set");
                    for (int index = 0; index < mounts.Count; index++)
                    {
                        string key = (string)mounts[index];
                        WeaponMount mount = null;
                        if (key != null && (!Encyclopedia.WeaponLookup.TryGetValue(key, out mount) || !hardpoints[index].weaponOptions.Contains(mount)))
                            throw new ArgumentException("Unknown/disallowed weapon mount at index " + index);
                        loadout.weapons.Add(mount);
                    }
                }
                bool accepted = await NetworkSceneSingleton<Spawner>.i.RequestSpawnAtAirbase(
                    airbase, definition, default(LiveryKey), loadout, (float?)args["fuel"] ?? 1f);
                return new { accepted, aircraft = definition.jsonKey,
                    airbase = FactionRegistry.airbaseLookup.First(a => ReferenceEquals(a.Value, airbase)).Key,
                    loadout = loadoutName, requestedWeaponMounts = loadout.weapons.Count };
            }
            case "controls":
                return ControlLease.Set(LocalPlayer(), args);
            case "fly":
                return ControlLease.Fly(LocalPlayer(), args);
            case "release-controls":
                LocalPlayer();
                ControlLease.Release();
                return new { accepted = true };
            case "engine":
            {
                var aircraft = LocalPlayer().Aircraft;
                if (aircraft == null) throw new InvalidOperationException("Player has no aircraft");
                if (aircraft.Ignition != ((bool?)args["on"] ?? true)) aircraft.CmdToggleIgnition();
                return new { accepted = true };
            }
            case "eject":
            {
                var aircraft = LocalPlayer().Aircraft;
                if (aircraft == null) throw new InvalidOperationException("Player has no aircraft");
                ControlLease.Release();
                aircraft.StartEjectionSequence();
                return new { accepted = true };
            }
            case "gear":
            {
                var aircraft = LocalPlayer().Aircraft;
                if (aircraft == null) throw new InvalidOperationException("Player has no aircraft");
                aircraft.SetGear((bool?)args["down"] ?? true);
                return new { accepted = true };
            }
            case "next-weapon":
            {
                var aircraft = LocalPlayer().Aircraft;
                if (aircraft == null) throw new InvalidOperationException("Player has no aircraft");
                aircraft.pilots[0].NextWeapon();
                return new { accepted = true };
            }
            default: throw new ArgumentException("Unknown command");
        }
    }

    private static AircraftDefinition FindAircraft(string key)
    {
        if (!Encyclopedia.Lookup.TryGetValue(key, out var definition) || !(definition is AircraftDefinition aircraft))
            throw new ArgumentException("Unknown aircraft key");
        return aircraft;
    }

    private object Snapshot()
    {
        bool menuReady = MainMenu.State == MainMenu.LoadingState.Loaded;
        // The singleton getter creates/logs an error if queried before menu preload.
        var network = menuReady ? NetworkManagerNuclearOption.i : null;
        var native = network != null ? network.DedicatedServerManager : null;
        return new
        {
            protocol = 1, instance, role, assemblySha256 = build, gameVersion = Application.version,
            clientMode = "dedicated-build UDP adapter", steamAuthenticationTested = false,
            headlessWaitingCamerasCreated = HeadlessUdpAdapter.WaitingCamerasCreated,
            menuReady,
            serverActive = network != null && network.Server.Active,
            clientActive = network != null && network.Client.Active,
            mission = MissionManager.CurrentMission?.Name, missionRunning = MissionManager.IsRunning,
            gameState = GameManager.gameState.ToString(),
            dedicated = new { running = DedicatedServerManager.IsRunning,
                hidden = native?.Config?.Hidden,
                currentMission = DedicatedServerManager.IsRunning && native != null ? native.CurrentMissionOption.Key.Name : null },
            units = UnitRegistry.allUnits.Count, aircraft = UnitRegistry.allAircraft.Count,
            remotePlayers = UnitRegistry.playerLookup.Values.Count(p => p != null && !p.IsHostPlayer),
            playerNetIds = UnitRegistry.playerLookup.Values.Where(p => p != null).Select(p => p.NetId).ToArray(),
            loadedPlugins = BepInEx.Bootstrap.Chainloader.PluginInfos.Values.OrderBy(p => p.Metadata.GUID, StringComparer.Ordinal)
                .Select(p => new { guid = p.Metadata.GUID, name = p.Metadata.Name, version = p.Metadata.Version.ToString() }).ToArray(),
            localPlayerNetId = GameManager.GetLocalPlayer<Player>(out var localPlayer) ? (uint?)localPlayer.NetId : null,
            localPlayerOwnedCount = localPlayer != null ? localPlayer.OwnedAirframes.Count : 0,
            localPlayerAircraftNetId = localPlayer != null && localPlayer.Aircraft != null ? (uint?)localPlayer.Aircraft.NetId : null,
            controlLease = ControlLease.Snapshot(),
            observation = Observation.Snapshot(),
            errorCount = Interlocked.Read(ref errorCount), errors = errors.ToArray(),
            players = UnitRegistry.playerLookup.Values.Where(p => p != null).OrderBy(p => p.PlayerIndex).Select(p => new
            {
                netId = p.NetId, index = p.PlayerIndex, host = p.IsHostPlayer, local = p.IsLocalPlayer,
                faction = p.HQ != null ? p.HQ.faction.factionName : null,
                rank = p.PlayerRank, allocation = p.Allocation,
                ownedAircraft = p.OwnedAirframes.Select(a => a.Definition.jsonKey).ToArray(),
                ownedCount = p.OwnedAirframes.Count,
                aircraftNetId = p.Aircraft != null ? (uint?)p.Aircraft.NetId : null,
                aircraftKey = p.Aircraft != null ? p.Aircraft.definition.jsonKey : null,
                ignition = p.Aircraft != null ? (bool?)p.Aircraft.Ignition : null,
                gearDown = p.Aircraft != null ? (bool?)p.Aircraft.gearDeployed : null,
                weapons = p.Aircraft != null ? p.Aircraft.weaponStations.Select(s => new {
                    index = s.Number, name = s.WeaponInfo?.weaponName, ammo = s.Ammo,
                    fullAmmo = s.FullAmmo, selected = ReferenceEquals(s, p.Aircraft.weaponManager.currentWeaponStation)
                }).ToArray() : null,
                position = p.Aircraft != null ? new[] { p.Aircraft.GlobalPosition().x, p.Aircraft.GlobalPosition().y, p.Aircraft.GlobalPosition().z } : null,
                speed = p.Aircraft != null ? (float?)p.Aircraft.speed : null,
                radarAltitude = p.Aircraft != null ? (float?)p.Aircraft.radarAlt : null,
                attitude = p.Aircraft != null ? new[] { p.Aircraft.transform.eulerAngles.x, p.Aircraft.transform.eulerAngles.y, p.Aircraft.transform.eulerAngles.z } : null,
                forward = p.Aircraft != null ? new[] { p.Aircraft.transform.forward.x, p.Aircraft.transform.forward.y, p.Aircraft.transform.forward.z } : null,
                disabled = p.Aircraft != null ? (bool?)p.Aircraft.disabled : null,
                unitState = p.Aircraft != null ? p.Aircraft.unitState.ToString() : null,
                pilotHealth = p.Aircraft != null ? p.Aircraft.pilots.Select(pilot => new { pilot.dead, pilot.ejected }).ToArray() : null,
                spawningAirbase = p.Aircraft != null ? p.Aircraft.NetworkspawningHangar?.parentAirbase?.name : null,
                pilotStates = p.Aircraft != null ? p.Aircraft.pilots.Select(pilot => pilot.GetCurrentState()).ToArray() : null,
                inputs = p.Aircraft != null ? p.Aircraft.GetInputs() : null
            }).ToArray(),
            factions = FactionRegistry.GetAllHQs().Select(h => new { name = h.faction.factionName, preventJoin = h.preventJoin }).ToArray(),
            airbases = FactionRegistry.airbaseLookup.Select(a => new { name = a.Key, faction = a.Value.CurrentHQ?.faction.factionName }).ToArray()
        };
    }

    private void OnDestroy()
    {
        stopping = true;
        ControlLease.Release();
        Application.logMessageReceived -= RecordError;
        listener?.Stop();
        harmony?.UnpatchSelf();
    }
}
