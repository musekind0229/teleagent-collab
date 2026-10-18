"""Self-contained, synthetic only. Popen and outbound connect are independently denied."""
import copy, importlib.util, json, os, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
import psutil
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from framework.app_service import CollabApplication, LeadAdapterPlanner
from framework.goal_service import ContinuityCoordinator, ResourceAuthority
from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend

class Lead:
 name='synthetic-inprocess-only'
 def __init__(self,a,answers):self.a=a;self.answers=list(answers);self.actions=[]
 def decide(self,r,**kw):
  binding={k:r.get(k)for k in ('application_id','context_summary','kind')}
  binding['task_id']=(r.get('extra')or{}).get('current_task',{}).get('task_id')
  receipt(self.a,'lead',binding)
  base={k:r[k]for k in ('application_id','context_summary')}
  if r['kind']=='plan':out={**base,'summary':'synthetic','tasks':[{'task_key':'one','title':'Synthetic task','instruction':'synthetic RESULT.md only','depends_on':[],'artifacts':['RESULT.md']}]}
  else:
   self.actions.append(copy.deepcopy(r['extra']['worker_request']))
   answer=self.answers.pop(0)if len(self.answers)>1 else self.answers[0]
   if answer=='timeout':raise TimeoutError('synthetic timeout')
   if answer=='partial':return '',{'structuredOutput':None,'structuredOutputError':'synthetic missing'}
   out={**base,'verdict':answer,'reason':'synthetic adjustment: inspect and repair once'}
  return json.dumps(out),out

def receipt(a,kind,binding):
 key=a.reserve(kind,binding);a.actual(key,pid=os.getpid(),birth=psutil.Process().create_time())

class Backend(AntigravityCliExecutionBackend):
 def __init__(self,a,failures=(),dispatch_error='',busy=False):
  super().__init__(bin_path=sys.executable,environ={'AGY_AUTO_APPROVE':'1'})
  self.configure_caller_permissions(allowed_operations=['read','write','command'],native_mode='skip_permissions')
  self.a=a;self.failures=list(failures);self.dispatch_error=dispatch_error;self.rows={};self.starts=0;self.collections=0;self.busy=busy
 def _start_run_locked(self,**kw):
  if self.dispatch_error:raise ValueError(self.dispatch_error)
  root=Path(kw['root']);receipt(self.a,'native',{'goal_id':root.parent.name,'task_id':root.name});self.starts+=1
  (root/'RESULT.md').write_text('SYNTHETIC ARTIFACT\n')
  error=self.failures.pop(0)if self.failures else ''
  rid='synthetic_'+__import__('uuid').uuid4().hex
  self.rows[rid]={'ok':not bool(error),'backend':self.backend_id,'run_id':rid,'workspace':str(root),'artifacts':[str(root/'RESULT.md')],'response':'synthetic evidence','native_status':'ERROR'if error else'SUCCESS','state':'failed'if error else'completed','usage':{'input_tokens':3,'output_tokens':2}}
  if error:self.rows[rid]['error']=error
  return {},{'ok':True,'run_id':rid,'backend':self.backend_id,'state':'running'}
 def observe_run(self,r):return {'ok':True,'busy':self.busy,'state':'running'if self.busy else'completed'}
 def collect_result(self,r):self.collections+=1;return copy.deepcopy(self.rows[r])
 def list_pending_actions(self,**kw):return 200,[]
 def close(self):pass

