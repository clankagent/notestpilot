import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from notestpilot import stock_server as s
class StockFoundationTests(unittest.TestCase):
 def setUp(self):
  m,_=self.manifest();self.pin=patch.object(s,'REVIEWED_STOCK_FILES_SHA',hashlib.sha256(json.dumps(m['files'],sort_keys=True,separators=(',',':')).encode()).hexdigest());self.pin.start();self.addCleanup(self.pin.stop)
 def manifest(self):
  pins={'NuclearOptionServer_Data/Managed/'+n:'a'*64 for n in s.REQUIRED_MANAGED};pins['NuclearOptionServer_Data/Managed/Assembly-CSharp.dll']=s.GAME_SHA
  return {'version':1,'kind':'stock-linux-server','files':dict(pins,**{'UnityPlayer.so':s.UNITY_SHA,s.EXECUTABLE:s.EXECUTABLE_SHA,'MonoBleedingEdge/EmbedRuntime/libmonobdwgc-2.0.so':s.MONO_SHA})},pins
 def native_pins(self,m):return {n:v for n,v in m['files'].items() if n in (s.EXECUTABLE,'UnityPlayer.so') or n.endswith('libmonobdwgc-2.0.so')}
 def test_exact_managed_pins_required(self):
  m,p=self.manifest();s.validate_manifest(m,p,set(m['files']),self.native_pins(m));m['files']['NuclearOptionServer_Data/Managed/Mirage.dll']='0'*64
  with self.assertRaisesRegex(ValueError,'pin mismatch'):s.validate_manifest(m,p,set(m['files']),self.native_pins(m))
 def test_no_loader_path_or_escape(self):
  for p in ('BepInEx/core/a.dll','libdoorstop.so','../game','/tmp/game','a\\b'):
   with self.subTest(p=p),self.assertRaises(ValueError):s.stock_relative(p)
 def test_server_environment_sanitized_clients_untouched(self):
  original={'PATH':'path','LD_PRELOAD':'loader','DOORSTOP_ENABLED':'1','NOTESTPILOT_TOKEN':'private','MONO_ENV_OPTIONS':'inject','MONO_PROFILER':'inject','LD_LIBRARY_PATH':'foreign','LD_DEBUG':'all'}
  self.assertEqual(s.clean_server_environment(original),{'PATH':'path'});self.assertIn('NOTESTPILOT_TOKEN',original)
 def test_windows_prepare_refuses_before_copy(self):
  with patch.object(s.sys,'platform','win32'),self.assertRaisesRegex(ValueError,'Linux only'):s.prepare_stock('missing','missing',{}, {},allowed_payload_paths=[],current_native_pins={},source_inventory={},input_root='input',lab_root='lab')
 def test_windows_launch_refuses_before_process(self):
  with patch.object(s.sys,'platform','win32'),patch.object(s.subprocess,'Popen') as launch,self.assertRaises(ValueError):s.launch_stock('missing',{},'log',allowed_payload_paths=[],current_managed_pins={},current_native_pins={},input_root='input',lab_root='lab',expected_manifest_sha='')
  launch.assert_not_called()
 def native_config(self):
  # Native serializer shape, sanitized portable fields; built-in rotation retained.
  return {'MissionDirectory':'','ModdedServer':False,'Hidden':True,'ServerName':'Disposable stock test','Port':{'IsOverride':True,'Value':17777},'QueryPort':{'IsOverride':True,'Value':19778},'Password':'','MaxPlayers':4,'BanListPaths':[],'DisableErrorKick':False,'ErrorKickImmuneListPaths':[],'NoPlayerStopTime':30.0,'PostMissionDelay':30.0,'RotationType':0,'MissionRotation':[{'Key':{'Group':'BuiltIn','Name':'Escalation'},'MaxTime':7200.0}]}
 def test_native_empty_mission_directory(self):s.validate_config(self.native_config())
 def test_error_suppression_config_rejected(self):
  c=self.native_config();c['DisableErrorKick']=True
  with self.assertRaises(ValueError):s.validate_config(c)
 def test_explicit_allowlist_rejects_extra_payload(self):
  m,p=self.manifest();allowed=set(m['files']);m['files']['foreign.so']='0'*64
  with self.assertRaisesRegex(ValueError,'allowlist'):s.validate_manifest(m,p,allowed,self.native_pins(m))
 def test_root_overlap_and_escape(self):
  with self.assertRaises(ValueError):s.separate_roots('/input','/input/lab')
  with self.assertRaises(ValueError):s.guarded_path('/outside/game','/lab')
  with self.assertRaisesRegex(ValueError,'traversal'):s.guarded_path('/lab/../outside','/lab')
  with self.assertRaisesRegex(ValueError,'traversal'):s.separate_roots('../source','/lab')
 def test_symlink_parent_refused(self):
  with patch.object(Path,'is_symlink',lambda p:p.name=='alias'):
   with self.assertRaisesRegex(ValueError,'symlink'):s.guarded_path('/lab/alias/game','/lab')
 def test_native_build_pin_not_caller_arbitrary(self):
  m,p=self.manifest();m['files'][s.EXECUTABLE]='b'*64
  with self.assertRaisesRegex(ValueError,'exact current native'):s.validate_manifest(m,p,set(m['files']),self.native_pins(m))
 def test_external_lists_query_and_rotation_rejected(self):
  for field,value in [('BanListPaths',['foreign']),('ErrorKickImmuneListPaths',['foreign']),('QueryPort',{'IsOverride':True,'Value':17777}),('MissionRotation',[{'Key':{'Group':'BuiltIn','Name':'Escalation'},'MaxTime':1}])]:
   c=self.native_config();c[field]=value
   with self.subTest(field=field),self.assertRaises(ValueError):s.validate_config(c)
 def test_bounded_read_rejects_oversize(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'evidence';p.write_bytes(b'12345')
   with self.assertRaisesRegex(ValueError,'bound'):s.bounded_read(p,4)
 def test_missing_command_argument_refused_cleanly(self):
  with tempfile.TemporaryDirectory() as d:
   folder=Path(d).resolve();root=self.proc(folder);(root/'cmdline').write_bytes((str(folder/s.EXECUTABLE)+'\0-socket\0').encode())
   with self.assertRaisesRegex(ValueError,'command'):self.verify(folder)
 def test_source_inventory_tamper_and_extra_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);p=root/'stock';p.write_bytes(b'original');inventory={'stock':s.digest(p)};s.verify_source_inventory(root,inventory)
   p.write_bytes(b'changed')
   with self.assertRaisesRegex(ValueError,'hash changed'):s.verify_source_inventory(root,inventory)
   p.write_bytes(b'original');(root/'extra').write_bytes(b'extra')
   with self.assertRaisesRegex(ValueError,'inventory changed'):s.verify_source_inventory(root,inventory)
 def test_prepared_copy_then_contamination_and_existing_log_refuse_launch(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);inputs=root/'inputs';lab=root/'lab';source=inputs/'game';dest=lab/'server';source.mkdir(parents=True);lab.mkdir()
   paths=['NuclearOptionServer_Data/Managed/'+n for n in s.REQUIRED_MANAGED]+['UnityPlayer.so',s.EXECUTABLE,'MonoBleedingEdge/EmbedRuntime/libmonobdwgc-2.0.so']
   files={}
   for n in paths:
    f=source/n;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(n.encode());files[n]=s.digest(f)
   pins={n:v for n,v in files.items() if '/Managed/' in n};manifest={'version':1,'kind':'stock-linux-server','files':files};native=self.native_pins(manifest)
   with patch.object(s,'REVIEWED_STOCK_FILES_SHA',hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()),patch.object(s.sys,'platform','linux'),patch.object(s,'GAME_SHA',pins['NuclearOptionServer_Data/Managed/Assembly-CSharp.dll']),patch.object(s,'UNITY_SHA',files['UnityPlayer.so']),patch.object(s,'EXECUTABLE_SHA',files[s.EXECUTABLE]),patch.object(s,'MONO_SHA',native['MonoBleedingEdge/EmbedRuntime/libmonobdwgc-2.0.so']),patch.object(s.subprocess,'Popen') as launch:
    kwargs=dict(allowed_payload_paths=set(files),current_native_pins=native,input_root=inputs,lab_root=lab)
    s.prepare_stock(source,dest,manifest,pins,source_inventory=files,**kwargs)
    (dest/'foreign.dll').write_bytes(b'foreign')
    with self.assertRaisesRegex(ValueError,'unlisted'):
     s.launch_stock(dest,self.native_config(),lab/'log',current_managed_pins=pins,expected_manifest_sha=s.digest(dest/'.stock-inputs.json'),**kwargs)
    (dest/'foreign.dll').unlink();(lab/'log').write_bytes(b'preserved')
    with self.assertRaisesRegex(ValueError,'exclusive log'):
     s.launch_stock(dest,self.native_config(),lab/'log',current_managed_pins=pins,expected_manifest_sha=s.digest(dest/'.stock-inputs.json'),**kwargs)
    launch.assert_not_called();self.assertEqual((lab/'log').read_bytes(),b'preserved')
 def test_caller_cannot_self_authorize_extra_stock_file(self):
  m,p=self.manifest();m['files']['foreign.asset']='0'*64
  with self.assertRaisesRegex(ValueError,'complete stock file pins'):s.validate_manifest(m,p,set(m['files']),self.native_pins(m))
 def proc(self,folder,pid=99):
  root=folder/'proc'/str(pid);root.mkdir(parents=True);fields=['S']+['0']*21;fields[19]='123'
  (root/'stat').write_text(str(pid)+' (game name) '+' '.join(fields));(root/'cmdline').write_bytes(('\0'.join([str(folder/s.EXECUTABLE),'-batchmode','-nographics','-limitframerate','60','-socket','UDP','-DedicatedServer',str(folder/'DedicatedServerConfig.json'),'-logFile',str(folder.parent/'log')])+'\0').encode());(root/'environ').write_bytes(('PATH=/bin\0LD_LIBRARY_PATH='+str(folder)+':'+str(folder/'linux64')+'\0').encode());(root/'maps').write_text('normal UnityPlayer.so');(root/'cgroup').write_text('0::/owned.service\n');return root
 def verify(self,folder):
  def link(p):return str(folder/s.EXECUTABLE) if Path(p).name=='exe' else str(folder)
  with patch.object(s.os,'readlink',side_effect=link):return s.verify_owned_stock_process(99,123,folder,'/owned.service',[str(folder/s.EXECUTABLE),'-batchmode','-nographics','-limitframerate','60','-socket','UDP','-DedicatedServer',str(folder/'DedicatedServerConfig.json'),'-logFile',str(folder.parent/'log')],folder/'proc')
 def test_owned_process_identity(self):
  with tempfile.TemporaryDirectory() as d:
   folder=Path(d).resolve();self.proc(folder);self.assertFalse(self.verify(folder)['serverRpc'])
 def test_wrong_cgroup_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   folder=Path(d).resolve();root=self.proc(folder);(root/'cgroup').write_text('0::/other.service\n')
   with self.assertRaisesRegex(ValueError,'cgroup'):self.verify(folder)
 def test_live_library_path_mismatch_refused(self):
  with tempfile.TemporaryDirectory() as d:
   folder=Path(d).resolve();root=self.proc(folder);(root/'environ').write_bytes(b'PATH=/bin\0LD_LIBRARY_PATH=/foreign\0')
   with self.assertRaisesRegex(ValueError,'library path'):self.verify(folder)
 def test_live_mono_profiler_refused(self):
  with tempfile.TemporaryDirectory() as d:
   folder=Path(d).resolve();root=self.proc(folder);(root/'environ').write_bytes((root/'environ').read_bytes()+b'MONO_PROFILER=foreign\0')
   with self.assertRaisesRegex(ValueError,'environment found'):self.verify(folder)
 def test_pid_reuse_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   folder=Path(d).resolve();root=self.proc(folder);(root/'stat').write_text((root/'stat').read_text().replace('123','124'))
   with self.assertRaisesRegex(ValueError,'start identity'):self.verify(folder)
 def test_mapped_loader_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   folder=Path(d).resolve();root=self.proc(folder);(root/'maps').write_text('libdoorstop.so')
   with self.assertRaisesRegex(ValueError,'module mapped'):self.verify(folder)
 def test_bridge_environment_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   folder=Path(d).resolve();root=self.proc(folder);(root/'environ').write_bytes((root/'environ').read_bytes()+b'NOTESTPILOT_ENABLE=1\0')
   with self.assertRaisesRegex(ValueError,'environment found'):self.verify(folder)
if __name__=='__main__':unittest.main()
