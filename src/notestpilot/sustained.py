"""Distinct client-witnessed sustained task, not strict survival or server authority."""
import copy
import math
import time

from .runner import TestFailure

GAME_ASSEMBLY_SHA256 = 'df5bed594dd84912efb3e57faa75b37d7e327bf4c8f5418f50411ad0ff46e24a'


def require(value, message, evidence=None):
    if not value:
        raise TestFailure(message, evidence)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def require_motion(owner, state):
    """One shared domain guard for every path that can renew a flight lease."""
    require(all(finite(owner.get(k)) and owner[k] >= 0 for k in ('radarAltitude', 'speed')),
            'Negative/nonfinite altitude or speed', state)


def rpc_caller(callers):
    return isinstance(callers, list) and any(
        isinstance(c, str) and (c == 'Aircraft.RpcDamage' or
        c.startswith('Aircraft.UserCode_RpcDamage_') or c.startswith('Aircraft.Skeleton_RpcDamage_'))
        for c in callers)


def incoming_rpc_loss(state, player, aircraft):
    """Return retained destructive RPC witness; predicted HP is not actual post-HP."""
    require(state.get('assemblySha256') == GAME_ASSEMBLY_SHA256, 'Unverified bridge game assembly', state)
    obs = state.get('observation', {})
    now = obs.get('realtimeSeconds')
    require(finite(now), 'Client observation time unavailable', state)
    events = obs.get('events'); first = obs.get('firstDamageEvents')
    require(isinstance(events, list) and isinstance(first, list), 'Damage journals unavailable', state)
    own = [e for e in events + first if isinstance(e, dict) and e.get('aircraftNetId') == aircraft]
    candidates = []
    for e in own:
        if e.get('action') != 'DestructivePartApplyDamage':
            continue
        d = e.get('damage', {})
        require(isinstance(d, dict), 'Malformed destructive damage payload', e)
        part = d.get('part', {})
        before = d.get('hitPointsBefore'); after = d.get('predictedHitPointsAfter')
        values = [d.get(k) for k in ('netPierceDamage', 'netBlastDamage', 'netFireDamage', 'netImpactDamage')]
        if (e.get('playerNetId') == player and finite(e.get('seconds')) and 0 <= now-e['seconds'] <= 30
                and finite(before) and before > 0 and finite(after) and after <= 0
                and all(finite(v) and v >= 0 for v in values) and values[2] == 0 and values[3] == 0
                and values[0]+values[1] > 0 and math.isclose(after, before-sum(values), abs_tol=.01)
                and isinstance(part, dict) and isinstance(part.get('name'), str) and bool(part['name'])
                and type(part.get('partID')) is int and rpc_caller(e.get('callers'))
                and e.get('disabled') is False and e.get('Ignition') is True
                and isinstance(e.get('pilots'), list) and e['pilots']
                and all(x.get('dead') is False and x.get('ejected') is False for x in e['pilots'])):
            candidates.append(e)
    if not candidates:
        return None
    chosen = min(candidates, key=lambda e: e['seconds'])
    # Contacts/joint loss preceding the initiating witness are not forgiven.
    for e in own:
        if not finite(e.get('seconds')):
            raise TestFailure('Malformed damage event time', e)
        if e['seconds'] <= chosen['seconds'] and e.get('action') in ('Collision', 'OnJointBreak', 'DetachPart'):
            raise TestFailure('Preceding collision/joint loss is unclassified', own)
        if e['seconds'] <= chosen['seconds'] and e.get('action') in ('PartApplyDamage', 'DestructivePartApplyDamage'):
            require(rpc_caller(e.get('callers')) and e.get('damage', {}).get('netImpactDamage') == 0,
                    'Unclassified prior damage', own)
    lease = state.get('controlLease', {})
    require(lease.get('active') is True and lease.get('aircraftNetId') == aircraft
            and finite(lease.get('secondsRemaining')) and lease['secondsRemaining'] > 0,
            'Loss has no active own control lease', state)
    return {'classification': 'CLIENT_WITNESSED_INCOMING_RPC_DESTRUCTIVE_DAMAGE',
            'postHpSemantics': 'predicted_by_bridge_before_original_apply',
            'verifiedMissileOrDealer': False, 'event': copy.deepcopy(chosen)}


