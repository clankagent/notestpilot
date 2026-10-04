using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using HarmonyLib;
using NuclearOption.Networking;
using UnityEngine;

namespace NOTestPilot.Runtime;

// Native Player bookkeeping with no connection, authentication, or Steam identity.
// Every exception to the game's remote-player lifecycle is keyed by this registry.
public static class MockPlayers
{
    private sealed class Record
    {
        public string Name;
        public string CreationId;
        public int Index;
        public bool Registered;
        public bool Removing;
    }

    private static readonly Dictionary<Player, Record> records = new Dictionary<Player, Record>();
    // Disposable-runtime lifetime ledger: retired/failed creation attempts are
    // never replayed. Bound its memory; start a fresh runtime after this limit.
    private const int MaximumCreations = 4096;
    private static readonly HashSet<string> creationIds = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    private static int nextIndex = 10000;
    private static bool initialized;
    private static readonly PropertyInfo playerRefProperty = typeof(Player).GetProperty("PlayerRef");

    public static IEnumerable<Player> Players => records.Keys.Where(p => p != null && !records[p].Removing).ToArray();
    public static bool IsMock(Player player) => !ReferenceEquals(player, null) && records.ContainsKey(player);
    public static string Name(Player player) => IsMock(player) ? records[player].Name : null;
    public static string CreationId(Player player) => IsMock(player) ? records[player].CreationId : null;

    public static void Initialize(Harmony harmony)
    {
        if (initialized) return;
        // Validate the reflected data dependencies before installing any patches.
        if (playerRefProperty?.GetSetMethod(true) == null || playerRefProperty.PropertyType != typeof(PlayerRef))
            throw new MissingMemberException("Player.PlayerRef setter does not match reviewed build");
        var playerPrefab = AccessTools.Field(typeof(NetworkManagerNuclearOption), "gamePlayerPrefab");
        if (playerPrefab == null || playerPrefab.FieldType != typeof(Player))
            throw new MissingFieldException("NetworkManagerNuclearOption.gamePlayerPrefab does not match reviewed build");
        if (AccessTools.Field(typeof(Player), "_playerNameCache")?.FieldType != typeof(PlayerName) ||
            AccessTools.Field(typeof(FactionHQ), "reserveRequests") == null)
            throw new MissingFieldException("Mock bookkeeping fields do not match reviewed build");
        Patch(harmony, typeof(Player), "OnStartServer", nameof(BeforePlayerServer));
        Patch(harmony, typeof(BasePlayer), "OnStartServer", nameof(BeforeBaseServer));
        Patch(harmony, typeof(Player), "OnStartClient", nameof(BeforePlayerClient));
        Patch(harmony, typeof(Player), "OnStartLocalPlayer", nameof(BeforePlayerLocal));
        Patch(harmony, typeof(Player), "GetPlayerName", nameof(BeforePlayerName));
        Patch(harmony, typeof(Player), "OnStopClient", nameof(BeforePlayerLocal));
        Patch(harmony, typeof(FactionHQ), "AddPlayer", nameof(BeforeFactionAdd));
        Patch(harmony, typeof(FactionHQ), "RemovePlayer", nameof(BeforeFactionRemove));
        Patch(harmony, typeof(FactionHQ), "RequestTrackingStates", nameof(BeforeTracking));
        Patch(harmony, typeof(Aircraft), "CheckIfLocalSim", nameof(BeforeLocalSim));
        Patch(harmony, typeof(Aircraft), "SetupLocalPlayerAndUI", nameof(BeforeAircraftUi));
        harmony.Patch(AccessTools.Method(typeof(Player), "OnDestroy"),
            postfix: new HarmonyMethod(typeof(MockPlayers), nameof(AfterDestroyed)));
        initialized = true;
    }

    private static void Patch(Harmony harmony, Type type, string method, string prefix)
    {
        var target = AccessTools.Method(type, method) ?? throw new MissingMethodException(type.FullName, method);
        harmony.Patch(target, prefix: new HarmonyMethod(typeof(MockPlayers), prefix));
    }

