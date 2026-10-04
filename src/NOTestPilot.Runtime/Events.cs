using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Threading;
using HarmonyLib;
using NuclearOption.Networking;

namespace NOTestPilot.Runtime;

// Bounded, read-only observations of native fire, hit, damage, and disable paths.
// Every Harmony callback is fail-open: observer exceptions are counted and the
// original game method always continues unchanged.
public static class Events
{
    private const int Capacity = 4096;
    private static readonly object gate = new object();
    private static readonly Queue<Dictionary<string, object>> events = new Queue<Dictionary<string, object>>();
    private static long sequence;
    private static long observerErrors;
    private static bool installed;

    [ThreadStatic]
    private static DamageContext currentDamage;

    private sealed class DamageContext
    {
        public DamageContext Previous;
        public uint? VictimId;
        public byte PartId;
        public uint? DealerId;
        public string MockPlayer;
        public string VictimMockPlayer;
    }

    public static void Install(Harmony harmony)
    {
        if (harmony == null) throw new ArgumentNullException(nameof(harmony));
        lock (gate)
        {
            if (installed) return;

            Patch(harmony, typeof(BulletSim), nameof(BulletSim.AddBullet), new[]
            {
                typeof(UnityEngine.Transform), typeof(UnityEngine.Vector3), typeof(float), typeof(float),
                typeof(bool), typeof(float), typeof(UnityEngine.Color), typeof(float), typeof(Unit)
            }, postfix: nameof(AfterAddBullet));
            Patch(harmony, typeof(Unit), nameof(Unit.RegisterHit), new[] { typeof(Unit), typeof(UnityEngine.Vector3), typeof(UnityEngine.Vector3), typeof(WeaponInfo) }, postfix: nameof(AfterRegisterHit));
            Patch(harmony, typeof(UnitPart), nameof(UnitPart.TakeDamage), new[] { typeof(float), typeof(float), typeof(float), typeof(float), typeof(float), typeof(PersistentID) }, prefix: nameof(BeforeTakeDamage), finalizer: nameof(AfterTakeDamage));
            Patch(harmony, typeof(Unit), nameof(Unit.Damage), new[] { typeof(byte), typeof(DamageInfo) }, prefix: nameof(BeforeDamageMessage));
            Patch(harmony, typeof(UnitPart), nameof(UnitPart.ApplyDamage), new[] { typeof(float), typeof(float), typeof(float), typeof(float) }, postfix: nameof(AfterApplyDamage));
            Patch(harmony, typeof(Unit), "ServerDisableUnit", Type.EmptyTypes, postfix: nameof(AfterServerDisable));

            installed = true;
        }
    }

    // Cursor is the latest retained sequence. overflow means at least one event
    // newer than `after` was evicted before it could be read.
    public static object Read(long after)
    {
        if (after < 0) throw new ArgumentOutOfRangeException(nameof(after));
        lock (gate)
        {
            long oldest = events.Count == 0 ? sequence + 1 : (long)events.Peek()["seq"];
            bool overflow = after < oldest - 1;
            var rows = events.Where(item => (long)item["seq"] > after).ToArray();
            return new
            {
                cursor = sequence,
                oldestSequence = oldest,
                overflow,
                observerErrors = Interlocked.Read(ref observerErrors),
                events = rows
            };
        }
    }

    private static void Patch(Harmony harmony, Type type, string name, Type[] arguments,
        string prefix = null, string postfix = null, string finalizer = null)
    {
        var target = AccessTools.Method(type, name, arguments)
            ?? throw new MissingMethodException(type.FullName, name);
        HarmonyMethod before = prefix == null ? null : new HarmonyMethod(typeof(Events), prefix);
        HarmonyMethod after = postfix == null ? null : new HarmonyMethod(typeof(Events), postfix);
        HarmonyMethod finish = finalizer == null ? null : new HarmonyMethod(typeof(Events), finalizer);
        harmony.Patch(target, prefix: before, postfix: after, finalizer: finish);
    }

