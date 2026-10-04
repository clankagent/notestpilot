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
using BepInEx;
using HarmonyLib;
using Cysharp.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using NuclearOption.BuildScripts;
using NuclearOption.Networking;
using UnityEngine;
using Player = NuclearOption.Networking.Player;
using SocketType = NuclearOption.Networking.SocketType;

namespace NOTestPilot.Runtime;

[BepInPlugin("clankagent.notestpilot.runtime", "NOTestPilot programmable mock players", "0.2.0")]
public sealed class Plugin : BaseUnityPlugin
{
    public const string ReviewedBuild = "df5bed594dd84912efb3e57faa75b37d7e327bf4c8f5418f50411ad0ff46e24a";
    private readonly ConcurrentQueue<Request> pending = new ConcurrentQueue<Request>();
    private readonly ConcurrentQueue<object> errors = new ConcurrentQueue<object>();
    private readonly string instance = Guid.NewGuid().ToString("N");
    private TcpListener listener;
    private Harmony harmony;
    private string token;
    private volatile bool stopping;
    private long errorCount;
    private sealed class Request
    {
        public JObject Data;
        public JObject Reply;
        public readonly ManualResetEventSlim Done = new ManualResetEventSlim();
        public readonly long Expires = Stopwatch.GetTimestamp() + 20 * Stopwatch.Frequency;
    }

    private void Awake()
    {
        if (Environment.GetEnvironmentVariable("NOTESTPILOT_ENABLE") != "1") return;
        token = Environment.GetEnvironmentVariable("NOTESTPILOT_TOKEN");
        if (token == null || token.Length < 32) throw new InvalidOperationException("A private lab token is required");
        using (var stream = File.OpenRead(typeof(GameManager).Assembly.Location))
        using (var sha = SHA256.Create())
            if (BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant() != ReviewedBuild)
                throw new InvalidOperationException("Unreviewed game build");
        if (!int.TryParse(Environment.GetEnvironmentVariable("NOTESTPILOT_PORT"), out int port) || port < 1024 || port > 65535)
            throw new ArgumentException("A valid private control port is required");
        CommandLineArgParser.IsAutoStart = true;
        CommandLineArgParser.ForceSteamServerInit = true;
        NuclearOption.DedicatedServer.DedicatedServerManager.AutoRun = false;
        harmony = new Harmony("clankagent.notestpilot.programmable");
        try {
            LocalHostAdapter.Initialize(harmony);
            MockPlayers.Initialize(harmony);
            MockLifecycle.Initialize(harmony);
            Events.Install(harmony);
        }
        catch { harmony.UnpatchSelf(); throw; }
        listener = new TcpListener(IPAddress.Loopback, port);
        listener.Start(8);
        Application.logMessageReceived += OnLog;
        new Thread(Serve) { IsBackground = true, Name = "NOTestPilot control" }.Start();
        Logger.LogInfo("Programmable mock-player control ready; build guard passed");
    }

    private void OnLog(string message, string trace, LogType kind)
    {
        if (kind != LogType.Error && kind != LogType.Exception && kind != LogType.Assert) return;
        long seq = Interlocked.Increment(ref errorCount);
        errors.Enqueue(new { seq, kind = kind.ToString(), message = message.Substring(0, Math.Min(2000, message.Length)), trace = trace.Substring(0, Math.Min(3000, trace.Length)) });
        while (errors.Count > 64) errors.TryDequeue(out _);
    }

    private void Serve()
    {
        while (!stopping)
        {
            try
            {
                using (var connection = listener.AcceptTcpClient())
                {
                    connection.ReceiveTimeout = connection.SendTimeout = 25000;
                    using (var stream = connection.GetStream())
                    {
                        var bytes = new MemoryStream();
                        int b;
                        while ((b = stream.ReadByte()) != '\n' && b != -1)
                        {
                            if (bytes.Length >= 65536) throw new ArgumentException("Request exceeds 64 KiB");
                            bytes.WriteByte((byte)b);
                        }
                        var data = JObject.Parse(Encoding.UTF8.GetString(bytes.ToArray()));
                        JObject reply;
                        if ((string)data["token"] != token)
                            reply = new JObject { ["id"] = data["id"], ["ok"] = false, ["error"] = "Invalid token" };
                        else
                        {
                            var request = new Request { Data = data };
                            pending.Enqueue(request);
                            reply = request.Done.Wait(25000) ? request.Reply : new JObject { ["id"] = data["id"], ["ok"] = false, ["error"] = "Command timeout; query state before retry" };
                        }
                        byte[] output = Encoding.UTF8.GetBytes(reply.ToString(Formatting.None) + "\n");
                        stream.Write(output, 0, output.Length);
                    }
                }
            }
            catch (Exception ex) { if (!stopping) Logger.LogWarning("Control transport: " + ex.GetType().Name); }
        }
    }