    public static Player Create(string name, string faction, string creationId)
    {
        RequireServer();
        if (!initialized) throw new InvalidOperationException("Mock player adapter is not initialized");
        if (creationId == null || creationId.Length != 32 || !Guid.TryParseExact(creationId, "N", out var creationGuid))
            throw new ArgumentException("creationId requires a GUID in N format (32 hexadecimal characters)");
        creationId = creationGuid.ToString("N");
        if (creationIds.Contains(creationId)) throw new InvalidOperationException("creationId was already used in this runtime; create commands are not replayed");
        if (creationIds.Count >= MaximumCreations) throw new InvalidOperationException("Disposable runtime creation limit reached (4096)");
        if (string.IsNullOrWhiteSpace(name) || name.Length > 64) throw new ArgumentException("Name must contain 1-64 characters");
        if (Players.Any(p => string.Equals(Name(p), name, StringComparison.OrdinalIgnoreCase)))
            throw new InvalidOperationException("A mock player already has that name");
        var hq = FactionRegistry.HqFromName(faction);
        if (hq == null) throw new ArgumentException("Unknown faction: " + faction);
        var network = NetworkManagerNuclearOption.i;
        var prefab = (Player)AccessTools.Field(typeof(NetworkManagerNuclearOption), "gamePlayerPrefab").GetValue(network);
        // Reserve before native instantiation. Keep it even if initialization
        // fails or cleanup removes the actor: uncertain creates must not repeat.
        creationIds.Add(creationId);
        var player = UnityEngine.Object.Instantiate(prefab);
        records.Add(player, new Record { Name = name, CreationId = creationId, Index = nextIndex++ });
        try
        {
            player.NetworkIsHostPlayer = false;
            // Spawn a server identity, NOT AddCharacter: there is no INetworkPlayer.
            network.ServerObjectManager.Spawn(player.Identity);
            Register(player);
            player.SetPlayerIndex(records[player].Index);
            player.SetServerTag("MOCK");
            player.SetFaction(hq, skipPreventJoin: true);
            if (player.HQ != hq) throw new InvalidOperationException("Mock faction initialization failed");
            if (player.Owner != null) throw new InvalidOperationException("Mock acquired an unexpected connection owner");
            return player;
        }
        catch
        {
            Remove(player);
            throw;
        }
    }

    public static Aircraft Spawn(Player player, string aircraftType, GlobalPosition position, Quaternion rotation, Vector3 velocity)
    {
        RequireServer();
        if (!IsMock(player) || records[player].Removing) throw new ArgumentException("Not an active mock player");
        if (player.Aircraft != null && !player.Aircraft.disabled)
            throw new InvalidOperationException("Remove the existing aircraft before spawning another");
        if (player.Owner != null || player.HQ == null) throw new InvalidOperationException("Mock ownership/faction invariant failed");
        // Public task types are the native lookup keys exposed by status.
        var definition = Encyclopedia.Lookup.TryGetValue(aircraftType, out var entry)
            ? entry as AircraftDefinition : null;
        if (definition == null) throw new ArgumentException("Unknown aircraft: " + aircraftType);
        var loadouts = definition.aircraftParameters.loadouts;
        if (loadouts == null || loadouts.Count == 0) throw new InvalidOperationException("Aircraft has no native loadout");
        var loadout = loadouts[Math.Min(1, loadouts.Count - 1)];
        if (player.Aircraft != null) RemoveAircraft(player);
        // Passing the mock preserves native PlayerRef/SetAircraft/persistent-player
        // linkage. Its null Owner means no remote authority is assigned. The
        // CheckIfLocalSim prefix selects server simulation on the FIRST init.
        var aircraft = NetworkSceneSingleton<Spawner>.i.SpawnAircraft(player, definition.unitPrefab,
            loadout, 1f, default(LiveryKey), position, rotation, velocity, null,
            player.HQ, Name(player), 1f, 0.5f);
        return aircraft;
    }

