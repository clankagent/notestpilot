using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using Mirage;
using Mirage.RemoteCalls;
using Mirage.Serialization;
using Newtonsoft.Json.Linq;
using NuclearOption.Networking;
using NuclearOption.SavedMission;
using Player = NuclearOption.Networking.Player;

namespace NOTestPilot.Runtime;

// Selects and packs requests; the original registered server dispatcher owns
// authentication, authority, rate limits and gameplay validation.
internal static class RequestActions
{
    private sealed class SpawnReceipt
    {
        internal int Id;
        internal long Started = Stopwatch.GetTimestamp();
        internal string State = "pending", Error;
        internal JObject Outcome;
        internal bool Uncertain;
    }
    private static readonly Dictionary<string, List<SpawnReceipt>> spawnRequests = new Dictionary<string, List<SpawnReceipt>>();
    private static readonly Dictionary<string, long> decodedThrough = new Dictionary<string, long>();
    internal static void Tick()
    {
        foreach (var entry in spawnRequests) {
            long after = decodedThrough.TryGetValue(entry.Key, out var cursor) ? cursor : 0;
            foreach (var packet in RequestConnections.PacketsSince(entry.Key, after)) {
                foreach (var receipt in entry.Value.Where(r => r.Outcome == null)) {
                    try {
                        var decoded = JObject.FromObject(RequestReplies.DecodeSpawn(packet, RequestConnections.GetConnection(entry.Key), receipt.Id));
                        if (!(bool)decoded["matched"]) continue;
                        receipt.Outcome = decoded;
                        receipt.State = receipt.Uncertain ? "replied-late" : "replied";
                    } catch (Exception failure) {
                        receipt.Error = failure.GetType().Name + ": " + failure.Message;
                        receipt.State = "uncertain"; receipt.Uncertain = true;
                    }
                }
                decodedThrough[entry.Key] = packet.Sequence;
            }
            foreach (var receipt in entry.Value.Where(r => r.Outcome == null && !r.Uncertain))
                if (Stopwatch.GetTimestamp() - receipt.Started > 30 * Stopwatch.Frequency) {
                    receipt.Uncertain = true; receipt.State = "uncertain";
                    receipt.Error = "Native spawn reply deadline passed; inspect state, never replay the request";
                }
        }
    }
    internal static string Bound(JObject args, string instance, bool pending = false)
    {
        if ((string)args["instance"] != instance) throw new InvalidOperationException("Stale or missing runtime instance");
        if ((string)args["mode"] != "player-requests") throw new InvalidOperationException("Player requests mode is required");
        string id = (string)args["connectionId"] ?? throw new ArgumentException("connectionId is required");
        var row = JObject.FromObject(RequestConnections.Status(id));
        if ((string)args["name"] != (string)row["name"]) throw new InvalidOperationException("Stale request player name");
        if (args.Property("playerId") == null || (uint?)args["playerId"] != (uint?)row["playerId"])
            throw new InvalidOperationException("Stale or missing request player identity");
        var player = RequestConnections.GetPlayer(id);
        uint? aircraft = player?.Aircraft == null ? (uint?)null : player.Aircraft.persistentID.Id;
        if (args.Property("aircraftId") == null || (uint?)args["aircraftId"] != aircraft)
            throw new InvalidOperationException("Stale or missing request aircraft generation");
        if (!pending && (!(bool)row["ready"] || player == null)) throw new InvalidOperationException("Native request player is not ready");
        if (player != null && !ReferenceEquals(player.Owner, RequestConnections.GetConnection(id)))
            throw new InvalidOperationException("Request player ownership invariant failed");
        return id;
    }

    internal static object[] Snapshot()
    {
        return RequestConnections.AllStatus().Select(value => {
            var row = JObject.FromObject(value);
            row["nativeGameErrors"] = ((NuclearOptionPlayerErrorFlags.Names)(int)row["nativeErrorBits"]).ToString();
            string connectionId = (string)row["connectionId"];
            var player = RequestConnections.GetPlayer(connectionId);
            row["nativeIdentityName"] = player == null ? JValue.CreateNull() : new JValue(player.Identity.name);
            var receipts = spawnRequests.TryGetValue(connectionId, out var list) ? list.ToArray() : new SpawnReceipt[0];
            row["lastSpawnReplyId"] = receipts.Length == 0 ? JValue.CreateNull() : new JValue(receipts.Last().Id);
            row["spawnRequests"] = JToken.FromObject(receipts.Select(r => new { replyId = r.Id, state = r.State,
                uncertain = r.Uncertain, error = r.Error, outcome = r.Outcome }).ToArray());
            row["capabilities"] = new JArray((bool)row["ready"]
                ? new[] { "status", "join_faction", "purchase_airframe", "request_spawn", "disconnect" }
                : new[] { "status", "disconnect" });
            row["aircraft"] = player?.Aircraft == null ? JValue.CreateNull() : JToken.FromObject(new {
                id = player.Aircraft.persistentID.Id, netId = player.Aircraft.NetId,
                localSim = player.Aircraft.LocalSim, remoteSim = player.Aircraft.remoteSim,
                disabled = player.Aircraft.disabled, linkedPlayerId = player.Aircraft.Player?.NetId });
            if (player?.Aircraft != null)
                ((JObject)row["aircraft"])["ownedByConnection"] = ReferenceEquals(player.Aircraft.Owner, RequestConnections.GetConnection(connectionId));
            if (player != null) {
                row["faction"] = player.HQ?.faction.factionName;
                row["allocation"] = player.Allocation;
                row["rank"] = player.PlayerRank;
                row["score"] = player.PlayerScore;
                row["inventory"] = JToken.FromObject(player.OwnedAirframes.Select(a => new {
                    aircraft = a.Definition?.jsonKey, reserved = a.Reserved }).ToArray());
                row["aircraftSpawnPending"] = player.AircraftSpawnPending;
                row["airframeInUse"] = player.AirframeInUse.HasValue ? JToken.FromObject(new {
                    aircraft = player.AirframeInUse.Value.Definition?.jsonKey, reserved = player.AirframeInUse.Value.Reserved }) : JValue.CreateNull();
            }
            return (object)row;
        }).ToArray();
    }

