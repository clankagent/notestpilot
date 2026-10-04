using System;
using System.IO;
using Mirage;
using Mirage.RemoteCalls;
using Mirage.Serialization;
using NuclearOption.Networking;

namespace NOTestPilot.Runtime;

// Reads a copied packet from the request player's private native-output journal.
// This is observation only: it never dequeues packets, invokes a client RPC,
// or routes a reply through the host player's skeletons.
internal static class RequestReplies
{
    // The expected ID comes from the immutable in-flight request record. Other
    // native replies are reported but their payload is never decoded as spawn.
    // This reader never consumes or mutates the journal packet.
    internal static object DecodeSpawn(VirtualRequestConnection.Packet packet, INetworkPlayer sender, int expectedReplyId)
    {
        if (packet == null) throw new ArgumentNullException(nameof(packet));
        if (sender == null) throw new ArgumentNullException(nameof(sender));
        if (expectedReplyId <= 0) throw new ArgumentOutOfRangeException(nameof(expectedReplyId));
        if (packet.Bytes == null || packet.Bytes.Length < MessagePacker.ID_BYTE_SIZE)
            return new { matched = false, sequence = packet.Sequence, messageId = (int?)null, replyId = (int?)null,
                reason = "packet has no complete message ID" };

        var network = NetworkManagerNuclearOption.i;
        var server = network?.Server;
        if (server == null || !server.Active)
            throw new InvalidOperationException("Cannot decode a native reply without the active server world");
        var world = server.World;

        int messageId;
        using (var reader = NetworkReaderPool.GetReader(packet.Bytes, world))
            messageId = MessagePacker.UnpackId(reader);
        int replyMessageId = MessagePacker.GetId<RpcReply>();
        if (messageId != replyMessageId)
            return new { matched = false, sequence = packet.Sequence, messageId = (int?)messageId,
                replyId = (int?)null, reason = "not a native RpcReply" };

        RpcReply reply = MessagePacker.Unpack<RpcReply>(packet.Bytes, world);
        if (reply.ReplyId != expectedReplyId)
            return new { matched = false, sequence = packet.Sequence, messageId = (int?)messageId,
                replyId = (int?)reply.ReplyId, reason = "reply ID belongs to another request" };
        if (!reply.Success)
        {
            // Mirage carries no exception body for an unsuccessful request.
            // Keep that distinct from a native TrySpawnResult with Allowed=false.
            return new
            {
                matched = true,
                sequence = packet.Sequence,
                replyId = reply.ReplyId,
                success = false,
                nativeErrorFlags = sender.ErrorFlags.ToString(),
                allowed = (bool?)null,
                delayedSpawn = (bool?)null,
                hangarNetId = (uint?)null,
                hangarName = (string)null
            };
        }
        if (reply.Payload.Array == null || reply.Payload.Count == 0)
            throw new InvalidDataException("Successful spawn RPC reply has no native result payload");

        Airbase.TrySpawnResult result;
        using (var reader = NetworkReaderPool.GetReader(reply.Payload, world))
            result = reader.Read<Airbase.TrySpawnResult>();

        Hangar hangar = result.Hangar;
        return new
        {
            matched = true,
            sequence = packet.Sequence,
            replyId = reply.ReplyId,
            success = true,
            nativeErrorFlags = sender.ErrorFlags.ToString(),
            allowed = result.Allowed,
            delayedSpawn = result.DelayedSpawn,
            hangarNetId = hangar == null ? (uint?)null : hangar.NetId,
            hangarName = hangar == null ? null : hangar.gameObject.name
        };
    }
}
