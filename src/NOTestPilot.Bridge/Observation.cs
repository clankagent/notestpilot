using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Reflection;
using HarmonyLib;
using UnityEngine;

namespace NOTestPilot;

// Read-only instrumentation, enabled only with the disposable bridge. Events
// retain enough context to distinguish a normal recovery from a lost connection.
internal static class Observation
{
    private static readonly Queue<object> events = new Queue<object>();
    private static long sequence, frames, fixedSteps;
    private static double previousFrame;
    private static readonly long[] frameBuckets = new long[6];
    private static double maximumFrameMs;
    private static readonly Process process = Process.GetCurrentProcess();
    private static readonly Dictionary<uint, float> lastDamage = new Dictionary<uint, float>();
    private static readonly Dictionary<uint, object> firstAirborneContacts = new Dictionary<uint, object>();
    // One physics step can emit more than the rolling queue's 64 entries.
    // Retain the initiating damage/joint event independently of that burst.
    private static readonly Dictionary<string, object> firstDamageEvents = new Dictionary<string, object>();
    private static readonly Queue<string> firstDamageKeys = new Queue<string>();
    private static readonly Queue<object> sceneLoadEvents = new Queue<object>();
    private static long mapTypeChecks;

    internal static void Install(Harmony harmony)
    {
        harmony.Patch(AccessTools.Method(typeof(NuclearOption.SavedMission.Mission), "OnSceneLoaded"),
            prefix: new HarmonyMethod(typeof(Observation), nameof(SceneLoadEvent)));
        harmony.Patch(AccessTools.Method(typeof(NuclearOption.SceneLoading.MapLoader), "GetObjectType"),
            prefix: new HarmonyMethod(typeof(Observation), nameof(MapTypeEvent)));
        foreach (var name in new[] { "StartEjectionSequence", "ReturnToInventory", "SetComplexPhysics", "SetSimplePhysics" })
            harmony.Patch(AccessTools.Method(typeof(Aircraft), name),
                prefix: new HarmonyMethod(typeof(Observation), nameof(AircraftEvent)));
        harmony.Patch(AccessTools.Method(typeof(Unit), "DisableUnit"),
            prefix: new HarmonyMethod(typeof(Observation), nameof(UnitEvent)));
        harmony.Patch(AccessTools.Method(typeof(Unit), "Damage"),
            prefix: new HarmonyMethod(typeof(Observation), nameof(DamageEvent)));
        harmony.Patch(AccessTools.Method(typeof(AeroPart), "OnCollisionEnter"),
            prefix: new HarmonyMethod(typeof(Observation), nameof(CollisionEvent)));
        harmony.Patch(AccessTools.Method(typeof(Unit), "DetachPart"),
            prefix: new HarmonyMethod(typeof(Observation), nameof(DetachEvent)));
        harmony.Patch(AccessTools.Method(typeof(AeroPart), "BreakAllJoints"),
            prefix: new HarmonyMethod(typeof(Observation), nameof(JointsEvent)));
        harmony.Patch(AccessTools.Method(typeof(UnitPart), "OnJointBreak"),
            prefix: new HarmonyMethod(typeof(Observation), nameof(JointBreakEvent)));
        harmony.Patch(AccessTools.Method(typeof(UnitPart), "TakeDamage"),
            prefix: new HarmonyMethod(typeof(Observation), nameof(PartTakeDamageEvent)));
        harmony.Patch(AccessTools.Method(typeof(UnitPart), "ApplyDamage"),
            prefix: new HarmonyMethod(typeof(Observation), nameof(PartApplyDamageEvent)));
    }

    private static void SceneLoadEvent()
    {
        // Read the registry before the original mission handler. Never remove
        // entries or replace the original handler, including when it will fail.
        try
        {
            var missing = UnitRegistry.customIDLookup
                .Where(p => p.Value == null || p.Value.Identity == null)
                .Take(16).Select(p => new { key = p.Key, unitMissing = p.Value == null,
                    identityMissing = p.Value != null && p.Value.Identity == null }).ToArray();
            SceneEvent(new { action = "BeforeMissionSceneLoaded", seconds = Time.realtimeSinceStartup,
                registeredUnits = UnitRegistry.allUnits.Count,
                customEntries = UnitRegistry.customIDLookup.Count, missing });
        }
        catch (Exception error)
        {
            SceneEvent(new { action = "SceneDiagnosticFailed", error = error.GetType().FullName });
        }
    }

    private static void MapTypeEvent(Mirage.NetworkIdentity identity)
    {
        mapTypeChecks++;
        if (identity != null) return;
        SceneEvent(new { action = "MissingMapIdentity", seconds = Time.realtimeSinceStartup,
            managedNull = ReferenceEquals(identity, null) });
        // The original GetObjectType still executes and reports its exception.
    }