    private static void AfterAddBullet(BulletSim __instance)
    {
        Observe(() =>
        {
            if (__instance == null) return;
            var ownerField = AccessTools.Field(typeof(BulletSim), "owner")
                ?? throw new MissingFieldException(typeof(BulletSim).FullName, "owner");
            var weaponInfoField = AccessTools.Field(typeof(BulletSim), "weaponInfo")
                ?? throw new MissingFieldException(typeof(BulletSim).FullName, "weaponInfo");
            Unit owner = ownerField.GetValue(__instance) as Unit;
            if (owner == null || !owner.IsServer) return;
            var data = Actor(owner, "gun_bullet_created");
            if (data["mockPlayer"] == null) return;
            var weaponInfo = weaponInfoField.GetValue(__instance) as WeaponInfo;
            data["weapon"] = weaponInfo != null ? weaponInfo.weaponName : null;
            data["evidence"] = "native BulletSim.AddBullet completed";
            Record(data);
        });
    }

    private static void AfterRegisterHit(Unit __instance, Unit __0)
    {
        Observe(() =>
        {
            Unit hitUnit = __0;
            if (__instance == null || hitUnit == null || !__instance.IsServer) return;
            var data = Actor(__instance, "unit_hit_registered");
            string victimMockPlayer = MockPlayerFor(hitUnit.persistentID);
            if (data["mockPlayer"] == null && victimMockPlayer == null) return;
            data["victimPersistentId"] = Id(hitUnit.persistentID);
            data["victimMockPlayer"] = victimMockPlayer;
            data["hitPath"] = "native Unit.RegisterHit; distinct from damage application";
            Record(data);
        });
    }

    private static void BeforeTakeDamage(UnitPart __instance, float __0, float __1,
        float __2, float __3, float __4, PersistentID __5,
        out DamageContext __state)
    {
        __state = null;
        try
        {
            float pierceDamage = __0;
            float blastDamage = __1;
            float amountAffected = __2;
            float fireDamage = __3;
            float impactDamage = __4;
            PersistentID dealerID = __5;
            if (__instance == null || __instance.parentUnit == null || !__instance.parentUnit.IsServer) return;
            var context = new DamageContext
            {
                Previous = currentDamage,
                VictimId = Id(__instance.parentUnit.persistentID),
                PartId = __instance.id,
                DealerId = Id(dealerID),
                MockPlayer = MockPlayerFor(dealerID),
                VictimMockPlayer = MockPlayerFor(__instance.parentUnit.persistentID)
            };
            __state = context;
            if (context.MockPlayer == null && context.VictimMockPlayer == null) return;
            currentDamage = context;
            var data = DamageBase("part_damage_call", context);
            data["pierceDamage"] = pierceDamage;
            data["blastDamage"] = blastDamage;
            data["amountAffected"] = amountAffected;
            data["fireDamage"] = fireDamage;
            data["impactDamage"] = impactDamage;
            data["meaning"] = "native TakeDamage call; may return early and is not itself proof of applied hit points";
            Record(data);
        }
        catch { Interlocked.Increment(ref observerErrors); }
    }

    private static Exception AfterTakeDamage(Exception __exception, DamageContext __state)
    {
        try
        {
            if (__state != null) currentDamage = __state.Previous;
        }
        catch { Interlocked.Increment(ref observerErrors); }
        return __exception;
    }

    private static void BeforeDamageMessage(Unit __instance, byte __0, DamageInfo __1)
    {
        Observe(() =>
        {
            byte index = __0;
            DamageInfo damageInfo = __1;
            if (__instance == null || !__instance.IsServer) return;
            string victimMockPlayer = MockPlayerFor(__instance.persistentID);
            bool sameDamageScope = currentDamage != null && currentDamage.VictimId == Id(__instance.persistentID)
                && currentDamage.PartId == __0;
            if (!sameDamageScope && victimMockPlayer == null) return;
            var data = new Dictionary<string, object>
            {
                ["type"] = "unit_damage_message",
                ["victimPersistentId"] = Id(__instance.persistentID),
                ["partId"] = __0,
                ["pierceDamage"] = damageInfo.pierceDamage.Decompress(),
                ["blastDamage"] = damageInfo.blastDamage.Decompress(),
                ["fireDamage"] = damageInfo.fireDamage.Decompress(),
                ["impactDamage"] = damageInfo.impactDamage.Decompress(),
                ["meaning"] = "native DamageInfo payload; it has no attacker field"
            };
            if (sameDamageScope)
            {
                data["dealerPersistentId"] = currentDamage.DealerId;
                data["mockPlayer"] = currentDamage.MockPlayer;
                data["victimMockPlayer"] = currentDamage.VictimMockPlayer;
                data["attribution"] = "synchronous native TakeDamage scope";
            }
            else data["victimMockPlayer"] = victimMockPlayer;
            Record(data);
        });
    }

