using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using Mirage;
using Mirage.RemoteCalls;
using Mirage.Serialization;
using Newtonsoft.Json.Linq;
using NuclearOption.Networking;

namespace NOTestPilot.Runtime;

// Disposable, opt-in request guards. Only the native read-only delay query is
// reachable. Neither gameplay state nor native error/rate buckets are modified.
internal static class RequestDiagnostics
{
    private const string RpcName = "NuclearOption.Networking.Player.CmdGetDelayContribute";
    private sealed class Receipt
    {
        internal int ReplyId;
        internal string Action, TargetConnectionId, State = "pending", Error;
        internal uint TargetPlayerId;
        internal long Started = Stopwatch.GetTimestamp();
        internal long? Sequence;
        internal bool Uncertain;
        internal bool? Success;
        internal float? Value;
    }
    private sealed class Record
    {
        internal string Id;
        internal INetworkPlayer Peer;
        internal RemoteCall Call;
        internal long Cursor;
        internal readonly List<Receipt> Receipts = new List<Receipt>();
    }
    private static readonly Dictionary<string, Record> records = new Dictionary<string, Record>();
    internal static bool HasUnresolved(string id) => records.TryGetValue(id, out var r)
        && r.Receipts.Any(receipt => receipt.State == "pending" || receipt.Uncertain);

    internal static object Run(JObject args, string instance)
    {
        if (Environment.GetEnvironmentVariable("NOTESTPILOT_REQUEST_PROBES") != "1")
            throw new InvalidOperationException("Native request probes require explicit opt-in");
        string id = RequestActions.Bound(args, instance);
        string action = (string)args["action"];
        if (action != "wrong-owner" && action != "rate-burst" && action != "query")
            throw new ArgumentException("Unknown bounded native request probe");
        if (HasUnresolved(id)) throw new InvalidOperationException("Pending or uncertain native probe; inspect evidence, never replay");
        string targetId = id;
        if (action == "wrong-owner")
        {
            var target = args["target"] as JObject ?? throw new ArgumentException("Immutable target request handle is required");
            targetId = RequestActions.Bound(target, instance);
            if (targetId == id) throw new ArgumentException("Wrong-owner probe requires a distinct connection");
        }
        else if (args.Property("target") != null) throw new ArgumentException("Own-player probe cannot choose another target");
        var peer = RequestConnections.GetConnection(id);
        var targetPlayer = RequestConnections.GetPlayer(targetId);
        if (RequestConnections.GetPlayer(id).Aircraft != null || targetPlayer.Aircraft != null)
            throw new InvalidOperationException("Bounded request probes require aircraft-free players");
        var calls = targetPlayer.Identity.RemoteCallCollection.RemoteCalls;
        var match = calls.Select((call, index) => new { call, index }).Where(x => x.call != null
            && x.call.Name == RpcName && ReferenceEquals(x.call.Behaviour, targetPlayer)).ToArray();
        if (match.Length != 1) throw new InvalidOperationException("Native diagnostic RPC binding is ambiguous");
        var call = match[0].call;
        if (call.InvokeType != RpcInvokeType.ServerRpc || !call.RequireAuthority || !call.RateLimit.IsEnabled
            || call.RateLimit.BucketConfig.Interval != 1f || call.RateLimit.BucketConfig.Refill != 2
            || call.RateLimit.BucketConfig.MaxTokens != 10 || call.RateLimit.Penalty != 2)
            throw new InvalidOperationException("Native diagnostic authority/rate binding changed");
        int errorBudget = action == "wrong-owner" ? 10 : action == "rate-burst" ? 22 : 2;
        if (peer.ErrorRateLimit == null || peer.ErrorRateLimit.Tokens < errorBudget)
            throw new InvalidOperationException("Native error budget is absent or too low for bounded probes");
        if (!records.TryGetValue(id, out var record))
            records[id] = record = new Record { Id = id, Peer = peer, Call = call };
        if (!ReferenceEquals(record.Peer, peer)) throw new InvalidOperationException("Diagnostic sender identity changed");
        int count = action == "rate-burst" ? 11 : 1;
        if (record.Receipts.Count > 24 - count) throw new InvalidOperationException("Disposable native probe call limit reached");
        // Reserve every receipt before the first native call. This includes
        // synchronous exceptions and prevents manufacturing fresh retry IDs.
        var batch = new List<Receipt>();
        for (int i = 0; i < count; i++)
        {
            var receipt = new Receipt { ReplyId = RequestActions.ReserveReplyId(), Action = action,
                TargetConnectionId = targetId, TargetPlayerId = targetPlayer.NetId };
            record.Receipts.Add(receipt); batch.Add(receipt);
        }
        using var writer = NetworkWriterPool.GetWriter();
        try
        {
            // No arguments. All eleven burst calls share this main-thread turn;
            // normal native per-player token accounting decides their results.
            foreach (var receipt in batch)
                RequestConnections.Send(id, new RpcWithReplyMessage { NetId = targetPlayer.NetId,
                    FunctionIndex = match[0].index, ReplyId = receipt.ReplyId, Payload = writer.ToArraySegment() });
        }
        catch (Exception error)
        {
            foreach (var receipt in batch)
            {
                receipt.State = "uncertain"; receipt.Uncertain = true;
                receipt.Error = "Native probe dispatch failed: " + error.GetType().Name;
            }
            throw;
        }
        return Status(record);
    }

