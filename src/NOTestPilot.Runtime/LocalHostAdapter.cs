using System;
using Cysharp.Threading.Tasks;
using HarmonyLib;
using Mirage;
using NuclearOption.DedicatedServer;
using NuclearOption.Networking;
using NuclearOption.Networking.Authentication;

namespace NOTestPilot.Runtime;

// The server-only build has no retail Steam client. This narrowly adapts its
// LOCAL host callback, retaining native build/password/permission validation.
// It never supplies a Steam ID or authenticates a remote test player.
internal static class LocalHostAdapter
{
    internal static long AuthenticationAttempts { get; private set; }
    internal static long NameFallbacks { get; private set; }

    internal static void Initialize(Harmony harmony)
    {
        var connected = AccessTools.Method(typeof(NetworkAuthenticatorNuclearOption), "OnClientConnected",
            new[] { typeof(INetworkPlayer) });
        if (connected == null || connected.ReturnType != typeof(UniTaskVoid))
            throw new MissingMethodException("Reviewed local-host authentication callback not found");
        harmony.Patch(connected, prefix: new HarmonyMethod(typeof(LocalHostAdapter), nameof(BeforeConnected)));
        harmony.Patch(AccessTools.Method(typeof(Player), nameof(Player.GetPlayerName)),
            prefix: new HarmonyMethod(typeof(LocalHostAdapter), nameof(BeforeName)));
    }

    private static bool Enabled() => Environment.GetEnvironmentVariable("NOTESTPILOT_ENABLE") == "1"
        && Environment.GetEnvironmentVariable("NOTESTPILOT_LOCAL_HOST_ADAPTER") == "1"
        && GameManager.IsHeadless && !SteamManager.ClientInitialized;

    private static bool EligibleNetwork(NetworkManagerNuclearOption network)
    {
        if (!Enabled() || network == null || !network.Server.Active || !network.Client.IsHost) return false;
        var udp = AccessTools.Field(typeof(NetworkManagerNuclearOption), "udpTransport").GetValue(network);
        return ReferenceEquals(network.Client.SocketFactory, udp);
    }

    private static bool BeforeConnected(NetworkAuthenticatorNuclearOption __instance, INetworkPlayer player, ref UniTaskVoid __result)
    {
        var network = NetworkManagerNuclearOption.i;
        if (!EligibleNetwork(network) || player == null || !player.IsHost || !ReferenceEquals(network.Client.Player, player)) return true;
        string password = Environment.GetEnvironmentVariable("NOTESTPILOT_TOKEN");
        if (password == null || password.Length < 32) throw new InvalidOperationException("Local host lab password is missing");
        // StartHost configures the server password only. Use the SAME explicit
        // lab password on its local client so the original challenge can pass.
        __instance.SetClientPassword(password);
        Register<NetworkAuthenticatorNuclearOption.PasswordChallenge>(network.Client, __instance, "HandlePasswordChallenge");
        Register<NetworkAuthenticatorNuclearOption.AuthFailReason>(network.Client, __instance, "HandleAuthFailReason");
        Register<NetworkAuthenticatorNuclearOption.BuildHashMismatch>(network.Client, __instance, "HandleBuildHashMismatch");
        uint hash = (uint)AccessTools.Method(typeof(NetworkAuthenticatorNuclearOption), "GetBuildHash").Invoke(null, null);
        AuthenticationAttempts++;
        __instance.SendAuthentication(network.Client, new NetworkAuthenticatorNuclearOption.AuthMessage
        {
            BuildHash = hash,
            JoinAs = DedicatedServerManager.IsRunning ? PlayerType.DedicatedServer : PlayerType.Normal,
            SteamName = "NOTestPilot local host"
        });
        __result = default;
        return false;
    }

    private static void Register<T>(NetworkClient client, NetworkAuthenticatorNuclearOption authenticator, string method)
    {
        var target = AccessTools.Method(typeof(NetworkAuthenticatorNuclearOption), method,
            new[] { typeof(INetworkPlayer), typeof(T) }) ?? throw new MissingMethodException(method);
        var callback = (MessageDelegateWithPlayer<T>)Delegate.CreateDelegate(typeof(MessageDelegateWithPlayer<T>), authenticator, target);
        client.MessageHandler.RegisterHandler(callback, allowUnauthenticated: true);
    }

    private static bool BeforeName(Player __instance, ref PlayerName __result)
    {
        var network = NetworkManagerNuclearOption.i;
        if (!EligibleNetwork(network) || __instance == null || __instance.Owner == null || !__instance.Owner.IsHost
            || !ReferenceEquals(network.Client.Player?.Identity, __instance.Identity)) return true;
        __result = PlayerName.FallbackNoSteamId("NOTestPilot local host");
        __result.RebuildCachedNames(__instance.PlayerIndex, __instance.ServerTag);
        AccessTools.Field(typeof(Player), "_playerNameCache").SetValue(__instance, __result);
        NameFallbacks++;
        return false;
    }
}
