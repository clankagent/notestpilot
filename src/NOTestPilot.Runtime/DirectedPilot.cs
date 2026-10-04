using System;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;
using UnityEngine;

namespace NOTestPilot.Runtime;

// Test-owned task orchestration. Native AI helpers calculate steering and gun
// lead; no autonomous NPC target selection, scheduler or brain is installed.
internal sealed class DirectedPilot : PilotBaseState
{
    private static readonly ConditionalWeakTable<Aircraft, DirectedPilot> Controllers = new();
    private static readonly FieldInfo LinkedGuns = typeof(WeaponManager).GetField("gunsLinked", BindingFlags.Instance | BindingFlags.NonPublic)
        ?? throw new MissingFieldException(typeof(WeaponManager).FullName, "gunsLinked");
    private readonly Aircraft actor;
    private Pilot driver;
    private PilotBaseState previousState;
    private WeaponStation previousStation;
    private Unit[] previousTargets;
    private bool previousAssist, previousLinked, attached;
    private string task = "idle", outcome = "idle", error;
    private Unit target;
    private int stationIndex = -1;
    private float requestedSpeed, expires, lastDistance;
    private long ticks, fireRequests;

    private DirectedPilot(Aircraft aircraft) { actor = aircraft; }

    internal static DirectedPilot For(Aircraft aircraft)
    {
        if (aircraft == null) throw new ArgumentNullException(nameof(aircraft));
        return Controllers.GetValue(aircraft, a => new DirectedPilot(a));
    }

    internal void Navigate(GlobalPosition point, float speed)
    {
        if (!Finite(point.x) || !Finite(point.y) || !Finite(point.z) || !Finite(speed) || speed <= 0 || speed > 1000)
            throw new ArgumentException("Navigation requires a finite position and speed in (0, 1000]");
        ValidateActor();
        Replace();
        destination = point;
        requestedSpeed = speed;
        task = "navigate";
        Attach();
    }

    internal void Attack(Unit selectedTarget, int station, float deadlineSeconds)
    {
        ValidateActor();
        if (!Finite(deadlineSeconds) || deadlineSeconds <= 0 || deadlineSeconds > 600)
            throw new ArgumentException("Attack deadline must be in (0, 600] seconds");
        ValidateTarget(selectedTarget);
        if (station < 0 || station >= actor.weaponStations.Count) throw new ArgumentOutOfRangeException(nameof(station));
        var chosen = actor.weaponStations[station];
        if (chosen.WeaponInfo == null || !chosen.WeaponInfo.gun || !chosen.WeaponInfo.boresight || chosen.TurretCount() != 0 || chosen.Ammo <= 0)
            throw new InvalidOperationException("Attack currently requires an armed fixed boresight gun station");
        if (!actor.NetworkHQ.TryGetKnownPosition(selectedTarget, out _) || !actor.NetworkHQ.IsTargetPositionAccurate(selectedTarget, 100f))
            throw new InvalidOperationException("Target must have accurate native HQ tracking");
        Replace();
        target = selectedTarget;
        stationIndex = station;
        expires = Time.realtimeSinceStartup + deadlineSeconds;
        task = "attack";
        Attach();
        actor.weaponManager.SetGunsLinked(false);
        actor.weaponManager.currentWeaponStation = chosen;
        actor.weaponManager.ClearTargetList();
        actor.weaponManager.AddTargetList(target);
    }

    internal void Cancel() => Finish("cancelled", null);

    private void Replace()
    {
        Finish("replaced", null);
        outcome = "running";
        error = null;
        ticks = fireRequests = 0;
    }

    private void ValidateActor()
    {
        if (actor == null || actor.disabled || !actor.IsServer || !actor.LocalSim || actor.remoteSim)
            throw new InvalidOperationException("Actor must be a live server-simulated aircraft");
        if (actor.pilots == null || actor.pilots.Length == 0 || actor.pilots[0] == null || actor.pilots[0].dead || actor.pilots[0].ejected)
            throw new InvalidOperationException("Actor requires a live controlling pilot");
        if (actor.NetworkHQ == null || actor.autopilot == null || actor.weaponManager == null || actor.GetControlsFilter() == null)
            throw new InvalidOperationException("Actor control dependencies are not initialized");
    }

    private void ValidateTarget(Unit selected)
    {
        if (selected == null || selected == actor || selected.disabled || selected.NetworkHQ == null || selected.NetworkHQ == actor.NetworkHQ)
            throw new InvalidOperationException("Attack requires a live opposing target");
    }

    private void Attach()
    {
        driver = actor.pilots[0];
        previousState = driver.currentState;
        previousStation = actor.weaponManager.currentWeaponStation;
        previousTargets = actor.weaponManager.GetTargetList().ToArray();
        previousLinked = (bool)LinkedGuns.GetValue(actor.weaponManager);
        previousAssist = actor.flightAssist;
        attached = true;
        driver.SwitchState(this);
        actor.SetFlightAssist(true);
    }

    public override void EnterState(Pilot selected)
    {
        pilot = selected;
        aircraft = actor;
        controlInputs = actor.GetInputs();
        stateDisplayName = "Directed " + task;
    }

