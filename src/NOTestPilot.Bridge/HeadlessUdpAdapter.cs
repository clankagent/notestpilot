using System;
using Cysharp.Threading.Tasks;
using HarmonyLib;
using Mirage;
using NuclearOption.Networking;
using NuclearOption.Networking.Authentication;

namespace NOTestPilot;

// The free server build contains the game's client but its stock connect callback
// creates a retail Steam-client callback even for UDP. This tiny test-only adapter
// sends the game's authentication message through its normal authenticator instead.
// The server still runs its original join/build/password validation. Steam transport
// is untouched. This is deliberately labelled a UDP adapter, not a retail client.
internal static class HeadlessUdpAdapter
{
    // With no retail Steam API, the game selects a server-only name path on a
    // client and dereferences a missing Owner. Use the game's display fallback.
    // This changes display names only; it does not supply authentication data.
    internal static bool DisplayName(Player __instance, ref PlayerName __result)
    {
        if (!GameManager.IsHeadless || __instance.IsServer || SteamManager.ClientInitialized) return true;
        __result = PlayerName.FallbackNoSteamId("NOTestPilot UDP actor");
        __result.RebuildCachedNames(__instance.PlayerIndex, __instance.ServerTag);
        AccessTools.Field(typeof(Player), "_playerNameCache").SetValue(__instance, __result);
        return false;
    }

    internal static bool OnConnected(NetworkAuthenticatorNuclearOption __instance, INetworkPlayer player, ref UniTaskVoid __result)
    {
        var network = NetworkManagerNuclearOption.i;
        if (!GameManager.IsHeadless || network == null || SteamManager.ClientInitialized) return true;
        var udp = AccessTools.Field(typeof(NetworkManagerNuclearOption), "udpTransport").GetValue(network);
        if (!ReferenceEquals(network.Client.SocketFactory, udp)) return true;

        Register<NetworkAuthenticatorNuclearOption.PasswordChallenge>(network.Client, __instance, "HandlePasswordChallenge");
        Register<NetworkAuthenticatorNuclearOption.AuthFailReason>(network.Client, __instance, "HandleAuthFailReason");
        Register<NetworkAuthenticatorNuclearOption.BuildHashMismatch>(network.Client, __instance, "HandleBuildHashMismatch");
        uint hash = (uint)AccessTools.Method(typeof(NetworkAuthenticatorNuclearOption), "GetBuildHash").Invoke(null, null);
        __instance.SendAuthentication(network.Client, new NetworkAuthenticatorNuclearOption.AuthMessage
        {
            BuildHash = hash,
            JoinAs = NuclearOption.DedicatedServer.DedicatedServerManager.IsRunning && player.IsHost
                ? PlayerType.DedicatedServer : PlayerType.Normal,
            SteamName = "NOTestPilot UDP actor"
        });
        __result = default;
        return false;
    }

    private static void Register<T>(NetworkClient client, NetworkAuthenticatorNuclearOption authenticator, string method)
    {
        var callback = (MessageDelegateWithPlayer<T>)Delegate.CreateDelegate(typeof(MessageDelegateWithPlayer<T>),
            authenticator, AccessTools.Method(typeof(NetworkAuthenticatorNuclearOption), method));
        client.MessageHandler.RegisterHandler(callback, allowUnauthenticated: true);
    }
}
