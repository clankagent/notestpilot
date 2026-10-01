using System;
using HarmonyLib;
using Newtonsoft.Json.Linq;
using UnityEngine;
using Player = NuclearOption.Networking.Player;

namespace NOTestPilot;

// A test pilot supplies raw controls; the game's filter, simulation, ownership
// and transform replication still execute. This is not a human-input/UI test.
internal static class ControlLease
{
    private static Aircraft aircraft;
    private static float until;
    private static float pitch, roll, yaw, throttle, brake;
    private static bool fire;
    private static FlightPlan flight;
    private static float customAxisBeforeFlight;
    private static bool skipNextFilter;
    private static float filteredAt;
    private static long duplicateFiltersSkipped;
    private static long applications;
    private static readonly System.Reflection.FieldInfo PilotField = AccessTools.Field(typeof(PilotBaseState), "pilot");

    internal static object Set(Player player, JObject args)
    {
        if (player.Aircraft == null) throw new InvalidOperationException("Player has no aircraft");
        float duration = Number(args, "seconds", 10, 0.1f, 60);
        float newPitch = Number(args, "pitch", 0, -1, 1);
        float newRoll = Number(args, "roll", 0, -1, 1);
        float newYaw = Number(args, "yaw", 0, -1, 1);
        float newThrottle = Number(args, "throttle", 0, 0, 1);
        float newBrake = Number(args, "brake", 0, 0, 1);
        bool newFire = (bool?)args["fire"] ?? false;
        Release(); // A new aircraft must not leave the previous lease's controls behind.
        pitch = newPitch; roll = newRoll; yaw = newYaw;
        throttle = newThrottle; brake = newBrake;
        fire = newFire;
        aircraft = player.Aircraft;
        until = Time.realtimeSinceStartup + duration;
        applications = 0;
        return new { accepted = true, seconds = duration, aircraftNetId = aircraft.NetId };
    }

    private static float Number(JObject args, string key, float fallback, float min, float max)
    {
        float value = (float?)args[key] ?? fallback;
        if (float.IsNaN(value) || float.IsInfinity(value) || value < min || value > max)
            throw new ArgumentException("Invalid " + key);
        return value;
    }

    internal static object Fly(Player player, JObject args)
    {
        if (player.Aircraft == null) throw new InvalidOperationException("Player has no aircraft");
        float duration = Number(args, "seconds", 60, 0.1f, 60);
        bool newFire = (bool?)args["fire"] ?? false;
        // Renewal keeps the route and taxi phase instead of restarting the script.
        var next = ReferenceEquals(aircraft, player.Aircraft) && flight != null ? flight : new FlightPlan(player.Aircraft);
        float restoreAxis = ReferenceEquals(aircraft, player.Aircraft) && flight != null
            ? customAxisBeforeFlight : player.Aircraft.GetInputs().customAxis1;
        Release();
        aircraft = player.Aircraft;
        flight = next;
        customAxisBeforeFlight = restoreAxis;
        fire = newFire;
        until = Time.realtimeSinceStartup + duration;
        return new { accepted = true, seconds = duration, aircraftNetId = aircraft.NetId };
    }

    internal static bool BeforeControls(PilotPlayerState __instance, System.Reflection.MethodBase __originalMethod)
    {
        if (aircraft == null || Time.realtimeSinceStartup >= until) { Release(); return true; }
        // The player state initializes its pilot, but does not populate the AI
        // state's cached aircraft field. Follow the actual player pilot instead.
        var pilot = (Pilot)PilotField.GetValue(__instance);
        if (pilot == null || !ReferenceEquals(pilot.aircraft, aircraft)) return true;
        if (flight != null)
        {
            // Steering helpers use FixedDeltaTime; do not also run them in Update.
            if (__originalMethod.Name == "PlayerAxisControls")
            {
                flight.Apply();
                skipNextFilter = flight.Filtered;
                filteredAt = Time.fixedTime;
                if (fire && flight.Airborne) pilot.Fire();
                applications++;
            }
            return false;
        }
        var inputs = aircraft.GetInputs();
        inputs.pitch = pitch; inputs.roll = roll; inputs.yaw = yaw;
        inputs.throttle = throttle; inputs.brake = brake;
        if (fire) pilot.Fire(); // Original safety, ammunition and firing cadence apply.
        applications++;
        return false;
    }

    internal static void Tick()
    {
        // Expire even if the pilot has left its player state (ejection/death).
        if (aircraft == null || Time.realtimeSinceStartup >= until) Release();
    }

    internal static bool BeforeFilter(Aircraft __instance)
    {
        // Native AutoAim already filtered this script's controls. Avoid running
        // the same stateful filter again when PlayerAxisControls returns.
        if (skipNextFilter && ReferenceEquals(__instance, aircraft) && Time.fixedTime == filteredAt)
        { skipNextFilter = false; duplicateFiltersSkipped++; return false; }
        return true;
    }

    internal static object Snapshot() => new
    {
        active = aircraft != null && Time.realtimeSinceStartup < until,
        aircraftNetId = aircraft != null ? (uint?)aircraft.NetId : null,
        secondsRemaining = Math.Max(0, until - Time.realtimeSinceStartup), applications,
        flight = flight?.Snapshot(), duplicateFiltersSkipped
    };

    internal static void Release()
    {
        if (aircraft != null)
        {
            var inputs = aircraft.GetInputs();
            inputs.pitch = inputs.roll = inputs.yaw = inputs.throttle = inputs.brake = 0;
            if (flight != null) inputs.customAxis1 = customAxisBeforeFlight;
        }
        aircraft = null;
        until = 0;
        fire = false;
        flight = null;
        skipNextFilter = false;
    }
}
