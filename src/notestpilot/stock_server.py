"""Explicit stock Linux server foundation. No bridge, loader, auth patch or server RPC.
Input manifest pins the complete separately reviewed stock payload.
Native coverage and remaining limits are recorded in docs/stock-server.md.
"""
from __future__ import annotations
import hashlib,json,os,shutil,subprocess,sys
from pathlib import Path,PurePosixPath
GAME_SHA='df5bed594dd84912efb3e57faa75b37d7e327bf4c8f5418f50411ad0ff46e24a'
UNITY_SHA='10a1fc42d2f7a7c6c4f24c7253b75b222d0d776d6e448b516eb8ecb5e9f88176'
EXECUTABLE='NuclearOptionServer.x86_64'
REVIEWED_STOCK_FILES_SHA='570b7a0ff4249161e7cf002d075006c5e7f7c1aaf1e9de61dc68571a7c4fee71'
EXECUTABLE_SHA='2f4534b3534d61c843a7f20cbd5df7c2deef9fb0211d60ea362cda7bdecdeabc'
MONO_SHA='4b4cb5bff9a6326b5423bcd0c4511f1b523f9f3323f90b5d57bf27323d8b6152'
REQUIRED_MANAGED=('Assembly-CSharp.dll','Mirage.dll','Mirage.SocketLayer.dll','mscorlib.dll','Newtonsoft.Json.dll','UniTask.dll','UnityEngine.AnimationModule.dll','UnityEngine.AudioModule.dll','UnityEngine.CoreModule.dll','UnityEngine.dll','UnityEngine.ParticleSystemModule.dll','UnityEngine.PhysicsModule.dll','UnityEngine.TerrainModule.dll')

def digest(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 return h.hexdigest()

def stock_relative(value):
 if not isinstance(value,str) or not value:raise ValueError('empty stock path')
 p=PurePosixPath(value)
 if p.is_absolute() or '..' in p.parts or ':' in value or '\\' in value or p.as_posix()!=value:raise ValueError('unsafe stock path')
 if any('doorstop' in part.lower() or 'bepinex' in part.lower() or 'notestpilot' in part.lower() for part in p.parts):raise ValueError('loader/bridge input forbidden')
 return p

def validate_manifest(manifest,current_managed_pins,allowed_payload_paths,current_native_pins):
 if manifest.get('version')!=1 or manifest.get('kind')!='stock-linux-server' or not isinstance(manifest.get('files'),dict):raise ValueError('stock manifest required')
 files=manifest['files'];fold=set()
 if set(files)!=set(allowed_payload_paths):raise ValueError('reviewed explicit payload allowlist mismatch')
 for name in allowed_payload_paths:stock_relative(name)
 for name,sha in files.items():
  stock_relative(name)
  if name.casefold() in fold:raise ValueError('duplicate stock path')
  fold.add(name.casefold())
  if not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):raise ValueError('invalid stock hash')
 if EXECUTABLE not in current_native_pins or 'UnityPlayer.so' not in current_native_pins or not any(PurePosixPath(n).name=='libmonobdwgc-2.0.so' for n in current_native_pins):raise ValueError('reviewed executable/Unity/Mono native pins required')
 if current_native_pins[EXECUTABLE]!=EXECUTABLE_SHA or current_native_pins['UnityPlayer.so']!=UNITY_SHA or any(v!=MONO_SHA for n,v in current_native_pins.items() if PurePosixPath(n).name=='libmonobdwgc-2.0.so'):raise ValueError('exact current native build required')
 if any(files.get(n)!=sha for n,sha in current_native_pins.items()):raise ValueError('current native pin mismatch')
 prefix='NuclearOptionServer_Data/Managed/'
 if set(current_managed_pins)!={prefix+n for n in REQUIRED_MANAGED}:raise ValueError('all13 reviewed current managed pins required')
 if any(files.get(n)!=sha for n,sha in current_managed_pins.items()):raise ValueError('current managed reference pin mismatch')
 if files.get(prefix+'Assembly-CSharp.dll')!=GAME_SHA or files.get('UnityPlayer.so')!=UNITY_SHA or EXECUTABLE not in files:raise ValueError('exact game/native/executable pins missing')
 if hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()!=REVIEWED_STOCK_FILES_SHA:raise ValueError('reviewed complete stock file pins mismatch')
 return files