    private static void SceneEvent(object snapshot)
    {
        sceneLoadEvents.Enqueue(snapshot);
        while (sceneLoadEvents.Count > 8) sceneLoadEvents.Dequeue();
    }

    private static void AircraftEvent(Aircraft __instance, MethodBase __originalMethod)
        => Record(__instance, __originalMethod.Name);
    private static void UnitEvent(Unit __instance, MethodBase __originalMethod)
    { if (__instance is Aircraft aircraft) Record(aircraft, __originalMethod.Name); }

    private static void DamageEvent(Unit __instance, byte index, DamageInfo damageInfo)
    {
        if (!(__instance is Aircraft aircraft) || aircraft.Player == null) return;
        float now = Time.realtimeSinceStartup;
        if (lastDamage.TryGetValue(aircraft.NetId, out var last) && now - last < 0.5f) return;
        if (lastDamage.Count > 64) lastDamage.Clear();
        lastDamage[aircraft.NetId] = now;
        Record(aircraft, "Damage", new { partIndex = index,
            pierce = damageInfo.pierceDamage.Decompress(), blast = damageInfo.blastDamage.Decompress(),
            fire = damageInfo.fireDamage.Decompress(), impact = damageInfo.impactDamage.Decompress() });
    }

    private static void DetachEvent(Unit __instance, byte partID)
    { if (__instance is Aircraft aircraft) Record(aircraft, "DetachPart", new { partID }); }

    private static void JointsEvent(AeroPart __instance)
    {
        if (__instance.parentUnit is Aircraft aircraft && aircraft.Player != null)
            Record(aircraft, "BreakAllJoints", PartSnapshot(__instance));
    }

    private static void JointBreakEvent(UnitPart __instance, float breakForce)
    {
        if (__instance.parentUnit is Aircraft aircraft && aircraft.Player != null)
        {
            var recorded = Record(aircraft, "OnJointBreak", new { breakForce, part = PartSnapshot(__instance) });
            RetainFirst(aircraft, "OnJointBreak", recorded);
        }
    }

    private static bool HasFirst(Aircraft aircraft, string kind)
        => firstDamageEvents.ContainsKey(aircraft.NetId + ":" + kind);

    private static void RetainFirst(Aircraft aircraft, string kind, object recorded)
    {
        if (recorded == null || HasFirst(aircraft, kind)) return;
        string key = aircraft.NetId + ":" + kind;
        firstDamageEvents.Add(key, recorded);
        firstDamageKeys.Enqueue(key);
        while (firstDamageKeys.Count > 64) firstDamageEvents.Remove(firstDamageKeys.Dequeue());
    }

    private static void PartTakeDamageEvent(UnitPart __instance, float pierceDamage, float blastDamage,
        float amountAffected, float fireDamage, float impactDamage, PersistentID dealerID)
    {
        if (!(__instance.parentUnit is Aircraft aircraft) || aircraft.Player == null
            || HasFirst(aircraft, "PartTakeDamage")
            || !(pierceDamage > 0 || blastDamage > 0 || fireDamage > 0 || impactDamage > 0)) return;
        // Incoming values precede armor calculations. This records the caller;
        // the original method still decides whether any damage is applied.
        RetainFirst(aircraft, "PartTakeDamage", Record(aircraft, "PartTakeDamage", new {
            part = PartSnapshot(__instance), pierceDamage, blastDamage, amountAffected, fireDamage,
            impactDamage, dealerValid = dealerID.IsValid, dealerIsSelf = dealerID == aircraft.persistentID }));
    }

    private static void PartApplyDamageEvent(UnitPart __instance, float netPierceDamage, float netBlastDamage,
        float netFireDamage, float netImpactDamage)
    {
        if (!(__instance.parentUnit is Aircraft aircraft) || aircraft.Player == null) return;
        float total = netPierceDamage + netBlastDamage + netFireDamage + netImpactDamage;
        if (!(total > 0)) return;
        bool destructive = total >= Math.Max(1f, __instance.hitPoints);
        string kind = destructive ? "DestructivePartApplyDamage" : "PartApplyDamage";
        if (HasFirst(aircraft, kind)) return;
        RetainFirst(aircraft, kind, Record(aircraft, kind, new {
            part = PartSnapshot(__instance), netPierceDamage, netBlastDamage, netFireDamage,
            netImpactDamage, hitPointsBefore = __instance.hitPoints, predictedHitPointsAfter = __instance.hitPoints - total }));
    }

    private static object PartSnapshot(UnitPart part)
    {
        if (part == null) return null;
        var body = part.rb;
        var aero = part as AeroPart;
        return new { name = part.name, partID = part.id, part.hitPoints,
            detached = part.IsDetached(), bodyId = body != null ? (int?)body.GetInstanceID() : null,
            bodyName = body != null ? body.name : null, bodyMass = body != null ? (float?)body.mass : null,
            bodyVelocity = body != null ? new[] { body.velocity.x, body.velocity.y, body.velocity.z } : null,
            joints = aero?.Joints?.Where(j => j != null).Select(j => new {
                connectedPart = j.connectedPart != null ? j.connectedPart.name : null,
                connectedPartID = j.connectedPart != null ? (byte?)j.connectedPart.id : null,
                exists = j.joint != null, j.breakForce, j.breakTorque }).ToArray() };
    }