    public static void Remove(Player player)
    {
        if (!IsMock(player) || records[player].Removing) return;
        records[player].Removing = true;
        var network = NetworkManagerNuclearOption.i;
        RemoveAircraft(player);
        if (player.HQ != null && network.Server.Active) player.HQ.RemovePlayer(player);
        if (records[player].Registered) UnitRegistry.RemovePlayer(player.PlayerRef);
        if (network.Server.Active && player.Identity.IsSpawned) network.ServerObjectManager.Destroy(player.Identity);
        else UnityEngine.Object.Destroy(player.gameObject);
        // Keep marker through deferred Unity destruction/native OnDestroy.
    }

    private static void RemoveAircraft(Player player)
    {
        var network = NetworkManagerNuclearOption.i;
        if (player.Aircraft != null)
        {
            var aircraft = player.Aircraft;
            DirectedPilot.For(aircraft).Cancel();
            aircraft.NetworkplayerRef = PlayerRef.Invalid;
            if (UnitRegistry.TryGetPersistentUnit(aircraft.persistentID, out var persistent)) persistent.player = null;
            player.RemoveAircraft(aircraft);
            if (network.Server.Active && aircraft.Identity.IsSpawned) network.ServerObjectManager.Destroy(aircraft.Identity);
            else UnityEngine.Object.Destroy(aircraft.gameObject);
        }
    }

    public static void Cleanup()
    {
        foreach (var player in records.Keys.ToArray())
        {
            if (player == null) records.Remove(player);
            else Remove(player);
        }
    }

    private static void RequireServer()
    {
        if (NetworkManagerNuclearOption.i == null || !NetworkManagerNuclearOption.i.Server.Active)
            throw new InvalidOperationException("Mock players require an active disposable server");
    }

    private static void Register(Player player)
    {
        var record = records[player];
        if (record.Registered) return;
        if (player.NetId == 0) throw new InvalidOperationException("Mock identity has no network id");
        var reference = new PlayerRef(player);
        playerRefProperty.GetSetMethod(true).Invoke(player, new object[] { reference });
        UnitRegistry.AddPlayer(reference, player);
        record.Registered = true;
    }

    private static bool BeforeBaseServer(BasePlayer __instance) => !(__instance is Player player && IsMock(player));
    private static bool BeforePlayerServer(Player __instance) => !IsMock(__instance);
    private static bool BeforePlayerLocal(Player __instance) => !IsMock(__instance);
    private static bool BeforePlayerClient(Player __instance)
    {
        if (!IsMock(__instance)) return true;
        Register(__instance);
        return false;
    }
    private static bool BeforePlayerName(Player __instance, ref PlayerName __result)
    {
        if (!IsMock(__instance)) return true;
        __result = PlayerName.FallbackNoSteamId(Name(__instance));
        __result.RebuildCachedNames(__instance.PlayerIndex, "MOCK");
        AccessTools.Field(typeof(Player), "_playerNameCache").SetValue(__instance, __result);
        return false;
    }
    private static bool BeforeFactionAdd(FactionHQ __instance, Player player)
    {
        if (!IsMock(player)) return true;
        if (!__instance.factionPlayers.Contains(player.PlayerRef)) __instance.factionPlayers.Add(player.PlayerRef);
        return false; // No authenticated save data, join allowance, or join UI.
    }
    private static bool BeforeFactionRemove(FactionHQ __instance, Player player)
    {
        if (!IsMock(player)) return true;
        ((IDictionary)AccessTools.Field(typeof(FactionHQ), "reserveRequests").GetValue(__instance)).Remove(player);
        __instance.factionPlayers.Remove(player.PlayerRef);
        return false; // No save-back to nonexistent authentication data.
    }
    private static bool BeforeTracking(Player requestingPlayer) => !IsMock(requestingPlayer);
    private static bool BeforeLocalSim(Aircraft __instance, ref bool __result)
    {
        if (!IsMock(__instance.Player)) return true;
        __result = __instance.IsServer;
        return false;
    }
    private static bool BeforeAircraftUi(Aircraft __instance) => !IsMock(__instance.Player);
    private static void AfterDestroyed(Player __instance)
    {
        if (!IsMock(__instance)) return;
        if (records[__instance].Registered) UnitRegistry.RemovePlayer(__instance.PlayerRef);
        records.Remove(__instance);
    }
}