class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR',str(ROOT)));self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  for target,value in [('framework.app_service.read_git_head','68050f2'),('framework.app_service.process_start_tip','68050f2')]:
   p=patch(target,return_value=value);p.start();self.addCleanup(p.stop)
  for target in ('subprocess.Popen','socket.socket.connect'):
   p=patch(target,side_effect=AssertionError('SIDE EFFECT FORBIDDEN: '+target));p.start();self.addCleanup(p.stop)
 def make(self,answers=('pass',),failures=(),native=4,leads=7,dispatch_error='',busy=False):
  app=CollabApplication(self.root);a=ResourceAuthority(app.layer);l=Lead(a,answers);b=Backend(a,failures,dispatch_error,busy)
  app.coordinator=ContinuityCoordinator(app.layer,planner=LeadAdapterPlanner(l,cwd=self.root),backend=b,workspaces_root=self.root/'workspaces');b._trusted_dispatch_hook=a
  gid=app.submit({'idempotency_key':'synthetic-only','title':'Synthetic goal','goal':'synthetic maintenance test','acceptance':{'artifacts':['RESULT.md']},'budget':{'wall_sec':300,'max_reworks':3}})['goal_id']
  now=time.time();app.layer.continuity_authorize(gid,decision_id='',grant={'goal_id':gid,'grant_id':'synthetic-grant','reason':'synthetic only','resource':{'package_id':'synthetic-package','original_epoch':now,'checkpoint_epoch':now+300,'native_limit':native,'lead_limit':leads}},trusted=True)
  return app,a,l,b,gid
 def drive(self,app,gid,n=8):
  for _ in range(n):out=app.coordinator.process_goal(gid)
  return out,app.layer.get_goal(gid)['goal']
 def test_dispatch_refusal_keeps_prior_review_native_and_artifact(self):
  app,a,l,b,gid=self.make(answers=('fail',),native=1)
  out,s=self.drive(app,gid);t=s['tasks'][0]
  self.assertEqual(b.starts,1);self.assertEqual(len(l.actions),1)
  self.assertEqual(t['result']['review']['status'],'failed')
  self.assertEqual(t['result']['native_status'],'SUCCESS')
  self.assertIn('resource exhausted',t['dispatch_block']['error'])
  self.assertEqual(t['attempt_history'][0]['result']['review']['status'],'failed')
  self.assertTrue(Path(t['result']['artifacts'][0]).is_file())
  self.assertEqual(out['state'],'awaiting_owner')
 def test_ordinary_execution_failure_returns_to_lead_then_passes(self):
  app,a,l,b,gid=self.make(answers=('fail','pass'),failures=('synthetic compiler crash',))
  out,s=self.drive(app,gid)
  self.assertEqual(s['state'],'completed');self.assertEqual(b.starts,2)
  self.assertTrue(l.actions[0]['execution_failure'])
  self.assertEqual(s['tasks'][0]['attempt_history'][0]['result']['native_status'],'ERROR')
 def test_same_failure_twice_stops_without_second_triage(self):
  app,a,l,b,gid=self.make(answers=('fail',),failures=('synthetic compiler crash','synthetic compiler crash'))
  out,s=self.drive(app,gid)
  self.assertEqual(out['state'],'awaiting_owner');self.assertEqual(b.starts,2)
  self.assertEqual(len(l.actions),1);self.assertIn('same failure',s['pending_decisions'][0]['reason'])
 def test_auth_and_budget_upgrade_without_buying_triage(self):
  for error in ('authentication failed','permission denied','resource exhausted'):
   with self.subTest(error=error):
    # New temporary store per subcase.
    self.root=Path(self.tmp.name)/error.replace(' ','_')
    app,a,l,b,gid=self.make(failures=(error,));out,s=self.drive(app,gid)
    self.assertEqual(out['state'],'awaiting_owner');self.assertEqual(len(l.actions),0)
    self.assertTrue(s['pending_decisions'][0]['return_to_upper'])
 def test_formal_fail_rework_then_pass(self):
  app,a,l,b,gid=self.make(answers=('fail','pass'));out,s=self.drive(app,gid)
  self.assertEqual(s['state'],'completed');self.assertEqual(b.starts,2)
  self.assertEqual([x['status']for x in s['tasks'][0]['result']['review_history']],['failed','passed'])
 def test_review_timeout_and_no_verdict_stop_on_first(self):
  for answer in ('timeout','partial'):
   with self.subTest(answer=answer):
    self.root=Path(self.tmp.name)/answer;app,a,l,b,gid=self.make(answers=(answer,));out,s=self.drive(app,gid)
    before=copy.deepcopy(s);t=s['tasks'][0]
    self.assertEqual(out['state'],'awaiting_owner');self.assertEqual(b.starts,1);self.assertEqual(len(l.actions),1)
    app.coordinator._finish_gated(s,t,b.rows[t['run_id']])
    self.assertEqual(app.layer.get_goal(gid)['goal'],before)
 def test_duplicate_terminal_collection_is_read_only(self):
  app,a,l,b,gid=self.make();out,s=self.drive(app,gid);before=copy.deepcopy(s);t=s['tasks'][0]
  app.coordinator._finish_gated(s,t,b.rows[t['run_id']])
  app.coordinator._after_observe(s,t,{'busy':False},observe_error='duplicate')
  self.assertEqual(app.layer.get_goal(gid)['goal'],before);self.assertEqual(b.collections,1);self.assertEqual(len(l.actions),1)
 def test_busy_does_not_dispatch_again(self):
  app,a,l,b,gid=self.make(busy=True);out,s=self.drive(app,gid)
  self.assertEqual(b.starts,1);self.assertEqual(b.collections,0);self.assertEqual(len(l.actions),0)
 def test_goal_entry_wait_prepares_event_without_forging_receipt(self):
  spec=importlib.util.spec_from_file_location('goal_entry',ROOT/'bin/goal-entry.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  final={'goal_id':'goal_synthetic','state':'completed','pending_decisions':[],'tasks':[]}
  with patch.object(m,'call',return_value=final):
   result=m.wait_for_owner(self.root,'http://127.0.0.1:1','goal_synthetic',timeout_sec=.1)
   again=m.wait_for_owner(self.root,'http://127.0.0.1:1','goal_synthetic',timeout_sec=.1)
  self.assertEqual(result['event_id'],again['event_id']);self.assertEqual(result['delivery'],'prepared')
  self.assertFalse(result['business_acceptance']);self.assertFalse((self.root/'owner-consumption').exists())

 def load_entry(self):
  spec=importlib.util.spec_from_file_location('goal_entry',ROOT/'bin/goal-entry.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
 def test_duplicate_after_rework_cannot_review_old_run_again(self):
  app,a,l,b,gid=self.make(answers=('fail','pass'));app.coordinator.process_goal(gid)
  s=app.layer.get_goal(gid)['goal'];t=copy.deepcopy(s['tasks'][0]['attempt_history'][0]);before=copy.deepcopy(s)
  app.coordinator._finish_gated(s,t,b.rows[t['run_id']]);app.coordinator._after_observe(s,t,{'busy':False},observe_error='old run')
  self.assertEqual(app.layer.get_goal(gid)['goal'],before);self.assertEqual(len(l.actions),2)
 def test_unknown_observation_never_redispatches(self):
  app,a,l,b,gid=self.make(busy=True);self.drive(app,gid)
  b.observe_run=lambda r:{'ok':False,'state':'unknown','busy':False}
  self.drive(app,gid)
  self.assertEqual(b.starts,1);self.assertEqual(b.collections,0);self.assertEqual(len(l.actions),0)
 def test_execution_diagnosis_timeout_stops_first_and_retains_failure(self):
  app,a,l,b,gid=self.make(answers=('timeout',),failures=('synthetic compiler crash',));out,s=self.drive(app,gid)
  self.assertEqual(out['state'],'awaiting_owner');self.assertEqual(len(l.actions),1);self.assertEqual(b.starts,1)
  self.assertEqual(s['tasks'][0]['result']['native_status'],'ERROR')
 def test_lead_budget_denial_before_diagnosis(self):
  app,a,l,b,gid=self.make(failures=('synthetic compiler crash',),leads=2,busy=True)
  app.coordinator.process_goal(gid);self.assertEqual(b.starts,1)
  receipt(a,'lead',{'goal_id':gid}) # synthetic concurrent package expenditure after native start
  b.busy=False;out,s=self.drive(app,gid)
  self.assertEqual(out['state'],'awaiting_owner');self.assertEqual(len(l.actions),0);self.assertEqual(b.starts,1)
  self.assertEqual(s['pending_decisions'][0]['kind'],'escalate_over_budget')
 def test_dispatch_ordinary_failure_goes_to_lead_but_same_twice_stops(self):
  app,a,l,b,gid=self.make(answers=('fail',),dispatch_error='synthetic spawn fault');out,s=self.drive(app,gid)
  self.assertEqual(out['state'],'awaiting_owner');self.assertEqual(len(l.actions),1);self.assertEqual(b.starts,0)
  self.assertTrue(l.actions[0]['execution_failure']);self.assertIn('same failures',s['pending_decisions'][0]['reason'])
 def test_owner_receipt_requires_exact_packet_and_is_idempotent(self):
  m=self.load_entry();final={'goal_id':'goal_synthetic','state':'awaiting_owner','pending_decisions':[{'continuity':True,'decision_id':'synthetic-decision'}]}
  with patch.object(m,'call',return_value=final):packet=m.wait_for_owner(self.root,'http://127.0.0.1:1','goal_synthetic',timeout_sec=.1)
  kw={'event_id':packet['event_id'],'packet_sha256':packet['packet_sha256'],'receipt_id':'synthetic-owner-explicit-receipt'}
  with self.assertRaises(ValueError):m.consume_owner_packet(self.root,'wrong-goal',**kw)
  with self.assertRaises(ValueError):m.consume_owner_packet(self.root,'goal_synthetic',**{**kw,'packet_sha256':'wrong'})
  got=m.consume_owner_packet(self.root,'goal_synthetic',**kw)
  self.assertEqual(got,m.consume_owner_packet(self.root,'goal_synthetic',**kw));self.assertFalse(got['business_acceptance'])
  with self.assertRaises(ValueError):m.consume_owner_packet(self.root,'goal_synthetic',**{**kw,'receipt_id':'conflicting'})
 def test_wait_unknown_and_busy_return_without_receipt_or_dispatch(self):
  m=self.load_entry()
  with patch.object(m,'call',side_effect=OSError('synthetic unavailable')):
   got=m.wait_for_owner(self.root,'http://127.0.0.1:1','goal_synthetic',timeout_sec=.01)
   self.assertEqual(got['state'],'unknown')
  with patch.object(m,'call',return_value={'goal_id':'goal_synthetic','state':'running'}):
   got=m.wait_for_owner(self.root,'http://127.0.0.1:1','goal_synthetic',timeout_sec=.01)
   self.assertEqual(got['state'],'observation_timeout')
  self.assertFalse((self.root/'owner-consumption').exists());self.assertFalse((self.root/'owner-handoff').exists())
 def test_call_rejects_remote_and_credential_endpoints_before_connect(self):
  m=self.load_entry()
  for endpoint in ('https://remote.invalid','http://remote.invalid','http://user:pw@127.0.0.1:1','http://127.0.0.1:1/other'):
   with self.subTest(endpoint=endpoint),self.assertRaises(ValueError):m.call(endpoint,'GET','/v1/requests')

 def test_timeout_checkpoint_survives_restart_and_only_exact_grant_resumes(self):
  app,a,l,b,gid=self.make(answers=('timeout',));out,s=self.drive(app,gid);oldtask=copy.deepcopy(s['tasks'][0]);oldresult=copy.deepcopy(oldtask['result'])
  reopened=CollabApplication(self.root);a2=ResourceAuthority(reopened.layer);l2=Lead(a2,('pass',));b2=Backend(a2)
  reopened.coordinator=ContinuityCoordinator(reopened.layer,planner=LeadAdapterPlanner(l2,cwd=self.root),backend=b2,workspaces_root=self.root/'workspaces');b2._trusted_dispatch_hook=a2
  out,s2=self.drive(reopened,gid);self.assertEqual(out['state'],'awaiting_owner');self.assertEqual(b2.starts,0);self.assertEqual(len(l2.actions),0)
  now=time.time();did=s2['pending_decisions'][0]['decision_id']
  grant={'goal_id':gid,'grant_id':'synthetic-explicit-next','reason':'synthetic owner explicit decision','resource':{'package_id':'synthetic-next','native_limit':1,'lead_limit':1,'original_epoch':now,'checkpoint_epoch':now+300}}
  with self.assertRaises(ValueError):reopened.layer.continuity_authorize(gid,decision_id=did,grant=grant,trusted=False)
  reopened.layer.continuity_authorize(gid,decision_id=did,grant=grant,trusted=True)
  granted=copy.deepcopy(reopened.layer.get_goal(gid)['goal'])
  reopened.coordinator._finish_gated(s,oldtask,b.rows[oldtask['run_id']])
  self.assertEqual(reopened.layer.get_goal(gid)['goal'],granted)
  out,done=self.drive(reopened,gid);self.assertEqual(done['state'],'completed');self.assertEqual(b2.starts,1);self.assertEqual(len(l2.actions),1)
  self.assertEqual(done['tasks'][0]['attempt_history'][0]['result'],oldresult)
 def test_new_artifact_content_allows_evidenced_recovery_but_reason_alone_does_not(self):
  app,a,l,b,gid=self.make(answers=('fail','fail','pass'),failures=('synthetic compiler crash','synthetic compiler crash'))
  original=b._start_run_locked
  def changed(**kw):
   ret=original(**kw)
   (Path(kw['root'])/'RESULT.md').write_text('SYNTHETIC CHANGED CONTENT '+str(b.starts))
   return ret
  b._start_run_locked=changed
  out,s=self.drive(app,gid);self.assertEqual(s['state'],'completed');self.assertEqual(b.starts,3);self.assertEqual(len(l.actions),3)
  self.assertEqual(len(s['tasks'][0]['execution_failure_history']),2)
 def test_synthetic_side_effect_guards_are_active(self):
  import subprocess,socket
  with self.assertRaises(AssertionError):subprocess.Popen(['grok','DO_NOT_EXECUTE'])
  with socket.socket()as sock:
   with self.assertRaises(AssertionError):sock.connect(('203.0.113.1',443))

 def test_wait_after_host_exit_uses_only_durable_goal_outcome(self):
  app,a,l,b,gid=self.make(busy=True);self.drive(app,gid);m=self.load_entry()
  with patch.object(m,'call',side_effect=OSError('host exited')):
   got=m.wait_for_owner(self.root,'http://127.0.0.1:1',gid,timeout_sec=.01);self.assertEqual(got['state'],'unknown')
  self.assertEqual(b.starts,1)
  b.busy=False;out,s=self.drive(app,gid)
  with patch.object(m,'call',side_effect=OSError('host exited')):
   event=m.wait_for_owner(self.root,'http://127.0.0.1:1',gid,timeout_sec=.01)
  self.assertEqual(event['state'],'completed');packet=json.loads(Path(event['packet']).read_bytes())
  self.assertEqual(packet['status']['source'],'existing_same_store');self.assertFalse((self.root/'owner-consumption').exists())

 def test_observe_exception_keeps_running_native_binding_and_blocks_new_package(self):
  app,a,l,b,gid=self.make(busy=True);self.drive(app,gid)
  def lost(run_id):raise OSError('synthetic observation channel lost')
  b.observe_run=lost
  out,s=self.drive(app,gid);self.assertEqual(out['state'],'awaiting_owner');self.assertEqual(b.starts,1)
  self.assertEqual(s['tasks'][0]['status'],'running');self.assertEqual(len(l.actions),0)
  now=time.time();d=s['pending_decisions'][0]
  with self.assertRaises(ValueError):app.layer.continuity_authorize(gid,decision_id=d['decision_id'],grant={'goal_id':gid,'grant_id':'forbidden-overlap','reason':'synthetic','resource':{'package_id':'forbidden','native_limit':1,'lead_limit':1,'original_epoch':now,'checkpoint_epoch':now+30}},trusted=True)
 def test_predispatch_staging_refusal_preserves_prior_verdict(self):
  app,a,l,b,gid=self.make(answers=('fail',))
  def stage(*args):
   if b.starts:raise ValueError('permission denied on synthetic staged archive')
  with patch('framework.app_service.stage_caller_source_archive',side_effect=stage):out,s=self.drive(app,gid)
  t=s['tasks'][0];self.assertEqual(out['state'],'awaiting_owner');self.assertEqual(b.starts,1);self.assertEqual(len(l.actions),1)
  self.assertEqual(t['result']['review']['status'],'failed');self.assertTrue(t['result']['native_result']['ok']);self.assertIn('permission denied',t['dispatch_block']['error'])

if __name__=='__main__':unittest.main(verbosity=2)