    internal static void Tick()
    {
        foreach (var record in records.Values)
        {
            foreach (var packet in RequestConnections.PacketsSince(record.Id, record.Cursor))
            {
                foreach (var receipt in record.Receipts.Where(r => r.Success == null))
                {
                    try { Observe(record, receipt, packet); }
                    catch (Exception error)
                    {
                        receipt.State = "uncertain"; receipt.Uncertain = true;
                        receipt.Error = "Native probe reply decode failed: " + error.GetType().Name;
                    }
                }
                record.Cursor = packet.Sequence;
            }
            foreach (var receipt in record.Receipts.Where(r => r.Success == null && !r.Uncertain))
                if (Stopwatch.GetTimestamp() - receipt.Started > 15 * Stopwatch.Frequency)
                {
                    receipt.State = "uncertain"; receipt.Uncertain = true;
                    receipt.Error = "Native probe reply deadline passed; do not replay";
                }
        }
    }
    private static void Observe(Record record, Receipt receipt, VirtualRequestConnection.Packet packet)
    {
        var world = NetworkManagerNuclearOption.i.Server.World;
        using (var reader = NetworkReaderPool.GetReader(packet.Bytes, world))
            if (MessagePacker.UnpackId(reader) != MessagePacker.GetId<RpcReply>()) return;
        var reply = MessagePacker.Unpack<RpcReply>(packet.Bytes, world);
        if (reply.ReplyId != receipt.ReplyId) return;
        float? value = null;
        if (reply.Success)
        {
            if (reply.Payload.Array == null || reply.Payload.Count == 0) throw new InvalidDataException("Native delay query has no float payload");
            using var reader = NetworkReaderPool.GetReader(reply.Payload, world);
            float nativeValue = reader.Read<float>();
            if (float.IsNaN(nativeValue) || float.IsInfinity(nativeValue) || nativeValue < 0)
                throw new InvalidDataException("Native delay query returned an invalid value");
            value = nativeValue;
        }
        receipt.Success = reply.Success; receipt.Value = value; receipt.Sequence = packet.Sequence;
        receipt.State = receipt.Uncertain ? "replied-late" : "replied";
    }
    private static object Status(Record record)
    {
        var config = record.Call.RateLimit;
        float? tokens = null;
        if (record.Peer is NetworkPlayer peer && peer.RpcRateLimit != null
            && peer.RpcRateLimit.TryGetValue(record.Call.RpcId, out var bucket)) tokens = bucket.Tokens;
        return new { connectionId = record.Id, calls = record.Receipts.Count, lastObservedSequence = record.Cursor,
            nativeUnscaledTime = UnityEngine.Time.unscaledTimeAsDouble,
            unresolved = HasUnresolved(record.Id), nativeErrorBits = (int)record.Peer.ErrorFlags,
            nativeErrorTokens = record.Peer.ErrorRateLimit?.Tokens,
            nativeErrorMaxTokens = record.Peer.ErrorRateLimit?.Config.MaxTokens,
            nativeRate = new { interval = config.BucketConfig.Interval, refill = config.BucketConfig.Refill,
                maxTokens = config.BucketConfig.MaxTokens, penalty = config.Penalty, tokens },
            receipts = record.Receipts.Select(r => new { replyId = r.ReplyId, action = r.Action,
                targetConnectionId = r.TargetConnectionId, targetPlayerId = r.TargetPlayerId,
                state = r.State, uncertain = r.Uncertain, error = r.Error,
                sequence = r.Sequence, success = r.Success, value = r.Value }).ToArray() };
    }
    internal static object[] AllStatus() => records.Values.Select(Status).ToArray();
}