    public override void UpdateState(Pilot selected)
    {
        if (attached && (actor == null || actor.disabled || selected.dead || selected.ejected || selected.aircraft != actor))
            Finish("actor-unavailable", null);
        else if (attached && task == "attack" && Time.realtimeSinceStartup >= expires)
            Finish("deadline", null);
    }

    public override void FixedUpdateState(Pilot selected)
    {
        if (!attached) return;
        try
        {
            ValidateActor();
            ticks++;
            controlInputs.pitch = controlInputs.roll = controlInputs.yaw = controlInputs.brake = 0;
            if (task == "navigate")
            {
                lastDistance = Vector3.Distance(actor.GlobalPosition().AsVector3(), destination.AsVector3());
                if (lastDistance < 100f) { Finish("arrived", null); return; }
                controlInputs.throttle = Mathf.Clamp01(actor.GetAircraftParameters().cruiseThrottle + (requestedSpeed - actor.speed) * 0.01f);
                actor.autopilot.AutoAim(destination, true, false, false, 1f, 35f, false, 0f, Vector3.zero);
                return;
            }
            if (Time.realtimeSinceStartup >= expires) { Finish("deadline", null); return; }
            ValidateTarget(target);
            var station = actor.weaponStations[stationIndex];
            if (station.Ammo <= 0) { Finish("out-of-ammunition", null); return; }
            if (!ReferenceEquals(actor.weaponManager.currentWeaponStation, station))
                throw new InvalidOperationException("Selected weapon station changed externally");
            var assignedTargets = actor.weaponManager.GetTargetList();
            if (assignedTargets.Count != 1 || !ReferenceEquals(assignedTargets[0], target) || (bool)LinkedGuns.GetValue(actor.weaponManager))
                throw new InvalidOperationException("Directed weapon targeting changed externally");
            if (!actor.NetworkHQ.TryGetKnownPosition(target, out var known) || !actor.NetworkHQ.IsTargetPositionAccurate(target, 100f))
                throw new InvalidOperationException("Directed target tracking lost");
            var info = station.WeaponInfo;
            float lead = TargetCalc.TargetLeadTime(target, actor.gameObject, actor.rb, info.muzzleVelocity, info.dragCoef, 2);
            if (!Finite(lead) || lead < 0) throw new InvalidOperationException("Native ballistic lead is invalid");
            Vector3 targetVelocity = target.rb != null ? target.rb.velocity : Vector3.zero;
            // Offset the aim against gravity over the native estimated flight
            // time. This is task-owned ballistic compensation, not an NPC brain.
            var aim = known + targetVelocity * lead - Physics.gravity * (0.5f * lead * lead);
            var delta = aim - actor.GlobalPosition();
            lastDistance = delta.magnitude;
            controlInputs.throttle = actor.GetAircraftParameters().cruiseThrottle;
            actor.autopilot.AutoAim(aim, false, false, false, 1f, 35f, false, 0f, targetVelocity);
            bool visible = target.LineOfSight(actor.transform.position, 1000f);
            bool aligned = station.Weapons.Count > 0 && station.Weapons.All(w => w != null
                && Vector3.Angle(w.transform.forward, delta) <= 2f);
            if (visible && lastDistance >= info.targetRequirements.minRange && lastDistance <= info.targetRequirements.maxRange
                && aligned
                && station.Ready())
            {
                // Normal safety, cadence, ammunition, bullet simulation and
                // aircraft persistent-ID damage attribution remain in the game.
                pilot.Fire();
                fireRequests++;
            }
        }
        catch (Exception failure) { Finish("failed", failure.Message); }
    }

    public override void LeaveState()
    {
        Restore();
        if (outcome == "running") outcome = "interrupted";
    }

    private void Finish(string result, string failure)
    {
        outcome = result;
        error = failure;
        if (attached && driver != null && ReferenceEquals(driver.currentState, this)) driver.SwitchState(previousState);
        else Restore();
        task = "idle";
        target = null;
        stationIndex = -1;
    }

    private void Restore()
    {
        if (!attached) return;
        attached = false;
        if (actor == null) return;
        var inputs = actor.GetInputs();
        inputs.pitch = inputs.roll = inputs.yaw = inputs.throttle = inputs.brake = 0;
        if (stationIndex >= 0) actor.SetFiringState(actor.weaponStations[stationIndex].Number, false);
        actor.weaponManager.SetGunsLinked(previousLinked);
        actor.weaponManager.currentWeaponStation = previousStation;
        actor.weaponManager.ClearTargetList();
        foreach (var old in previousTargets ?? Array.Empty<Unit>()) if (old != null) actor.weaponManager.AddTargetList(old);
        actor.SetFlightAssist(previousAssist);
    }

    internal object Snapshot() => new {
        active = attached, task, outcome, error, ticks, fireRequests,
        aircraftNetId = actor != null ? (uint?)actor.NetId : null,
        targetNetId = target != null ? (uint?)target.NetId : null, station = stationIndex,
        distance = lastDistance,
        deadlineRemaining = attached && task == "attack" ? Math.Max(0, expires - Time.realtimeSinceStartup) : 0,
        destination = new[] { destination.x, destination.y, destination.z },
        algorithms = "native AutoAim and TargetCalc.TargetLeadTime; test-owned task and firing gates"
    };

    private static bool Finite(float value) => !float.IsNaN(value) && !float.IsInfinity(value);
}