    private void Update()
    {
        for (int i = 0; i < 4 && pending.TryDequeue(out var request); i++)
            Execute(request).Forget();
    }

    private async UniTask Execute(Request request)
    {
        try
        {
            if (Stopwatch.GetTimestamp() > request.Expires) throw new TimeoutException("Expired before execution");
            var result = await Dispatch((string)request.Data["command"], request.Data["args"] as JObject ?? new JObject());
            request.Reply = new JObject { ["id"] = request.Data["id"], ["ok"] = true, ["result"] = result == null ? JValue.CreateNull() : JToken.FromObject(result) };
        }
        catch (Exception error) { request.Reply = new JObject { ["id"] = request.Data["id"], ["ok"] = false, ["error"] = error.GetType().Name + ": " + error.Message }; }
        finally { request.Done.Set(); }
    }

    private Player Actor(JObject args)
    {
        string actor = (string)args["actor"] ?? throw new ArgumentException("actor is required");
        var player = MockPlayers.Players.SingleOrDefault(p => MockPlayers.Name(p) == actor) ?? throw new ArgumentException("Unknown actor");
        if ((uint?)args["playerId"] != player.NetId) throw new InvalidOperationException("Stale or missing player identity");
        if ((string)args["instance"] != instance) throw new InvalidOperationException("Stale or missing runtime instance");
        return player;
    }
    private Aircraft AircraftFor(JObject args)
    {
        var aircraft = Actor(args).Aircraft;
        if (aircraft == null || aircraft.disabled) throw new InvalidOperationException("Actor has no live aircraft");
        uint? expected = (uint?)args["aircraftId"];
        if (!expected.HasValue || aircraft.persistentID.Id != expected.Value) throw new InvalidOperationException("Stale or missing aircraft generation");
        return aircraft;
    }
    private static Vector3 Vector(JObject args, string key)
    {
        var a = args[key] as JArray;
        if (a == null || a.Count != 3) throw new ArgumentException(key + " requires three numbers");
        var v = new Vector3((float)a[0], (float)a[1], (float)a[2]);
        if (float.IsNaN(v.x) || float.IsInfinity(v.x) || float.IsNaN(v.y) || float.IsInfinity(v.y) || float.IsNaN(v.z) || float.IsInfinity(v.z)) throw new ArgumentException("Nonfinite vector");
        return v;
    }
    private static float[] Coordinates(Vector3 v) => new[] { v.x, v.y, v.z };

