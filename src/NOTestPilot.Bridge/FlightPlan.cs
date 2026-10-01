using System;
using UnityEngine;

namespace NOTestPilot;

// A deliberately simple test script. The client still owns/simulates its
// aircraft. Game pathfinding and steering helpers generate control inputs;
// no position, velocity, authority or server AI state is assigned here.
internal sealed class FlightPlan
{
    private readonly Aircraft aircraft;
    private readonly PathfindingAgent taxi;
    private readonly Airbase.Runway.RunwayUsage runway;
    private readonly GlobalPosition[] route;
    private readonly float started;
    private int routeIndex;
    private string phase = "taxi";
    private long ticks;
    internal bool Airborne => phase == "circuit";
    internal bool Filtered { get; private set; }

    internal FlightPlan(Aircraft aircraft)
    {
        this.aircraft = aircraft;
        if (!(aircraft.autopilot is AutopilotPlane))
            throw new InvalidOperationException("Flight script currently supports fixed-wing aircraft only");
        var airbase = aircraft.NetworkspawningHangar?.parentAirbase;
        if (airbase == null) throw new InvalidOperationException("Missing spawning airbase");
        Airbase.Runway best = null;
        bool reverse = false;
        float distance = float.MaxValue;
        foreach (var candidate in airbase.runways)
        {
            if (!candidate.Takeoff || candidate.Length < aircraft.GetAircraftParameters().takeoffDistance) continue;
            var result = candidate.GetDistance(aircraft.transform);
            if (result.Distance < distance) { best = candidate; reverse = result.Reverse; distance = result.Distance; }
        }
        if (best == null) throw new InvalidOperationException("No suitable takeoff runway");
        runway = new Airbase.Runway.RunwayUsage(best, reverse);
        taxi = new PathfindingAgent(aircraft);
        var network = airbase.GetTaxiNetwork();
        if (network != null && network.Exists())
        {
            // The normal game builds taxi nodes only in OnStartServer. This
            // process is a client; construct its local navigation graph too.
            if (network.nodes.Count == 0) network.RegenerateNetwork();
            taxi.Pathfind(network, runway.GetStart().GlobalPosition(), null);
        }
        else taxi.SetMovingTarget(runway.GetStart());
        Vector3 forward = runway.GetDirection().normalized;
        Vector3 right = Vector3.Cross(Vector3.up, forward).normalized;
        var origin = runway.GetEnd().GlobalPosition();
        route = new[] { origin + forward * 4000, origin + forward * 4000 + right * 4000,
            origin + right * 4000, origin };
        started = Time.realtimeSinceStartup;
    }

    internal void Apply()
    {
        Filtered = false;
        ticks++;
        var inputs = aircraft.GetInputs();
        inputs.pitch = inputs.roll = inputs.yaw = inputs.brake = 0;
        inputs.customAxis1 = 0.5f;
        if (phase == "taxi")
        {
            float distance = Vector3.Distance(aircraft.transform.position, runway.GetStart().position);
            if (distance < 18) phase = "takeoff";
            else
            {
                var point = taxi.GetSteerpoint(aircraft.GlobalPosition(), aircraft.transform.forward,
                    Mathf.Max(8, aircraft.speed * 1.5f), stayOnRoad: false);
                if (!point.HasValue) { inputs.throttle = 0; inputs.brake = 1; return; }
                float targetSpeed = Mathf.Clamp(10 / (1 + point.Value.nextWaypointAngle * 0.03f), 3, 10);
                inputs.throttle = Mathf.Clamp(0.25f + (targetSpeed - aircraft.speed) * 0.15f, 0.01f, 0.5f);
                inputs.brake = aircraft.speed > targetSpeed + 1 ? 0.6f : 0;
                // Normal hangar opening/aircraft start animation gets time to finish.
                if (Time.realtimeSinceStartup - started < 8) { inputs.throttle = 0.05f; inputs.brake = 1; }
                var target = aircraft.GlobalPosition() + point.Value.steerVector.normalized * 25;
                Aim(target, false, true, true, 1, 0, false, 0);
                return;
            }
        }
        if (phase == "takeoff")
        {
            Vector3 direction = runway.GetDirection().normalized;
            var localTarget = runway.Runway.GetNearestPoint(aircraft.transform.position, true) + direction * 350;
            bool aligned = Vector3.Dot(aircraft.transform.forward, direction) > 0.97f;
            inputs.throttle = aligned || aircraft.radarAlt > 3 ? 1 : Mathf.Clamp(0.35f - aircraft.speed * 0.05f, 0.05f, 0.35f);
            inputs.brake = !aligned && aircraft.speed > 5 ? 0.8f : 0;
            if (aircraft.speed > aircraft.GetAircraftParameters().takeoffSpeed * 0.85f || aircraft.radarAlt > 3)
                localTarget.y += 45;
            Aim(localTarget.ToGlobalPosition(), true, false, aircraft.radarAlt < 3, 0.95f, 30, false, 0);
            // Once clear of the runway, climb against the terrain ahead. Keeping
            // a runway-relative target pins the plane near runway elevation and
            // eventually points backwards after it flies beyond the runway.
            if (aircraft.radarAlt > 5) { phase = "climb"; aircraft.SetGear(false); }
            return;
        }
        if (phase == "climb")
        {
            inputs.throttle = 1;
            Aim(aircraft.GlobalPosition() + runway.GetDirection().normalized * 4000,
                true, false, false, 0.95f, 15, true, 700);
            if (aircraft.radarAlt > 200 && aircraft.speed > aircraft.GetAircraftParameters().takeoffSpeed)
                phase = "circuit";
            return;
        }
        var waypoint = route[routeIndex];
        Vector3 delta = waypoint - aircraft.GlobalPosition();
        delta.y = 0;
        if (delta.magnitude < 600) routeIndex = (routeIndex + 1) % route.Length;
        inputs.throttle = aircraft.GetAircraftParameters().cruiseThrottle;
        Aim(route[routeIndex], true, false, false, 1, 35, true, 700);
    }

    private void Aim(GlobalPosition target, bool velocity, bool ignoreCollisions, bool runwayAlign,
        float effort, float bank, bool followTerrain, float altitude)
    {
        aircraft.autopilot.AutoAim(target, velocity, ignoreCollisions, runwayAlign, effort, bank, followTerrain, altitude, Vector3.zero);
        Filtered = true; // AutoAim itself applies the game's input filter.
    }

    internal object Snapshot() => new { phase, routeIndex, ticks, runway = runway.GetName() };
}