def execute_sustained(bridges, initial, spec, sample, check_cancel, event,
                      clock=time.monotonic, sleep=time.sleep):
    """Already spawned actors; spec has seconds, mission, reserve/spawn args per role.

    Each call/status must have a caller-configured finite transport timeout. Scheduler
    fails if blocking calls cause >5s sample gaps. Fixed 180s recovery deadline,
    max three recoveries per actor, 45s fly leases renewed every20s; no fire.
    """
    names = tuple(bridges)
    require(names and set(names) == set(initial) == set(spec['reserve']) == set(spec['spawn']), 'Actor inputs disagree')
    require(finite(spec.get('seconds')) and 0 < spec['seconds'] <= 3600, 'Invalid task deadline')
    require(all(isinstance(spec[k][n], dict) for k in ('reserve', 'spawn') for n in names), 'Original action arguments invalid')
    mission = spec.get('mission')
    require(isinstance(mission, str) and bool(mission), 'Mission required')
    started = clock(); last = started; seen = set(); actors = {}; actions = []
    for n in names:
        p = initial[n]
        require(type(p.get('netId')) is int and p['netId'] > 0 and type(p.get('aircraftNetId')) is int
                and p['aircraftNetId'] > 0, 'Initial IDs invalid')
        require(p['aircraftNetId'] not in seen, 'Initial aircraft duplicate')
        seen.add(p['aircraftNetId'])
        actors[n] = dict(player=p['netId'], aircraft=p['aircraftNetId'], lives=[p['aircraftNetId']],
                         faction=p.get('faction'), stage='active', losses=[], recoveries=[],
                         airborneSeconds=0., activeSeconds=0., nextFly=started, instance=None, errors=None,
                         lifeStart=started, lifeInitialPosition=None, lifeMotion=0., lifeReachedAirborne=False,
                         completedLives=[], previousEndpoint=None)
    require(len({a['player'] for a in actors.values()}) == len(names), 'Duplicate initial players')

    def action(n, command, args):
        check_cancel(); t = clock(); result = bridges[n].call(command, copy.deepcopy(args))
        row = dict(target=n, command=command, args=copy.deepcopy(args), seconds=t-started,
                   finishedSeconds=clock()-started, result=copy.deepcopy(result))
        actions.append(row); event('Sustained ordinary action', row)
        require(isinstance(result, dict) and result.get('accepted') is True, 'Ordinary action rejected', row)

    while True:
        check_cancel(); states = {n: bridges[n].status() for n in names}; now = clock()
        sample(dict(seconds=now-started, states=states, actors=copy.deepcopy(actors), actionsIssued=len(actions)))
        require(0 <= now-last <= 5, 'Status sampling gap exceeded', states)
        last = now
        rows = {}
        for n, s in states.items():
            a = actors[n]
            require(s.get('assemblySha256') == GAME_ASSEMBLY_SHA256, 'Unverified bridge game assembly', s)
            require(s.get('role') == 'client' and s.get('clientActive') is True and s.get('mission') == mission
                    and s.get('missionRunning') is True and s.get('localPlayerNetId') == a['player'], 'Mission/player changed', s)
            require(isinstance(s.get('instance'), str) and bool(s['instance']), 'Peer identity missing', s)
            if a['instance'] is None: a['instance'] = s['instance']
            require(a['instance'] == s['instance'], 'Peer identity changed', s)
            require(type(s.get('errorCount')) is int and s['errorCount'] >= 0, 'Error counter missing', s)
            if a['errors'] is None: a['errors'] = s['errorCount']
            require(s['errorCount'] == a['errors'], 'New client errors', s)
            own = [p for p in s.get('players', []) if p.get('netId') == a['player'] and p.get('local') is True]
            require(len(own) == 1, 'Own player ambiguous', s); p = own[0]; rows[n] = p
            require(p.get('aircraftNetId') == s.get('localPlayerAircraftNetId') and p.get('faction') == a['faction'], 'Association/faction mismatch', s)
            if a['stage'] == 'active':
                require(p.get('aircraftNetId') == a['aircraft'], 'Unexpected aircraft identity', s)
                require_motion(p, s)
                loss = incoming_rpc_loss(s, a['player'], a['aircraft'])
                if loss:
                    require(len(a['losses']) < 3, 'Recovery budget exhausted', s)
                    a['losses'].append(loss); a['recoveryStart'] = now; a['deadline'] = now+180
                    a['previousEndpoint'] = None
                    action(n, 'eject', {}); a['stage'] = 'eligible'; continue
                obs = s['observation']
                require(not any(e.get('aircraftNetId') == a['aircraft'] and e.get('action') in
                                ('PartApplyDamage', 'DestructivePartApplyDamage', 'Collision', 'OnJointBreak', 'DetachPart')
                                for e in obs['events'] + obs['firstDamageEvents'] if isinstance(e, dict)),
                        'Unclassified damage/contact', s)
                if a['nextFly'] != started:
                    lease = s.get('controlLease', {})
                    require(lease.get('active') is True and lease.get('aircraftNetId') == a['aircraft']
                            and finite(lease.get('secondsRemaining')) and lease['secondsRemaining'] > 0,
                            'Active control lease lost', s)
                pilots = p.get('pilotHealth')
                require(p.get('disabled') is False and p.get('ignition') is True and isinstance(pilots, list)
                        and pilots and all(x.get('dead') is False and x.get('ejected') is False for x in pilots), 'Unclassified aircraft/pilot failure', s)
                require(all(finite(p.get(k)) for k in ('radarAltitude', 'speed')), 'Motion unavailable', s)
                position = p.get('position')
                require(isinstance(position, list) and len(position) == 3 and all(finite(v) for v in position),
                        'Finite 3D position unavailable', s)
                if a['lifeInitialPosition'] is None: a['lifeInitialPosition'] = list(position)
                a['lifeMotion'] = max(a['lifeMotion'], math.dist(a['lifeInitialPosition'], position))
                airborne = p['radarAltitude'] >= 80 and p['speed'] >= 50
                require(not a['lifeReachedAirborne'] or airborne, 'Unclassified grounded/slow airborne life', s)
                if airborne: a['lifeReachedAirborne'] = True
                require(a['lifeReachedAirborne'] or now-a['lifeStart'] < 180, 'Life takeoff deadline exceeded', s)
                previous = a['previousEndpoint']
                if previous is not None and previous['aircraft'] == a['aircraft']:
                    span = now-previous['seconds']
                    a['activeSeconds'] += span
                    if previous['airborne'] and airborne: a['airborneSeconds'] += span
                a['previousEndpoint'] = dict(seconds=now, aircraft=a['aircraft'], airborne=airborne)
                if now >= a['nextFly']:
                    action(n, 'fly', {'seconds': 45, 'fire': False}); a['nextFly'] = clock()+20
            else:
                require(now <= a['deadline'], 'Recovery timeout', dict(actor=n, state=s, progress=a))
                current = p.get('aircraftNetId')
                if a['stage'] in ('eligible', 'inventory'):
                    require(current in (None, a['aircraft']), 'Unexpected pre-spawn airframe', s)
                    pilots = p.get('pilotHealth'); eligible = current is None or p.get('disabled') is True
                    if not eligible:
                        require(isinstance(pilots, list) and pilots and all(type(x.get('dead')) is bool and type(x.get('ejected')) is bool for x in pilots), 'Invalid pilot eligibility', s)
                        eligible = all(x['dead'] or x['ejected'] for x in pilots)
                    lease = s.get('controlLease', {})
                    require(lease.get('active') is False and lease.get('aircraftNetId') is None and lease.get('fireRequested') is False, 'Post-eject lease uncleared', s)
                    if eligible and a['stage'] == 'eligible':
                        action(n, 'reserve', spec['reserve'][n]); a['stage'] = 'inventory'
                    elif eligible and type(s.get('localPlayerOwnedCount')) is int and s['localPlayerOwnedCount'] >= 1:
                        action(n, 'spawn', spec['spawn'][n]); a['stage'] = 'association'
                elif a['stage'] == 'association':
                    if current in (None, a['aircraft']): continue
                    require(type(current) is int and current not in seen, 'Replacement reused aircraft ID', s)
                    require(p.get('disabled') is False, 'Replacement disabled', s)
                    position = p.get('position')
                    require(isinstance(position, list) and len(position) == 3 and all(finite(v) for v in position),
                            'Replacement finite 3D position unavailable', s)
                    a['completedLives'].append(dict(aircraft=a['aircraft'], motion=a['lifeMotion'],
                                                    reachedAirborne=a['lifeReachedAirborne']))
                    seen.add(current); a['aircraft'] = current; a['lives'].append(current)
                    a['lifeStart'] = now; a['lifeInitialPosition'] = list(position)
                    a['lifeMotion'] = 0.; a['lifeReachedAirborne'] = False; a['previousEndpoint'] = None
                    action(n, 'engine', {'on': True}); a['stage'] = 'engine'
                elif a['stage'] == 'engine':
                    require(current == a['aircraft'], 'Replacement association changed', s)
                    if p.get('ignition') is True:
                        require_motion(p, s)
                        pilots = p.get('pilotHealth')
                        require(p.get('disabled') is False and isinstance(pilots, list) and pilots
                                and all(x.get('dead') is False and x.get('ejected') is False for x in pilots),
                                'Replacement pilot unavailable', s)
                        a['recoveries'].append(dict(startSeconds=a['recoveryStart']-started, finishedSeconds=now-started))
                        a['stage'] = 'active'; a['nextFly'] = now
                        action(n, 'fly', {'seconds': 45, 'fire': False}); a['nextFly'] = clock()+20
        if now-started >= spec['seconds']:
            require(all(a['stage'] == 'active' for a in actors.values()), 'Task ended during recovery', actors)
            require(all(any(life['motion'] >= 100 and life['reachedAirborne'] for life in
                            a['completedLives'] + [dict(motion=a['lifeMotion'], reachedAirborne=a['lifeReachedAirborne'])])
                        for a in actors.values()), 'Meaningful airborne moving life missing', actors)
            for a in actors.values(): a['lifeCount'] = len(a['lives'])
            return dict(taskOutcome='SUSTAINED_PLAYER_TASK_COMPLETED', actors=actors, actions=actions,
                        survivalRequirementPassed=all(not a['losses'] for a in actors.values()),
                        fullDurationAirborneRequirementPassed=all(a['airborneSeconds'] >= spec['seconds'] for a in actors.values()),
                        airborneCoverageSemantics='adjacent client sampled endpoint coverage; not continuous native proof',
                        authoritativeServerStateObserved=False, perfComparisonEligible=False)
        check_cancel(); sleep(1)