    private static AircraftDefinition Definition(string key)
    {
        var result = key != null && Encyclopedia.Lookup.TryGetValue(key, out var entry) ? entry as AircraftDefinition : null;
        return result ?? throw new ArgumentException("Unknown aircraft definition");
    }

    private static int Function(NetworkBehaviour target, string name, bool authority)
    {
        var calls = target.Identity.RemoteCallCollection.RemoteCalls;
        var matches = calls.Select((call, index) => new { call, index })
            .Where(x => x.call != null && x.call.Name == name && ReferenceEquals(x.call.Behaviour, target)).ToArray();
        if (matches.Length != 1 || matches[0].call.InvokeType != RpcInvokeType.ServerRpc || matches[0].call.RequireAuthority != authority)
            throw new InvalidOperationException("Reviewed native RPC binding does not match: " + name);
        return matches[0].index;
    }

    private static void Send(string id, NetworkBehaviour target, string name, Action<NetworkWriter> write)
    {
        int index = Function(target, name, true);
        using var writer = NetworkWriterPool.GetWriter();
        write(writer);
        RequestConnections.Send(id, new RpcMessage { NetId = target.NetId, FunctionIndex = index, Payload = writer.ToArraySegment() });
    }
    internal static void JoinFaction(string id, string faction)
    {
        var hq = FactionRegistry.HqFromName(faction) ?? throw new ArgumentException("Unknown faction");
        Send(id, RequestConnections.GetPlayer(id), "NuclearOption.Networking.Player.CmdSetFaction", w => w.Write(hq));
    }
    internal static void Purchase(string id, string aircraft)
    {
        var definition = Definition(aircraft);
        Send(id, RequestConnections.GetPlayer(id), "NuclearOption.Networking.Player.CmdPurchaseAirframe", w => w.Write(definition));
    }
    internal static void Spawn(string id, JObject args)
    {
        string key = (string)args["airbase"];
        if (key == null || !FactionRegistry.airbaseLookup.TryGetValue(key, out var airbase)) throw new ArgumentException("Unknown airbase");
        var definition = Definition((string)args["aircraft"]);
        float fuel = (float?)args["fuel"] ?? 1f;
        if (float.IsNaN(fuel) || float.IsInfinity(fuel)) throw new ArgumentException("Finite fuel is required");
        var supplied = args["loadout"] as JArray;
        var loadout = new Loadout();
        if (supplied == null) {
            var defaults = definition.aircraftParameters.loadouts;
            if (defaults == null || defaults.Count == 0) throw new InvalidOperationException("Aircraft has no native default loadout");
            loadout.weapons.AddRange(defaults[0].weapons);
        } else {
            if (supplied.Count > 16) throw new ArgumentException("At most 16 native mounts");
            foreach (var selection in supplied) {
                if (selection.Type == JTokenType.Null) { loadout.weapons.Add(null); continue; }
                var weapon = selection.Type == JTokenType.String
                    ? Encyclopedia.Lookup.Values.OfType<AircraftDefinition>().SelectMany(d => d.aircraftParameters.loadouts)
                        .SelectMany(l => l.weapons).FirstOrDefault(m => m != null && m.jsonKey == (string)selection)
                    : null;
                if (weapon == null) throw new ArgumentException("Loadout requires a native preset mount lookup key or null");
                loadout.weapons.Add(weapon);
            }
        }
        var spawner = NetworkSceneSingleton<Spawner>.i;
        int index = Function(spawner, "Spawner.CmdRequestSpawnAircraft", false);
        using var writer = NetworkWriterPool.GetWriter();
        writer.Write(airbase); writer.Write(definition); writer.Write(new LiveryKey((int?)args["livery"] ?? 0));
        writer.Write(loadout); writer.WriteSingleConverter(fuel);
        if (!spawnRequests.TryGetValue(id, out var receipts)) spawnRequests[id] = receipts = new List<SpawnReceipt>();
        if (receipts.Count >= 64) throw new InvalidOperationException("Disposable spawn receipt limit reached; start a fresh runtime");
        if (receipts.Any(r => r.State == "pending" || r.Uncertain))
            throw new InvalidOperationException("Unresolved native spawn request; inspect its state before another request");
        // Reserve correlation BEFORE native dispatch, including its synchronous
        // exception path. No local shortcut or second spawn occurs on uncertainty.
        var receipt = new SpawnReceipt { Id = NextReply() };
        receipts.Add(receipt);
        try {
        RequestConnections.Send(id, new RpcWithReplyMessage { NetId = spawner.NetId, FunctionIndex = index,
            ReplyId = receipt.Id, Payload = writer.ToArraySegment() });
        } catch (Exception failure) {
            receipt.Uncertain = true; receipt.State = "uncertain";
            receipt.Error = failure.GetType().Name + ": " + failure.Message; throw;
        }
    }
    private static int replyId;
    // Shared with bounded guard diagnostics so return RPCs cannot collide.
    internal static int ReserveReplyId() => NextReply();
    private static int NextReply()
    {
        if (replyId == int.MaxValue) throw new InvalidOperationException("Disposable reply ID limit reached");
        return ++replyId;
    }
}