    private static void CollisionEvent(AeroPart __instance, Collision collision)
    {
        if (!(__instance.parentUnit is Aircraft aircraft) || aircraft.Player == null || !aircraft.LocalSim) return;
        // Keep the first contact above ground even if it falls below the hard
        // contact filter. A later breakup must not erase the initiating contact.
        bool firstAirborne = aircraft.radarAlt > 80 && collision.impulse.sqrMagnitude > 0
            && !firstAirborneContacts.ContainsKey(aircraft.NetId);
        if (!firstAirborne && (collision.impulse.magnitude < 1000 || collision.relativeVelocity.sqrMagnitude < 25)) return;
        var collider = collision.collider;
        var part = collider != null ? collider.GetComponentInParent<UnitPart>() : null;
        var other = part != null ? part.parentUnit : collider?.GetComponentInParent<Unit>();
        var recorded = Record(aircraft, "Collision", new {
            sourcePart = PartSnapshot(__instance), otherPartState = PartSnapshot(part),
            colliderId = collider != null ? (int?)collider.GetInstanceID() : null,
            otherBodyId = collision.rigidbody != null ? (int?)collision.rigidbody.GetInstanceID() : null,
            collider = collider?.name, otherUnitNetId = other != null ? (uint?)other.NetId : null,
            otherUnitType = other?.definition?.jsonKey, otherPart = part?.name,
            impulse = collision.impulse.magnitude, relativeSpeed = collision.relativeVelocity.magnitude
        });
        if (firstAirborne && recorded != null)
        {
            if (firstAirborneContacts.Count >= 64) firstAirborneContacts.Clear();
            firstAirborneContacts[aircraft.NetId] = recorded;
        }
    }

    private static object Record(Aircraft aircraft, string action, object damage = null)
    {
        if (aircraft == null || aircraft.Player == null) return null;
        // Only infrequent lifecycle events get a stack; no per-frame stacks.
        var callers = new StackTrace(false).GetFrames()?.Skip(2).Take(8)
            .Select(f => f.GetMethod().DeclaringType?.FullName + "." + f.GetMethod().Name).ToArray();
        var position = aircraft.GlobalPosition();
        var view = SceneSingleton<CameraStateManager>.i;
        var recorded = new { seq = ++sequence, seconds = Time.realtimeSinceStartup,
            action, aircraftNetId = aircraft.NetId, playerNetId = aircraft.Player.NetId,
            state = aircraft.unitState.ToString(), aircraft.disabled, aircraft.Ignition,
            aircraft.speed, radarAltitude = aircraft.radarAlt, aircraft.gForce, aircraft.simplePhysics,
            cameraDistance = view != null ? (float?)Vector3.Distance(view.transform.position, aircraft.transform.position) : null,
            position = new[] { position.x, position.y, position.z },
            pilots = aircraft.pilots.Select(p => new { p.dead, p.ejected, state = p.GetCurrentState() }).ToArray(), callers, damage };
        events.Enqueue(recorded);
        while (events.Count > 64) events.Dequeue();
        return recorded;
    }

    internal static void Frame()
    {
        double now = Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency;
        if (previousFrame > 0)
        {
            double elapsed = (now - previousFrame) * 1000;
            maximumFrameMs = Math.Max(maximumFrameMs, elapsed);
            int bucket = elapsed <= 16.7 ? 0 : elapsed <= 25 ? 1 : elapsed <= 33.4 ? 2 : elapsed <= 50 ? 3 : elapsed <= 100 ? 4 : 5;
            frameBuckets[bucket]++;
        }
        previousFrame = now;
        frames++;
    }

    internal static void FixedStep() => fixedSteps++;
    internal static object Snapshot()
    {
        process.Refresh();
        return new {
        eventCount = sequence, events = events.ToArray(),
        firstAirborneContacts = firstAirborneContacts.Values.ToArray(),
        firstDamageEvents = firstDamageEvents.Values.ToArray(),
        mapTypeChecks, sceneLoadEvents = sceneLoadEvents.ToArray(),
        // Cumulative samples include loading/menu time. Runner uses window deltas.
        frames, fixedSteps, frameBuckets = frameBuckets.ToArray(), maximumFrameMs,
        realtimeSeconds = Time.realtimeSinceStartup,
        gameSeconds = Time.time, fixedDeltaSeconds = Time.fixedDeltaTime, timeScale = Time.timeScale,
        processCpuSeconds = process.TotalProcessorTime.TotalSeconds,
        residentBytes = process.WorkingSet64
        };
    }
}
