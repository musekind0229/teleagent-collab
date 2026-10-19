"""Physical CLI tests on OWN synthetic external same-store; no models."""
import importlib.util,sys,os,json,tempfile,unittest,subprocess,hashlib,threading,socket,time
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
import test_goal_loop_maintenance as fixture
from framework.review_recovery import backup_existing_state
from framework.durable_api import DurableLayer
from test_goal_entry_handoff import GUARD
# Explicit child-only synthetic seam. No production CLI/environment bypass;
# child audit still forbids any model subprocess and remote access.
RUNTIME_TEST_SEAM='\nimport sys\nsys.path.insert(0,'+repr(str(ROOT/'src'))+')\nimport framework.runtime_guard as rg\nrg.verify_runtime=lambda *a,**k:{"synthetic_test_only":True}\n'

class Tests(unittest.TestCase):
 def test_cli_exact_review_grant_is_idempotent_without_native_replay(self):
  f=fixture.Tests();f.setUp()
  try:
   app,a,l,b,gid=f.make(answers=('partial',));out,s=f.drive(app,gid);t=s['tasks'][0];d=s['pending_decisions'][0]
   # Physical child only; parent fixture has Popen deny, explicitly scope owned CLI.
   (f.root/'host.json').write_text(json.dumps({'endpoint':'http://127.0.0.1:1','synthetic':True,'model_release':False}))
   guard=f.root/'guard';guard.mkdir();(guard/'sitecustomize.py').write_text(GUARD.replace('__PORT__','1'))
   # Release fixture Popen patch solely around owned CLI, child audit prevents grandchildren.
   import subprocess as sp
   # Get original callable from patcher's recorded originals, without unpatching other guards.
   raw=next(p.temp_original for p in unittest.mock._patch._active_patches if p.attribute=='Popen')
   def owned(argv,*args,**kw):
    if argv[:3]!=[sys.executable,'-B',str(ROOT/'bin/goal-entry.py')]:raise AssertionError('non-owned CLI')
    return raw(argv,*args,**kw)
   commands=[]
   def cli(*args,rc=0):
    env={k:os.environ[k]for k in ('SYSTEMROOT','WINDIR')if k in os.environ};env.update(PYTHONPATH=str(guard),PYTHONIOENCODING='utf-8')
    with patch('subprocess.Popen',side_effect=owned):
     p=sp.run([sys.executable,'-B',str(ROOT/'bin/goal-entry.py'),'--state',str(f.root),'--existing-state',*args],capture_output=True,text=True,env=env,timeout=20)
    commands.append({'args':args,'rc':p.returncode});self.assertEqual(p.returncode,rc,p.stdout+p.stderr);return json.loads(p.stdout)
   prepared=cli('review-prepare','--goal-id',gid,'--task-id',t['task_id'],'--decision-id',d['decision_id'])
   now=time.time();grant={'goal_id':gid,'grant_id':'physical-review','reason':'synthetic explicit owner','review_only':prepared['review_only'],'resource':{'package_id':'physical-review','native_limit':0,'lead_limit':1,'original_epoch':now,'checkpoint_epoch':now+60}}
   grantfile=f.root/'synthetic-review-grant.json';grantfile.write_text(json.dumps(grant))
   cli('review-authorize','--goal-id',gid,'--grant-file',str(grantfile));self.assertTrue(cli('review-authorize','--goal-id',gid,'--grant-file',str(grantfile))['duplicate'])
   l.answers=['pass'];f.drive(app,gid);self.assertEqual(b.starts,1);self.assertEqual(b.collections,1);self.assertEqual(app.layer.get_goal(gid)['goal']['state'],'completed')
   (f.root/'CLI-COMMANDS.json').write_text(json.dumps(commands,indent=2))
  finally:f.doCleanups()
 def test_external_settled_backup_and_wrong_hash_live_owner_refused(self):
  with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR',ROOT))as td:
   state=Path(td)/'state';layer=DurableLayer.open(state,use_cache=False)
   raw=(state/'.collab-durable/store.json').read_bytes();sha=hashlib.sha256(raw).hexdigest()
   host={'pid':1000000000,'birth':1.0};(state/'host.json').write_text(json.dumps(host));(state/'settled.json').write_text(json.dumps({'thread_alive':False}))
   backup=Path(td)/'new-backup'
   with self.assertRaises(ValueError):backup_existing_state(state,expected_store_sha256='wrong',backup=backup,expected_birth=1.)
   self.assertFalse(backup.exists())
   result=backup_existing_state(state,expected_store_sha256=sha,backup=backup,expected_birth=1.)
   self.assertEqual((backup/'.collab-durable/store.json').read_bytes(),raw);self.assertEqual((state/'.collab-durable/store.json').read_bytes(),raw)
   with self.assertRaises(ValueError):backup_existing_state(state,expected_store_sha256=sha,backup=backup,expected_birth=1.)
   import psutil
   (state/'host.json').write_text(json.dumps({'pid':os.getpid(),'birth':psutil.Process().create_time()}))
   with self.assertRaises(ValueError):backup_existing_state(state,expected_store_sha256=sha,backup=Path(td)/'live-refused',expected_birth=1.)

 def test_physical_candidate_loads_existing_store_hold_with_verified_backup(self):
  with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR',ROOT))as td:
   base=Path(td);state=base/'same-store';layer=DurableLayer.open(state,use_cache=False)
   raw=(state/'.collab-durable/store.json').read_bytes();sha=hashlib.sha256(raw).hexdigest()
   (state/'host.json').write_text(json.dumps({'pid':1000000000,'birth':1.0}))
   (state/'settled.json').write_text(json.dumps({'thread_alive':False}))
   guard=base/'guard';guard.mkdir();(guard/'sitecustomize.py').write_text(GUARD.replace('__PORT__','1')+RUNTIME_TEST_SEAM)
   env={k:os.environ[k]for k in ('SYSTEMROOT','WINDIR')if k in os.environ};env.update(PYTHONPATH=str(guard),PYTHONIOENCODING='utf-8')
   argv=[sys.executable,'-B',str(ROOT/'bin/goal-entry.py'),'--state',str(state),'--existing-state','serve','--restart-settled','1.0','--expected-store-sha256',sha,'--state-backup',str(base/'new-backup'),'--native-timeout-sec','900']
   proc=subprocess.Popen(argv,env=env,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
   try:
    deadline=time.monotonic()+10
    host={}
    while time.monotonic()<deadline:
     if proc.poll()is not None:break
     try:host=json.loads((state/'host.json').read_bytes())
     except(FileNotFoundError,ValueError):host={}
     if host.get('pid')==proc.pid:break
     time.sleep(.05)
    self.assertEqual(host.get('pid'),proc.pid)
    self.assertEqual(Path(host['runtime_root']).resolve(),ROOT.resolve());self.assertFalse(host['model_release'])
    self.assertEqual((state/'.collab-durable/store.json').read_bytes(),raw)
    self.assertEqual((base/'new-backup/.collab-durable/store.json').read_bytes(),raw)
    (state/'stop.json').write_text('{}')
    out,err=proc.communicate(timeout=10);self.assertEqual(proc.returncode,0,out+err)
    self.assertFalse(json.loads((state/'settled.json').read_bytes())['thread_alive'])
   finally:
    if proc.poll()is None:proc.terminate();proc.communicate(timeout=10)

if __name__=='__main__':unittest.main(verbosity=2)