def guarded_path(value,root):
 if '..' in Path(value).parts or '..' in Path(root).parts:raise ValueError('path traversal forbidden')
 p=Path(value).absolute();r=Path(root).absolute()
 if any(x.is_symlink() for x in [p,*p.parents,r,*r.parents]):raise ValueError('symlink path/parent forbidden')
 if p==r or not p.is_relative_to(r):raise ValueError('path outside explicit root')
 return p

def separate_roots(input_root,lab_root):
 if '..' in Path(input_root).parts or '..' in Path(lab_root).parts:raise ValueError('root traversal forbidden')
 a=Path(input_root).absolute();b=Path(lab_root).absolute()
 if a.is_relative_to(b) or b.is_relative_to(a):raise ValueError('input/lab roots overlap')
 if any(x.is_symlink() for x in [a,*a.parents,b,*b.parents]):raise ValueError('symlink roots forbidden')

def verify_source_inventory(source,inventory):
 # Mixed instrumented input may be the source; only an explicit stock subset is copied.
 # Its reviewed complete inventory must remain unchanged, including omitted loader files.
 actual=set()
 for p in source.rglob('*'):
  if p.is_symlink():raise ValueError('source inventory symlink')
  if p.is_file():actual.add(p.relative_to(source).as_posix())
 if actual!=set(inventory):raise ValueError('source inventory changed')
 for name,sha in inventory.items():
  rel=PurePosixPath(name)
  if rel.is_absolute() or '..' in rel.parts or ':' in name or '\\' in name or rel.as_posix()!=name:raise ValueError('unsafe source inventory path')
  if digest(source/name)!=sha:raise ValueError('source inventory hash changed')

def prepare_stock(source,destination,manifest,current_managed_pins,*,allowed_payload_paths,current_native_pins,source_inventory,input_root,lab_root):
 if sys.platform!='linux':raise ValueError('stock preparation Linux only; no Windows game preparation')
 separate_roots(input_root,lab_root)
 source=guarded_path(source,input_root);destination=guarded_path(destination,lab_root);files=validate_manifest(manifest,current_managed_pins,allowed_payload_paths,current_native_pins)
 if destination.exists() or destination.is_symlink():raise ValueError('fresh exclusive stock destination required')
 if destination.resolve().is_relative_to(source):raise ValueError('stock destination inside input source')
 verify_source_inventory(source,source_inventory)
 # Validate every file before creating anything. Never follow input symlinks.
 for name,expected in files.items():
  p=source/name
  if any(x.is_symlink() for x in [p,*p.parents] if x.is_relative_to(source)) or not p.is_file() or digest(p)!=expected:raise ValueError('stock input changed/nonregular: '+name)
 destination.mkdir(parents=True,exist_ok=False)
 for name,expected in files.items():
  p=source/name;q=destination/name;q.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,q)
  if digest(q)!=expected:raise ValueError('stock copy mismatch: '+name)
 verify_source_inventory(source,source_inventory)
 (destination/'.stock-inputs.json').write_text(json.dumps(manifest,sort_keys=True)+'\n',encoding='utf-8')
 return destination

def clean_server_environment(environment):
 return {k:v for k,v in environment.items() if not k.startswith(('LD_','MONO_','DOORSTOP_','NOTESTPILOT_','BEPINEX_'))}