    private async UniTask<object> Dispatch(string command, JObject args)
    {
        switch (command)
        {
            case "status": return Snapshot();
            case "events":
                if ((string)args["instance"] != instance) throw new InvalidOperationException("Stale or missing runtime instance");
                return Events.Read((long?)args["after"] ?? 0);
            case "units":
                if ((string)args["instance"] != instance) throw new InvalidOperationException("Stale or missing runtime instance");
                int limit = (int?)args["limit"] ?? 100;
                if (limit < 1 || limit > 256) throw new ArgumentOutOfRangeException("limit", "Choose 1-256 units");
                string factionFilter = (string)args["faction"], nameFilter = (string)args["nameContains"];
                bool includeDisabled = (bool?)args["includeDisabled"] ?? false;
                return UnitRegistry.allUnits.Where(u => u != null && (includeDisabled || !u.disabled)
                    && (factionFilter == null || u.NetworkHQ?.faction.factionName == factionFilter)
                    && (nameFilter == null || (u.unitName ?? "").IndexOf(nameFilter, StringComparison.OrdinalIgnoreCase) >= 0))
                    .Take(limit).Select(u => new { id = u.persistentID.Id, netId = u.NetId, name = u.unitName,
                        faction = u.NetworkHQ?.faction.factionName, u.disabled, kind = u.GetType().Name,
                        position = Coordinates(u.GlobalPosition().AsVector3()) }).ToArray();
            case "host":
                if ((string)args["instance"] != instance) throw new InvalidOperationException("Stale or missing runtime instance");
                if (MainMenu.State != MainMenu.LoadingState.Loaded || NetworkManagerNuclearOption.i.Server.Active) throw new InvalidOperationException("Idle loaded menu required");
                string name = (string)args["mission"] ?? "Escalation";
                if (name != "Escalation" && name != "Terminal Control") throw new ArgumentException("Built-in lab mission required");
                var mission = CommandLineArgParser.LoadMission(name, false);
                MissionManager.SetMission(mission, false);
                await NetworkManagerNuclearOption.i.StartHostAsync(new HostOptions(SocketType.UDP, GameState.Multiplayer, mission.MapKey) { UdpPort = 17777, MaxConnections = 1, Password = token });
                return Snapshot();
            case "actor.create":
                if ((string)args["instance"] != instance) throw new InvalidOperationException("Stale or missing runtime instance");
                if (!MissionManager.IsRunning) throw new InvalidOperationException("Mission required");
                MockPlayers.Create((string)args["actor"], (string)args["faction"], (string)args["creationId"]);
                return Snapshot();
            case "actor.spawn":
                var p = Actor(args);
                if (args.Property("aircraftId") == null || (uint?)args["aircraftId"] != (p.Aircraft == null ? (uint?)null : p.Aircraft.persistentID.Id))
                    throw new InvalidOperationException("Stale or missing aircraft generation");
                if (p.Aircraft != null && !p.Aircraft.disabled) throw new InvalidOperationException("Cancel/eject/remove existing aircraft first");
                var position = Vector(args, "position");
                var rotation = Quaternion.Euler(Vector(args, "rotation"));
                var velocity = Vector(args, "velocity");
                MockPlayers.Spawn(p, (string)args["type"] ?? "COIN", new GlobalPosition(position), rotation, velocity);
                return Snapshot();
            case "actor.navigate":
                DirectedPilot.For(AircraftFor(args)).Navigate(new GlobalPosition(Vector(args, "destination")), (float?)args["speed"] ?? 100);
                return Snapshot();
            case "actor.attack":
                var a = AircraftFor(args);
                uint targetId = (uint?)args["targetId"] ?? throw new ArgumentException("targetId required");
                var target = UnitRegistry.allUnits.FirstOrDefault(u => u != null && u.persistentID.Id == targetId);
                if (target == null) throw new ArgumentException("Unknown target");
                DirectedPilot.For(a).Attack(target, (int?)args["station"] ?? 0, (float?)args["seconds"] ?? 20);
                return Snapshot();
            case "actor.inspect_target":
                var observer = AircraftFor(args);
                uint inspectedId = (uint?)args["targetId"] ?? throw new ArgumentException("targetId required");
                var inspected = UnitRegistry.allUnits.FirstOrDefault(u => u != null && u.persistentID.Id == inspectedId);
                if (inspected == null) throw new ArgumentException("Unknown target");
                bool known = observer.NetworkHQ.TryGetKnownPosition(inspected, out var knownPosition);
                return new { targetId = inspectedId, inspected.disabled,
                    opposing = inspected.NetworkHQ != null && inspected.NetworkHQ != observer.NetworkHQ,
                    known, accurate = known && observer.NetworkHQ.IsTargetPositionAccurate(inspected, 100f),
                    knownPosition = known ? Coordinates(knownPosition.AsVector3()) : null,
                    distance = Vector3.Distance(observer.GlobalPosition().AsVector3(), inspected.GlobalPosition().AsVector3()) };
            case "actor.cancel": DirectedPilot.For(AircraftFor(args)).Cancel(); return Snapshot();
            case "actor.eject":
                var eject = AircraftFor(args); DirectedPilot.For(eject).Cancel(); eject.StartEjectionSequence(); return Snapshot();
            case "actor.remove":
                var removed = Actor(args);
                if (removed.Aircraft != null) {
                    if ((uint?)args["aircraftId"] != removed.Aircraft.persistentID.Id)
                        throw new InvalidOperationException("Stale or missing aircraft generation");
                    DirectedPilot.For(removed.Aircraft).Cancel();
                }
                MockPlayers.Remove(removed); return Snapshot();
            case "quit":
                if ((string)args["instance"] != instance) throw new InvalidOperationException("Stale or missing runtime instance");
                foreach (var player in MockPlayers.Players.ToArray()) { if (player.Aircraft != null) DirectedPilot.For(player.Aircraft).Cancel(); MockPlayers.Remove(player); }
                Application.Quit(); return new { accepted = true };
            default: throw new ArgumentException("Unknown command");
        }
    }

