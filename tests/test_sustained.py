import copy
import unittest
from notestpilot.sustained import execute_sustained, incoming_rpc_loss, require_motion, GAME_ASSEMBLY_SHA256
from notestpilot.runner import TestFailure


class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += seconds


class Bridge:
    def __init__(self, clock, ident, loss=False):
        self.clock=clock; self.ident=ident; self.aircraft=ident+100; self.loss=loss
        self.calls=[]; self.ejected=False; self.spawned=False; self.ignition=True
        self.reject=None; self.reuse=False; self.peer=False; self.stall=False
    def status(self):
        t=self.clock(); p=dict(netId=self.ident, local=True, faction='A', aircraftNetId=self.aircraft,
            disabled=False, ignition=self.ignition, pilotHealth=[dict(dead=False,ejected=self.ejected)], radarAltitude=100, speed=80,
            position=[t*10.,0.,100.])
        events=[]
        if self.loss and t>=5 and not self.spawned:
            events=[dict(action='DestructivePartApplyDamage', aircraftNetId=self.aircraft, playerNetId=self.ident,
                disabled=False, Ignition=True, pilots=[dict(dead=False,ejected=False)],
                seconds=5, callers=['Aircraft.UserCode_RpcDamage_870168505'], damage=dict(hitPointsBefore=100,
                predictedHitPointsAfter=-2,netPierceDamage=0,netBlastDamage=102,netFireDamage=0,netImpactDamage=0,
                part=dict(name='wing3_L',partID=13)))]
        lastfly=max((at for cmd,at in self.calls if cmd=='fly'), default=0)
        return dict(assemblySha256=GAME_ASSEMBLY_SHA256, role='client',clientActive=True, mission='Escalation',missionRunning=True,localPlayerNetId=self.ident,
            instance='newpeer' if self.peer and t>=10 else str(self.ident),errorCount=0,players=[p],localPlayerAircraftNetId=self.aircraft,
            localPlayerOwnedCount=0 if self.stall or (self.ejected and t<40) else 1,
            controlLease=dict(active=not self.ejected and t-lastfly<45, aircraftNetId=None if self.ejected else self.aircraft,
                              fireRequested=False,secondsRemaining=max(0,45-(t-lastfly))),
            observation=dict(realtimeSeconds=t,events=events,firstDamageEvents=[]))
    def call(self,cmd,args):
        self.calls.append((cmd,self.clock()))
        if cmd==self.reject:return {'accepted':False}
        if cmd=='eject':self.ejected=True
        if cmd=='spawn':
            self.aircraft=102 if self.reuse else self.ident+200
            self.spawned=True;self.ejected=False;self.ignition=False
        if cmd=='engine':self.ignition=True
        return {'accepted':True}


