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

    internal static void Install(Harmony harmony)
    {
        foreach (var name in new[] { "StartEjectionSequence", "ReturnToInventory" })
            harmony.Patch(AccessTools.Method(typeof(Aircraft), name),
                prefix: new HarmonyMethod(typeof(Observation), nameof(AircraftEvent)));
        harmony.Patch(AccessTools.Method(typeof(Unit), "DisableUnit"),
            prefix: new HarmonyMethod(typeof(Observation), nameof(UnitEvent)));
        harmony.Patch(AccessTools.Method(typeof(Unit), "Damage"),
            prefix: new HarmonyMethod(typeof(Observation), nameof(DamageEvent)));
        harmony.Patch(AccessTools.Method(typeof(AeroPart), "OnCollisionEnter"),
            prefix: new HarmonyMethod(typeof(Observation), nameof(CollisionEvent)));
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

    private static void CollisionEvent(AeroPart __instance, Collision collision)
    {
        if (!(__instance.parentUnit is Aircraft aircraft) || aircraft.Player == null || !aircraft.LocalSim) return;
        // Discard gentle wheel/ground contacts; record hard contacts without
        // changing the original collision or damage calculation.
        if (collision.impulse.magnitude < 1000 || collision.relativeVelocity.sqrMagnitude < 25) return;
        var collider = collision.collider;
        var part = collider != null ? collider.GetComponentInParent<UnitPart>() : null;
        var other = part != null ? part.parentUnit : collider?.GetComponentInParent<Unit>();
        Record(aircraft, "Collision", new {
            collider = collider?.name, otherUnitNetId = other != null ? (uint?)other.NetId : null,
            otherUnitType = other?.definition?.jsonKey, otherPart = part?.name,
            impulse = collision.impulse.magnitude, relativeSpeed = collision.relativeVelocity.magnitude
        });
    }

    private static void Record(Aircraft aircraft, string action, object damage = null)
    {
        if (aircraft == null || aircraft.Player == null) return;
        // Only infrequent lifecycle events get a stack; no per-frame stacks.
        var callers = new StackTrace(false).GetFrames()?.Skip(2).Take(8)
            .Select(f => f.GetMethod().DeclaringType?.FullName + "." + f.GetMethod().Name).ToArray();
        var position = aircraft.GlobalPosition();
        events.Enqueue(new { seq = ++sequence, seconds = Time.realtimeSinceStartup,
            action, aircraftNetId = aircraft.NetId, playerNetId = aircraft.Player.NetId,
            state = aircraft.unitState.ToString(), aircraft.disabled, aircraft.Ignition,
            aircraft.speed, radarAltitude = aircraft.radarAlt,
            position = new[] { position.x, position.y, position.z },
            pilots = aircraft.pilots.Select(p => new { p.dead, p.ejected, state = p.GetCurrentState() }).ToArray(), callers, damage });
        while (events.Count > 64) events.Dequeue();
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
        // Cumulative samples include loading/menu time. Runner uses window deltas.
        frames, fixedSteps, frameBuckets = frameBuckets.ToArray(), maximumFrameMs,
        realtimeSeconds = Time.realtimeSinceStartup,
        gameSeconds = Time.time, fixedDeltaSeconds = Time.fixedDeltaTime, timeScale = Time.timeScale,
        processCpuSeconds = process.TotalProcessorTime.TotalSeconds,
        residentBytes = process.WorkingSet64
        };
    }
}