    private static void AfterApplyDamage(UnitPart __instance, float __0, float __1,
        float __2, float __3)
    {
        Observe(() =>
        {
            float pierceDamage = __0;
            float blastDamage = __1;
            float fireDamage = __2;
            float impactDamage = __3;
            if (__instance == null || __instance.parentUnit == null || !__instance.parentUnit.IsServer) return;
            string victimMockPlayer = MockPlayerFor(__instance.parentUnit.persistentID);
            bool sameDamageScope = currentDamage != null && currentDamage.VictimId == Id(__instance.parentUnit.persistentID)
                && currentDamage.PartId == __instance.id;
            if (!sameDamageScope && victimMockPlayer == null) return;
            var data = new Dictionary<string, object>
            {
                ["type"] = "part_damage_applied",
                ["victimPersistentId"] = Id(__instance.parentUnit.persistentID),
                ["partId"] = __instance.id,
                ["pierceDamage"] = pierceDamage,
                ["blastDamage"] = blastDamage,
                ["fireDamage"] = fireDamage,
                ["impactDamage"] = impactDamage,
                ["meaning"] = "native ApplyDamage call completed; damage source is included only when synchronous TakeDamage context is present"
            };
            if (sameDamageScope)
            {
                data["dealerPersistentId"] = currentDamage.DealerId;
                data["mockPlayer"] = currentDamage.MockPlayer;
                data["victimMockPlayer"] = currentDamage.VictimMockPlayer;
                data["attribution"] = "synchronous native TakeDamage scope";
            }
            else data["victimMockPlayer"] = victimMockPlayer;
            Record(data);
        });
    }

    private static void AfterServerDisable(Unit __instance)
    {
        Observe(() =>
        {
            if (__instance == null || !__instance.IsServer || !__instance.disabled) return;
            string mockPlayer = MockPlayerFor(__instance.persistentID);
            if (mockPlayer == null) return;
            var data = new Dictionary<string, object>
            {
                ["type"] = "unit_disabled",
                ["victimPersistentId"] = Id(__instance.persistentID),
                ["mockPlayer"] = mockPlayer,
                ["unitType"] = __instance.GetType().Name
            };
            Record(data);
        });
    }

    private static Dictionary<string, object> Actor(Unit unit, string type)
    {
        return new Dictionary<string, object>
        {
            ["type"] = type,
            ["attackerPersistentId"] = Id(unit.persistentID),
            ["attackerAircraftPersistentId"] = unit is Aircraft ? Id(unit.persistentID) : null,
            ["mockPlayer"] = MockPlayerFor(unit.persistentID)
        };
    }

    private static Dictionary<string, object> DamageBase(string type, DamageContext context)
    {
        return new Dictionary<string, object>
        {
            ["type"] = type,
            ["victimPersistentId"] = context.VictimId,
            ["partId"] = context.PartId,
            ["dealerPersistentId"] = context.DealerId,
            ["mockPlayer"] = context.MockPlayer
        };
    }

    private static string MockPlayerFor(PersistentID id)
    {
        if (!id.IsValid || !UnitRegistry.TryGetPersistentUnit(id, out var persistent) || persistent == null) return null;
        Player player = persistent.player;
        if (persistent.unit is Aircraft aircraft) player = aircraft.Player;
        return MockPlayers.IsMock(player) ? MockPlayers.Name(player) : null;
    }

    private static uint? Id(PersistentID id) => id.IsValid ? (uint?)id.Id : null;

    private static void Observe(Action action)
    {
        try { action(); }
        catch { Interlocked.Increment(ref observerErrors); }
    }

    private static void Record(Dictionary<string, object> data)
    {
        lock (gate)
        {
            data["seq"] = ++sequence;
            data["timeUtc"] = DateTime.UtcNow.ToString("O");
            events.Enqueue(data);
            while (events.Count > Capacity) events.Dequeue();
        }
    }
}