def validate_config(config):
 if config.get('Hidden') is not True or config.get('ModdedServer') is not False or config.get('DisableErrorKick',False) is not False:raise ValueError('private stock/error-preserving config required')
 if config.get('MaxPlayers')!=4 or config.get('Password') is not None and not isinstance(config.get('Password'),str):raise ValueError('four-client native password field required')
 port=config.get('Port',{})
 if port.get('IsOverride') is not True or not isinstance(port.get('Value'),int) or not 1024<=port['Value']<=65535:raise ValueError('explicit UDP port required')
 rotation=config.get('MissionRotation')
 if not isinstance(rotation,list) or len(rotation)!=1 or rotation[0].get('Key')!={'Group':'BuiltIn','Name':'Escalation'}:raise ValueError('stock Escalation rotation required')
 if config.get('BanListPaths')!=[] or config.get('ErrorKickImmuneListPaths')!=[]:raise ValueError('external fixture lists forbidden')
 query=config.get('QueryPort',{})
 if query.get('IsOverride') is not True or type(query.get('Value')) is not int or not 1024<=query['Value']<=65535 or query['Value']==port['Value']:raise ValueError('explicit distinct query port required')
 if rotation[0].get('MaxTime')!=7200.0:raise ValueError('reviewed rotation max time required')
 if config.get('MissionDirectory') not in (None,''):raise ValueError('custom mission directory forbidden')