class SustainedTests(unittest.TestCase):
    def setup_run(self):
        c=Clock(); bs={'a':Bridge(c,1,True),'b':Bridge(c,2)}
        initial={n:b.status()['players'][0] for n,b in bs.items()}
        spec=dict(seconds=65,mission='Escalation',reserve={n:{'aircraft':'original'} for n in bs},spawn={n:{'loadout':'original'} for n in bs})
        return c,bs,initial,spec
    def run_task(self,c,bs,initial,spec):
        self.samples=[]
        return execute_sustained(bs,initial,spec,self.samples.append,lambda:None,lambda *args:None,c,c.sleep)
    def test_recovery_keeps_other_lease_and_unique_actions(self):
        c,bs,i,s=self.setup_run(); result=self.run_task(c,bs,i,s)
        self.assertEqual([0,20,40,60],[t for cmd,t in bs['b'].calls if cmd=='fly'])
        for cmd in ('eject','reserve','spawn','engine'):self.assertEqual(1,sum(x==cmd for x,t in bs['a'].calls))
        self.assertEqual([101,201],result['actors']['a']['lives'])
        self.assertFalse(result['survivalRequirementPassed']);self.assertFalse(result['perfComparisonEligible'])
        self.assertEqual(66,len(self.samples))
    def test_classifier_negative_evidence(self):
        c,bs,i,s=self.setup_run();c.now=5;original=bs['a'].status()
        self.assertIsNotNone(incoming_rpc_loss(original,1,101))
        for field,value in [('callers',['AeroPart.ApplyDamage']),('playerNetId',2),('seconds',-40)]:
            state=copy.deepcopy(original);state['observation']['events'][0][field]=value
            self.assertIsNone(incoming_rpc_loss(state,1,101))
        for field,value in [('netImpactDamage',1),('hitPointsBefore',float('nan')),('predictedHitPointsAfter',1)]:
            state=copy.deepcopy(original);state['observation']['events'][0]['damage'][field]=value
            self.assertIsNone(incoming_rpc_loss(state,1,101))
    def test_contact_before_rpc_rejected(self):
        c,bs,i,s=self.setup_run();c.now=5;state=bs['a'].status()
        state['observation']['events'].append(dict(action='Collision',aircraftNetId=101,seconds=4))
        with self.assertRaises(TestFailure):incoming_rpc_loss(state,1,101)
    def test_rejected_action_and_timeout(self):
        for mode in ('reject','stall'):
            c,bs,i,s=self.setup_run();s['seconds']=250
            if mode=='reject':bs['a'].reject='reserve'
            else:bs['a'].stall=True
            with self.assertRaises(TestFailure):self.run_task(c,bs,i,s)
            self.assertLessEqual(sum(cmd=='reserve' for cmd,t in bs['a'].calls),1)
    def test_peer_and_airframe_reuse_fail(self):
        for mode in ('peer','reuse'):
            c,bs,i,s=self.setup_run();setattr(bs['a'],mode,True)
            with self.assertRaises(TestFailure):self.run_task(c,bs,i,s)
    def test_failing_sample_saved_before_checks(self):
        c,bs,i,s=self.setup_run();bs['a'].peer=True
        with self.assertRaises(TestFailure):self.run_task(c,bs,i,s)
        self.assertEqual(10,self.samples[-1]['seconds'])
    def test_missing_journal_malformed_damage_and_expired_lease(self):
        c,bs,i,s=self.setup_run();c.now=5; original=bs['a'].status()
        for mode in ('journal','payload','lease'):
            state=copy.deepcopy(original)
            if mode=='journal':state['observation']['events']=None
            if mode=='payload':state['observation']['events'][0]['damage']=None
            if mode=='lease':state['controlLease']['secondsRemaining']=0
            with self.assertRaises(TestFailure):incoming_rpc_loss(state,1,101)
    def test_fourth_recovery_and_errors_fail(self):
        c,bs,i,s=self.setup_run();s['seconds']=120
        original_status=bs['a'].status;c.now=5; witness=copy.deepcopy(original_status()['observation']['events'][0]);c.now=0
        def repeated():
            state=original_status()
            if bs['a'].spawned and bs['a'].ignition:
                e=copy.deepcopy(witness);e['aircraftNetId']=bs['a'].aircraft;e['seconds']=c()
                state['observation']['events']=[e]
            return state
        bs['a'].status=repeated
        # Distinct ordinary replacement IDs across each life.
        original_call=bs['a'].call
        def new_spawn(command,args):
            result=original_call(command,args)
            if command=='spawn':bs['a'].aircraft=200+sum(cmd=='spawn' for cmd,t in bs['a'].calls)
            return result
        bs['a'].call=new_spawn
        with self.assertRaisesRegex(TestFailure,'Recovery budget exhausted'):self.run_task(c,bs,i,s)
        self.assertEqual(3,sum(cmd=='eject' for cmd,t in bs['a'].calls))
        c,bs,i,s=self.setup_run();original_status=bs['b'].status
        def error_status():
            state=original_status();state['errorCount']=1 if c()>=3 else 0;return state
        bs['b'].status=error_status
        with self.assertRaisesRegex(TestFailure,'New client errors'):self.run_task(c,bs,i,s)
    def change_motion(self,bs,transform):
        for bridge in bs.values():
            bridge.loss=False; original=bridge.status
            def status(original=original):
                state=original();transform(state['players'][0]);return state
            bridge.status=status
    def test_stationary_or_never_airborne_not_completed(self):
        for mode in ('stationary','taxi'):
            c,bs,i,s=self.setup_run()
            def transform(p):
                if mode=='stationary':p['position']=[0.,0.,100.]
                else:p['radarAltitude']=0.;p['speed']=20.
            self.change_motion(bs,transform)
            with self.assertRaisesRegex(TestFailure,'Meaningful airborne moving life missing'):self.run_task(c,bs,i,s)
    def test_takeoff_deadline_and_invalid_positions(self):
        c,bs,i,s=self.setup_run();s['seconds']=200
        self.change_motion(bs,lambda p:p.update(radarAltitude=0.,speed=20.))
        with self.assertRaisesRegex(TestFailure,'Life takeoff deadline exceeded'):self.run_task(c,bs,i,s)
        self.assertEqual(180,self.samples[-1]['seconds'])
        for position in ([0.,float('nan'),0.],[0.,0.],None):
            c,bs,i,s=self.setup_run();self.change_motion(bs,lambda p:p.update(position=position))
            with self.assertRaisesRegex(TestFailure,'Finite 3D position unavailable'):self.run_task(c,bs,i,s)
    def test_after_airborne_low_or_slow_is_not_recovery(self):
        for mode in ('alt','speed'):
            c,bs,i,s=self.setup_run()
            def transform(p):
                if c()>=1:p['radarAltitude' if mode=='alt' else 'speed']=0.
            self.change_motion(bs,transform)
            with self.assertRaisesRegex(TestFailure,'Unclassified grounded/slow airborne life'):self.run_task(c,bs,i,s)
            self.assertFalse(any(cmd=='eject' for cmd,t in bs['a'].calls))
    def test_coverage_excludes_recovery_and_life_transition(self):
        c,bs,i,s=self.setup_run();result=self.run_task(c,bs,i,s)
        a=result['actors']['a'];b=result['actors']['b']
        self.assertEqual(65.,b['airborneSeconds']);self.assertEqual(65.,b['activeSeconds'])
        self.assertEqual(26.,a['activeSeconds']);self.assertEqual(26.,a['airborneSeconds'])
        self.assertEqual(40.,a['completedLives'][0]['motion'])
        self.assertFalse(result['fullDurationAirborneRequirementPassed'])
    def test_continuous_airborne_sixty_five_seconds(self):
        c,bs,i,s=self.setup_run();self.change_motion(bs,lambda p:None)
        result=self.run_task(c,bs,i,s)
        self.assertTrue(result['fullDurationAirborneRequirementPassed'])
        for a in result['actors'].values():self.assertEqual(65.,a['airborneSeconds'])
    def test_initial_negative_motion_rejected_before_any_action(self):
        for key in ('speed',):
            c,bs,i,s=self.setup_run();self.change_motion(bs,lambda p:p.update({key:-10.}))
            with self.assertRaises(TestFailure):self.run_task(c,bs,i,s)
            self.assertTrue(all(not b.calls for b in bs.values()))
    def test_replacement_negative_motion_prevents_fly(self):
        for key in ('speed',):
            c,bs,i,s=self.setup_run();original=bs['a'].status
            def status():
                state=original()
                if bs['a'].spawned:state['players'][0][key]=-10.
                return state
            bs['a'].status=status
            with self.assertRaises(TestFailure):self.run_task(c,bs,i,s)
            self.assertEqual([0],[t for cmd,t in bs['a'].calls if cmd=='fly'])
            self.assertEqual('engine',bs['a'].calls[-1][0])
    def test_signed_ground_altitude_initial_lease_preserves_raw_value(self):
        c,bs,i,s=self.setup_run()
        def transform(p):
            if c()==0:p.update(radarAltitude=-0.000122189522,speed=0.256693065)
        self.change_motion(bs,transform)
        result=self.run_task(c,bs,i,s)
        self.assertEqual([0,20,40,60],[t for cmd,t in bs['b'].calls if cmd=='fly'])
        self.assertFalse(result['perfComparisonEligible'])
    def test_signed_ground_altitude_replacement_lease(self):
        c,bs,i,s=self.setup_run();original=bs['a'].status
        def status():
            state=original()
            if bs['a'].spawned and not any(cmd=='fly' and t>0 for cmd,t in bs['a'].calls):
                state['players'][0].update(radarAltitude=-0.000122189522,speed=0.256693065)
            return state
        bs['a'].status=status
        result=self.run_task(c,bs,i,s)
        self.assertEqual(1,len(result['actors']['a']['recoveries']))
        self.assertTrue(any(cmd=='fly' and t>0 for cmd,t in bs['a'].calls))
    def test_motion_domain_missing_bool_nonfinite_and_negative_speed(self):
        owner=dict(radarAltitude=-0.000122189522,speed=0.256693065)
        require_motion(owner,{})
        self.assertEqual(-0.000122189522,owner['radarAltitude'])
        for key in ('radarAltitude','speed'):
            for value in (None,True,False,float('nan'),float('inf'),-float('inf')):
                bad=dict(owner);bad[key]=value
                with self.assertRaises(TestFailure):require_motion(bad,{})
            bad=dict(owner);del bad[key]
            with self.assertRaises(TestFailure):require_motion(bad,{})
        with self.assertRaises(TestFailure):require_motion(dict(owner,speed=-0.0001),{})
    def test_missing_or_wrong_build_never_authorizes_loss(self):
        for build in (None,'wrong'):
            c,bs,i,s=self.setup_run();c.now=5;state=bs['a'].status();state['assemblySha256']=build
            with self.assertRaises(TestFailure):incoming_rpc_loss(state,1,101)
            c.now=0;original=bs['a'].status
            def status():
                state=original();state['assemblySha256']=build;return state
            bs['a'].status=status
            with self.assertRaises(TestFailure):self.run_task(c,bs,i,s)
            self.assertTrue(all(not b.calls for b in bs.values()))


if __name__=='__main__':unittest.main()
