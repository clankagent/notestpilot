using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.IO;
using System.Security.Cryptography;
using System.Threading;
using HarmonyLib;
using Mirage;
using Mirage.Serialization;
using Mirage.SocketLayer;
using NuclearOption.Networking;
using NuclearOption.Networking.Authentication;
using NuclearOption.Networking.Lobbies;

namespace NOTestPilot.Runtime;

// All methods run on the game's main thread. These peers deliberately bypass
// sockets, but never successful-auth stamping or the registered RPC dispatcher.
internal static class RequestConnections
{
    private static bool initialized;
    private static int threadId;
    internal static void Initialize()
    {
        if (Environment.GetEnvironmentVariable("NOTESTPILOT_ENABLE") != "1"
            || Environment.GetEnvironmentVariable("NOTESTPILOT_PLAYER_REQUESTS") != "1")
            throw new InvalidOperationException("Player requests require explicit lab opt-in");
        VerifyAssembly(typeof(NetworkServer).Assembly, "d1e0dba87d81a1678d1ecfb6276b5b0d640bcec37b6dda266223f9f48ea34954");
        VerifyAssembly(typeof(IConnection).Assembly, "8c08a3f33144c60c7fcbcf11c6d1cf942350ce6f42f09436e28fd5cbadfc69f1");
        threadId = Thread.CurrentThread.ManagedThreadId;
        initialized = true;
    }
    private static void VerifyAssembly(Assembly assembly, string expected)
    {
        using var stream = File.OpenRead(assembly.Location);
        using var hash = SHA256.Create();
        string actual = BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
        if (actual != expected) throw new InvalidOperationException("Unreviewed requests assembly: " + assembly.GetName().Name);
    }
    private static void RequireMainThread()
    {
        if (!initialized || Thread.CurrentThread.ManagedThreadId != threadId)
            throw new InvalidOperationException("Requests adapter is not initialized on this main thread");
    }
    private sealed class Record
    {
        internal string Id, Name, CreationId, Password, Failure;
        internal VirtualRequestConnection Transport;
        internal INetworkPlayer Peer;
        internal NetworkServer Server;
        internal bool ReadySent, DisconnectPending, Retired;
        internal bool CleanupAttempted, CleanupIncomplete;
        internal DisconnectReason DisconnectReason;
        internal double Started;
        internal long Received;
        internal int JournalBytes;
        internal readonly List<VirtualRequestConnection.Packet> Journal = new List<VirtualRequestConnection.Packet>();
        internal readonly List<string> Notices = new List<string>();
    }
    private static readonly Dictionary<string, Record> records = new Dictionary<string, Record>();
    // Receipt tombstones survive removal for the disposable runtime lifetime.
    private static readonly HashSet<string> receipts = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    private static readonly MethodInfo connected = Required("Peer_OnConnected", typeof(IConnection));
    private static readonly MethodInfo disconnected = Required("Peer_OnDisconnected", typeof(IConnection), typeof(DisconnectReason));
    private static MethodInfo Required(string name, params Type[] arguments)
        => AccessTools.Method(typeof(NetworkServer), name, arguments) ?? throw new MissingMethodException(name);
    internal static object Create(string name, string password, string creationId)
    {
        RequireMainThread();
        var network = NetworkManagerNuclearOption.i;
        if (network == null || !network.Server.Active || !GameManager.IsHeadless || SteamManager.ClientInitialized)
            throw new InvalidOperationException("Requests require an active headless non-Steam lab");
        var udp = AccessTools.Field(typeof(NetworkManagerNuclearOption), "udpTransport").GetValue(network);
        if (!ReferenceEquals(network.Server.SocketFactory, udp)) throw new InvalidOperationException("Requests require native UDP authentication");
        if (string.IsNullOrWhiteSpace(name) || name.Length > 64) throw new ArgumentException("Invalid request player name");
        if (password == null) throw new ArgumentNullException(nameof(password));
        if (creationId == null || creationId.Length != 32 || !Guid.TryParseExact(creationId, "N", out _)) throw new ArgumentException("creationId must be GUID N");
        if (receipts.Contains(creationId)) throw new InvalidOperationException("creationId already consumed");
        if (receipts.Count >= 4096) throw new InvalidOperationException("Request creation receipt limit reached");
        if (records.Values.Any(r => !r.Retired && r.Name == name)) throw new InvalidOperationException("Request name already active");
        receipts.Add(creationId);
        var record = new Record { Id = Guid.NewGuid().ToString("N"), Name = name, CreationId = creationId.ToLowerInvariant(), Password = password,
            Server = network.Server, Started = UnityEngine.Time.unscaledTimeAsDouble };
        record.Transport = new VirtualRequestConnection(record.Id);
        record.Transport.OnDisconnect = reason => { record.DisconnectPending = true; record.DisconnectReason = reason; };
        records.Add(record.Id, record);
        try
        {
            connected.Invoke(record.Server, new object[] { record.Transport });
            record.Peer = record.Server.AllPlayers.Single(p => ReferenceEquals(p.Connection, record.Transport));
            if (record.Peer.IsHost) throw new InvalidOperationException("Virtual request peer unexpectedly host");
            uint hash = (uint)AccessTools.Method(typeof(NetworkAuthenticatorNuclearOption), "GetBuildHash").Invoke(null, null);
            byte[] inner = MessagePacker.Pack(new NetworkAuthenticatorNuclearOption.AuthMessage { BuildHash = hash, JoinAs = PlayerType.Normal, SteamName = name });
            Send(record.Id, new Mirage.Authentication.AuthMessage { Payload = new ArraySegment<byte>(inner) });
        }
        catch (Exception error)
        {
            Fail(record, error.GetBaseException().Message);
            // The receipt and failure row remain recoverable, but registration
            // must not survive a partially initialized create.
            Retire(record);
            throw;
        }
        return Status(record.Id);
    }
    internal static INetworkPlayer GetConnection(string id) => Find(id).Peer ?? throw new InvalidOperationException("Native peer not registered");
    internal static Player GetPlayer(string id)
    {
        var record = Find(id);
        return record.Retired || record.Peer?.Identity == null ? null : record.Peer.Identity.GetComponent<Player>();
    }
    internal static void Send<T>(string id, T message)
    {
        RequireMainThread();
        var record = Find(id);
        if (record.Retired || record.DisconnectPending || record.Transport.State != ConnectionState.Connected || record.Peer == null)
            throw new InvalidOperationException("Request connection is not connected");
        byte[] packet = MessagePacker.Pack(message);
        record.Server.MessageHandler.HandleMessage(record.Peer, new ArraySegment<byte>(packet));
    }
    internal static void Tick()
    {
        if (!initialized) return;
        RequireMainThread();
        foreach (var record in records.Values.ToArray())
        {
            if (record.Retired) continue;
            try
            {
                if (!record.Server.Active) Fail(record, "Native server stopped");
                int budget = 256;
                while (budget-- > 0 && record.Transport.TryReceive(out var packet))
                {
                    record.Journal.Add(packet); record.JournalBytes += packet.Bytes.Length; record.Received = packet.Sequence;
                    // Retain the packet we already received before failing the
                    // active journal bound; retirement preserves the remainder.
                    if (record.Journal.Count > 4096 || record.JournalBytes > 16 * 1024 * 1024)
                        throw new InvalidOperationException("Request packet journal exhausted; drain required");
                    int type;
                    using (var reader = NetworkReaderPool.GetReader(packet.Bytes, record.Server.World))
                        type = MessagePacker.UnpackId(reader);
                    if (!record.DisconnectPending && type == MessagePacker.GetId<NetworkAuthenticatorNuclearOption.PasswordChallenge>())
                    {
                        var challenge = MessagePacker.Unpack<NetworkAuthenticatorNuclearOption.PasswordChallenge>(packet.Bytes, record.Server.World);
                        Send(record.Id, new NetworkAuthenticatorNuclearOption.PasswordResponse { Response = new ArraySegment<byte>(LobbyPassword.GenerateResponse(record.Password, challenge.Nonce)) });
                    }
                    else if (type == MessagePacker.GetId<NetworkAuthenticatorNuclearOption.AuthFailReason>())
                        Fail(record, "native auth: " + MessagePacker.Unpack<NetworkAuthenticatorNuclearOption.AuthFailReason>(packet.Bytes, record.Server.World).Reason);
                    else if (type == MessagePacker.GetId<NetworkAuthenticatorNuclearOption.BuildHashMismatch>())
                        Fail(record, "native build mismatch");
                }
                if (record.Transport.Failure != null) Fail(record, record.Transport.Failure);
                if (!record.DisconnectPending && record.Peer != null && record.Peer.IsAuthenticated && !record.ReadySent)
                {
                    bool loading = (bool)AccessTools.Field(typeof(NetworkManagerNuclearOption), "loadingScene").GetValue(NetworkManagerNuclearOption.i);
                    if (MissionManager.IsRunning && !loading) { Send(record.Id, new SceneReadyMessage()); record.ReadySent = true; record.Password = null; }
                }
                if ((!record.ReadySent || GetPlayer(record.Id) == null) && UnityEngine.Time.unscaledTimeAsDouble - record.Started > 65)
                    Fail(record, "Native authentication/readiness timed out");
            }
            catch (Exception error) { Fail(record, error.GetBaseException().Message); }
            if (record.DisconnectPending) Retire(record);
        }
    }
    internal static object Status(string id)
    {
        var r = Find(id); var player = GetPlayer(id);
        return new { mode = "player-requests", connectionId = r.Id, name = r.Name, creationId = r.CreationId,
            host = r.Peer?.IsHost ?? false,
            registered = r.Peer != null && r.Server.AllPlayers.Any(peer => ReferenceEquals(peer, r.Peer)),
            authenticatedMember = r.Peer != null && r.Server.AuthenticatedPlayers.Any(peer => ReferenceEquals(peer, r.Peer)),
            nativeOwnedIdentityCount = r.Peer?.OwnedObjects.Count ?? 0,
            nativeVisibleIdentityCount = r.Peer?.VisList.Count ?? 0,
            playerId = player == null ? (uint?)null : player.NetId, authenticated = r.Peer?.IsAuthenticated ?? false,
            ready = r.ReadySent && player != null && !r.Retired && r.Failure == null,
            state = r.Failure != null ? "failed" : r.Retired ? "disconnected" : r.ReadySent && player != null ? "ready" : "authenticating",
            error = r.Failure, nativeErrors = r.Peer?.ErrorFlags.ToString(), nativeErrorBits = (int)(r.Peer?.ErrorFlags ?? PlayerErrorFlags.None), notices = r.Notices.ToArray(),
            sentPackets = r.Transport.Sent, receivedPackets = r.Received, queuedPackets = r.Transport.Pending, journalPackets = r.Journal.Count,
            disconnectReason = r.DisconnectReason.ToString(), gracefulReason = r.Transport.GracefulReason,
            cleanupAttempted = r.CleanupAttempted, cleanupIncomplete = r.CleanupIncomplete,
            packetGap = r.Transport.PacketGap };
    }
    internal static IEnumerable<object> Statuses => records.Keys.Select(Status).ToArray();
    internal static object[] AllStatus() => Statuses.ToArray();
    internal static VirtualRequestConnection.Packet[] DrainPackets(string id)
    {
        var r = Find(id);
        const int chunkBytes = 512 * 1024;
        const int singlePacketBytes = 1024 * 1024;
        int count = 0, bytes = 0;
        while (count < r.Journal.Count && count < 256)
        {
            int length = r.Journal[count].Bytes.Length;
            if (count == 0 && length > singlePacketBytes)
            {
                Fail(r, "Native packet exceeds bounded control response size");
                // Keep its diagnostic bytes and every subsequent packet.
                throw new InvalidOperationException(r.Failure);
            }
            if (count > 0 && length > chunkBytes - bytes) break;
            bytes += length;
            count++;
            // A larger first packet gets its own response, capped at 1MiB.
            if (bytes >= chunkBytes) break;
        }
        var packets = r.Journal.GetRange(0, count).ToArray();
        r.Journal.RemoveRange(0, count);
        r.JournalBytes -= bytes;
        return packets;
    }
    // New array, retained immutable packet bytes: observation does not consume
    // the journal. The caller tracks its last sequence before explicit drain.
    internal static VirtualRequestConnection.Packet[] PacketsSince(string id, long after)
    {
        if (after < 0) throw new ArgumentOutOfRangeException(nameof(after));
        return Find(id).Journal.Where(packet => packet.Sequence > after).ToArray();
    }
    internal static void Disconnect(string id) { Find(id).Transport.Disconnect(); Tick(); }
    internal static void Cleanup() { foreach (var record in records.Values.ToArray()) if (!record.Retired) record.Transport.Disconnect(); Tick(); }
    private static Record Find(string id) => records.TryGetValue(id ?? "", out var record) ? record : throw new ArgumentException("Unknown request connection");
    private static void Fail(Record record, string reason)
    {
        if (record.Failure == null) record.Failure = reason;
        if (record.Notices.Count < 64) record.Notices.Add(reason);
        record.Transport.Disconnect();
    }
    private static void Retire(Record record)
    {
        if (record.Retired) return;
        // The active journal and transport queue are each capped at 4096/16MiB.
        // Their retirement union is therefore bounded at 8192/32MiB. Keep every
        // remaining copied packet drainable, including bytes after packet 256.
        while (record.Transport.TryReceive(out var packet))
        {
            record.Journal.Add(packet);
            record.JournalBytes += packet.Bytes.Length;
            record.Received = packet.Sequence;
        }
        if (record.Transport.Failure != null && record.Failure == null)
            record.Failure = record.Transport.Failure;
        if (!record.CleanupAttempted)
        {
            record.CleanupAttempted = true;
            try { disconnected.Invoke(record.Server, new object[] { record.Transport, record.DisconnectReason }); }
            catch (Exception error)
            {
                record.CleanupIncomplete = true;
                string failure = "native disconnect cleanup failed: " + error.GetBaseException().GetType().Name;
                if (record.Failure == null) record.Failure = failure;
                if (record.Notices.Count < 64) record.Notices.Add(failure);
            }
        }
        // Native callbacks may already have removed ownership or membership
        // before throwing. Never replay that partially applied lifecycle.
        record.Retired = true;
        record.Password = null;
    }
}