    private object Snapshot()
    {
        bool ready = MainMenu.State == MainMenu.LoadingState.Loaded;
        return new {
            protocol = 2, instance, assemblySha256 = ReviewedBuild, menuReady = ready,
            serverActive = ready && NetworkManagerNuclearOption.i.Server.Active,
            missionRunning = MissionManager.IsRunning, mission = MissionManager.CurrentMission?.Name,
            coverage = "programmable server mock players; no real client/authentication coverage",
            localHostAdapter = new { authenticationAttempts = LocalHostAdapter.AuthenticationAttempts,
                nameFallbacks = LocalHostAdapter.NameFallbacks,
                identityReady = ready && NetworkManagerNuclearOption.i.Client.Player?.Identity != null },
            errors = new { count = Interlocked.Read(ref errorCount), recent = errors.ToArray() },
            factions = FactionRegistry.GetAllHQs().Select(h => h.faction.factionName).ToArray(),
            airbases = FactionRegistry.airbaseLookup.Select(b => new { name = b.Key, faction = b.Value.CurrentHQ?.faction.factionName,
                position = Coordinates(b.Value.transform.position.ToGlobalPosition().AsVector3()) }).ToArray(),
            aircraftTypes = ready ? Encyclopedia.Lookup.Where(x => x.Value is AircraftDefinition).Select(x => x.Key).ToArray() : new string[0],
            actors = MockPlayers.Players.Select(p => new {
                actor = MockPlayers.Name(p), creationId = MockPlayers.CreationId(p), playerId = p.NetId, score = p.PlayerScore, faction = p.HQ?.faction.factionName,
                aircraft = p.Aircraft == null ? null : new {
                    id = p.Aircraft.persistentID.Id, netId = p.Aircraft.NetId, localSim = p.Aircraft.LocalSim, remoteSim = p.Aircraft.remoteSim, disabled = p.Aircraft.disabled,
                    initialized = p.Aircraft.rb != null && p.Aircraft.pilots != null && p.Aircraft.weaponStations != null,
                    linkedPlayerId = p.Aircraft.Player?.NetId,
                    position = Coordinates(p.Aircraft.GlobalPosition().AsVector3()), ignition = p.Aircraft.Ignition,
                    forward = Coordinates(p.Aircraft.transform.forward),
                    velocity = p.Aircraft.rb == null ? null : Coordinates(p.Aircraft.rb.velocity),
                    health = p.Aircraft.pilots?.Select((pilot, seat) => new { pilot, seat }).Where(entry => entry.pilot != null).Select(entry => new {
                        seat = entry.seat, dead = entry.pilot.dead, ejected = entry.pilot.ejected, state = entry.pilot.currentState?.GetType().Name,
                        npcBrainInitialized = entry.pilot.AICombatState != null || entry.pilot.AIHeloCombatState != null
                    }).ToArray(),
                    stations = p.Aircraft.weaponStations?.Select((s, index) => new {
                        index, ammo = s.Ammo, weapon = s.WeaponInfo?.name,
                        fixedGun = s.WeaponInfo != null && s.WeaponInfo.gun && s.WeaponInfo.boresight && s.TurretCount() == 0
                    }).ToArray(),
                    task = DirectedPilot.For(p.Aircraft).Snapshot()
                }
            }).ToArray()
        };
    }

    private void OnDestroy()
    {
        stopping = true; listener?.Stop(); Application.logMessageReceived -= OnLog;
        foreach (var p in MockPlayers.Players.ToArray()) if (p.Aircraft != null) DirectedPilot.For(p.Aircraft).Cancel();
        MockPlayers.Cleanup();
        harmony?.UnpatchSelf();
    }
}
