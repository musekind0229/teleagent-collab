"""Fail-closed registration/GitHub gate for the single production checkout.

Registration is local operator state, never an environment switch or repo file.
Function injection is for synthetic unit tests, not a CLI bypass/second entry.
This is an operational guard, not an OS sandbox against arbitrary Python code.
"""
from pathlib import Path
import ctypes,http.client,json,os,re,subprocess
GITHUB_URL='https://github.com/musekind0229/teleagent-collab.git'
BRANCH='integration/core-convergence-20261018'
class RuntimeRejected(ValueError):pass

def registration_path():
 if os.name=='nt':
  # Ask Windows, not LOCALAPPDATA/HOME supplied by a launcher.
  buf=ctypes.create_unicode_buffer(32768)
  if ctypes.windll.shell32.SHGetFolderPathW(None,28,None,0,buf)!=0:
   raise RuntimeRejected('cannot resolve authoritative Windows LocalAppData')
  return Path(buf.value)/'hermes/project-records/collab-v2-maintenance/runtime-registration.json'
 return Path.home()/'.local/state/teleagent-collab/runtime-registration.json'

def github_head():
 """Public, fixed-host TLS ref lookup: no Git URL rewrites, proxy or auth pool."""
 conn=http.client.HTTPSConnection('api.github.com',timeout=15)
 try:
  conn.request('GET','/repos/musekind0229/teleagent-collab/git/ref/heads/'+BRANCH,headers={'User-Agent':'collab-runtime-guard','Accept':'application/vnd.github+json'})
  reply=conn.getresponse()
  if reply.status!=200:raise RuntimeRejected('GitHub published ref unavailable (HTTP '+str(reply.status)+')')
  raw=reply.read(65537)
  if len(raw)>65536:raise RuntimeRejected('GitHub ref response over bound')
  value=json.loads(raw)
  if value.get('ref')!='refs/heads/'+BRANCH or value.get('object',{}).get('type')!='commit':raise RuntimeRejected('unexpected GitHub ref object')
  head=value['object']['sha']
  if not re.fullmatch('[0-9a-f]{40}',head):raise RuntimeRejected('invalid GitHub commit SHA')
  return head
 except (OSError,ValueError,KeyError,TypeError,http.client.HTTPException)as exc:
  raise RuntimeRejected('GitHub published ref unknown: '+str(exc))from exc
 finally:conn.close()

def verify_runtime(root=None, *, registration_path=None):
 root=Path(root or Path(__file__).resolve().parents[2]).resolve()
 registry=Path(registration_path)if registration_path is not None else globals()['registration_path']()
 try:
  if any(p.is_symlink()or getattr(p.lstat(),'st_file_attributes',0)&0x400 for p in [registry,*registry.parents]if p.exists()):
   raise RuntimeRejected('runtime registry reparse path refused')
  rule=json.loads(registry.read_bytes())
  registered=Path(rule['runtime_root']).resolve()
 except (OSError,KeyError,TypeError,ValueError)as exc:
  raise RuntimeRejected('runtime registration missing/invalid: '+str(exc))from exc
 if root!=registered:raise RuntimeRejected('unregistered runtime directory: '+str(root))
 if rule.get('github_url')!=GITHUB_URL or rule.get('branch')!=BRANCH:
  raise RuntimeRejected('registration is not the user-authorized GitHub branch')
 if any(p.is_symlink()or getattr(p.lstat(),'st_file_attributes',0)&0x400 for p in [root,*root.parents]if p.exists()):
  raise RuntimeRejected('runtime reparse path refused')
 def git(args):
  env=dict(os.environ);env.update(GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='Never')
  # Do not accept caller Git config injection; ordinary installed Git/GCM
  # identity is used without reading/printing credentials.
  for key in list(env):
   if key in ('GIT_DIR','GIT_WORK_TREE','GIT_INDEX_FILE','GIT_CONFIG','GIT_CONFIG_COUNT')or key.startswith(('GIT_CONFIG_KEY_','GIT_CONFIG_VALUE_')):env.pop(key,None)
  try:
   proc=subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True,timeout=20,env=env)
  except (OSError,subprocess.TimeoutExpired)as exc:raise RuntimeRejected('GitHub/runtime evidence unavailable')from exc
  if proc.returncode:raise RuntimeRejected('GitHub/runtime verification failed; no startup')
  return proc.stdout.strip()
 if Path(git(['rev-parse','--show-toplevel'])).resolve()!=root:
  raise RuntimeRejected('runtime is not repository checkout root')
 if git(['branch','--show-current'])!=BRANCH:raise RuntimeRejected('runtime is not on authorized branch')
 head=git(['rev-parse','HEAD'])
 if not re.fullmatch('[0-9a-f]{40}',head):raise RuntimeRejected('invalid runtime HEAD')
 if git(['status','--porcelain','--untracked-files=no']):raise RuntimeRejected('runtime tracked files/index are dirty')
 if git(['status','--porcelain','--untracked-files=all','--','src','bin','win_collab']):raise RuntimeRejected('runtime source contains uncommitted or untracked files')
 if github_head()!=head:
  raise RuntimeRejected('HEAD is not the current published GitHub authorized branch tip')
 return {'runtime_root':str(root),'head':head,'branch':BRANCH,'github_url':GITHUB_URL,'github_verified':True,'registration_path':str(registry)}
