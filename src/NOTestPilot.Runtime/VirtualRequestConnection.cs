using System;
using System.Collections.Generic;
using Mirage.SocketLayer;

namespace NOTestPilot.Runtime;

// In-memory delivery only. Every send owns its bytes; no pooled writer storage
// survives the call. Queue exhaustion is a fixture failure, never a silent drop.
internal sealed class VirtualRequestConnection : IConnection
{
    internal sealed class Packet
    {
        internal long Sequence;
        internal byte[] Bytes;
        internal string Channel;
        internal Action Delivered;
        internal Action Lost;
        internal bool Settled;
    }
    private const int MaxPackets = 4096;
    private const int MaxBytes = 16 * 1024 * 1024;
    private readonly Queue<Packet> packets = new Queue<Packet>();
    private int bytes;
    private long sequence;
    internal Action<DisconnectReason> OnDisconnect;
    internal string Failure { get; private set; }
    internal bool PacketGap { get; private set; }
    internal string GracefulReason { get; private set; }
    internal long Sent => sequence;
    internal int Pending => packets.Count;
    public IConnectionHandle Handle { get; }
    public ConnectionState State { get; private set; } = ConnectionState.Connected;

    internal VirtualRequestConnection(string id) { Handle = new HandleValue(id, this); }
    public void Disconnect() => Disconnect(default);
    public void Disconnect(DisconnectReason reason)
    {
        if (State != ConnectionState.Connected) return;
        State = ConnectionState.Disconnected;
        foreach (var packet in packets) Settle(packet, false);
        // Defer native lifecycle dispatch to the main-thread pump. Native
        // NetworkPlayer.Disconnect itself must finish before its event runs.
        OnDisconnect?.Invoke(reason);
    }
    public void SendReliable(byte[] data, int offset, int length) => Enqueue(data, offset, length, "reliable", null, null);
    public void SendUnreliable(byte[] data, int offset, int length) => Enqueue(data, offset, length, "unreliable", null, null);
    public INotifyToken SendNotify(byte[] data, int offset, int length)
    {
        var token = new NotifyToken();
        Enqueue(data, offset, length, "notify", token.Deliver, token.Lose);
        return token;
    }
    public void SendNotify(byte[] data, int offset, int length, INotifyCallBack callbacks)
        => Enqueue(data, offset, length, "notify", callbacks.OnDelivered, callbacks.OnLost);
    public void FlushBatch() { }
    private void Enqueue(byte[] data, int offset, int length, string channel, Action delivered, Action lost)
    {
        if (State != ConnectionState.Connected) { Settle(new Packet { Lost = lost }, false); return; }
        if (data == null || offset < 0 || length < 0 || offset > data.Length - length)
            throw new ArgumentException("Invalid virtual packet segment");
        if (packets.Count >= MaxPackets || length > MaxBytes - bytes)
        {
            Failure = "virtual outbound queue exhausted";
            PacketGap = true;
            Settle(new Packet { Lost = lost }, false);
            Disconnect();
            throw new InvalidOperationException(Failure);
        }
        var copy = new byte[length];
        Buffer.BlockCopy(data, offset, copy, 0, length);
        packets.Enqueue(new Packet { Sequence = ++sequence, Bytes = copy, Channel = channel, Delivered = delivered, Lost = lost });
        bytes += length;
    }
    internal bool TryReceive(out Packet packet)
    {
        if (packets.Count == 0) { packet = null; return false; }
        packet = packets.Dequeue(); bytes -= packet.Bytes.Length;
        // Delivery means receipt by this virtual endpoint, not retail delivery.
        Settle(packet, State == ConnectionState.Connected);
        return true;
    }
    private void Settle(Packet packet, bool delivered)
    {
        if (packet.Settled) return;
        packet.Settled = true;
        try { if (delivered) packet.Delivered?.Invoke(); else packet.Lost?.Invoke(); }
        catch (Exception error)
        {
            Failure = "virtual notification callback failed: " + error.GetType().Name;
            Disconnect();
        }
    }
    private sealed class NotifyToken : INotifyToken
    {
        public event Action Delivered;
        public event Action Lost;
        internal void Deliver() => Delivered?.Invoke();
        internal void Lose() => Lost?.Invoke();
    }
    private sealed class HandleValue : IConnectionHandle
    {
        private readonly string id;
        public bool IsStateful => true;
        public bool SupportsGracefulDisconnect => false;
        public ISocketLayerConnection SocketLayerConnection { get; set; }
        internal HandleValue(string id, ISocketLayerConnection connection) { this.id = id; SocketLayerConnection = connection; }
        public void Disconnect(string reason)
        {
            var transport = (VirtualRequestConnection)SocketLayerConnection;
            transport.GracefulReason = reason;
            transport.Disconnect(DisconnectReason.RequestedByLocalPeer);
        }
        public IConnectionHandle CreateCopy() => new HandleValue(id, SocketLayerConnection);
        public override int GetHashCode() => id.GetHashCode();
        public override bool Equals(object other) => other is HandleValue value && value.id == id;
        public override string ToString() => "NOTestPilot-request-" + id;
    }
}
