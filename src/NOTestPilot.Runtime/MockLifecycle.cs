using System;
using HarmonyLib;
using Mirage;
using NuclearOption.Networking;

namespace NOTestPilot.Runtime;

// Keeps native scoring/reward behavior intact for server-only mock players,
// while avoiding a player-targeted UI RPC that has no network recipient.
public static class MockLifecycle
{
    [ThreadStatic]
    private static RewardContext currentReward;

    private sealed class RewardContext
    {
        public RewardContext Previous;
        public bool IsOwnerlessMock;
    }

    public static void Initialize(Harmony harmony)
    {
        if (harmony == null) throw new ArgumentNullException(nameof(harmony));
        var reward = AccessTools.Method(typeof(FactionHQ), nameof(FactionHQ.RewardPlayer),
            new[] { typeof(Player), typeof(Unit), typeof(float), typeof(float), typeof(FactionHQ.RewardType) })
            ?? throw new MissingMethodException(typeof(FactionHQ).FullName, nameof(FactionHQ.RewardPlayer));
        var credit = AccessTools.Method(typeof(MessageManager), nameof(MessageManager.TargetCreditMessage),
            new[] { typeof(INetworkPlayer), typeof(PersistentID), typeof(float), typeof(FactionHQ.RewardType) })
            ?? throw new MissingMethodException(typeof(MessageManager).FullName, nameof(MessageManager.TargetCreditMessage));
        harmony.Patch(reward,
            prefix: new HarmonyMethod(typeof(MockLifecycle), nameof(BeforeRewardPlayer)),
            finalizer: new HarmonyMethod(typeof(MockLifecycle), nameof(AfterRewardPlayer)));
        harmony.Patch(credit, prefix: new HarmonyMethod(typeof(MockLifecycle), nameof(BeforeTargetCreditMessage)));
    }

    private static void BeforeRewardPlayer(Player player, out RewardContext __state)
    {
        __state = new RewardContext
        {
            Previous = currentReward,
            IsOwnerlessMock = MockPlayers.IsMock(player) && player.Owner == null
        };
        currentReward = __state;
    }

    private static Exception AfterRewardPlayer(Exception __exception, RewardContext __state)
    {
        if (__state != null) currentReward = __state.Previous;
        return __exception;
    }

    private static bool BeforeTargetCreditMessage(INetworkPlayer __0)
    {
        // Only suppress the native recipient-dependent credit display while
        // executing the original RewardPlayer for an ownerless mock. The native
        // method still performs AddAllocation, AddScore, and sortieScore updates.
        return !(currentReward != null && currentReward.IsOwnerlessMock && __0 == null);
    }
}