def launch_stock(folder,config,log_path,environment=None,*,allowed_payload_paths,current_managed_pins,current_native_pins,input_root,lab_root,expected_manifest_sha):
 if sys.platform!='linux':raise ValueError('stock Linux server only; no Windows game launch')
 separate_roots(input_root,lab_root)
 folder=guarded_path(folder,lab_root);log_path=guarded_path(log_path,lab_root)
 if log_path.exists():raise ValueError('fresh exclusive log required')
 if log_path.is_relative_to(folder) or folder.is_relative_to(log_path):raise ValueError('log/payload separation required')
 validate_config(config)
 marker=folder/'.stock-inputs.json'
 if marker.is_symlink() or digest(marker)!=expected_manifest_sha:raise ValueError('prepared manifest identity changed')
 manifest=json.loads(marker.read_text());files=validate_manifest(manifest,current_managed_pins,allowed_payload_paths,current_native_pins)
 # Full payload hash and forbidden-loader recheck immediately before launch.
 for name,expected in files.items():
  stock_relative(name);p=folder/name
  if p.is_symlink() or not p.is_file() or digest(p)!=expected:raise ValueError('prepared stock payload changed: '+name)
 for p in folder.rglob('*'):
  if p.is_file() and p.relative_to(folder).as_posix() not in set(files)|{'.stock-inputs.json'}:raise ValueError('unlisted stock payload file')
  if p.is_symlink() or any(x in p.name.lower() for x in ('doorstop','bepinex','notestpilot')):raise ValueError('stock folder contaminated')
 config_path=folder/'DedicatedServerConfig.json'
 with config_path.open('x',encoding='utf-8') as f:json.dump(config,f)
 env=clean_server_environment(os.environ if environment is None else environment)
 env['LD_LIBRARY_PATH']=str(folder)+':'+str(folder/'linux64')
 command=[str(folder/EXECUTABLE),'-batchmode','-nographics','-limitframerate','60','-socket','UDP','-DedicatedServer',str(config_path),'-logFile',str(Path(log_path).absolute())]
 # Client-only bridge process setup remains the existing runner's responsibility.
 return subprocess.Popen(command,cwd=folder,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

def bounded_read(path,cap):
 with Path(path).open('rb') as f:data=f.read(cap+1)
 if len(data)>cap:raise ValueError('proc evidence exceeds bound')
 return data

def proc_start_tick(stat_text):
 end=stat_text.rfind(')');fields=stat_text[end+2:].split()
 if end<0 or len(fields)<20:raise ValueError('invalid proc stat')
 return int(fields[19])

def verify_owned_stock_process(pid,expected_start_tick,folder,expected_cgroup,expected_argv,proc_root=Path('/proc')):
 folder=Path(folder).resolve();root=Path(proc_root)/str(pid)
 if not expected_cgroup.startswith('/') or '..' in PurePosixPath(expected_cgroup).parts:raise ValueError('exact expected cgroup required')
 if proc_start_tick((root/'stat').read_text())!=expected_start_tick:raise ValueError('PID start identity changed')
 groups=(root/'cgroup').read_text().splitlines()
 if groups!=['0::'+expected_cgroup]:raise ValueError('owned cgroup mismatch')
 args=bounded_read(root/'cmdline',65536).rstrip(b'\0').decode().split('\0')
 expected=list(expected_argv)
 required=[str(folder/EXECUTABLE),'-batchmode','-nographics','-limitframerate','60','-socket','UDP','-DedicatedServer',str(folder/'DedicatedServerConfig.json'),'-logFile']
 if len(expected)!=11 or expected[:10]!=required or not Path(expected[10]).is_absolute():raise ValueError('explicit stock argv required')
 if args!=expected or not args or Path(args[0]).resolve()!=folder/EXECUTABLE:raise ValueError('stock command identity mismatch')
 env={x.split('=',1)[0]:x.split('=',1)[1] for x in bounded_read(root/'environ',1024*1024).decode().split('\0') if '=' in x}
 expected_library_path=str(folder)+':'+str(folder/'linux64')
 if env.get('LD_LIBRARY_PATH')!=expected_library_path:raise ValueError('stock library path mismatch')
 ordinary={k:v for k,v in env.items() if k!='LD_LIBRARY_PATH'}
 if ordinary!=clean_server_environment(ordinary):raise ValueError('loader/bridge environment found')
 maps=bounded_read(root/'maps',16*1024*1024).decode()
 if any(x in maps.lower() for x in ('doorstop','bepinex','notestpilot.bridge')):raise ValueError('loader/bridge module mapped')
 if Path(os.readlink(root/'cwd')).resolve()!=folder or Path(os.readlink(root/'exe')).resolve()!=folder/EXECUTABLE:raise ValueError('stock executable/cwd mismatch')
 if (root/'cgroup').read_text().splitlines()!=['0::'+expected_cgroup]:raise ValueError('owned cgroup changed during verification')
 if proc_start_tick((root/'stat').read_text())!=expected_start_tick:raise ValueError('PID changed during identity verification')
 return {'pid':pid,'startTick':expected_start_tick,'mode':'stock-server/client-adapter','serverRpc':False,'authoritativeServerStateObserved':False,'expectedCgroup':expected_cgroup,'dynamicManagedAssemblyAbsenceProven':False}


def external_resource_sample(pid,expected_start_tick,folder,expected_cgroup,expected_argv,proc_root=Path('/proc')):
 """Whole owned process counters only; no Unity frame/fixed-step/state claims."""
 identity=verify_owned_stock_process(pid,expected_start_tick,folder,expected_cgroup,expected_argv,proc_root)
 root=Path(proc_root)/str(pid);stat=(root/'stat').read_text();fields=stat[stat.rfind(')')+2:].split()
 if proc_start_tick(stat)!=expected_start_tick:raise ValueError('PID changed before resource sample')
 if len(fields)<22:raise ValueError('incomplete process counters')
 cpu_ticks=int(fields[11])+int(fields[12]);rss_pages=int(fields[21])
 if cpu_ticks<0 or rss_pages<0:raise ValueError('invalid process counters')
 if proc_start_tick((root/'stat').read_text())!=expected_start_tick:raise ValueError('PID changed during resource sample')
 verify_owned_stock_process(pid,expected_start_tick,folder,expected_cgroup,expected_argv,proc_root)
 return dict(identity,cpuSeconds=cpu_ticks/os.sysconf('SC_CLK_TCK'),residentBytes=rss_pages*os.sysconf('SC_PAGE_SIZE'))
