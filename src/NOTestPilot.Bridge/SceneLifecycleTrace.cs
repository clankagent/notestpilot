using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.IO;
using System.Security.Cryptography;
using HarmonyLib;
using Mirage;
using Cysharp.Threading.Tasks;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace NOTestPilot;
// Opt-in bounded observation only. Prefixes/postfixes never skip or replace originals.
internal static class SceneLifecycleTrace
{
    private const int Cap = 128;
    private static readonly string[] TrainerKeys = { "trainer_1", "trainer_2", "trainer_3", "trainer_4" };
    private static readonly List<object> events = new List<object>();
    private static readonly List<Unit> references = new List<Unit>();
    private static readonly Dictionary<int, Tuple<int?, uint?>> cachedNativeIds = new Dictionary<int, Tuple<int?, uint?>>();
    private static readonly Dictionary<Unit, string> keys = new Dictionary<Unit, string>(ReferenceComparer.Instance);
    private static readonly Dictionary<int, NetworkIdentity> cachedIdentities = new Dictionary<int, NetworkIdentity>();
    private static bool enabled, installationComplete;
    private static long incomingCorrelationSkipped;
    private static long overflow, observerFailures;
    private static string installationError;
    internal static void Install(Harmony harmony)
    {
        if (Environment.GetEnvironmentVariable("NOTESTPILOT_SCENE_LIFECYCLE_TRACE") != "1") return;
        try
        {
            string mirageHash;
            using (var file = File.OpenRead(typeof(ClientObjectManager).Assembly.Location))
            using (var sha = SHA256.Create())
                mirageHash = BitConverter.ToString(sha.ComputeHash(file)).Replace("-", "").ToLowerInvariant();
            if (mirageHash != "d1e0dba87d81a1678d1ecfb6276b5b0d640bcec37b6dda266223f9f48ea34954")
                throw new InvalidOperationException("Unreviewed scene lifecycle Mirage assembly: " + mirageHash);
            var register = Exact(typeof(UnitRegistry), "RegisterCustomID", typeof(string), typeof(Unit));
            var unregister = Exact(typeof(UnitRegistry), "UnregisterUnit", typeof(Unit));
            var clear = Exact(typeof(UnitRegistry), "Clear");
            var destroy = typeof(Unit).GetMethod("OnDestroy", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
            if (destroy == null || destroy.ReturnType != typeof(void) || destroy.GetParameters().Length != 0) throw new MissingMethodException("Unit.OnDestroy signature");
            var incomingDestroy = InstanceExact(typeof(ClientObjectManager), "OnObjectDestroy", typeof(void), typeof(ObjectDestroyMessage));
            var incomingHide = InstanceExact(typeof(ClientObjectManager), "OnObjectHide", typeof(void), typeof(ObjectHideMessage));
            var clientDestroy = InstanceExact(typeof(ClientObjectManager), "DestroyObject", typeof(void), typeof(uint));
            var unspawn = InstanceExact(typeof(ClientObjectManager), "UnSpawn", typeof(void), typeof(NetworkIdentity));
            var serverDestroy = InstanceExact(typeof(ServerObjectManager), "DestroyObject", typeof(void), typeof(NetworkIdentity), typeof(bool));
            var schedule = InstanceExact(typeof(Aircraft), "WaitRemoveAircraft", typeof(UniTask), typeof(float));
            enabled = true;
            Patch(harmony, register, nameof(RegisterBefore), nameof(RegisterAfter));
            Patch(harmony, unregister, nameof(UnregisterBefore), nameof(UnregisterAfter));
            Patch(harmony, clear, nameof(ClearBefore), nameof(ClearAfter));
            Patch(harmony, destroy, nameof(DestroyBefore), nameof(DestroyAfter));
            Prefix(harmony, incomingDestroy, nameof(IncomingDestroy));
            Prefix(harmony, incomingHide, nameof(IncomingHide));
            Prefix(harmony, clientDestroy, nameof(ClientDestroy));
            Prefix(harmony, unspawn, nameof(ClientUnspawn));
            Prefix(harmony, serverDestroy, nameof(ServerDestroy));
            Prefix(harmony, schedule, nameof(RemovalScheduled));
            installationComplete = true;
        }
        catch (Exception e) { installationError = e.GetType().FullName + ": " + e.Message; observerFailures++; }
    }
    private static MethodInfo Exact(Type type, string name, params Type[] arguments)
    {
        var m = type.GetMethod(name, BindingFlags.Public | BindingFlags.Static | BindingFlags.DeclaredOnly, null, arguments, null);
        if (m == null || m.ReturnType != typeof(void)) throw new MissingMethodException(type.FullName, name);
        return m;
    }
    private static MethodInfo InstanceExact(Type type, string name, Type result, params Type[] arguments)
    {
        var m = type.GetMethod(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.DeclaredOnly, null, arguments, null);
        if (m == null || m.ReturnType != result) throw new MissingMethodException(type.FullName, name);
        return m;
    }
    private static void Prefix(Harmony h, MethodInfo m, string prefix) => h.Patch(m, prefix: new HarmonyMethod(typeof(SceneLifecycleTrace), prefix));
    private static void IncomingDestroy(ClientObjectManager __instance, ObjectDestroyMessage msg) => Incoming("Client.OnObjectDestroy", __instance, msg.NetId);
    private static void IncomingHide(ClientObjectManager __instance, ObjectHideMessage msg) => Incoming("Client.OnObjectHide", __instance, msg.NetId);
    private static void ClientDestroy(ClientObjectManager __instance, uint netId) => Incoming("Client.DestroyObject", __instance, netId);
    private static void Incoming(string action, ClientObjectManager manager, uint netId)
    {
        try
        {
            // A number alone is insufficient: require the current world identity to
            // be the exact reference captured from a live trainer registration.
            if (netId == 0) return;
            bool candidate = cachedNativeIds.Values.Any(x => x.Item2 == netId);
            if (!candidate) return;
            NetworkIdentity identity;
            if (manager.Client == null || !manager.Client.World.TryGetIdentity(netId, out identity)) { incomingCorrelationSkipped++; return; }
            if (!IdentityEvent(action, identity, new { messageNetId = netId, correlation = "current-world-reference" })) incomingCorrelationSkipped++;
        }
        catch { observerFailures++; }
    }
    private static void ClientUnspawn(NetworkIdentity identity) { try { IdentityEvent("Client.UnSpawn", identity, null); } catch { observerFailures++; } }
    private static void ServerDestroy(NetworkIdentity identity, bool destroyServerObject) { try { IdentityEvent("Server.DestroyObject", identity, new { destroyServerObject }); } catch { observerFailures++; } }
    private static bool IdentityEvent(string action, NetworkIdentity identity, object detail)
    {
        if (ReferenceEquals(identity, null)) return false;
        foreach (var pair in cachedIdentities)
        {
            if (!ReferenceEquals(pair.Value, identity)) continue;
            Tuple<int?, uint?> cached;
            if (!cachedNativeIds.TryGetValue(pair.Key, out cached)) return false;
            if (identity != null && identity.NetId != 0 && identity.NetId != cached.Item2) { incomingCorrelationSkipped++; return false; }
            Unit unit = references[pair.Key - 1]; string key;
            if (!keys.TryGetValue(unit, out key)) return false;
            Safe(() => {
                bool live = identity != null;
                Record(action, key, unit, true, new { detail, identityLive = live,
                    currentIdentityNetId = live ? identity.NetId : (uint?)null,
                    isSceneObject = live ? identity.IsSceneObject : (bool?)null,
                    prefabHash = live ? identity.PrefabHash : (int?)null,
                    hasAuthority = live ? identity.HasAuthority : (bool?)null });
            });
            return true;
        }
        return false;
    }
    private static void RemovalScheduled(Aircraft __instance, float delay)
    {
        try { string key; if (keys.TryGetValue(__instance, out key)) Safe(() => Record("Aircraft.WaitRemoveAircraft.scheduled", key, __instance, true, new { delay })); }
        catch { observerFailures++; }
    }
    private static void Patch(Harmony h, MethodInfo m, string before, string after) => h.Patch(m,
        prefix: new HarmonyMethod(typeof(SceneLifecycleTrace), before), postfix: new HarmonyMethod(typeof(SceneLifecycleTrace), after));
    private static bool Trainer(string key) => key == "trainer_1" || key == "trainer_2" || key == "trainer_3" || key == "trainer_4";
    private static void Safe(Action action) { try { if (events.Count >= Cap) { overflow++; return; } action(); } catch { observerFailures++; } }
    private static void Remember(Unit unit, string key)
    {
        if (!keys.ContainsKey(unit) && keys.Count >= Cap) throw new InvalidOperationException("bounded trainer key capacity");
        keys[unit] = key;
    }
    private static void RegisterBefore(string customID, Unit unit) { if (Trainer(customID)) Safe(() => { if (!ReferenceEquals(unit, null)) Remember(unit, customID); Record("RegisterCustomID.before", customID, unit, true); }); }
    private static void RegisterAfter(string customID, Unit unit) { if (Trainer(customID)) Safe(() => Record("RegisterCustomID.after", customID, unit, false)); }
    private static void UnitEvent(string action, Unit unit, bool stack)
    {
        try
        {
            if (ReferenceEquals(unit, null)) return;
            string key;
            if (keys.TryGetValue(unit, out key)) { Safe(() => Record(action, key, unit, stack)); return; }
            // Four fixed lookups, never a full registry walk per destroyed unit.
            foreach (string candidate in TrainerKeys)
            {
                Unit mapped;
                if (UnitRegistry.customIDLookup.TryGetValue(candidate, out mapped) && ReferenceEquals(mapped, unit))
                { Safe(() => { Remember(unit, candidate); Record(action, candidate, unit, stack); }); return; }
            }
        }
        catch { observerFailures++; }
    }
    private static void UnregisterBefore(Unit unit) => UnitEvent("UnregisterUnit.before", unit, true);
    private static void UnregisterAfter(Unit unit) => UnitEvent("UnregisterUnit.after", unit, false);
    private static void DestroyBefore(Unit __instance) => UnitEvent("Unit.OnDestroy.before", __instance, true);
    private static void DestroyAfter(Unit __instance) => UnitEvent("Unit.OnDestroy.after", __instance, false);
    private static void ClearBefore() => Safe(() => { foreach (var pair in UnitRegistry.customIDLookup) if (Trainer(pair.Key)) Record("Clear.before", pair.Key, pair.Value, true); });
    private static void ClearAfter() => Safe(() => Record("Clear.after", null, null, false));
    private static int Token(Unit unit)
    {
        if (ReferenceEquals(unit, null)) return 0;
        for (int i = 0; i < references.Count; i++) if (ReferenceEquals(references[i], unit)) return i + 1;
        if (references.Count >= Cap * 2) throw new InvalidOperationException("bounded reference token capacity");
        references.Add(unit); return references.Count;
    }
    private static void Record(string action, string key, Unit unit, bool stack, object detail = null)
    {
        if (events.Count >= Cap) { overflow++; return; }
        Unit mapped = null; bool found = key != null && UnitRegistry.customIDLookup.TryGetValue(key, out mapped);
        bool live = !ReferenceEquals(unit, null) && unit != null;
        // No native identity/property dereference when the Unity object is destroyed.
        int? instanceId = live ? unit.GetInstanceID() : (int?)null;
        uint? netId = live && unit.Identity != null ? unit.NetId : (uint?)null;
        int token = Token(unit);
        if (live && Trainer(key) && action.StartsWith("RegisterCustomID.", StringComparison.Ordinal) && !cachedNativeIds.ContainsKey(token))
        {
            if (cachedNativeIds.Count >= Cap) throw new InvalidOperationException("bounded native ID cache capacity");
            cachedNativeIds[token] = Tuple.Create(instanceId, netId);
            if (unit.Identity != null) cachedIdentities[token] = unit.Identity;
        }
        Tuple<int?, uint?> cached; bool hasCachedNativeIds = cachedNativeIds.TryGetValue(token, out cached);
        events.Add(new { action, key, token, detail, mappedToken = Token(mapped), found,
            sameReference = found && ReferenceEquals(mapped, unit), managedNull = ReferenceEquals(unit, null), unityNull = unit == null,
            mappedUnityNull = mapped == null, inAllUnits = UnitRegistry.allUnits.Any(x => ReferenceEquals(x, unit)),
            currentInstanceId = instanceId, currentNetId = netId, hasCachedNativeIds,
            cachedInstanceId = hasCachedNativeIds ? cached.Item1 : null, cachedNetId = hasCachedNativeIds ? cached.Item2 : null, frame = Time.frameCount, seconds = Time.realtimeSinceStartup,
            role = Environment.GetEnvironmentVariable("NOTESTPILOT_ROLE"), scene = SceneManager.GetActiveScene().name,
            stack = stack ? new System.Diagnostics.StackTrace(2, false).ToString() : null });
    }
    internal static object Snapshot() => new { enabled, installationComplete, cap = Cap, rememberedTrainerReferences = keys.Count, referenceTokens = references.Count, nativeIdCacheEntries = cachedNativeIds.Count, cachedIdentityReferences = cachedIdentities.Count, incomingCorrelationSkipped, overflow, observerFailures, installationError,
        events = events.ToArray(), limits = "Read-only trainer lifecycle; delayed Unity destruction stack may not identify requester. Incoming messages require current-world exact cached identity reference; absent/pending identities are explicitly skipped. Server dispatch can follow native OnDestroy, not prove requester." };
    private sealed class ReferenceComparer : IEqualityComparer<Unit>
    {
        internal static readonly ReferenceComparer Instance = new ReferenceComparer();
        public bool Equals(Unit x, Unit y) => ReferenceEquals(x, y);
        public int GetHashCode(Unit obj) => System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(obj);
    }
}
