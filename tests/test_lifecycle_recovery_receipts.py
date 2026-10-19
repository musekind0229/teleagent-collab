"""Second-round regression: actual GoalService order and backend receipt schema."""
import copy,json,os,sys,tempfile,unittest,subprocess
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
with patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,'synthetic\n','')):
 from framework.goal_service import GoalService
from framework.lifecycle_events import LifecycleOutbox,task_evidence
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']);self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  for target in ('subprocess.Popen','socket.socket.connect'):
   p=patch(target,side_effect=AssertionError('NO EXTERNAL SIDE EFFECT'));p.start();self.addCleanup(p.stop)
 def task(self):return {'task_id':'t','run_id':'r','status':'succeeded','result':{'backend':'antigravity.cli_v1','session_receipt':{'source':'backend_observed_process_and_files','task_id':'t','run_id':'r','workspace':'PRIVATE ABSOLUTE','conversation_id':'PRIVATE SESSION','fresh_session':True,'returncode':0,'stdout':'PRIVATE RAW','outputs':[{'relative':'src/synthetic.js','bytes':4,'sha256':'a'*64,'private':'PRIVATE LIST'}]},'usage':None,'response':'PRIVATE RESPONSE'}}
 def snapshot(self):return {'goal_id':'synthetic_goal','state':'completed','goal':{'title':'synthetic'},'tasks':[self.task()],'pending_decisions':[]}
 def service(self):
  from test_goal_loop_maintenance import Lead,Backend
  with patch('framework.app_service.read_git_head',return_value='synthetic'),patch('framework.app_service.process_start_tip',return_value='synthetic'):
   service=GoalService(self.root,test_factory=lambda a:(Lead(a,('pass',)),Backend(a)))
  self.addCleanup(service.server.server_close)
  return service
 def test_actual_service_constructor_recovers_missing_receipt_before_restore(self):
  box=LifecycleOutbox(self.root);e=box.publish(self.snapshot());(box.receipts/(e['event_id']+'.json')).unlink()
  service=self.service();self.assertEqual(service.lifecycle.status(e['event_id'])['pm']['state'],'pending')
  # Production constructor, not reversed publish-before-recovery helper.
  self.assertEqual(service.lifecycle._event(e['event_id']),e)
 def test_actual_service_corrupt_packet_and_receipt_preserved_not_startup_gate(self):
  box=LifecycleOutbox(self.root);e=box.publish(self.snapshot());p=box.root/(e['event_id']+'.json');row=json.loads(p.read_bytes());row['goal_id']='wrong';p.write_text(json.dumps(row));before=p.read_bytes()
  service=self.service();self.assertEqual(p.read_bytes(),before);self.assertTrue((self.root/'lifecycle-recovery-blocked'/p.name).exists())
  with self.assertRaises(ValueError):service.lifecycle.status(e['event_id'])
 def test_inflight_retains_unknown_not_replayed_and_corrupt_row_not_overwritten(self):
  box=LifecycleOutbox(self.root);e=box.publish(self.snapshot());s=box.status(e['event_id']);s['pm']['state']='inflight';box._save(e['event_id'],s)
  service=self.service();self.assertEqual(service.lifecycle.status(e['event_id'])['pm']['state'],'delivery_blocked')
  p=box.receipts/(e['event_id']+'.json');p.write_text('{BROKEN');before=p.read_bytes();self.service();self.assertEqual(p.read_bytes(),before)
 def test_real_collector_fields_bound_private_material_absent(self):
  obj=task_evidence(self.task());self.assertEqual(obj['manifest'],[{'path':'src/synthetic.js','bytes':4,'sha256':'a'*64}]);self.assertEqual(obj['session_receipt']['source'],'backend_observed_process_and_files');self.assertEqual(obj['session_receipt']['returncode'],0);self.assertNotIn('PRIVATE',json.dumps(obj));self.assertFalse(obj['usage_available']);self.assertEqual(obj['usage'],{})
 def test_mismatch_dirty_paths_sha_and_unknown_run_do_not_forge_observed(self):
  cases=[]
  for field in ('task_id','run_id','source'):
   t=self.task();t['result']['session_receipt'][field]='wrong';cases.append(t)
  for path in ('/absolute','../escape','C:/private','back\\slash'):
   t=self.task();t['result']['session_receipt']['outputs'][0]['relative']=path;cases.append(t)
  t=self.task();t['result']['session_receipt']['outputs'][0]['sha256']='bad';cases.append(t)
  t=self.task();t['run_id']=None;cases.append(t)
  for t in cases:
   with self.subTest(t=t.get('run_id')):
    obj=task_evidence(t);self.assertFalse(obj['manifest_available']);self.assertEqual(obj['manifest'],[]);self.assertIsNone(obj['observed_run_count'])
 def test_prior_attempts_same_whitelist_and_no_run_unknown_usage(self):
  box=LifecycleOutbox(self.root);s=self.snapshot();t=s['tasks'][0];t['attempt_history']=[self.task(),{'task_id':'t','result':{'usage':{'input_tokens':float('nan'),'output_tokens':-1},'response':'PRIVATE'}}]
  e=box.publish(s);self.assertNotIn('PRIVATE',json.dumps(e));self.assertEqual(e['tasks'][0]['prior_attempts'][0]['manifest'][0]['sha256'],'a'*64);missing=e['tasks'][0]['prior_attempts'][1];self.assertFalse(missing['usage_available']);self.assertIsNone(missing['observed_run_count']);self.assertNotIn('attempt_count',e['tasks'][0])
 def test_actual_service_reconciles_real_durable_fixture_snapshot_without_store_rewrite(self):
  from test_goal_loop_maintenance import Tests as Fixture
  f=Fixture();f.setUp();self.addCleanup(f.doCleanups)
  app,a,lead,backend,gid=f.make(answers=('pass',));out,s=f.drive(app,gid)
  self.assertEqual(s['state'],'completed');self.root=f.root
  raw=(self.root/'.collab-durable/store.json').read_bytes();box=LifecycleOutbox(self.root);event=box.publish(s)
  (box.receipts/(event['event_id']+'.json')).unlink()
  service=self.service();self.assertEqual(service.lifecycle.status(event['event_id'])['pm']['state'],'pending')
  self.assertEqual((self.root/'.collab-durable/store.json').read_bytes(),raw)
  self.assertEqual(service.app.layer.get_goal(gid)['goal']['state'],'completed')

if __name__=='__main__':unittest.main(verbosity=2)
