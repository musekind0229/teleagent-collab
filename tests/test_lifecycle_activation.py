"""Round3 activation gates: synthetic source and transports only."""
import json,os,sys,tempfile,unittest,subprocess
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from framework.lifecycle_events import LifecycleOutbox
from framework.lifecycle_transport import HermesLifecycleSender
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']);self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  for target in ('subprocess.Popen','socket.socket.connect'):
   p=patch(target,side_effect=AssertionError('NO REAL SIDE EFFECT'));p.start();self.addCleanup(p.stop)
 def snapshot(self,state='running',decision=None):return {'goal_id':'SYNTHETIC_GOAL','state':state,'goal':{'title':'SYNTHETIC'},'tasks':[],'pending_decisions':[{'continuity':True,'decision_id':decision}]if decision else[]}
 def box(self,kind='real'):return LifecycleOutbox(self.root,runtime={'execution_class':kind,'head':'a'*40,'runtime_root':'SYNTHETIC_RUNTIME'})
 def arm(self,box,snapshot):return box.arm([snapshot],authorized_goal_id='SYNTHETIC_GOAL',approval_reference='SYNTHETIC OWNER APPROVAL',source_sha256='a'*64,now=100)
 def test_qqbot_explicit_default_supported_no_other_profile(self):
  route=self.root/'route.json';route.write_text(json.dumps({'approved':True,'hermes_executable':sys.executable,'qq':{'profile':'default','target':'qqbot:SYNTHETIC'}}));sender=HermesLifecycleSender(route)
  with patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,'{"success":true,"message_id":"synthetic"}',''))as run:
   self.assertEqual(sender('qq','one\ntwo\nthree')['status'],'sent');self.assertEqual(run.call_args.args[0][1:3],['-p','default']);self.assertIn('qqbot:SYNTHETIC',run.call_args.args[0])
  rule=json.loads(route.read_bytes());rule['qq']['profile']='worker';route.write_text(json.dumps(rule));self.assertEqual(sender('qq','x')['status'],'route_profile_not_approved')
 def test_unarmed_approved_route_cannot_send(self):
  box=self.box();e=box.publish(self.snapshot('completed'));calls=[];box.deliver(e['event_id'],lambda c,p:calls.append(c)or{'status':'sent'},now=120);self.assertEqual(calls,[])
 def test_old_decision_first_reconcile_and_old_terminal_remain_excluded(self):
  box=self.box();old=self.snapshot(decision='old');self.arm(box,old)
  e=self.box().publish(old);calls=[];self.box().deliver(e['event_id'],lambda c,p:calls.append(c)or{'status':'sent'},now=120);self.assertEqual(calls,[])
  other=LifecycleOutbox(self.root/'other',runtime={'execution_class':'real'});self.arm(other,self.snapshot('completed'));event=other.publish(self.snapshot('completed'));other.deliver(event['event_id'],lambda c,p:calls.append(c)or{'status':'sent'},now=120);self.assertEqual(calls,[])
 def test_new_decision_real_qualified_dedupe_restart_synthetic_wrong_goal_block(self):
  box=self.box();self.arm(box,self.snapshot(decision='old'));s=self.snapshot(decision='new');e=box.publish(s);calls=[]
  self.box().deliver(e['event_id'],lambda c,p:calls.append(c)or{'status':'sent'},now=120);self.box().deliver(e['event_id'],lambda c,p:calls.append(c)or{'status':'sent'},now=121);self.assertEqual(calls,['qq','pm'])
  for kind,gid in [('synthetic','SYNTHETIC_GOAL'),('real','OTHER_GOAL'),('unknown','SYNTHETIC_GOAL')]:
   other=self.box(kind);s=self.snapshot(decision='new-'+kind);s['goal_id']=gid;ev=other.publish(s);other.deliver(ev['event_id'],lambda c,p:calls.append('BAD')or{'status':'sent'},now=130)
  self.assertNotIn('BAD',calls)
 def test_prearm_packet_backlog_and_rearm_refused(self):
  box=self.box();old=box.publish(self.snapshot(decision='before'));self.arm(box,self.snapshot());calls=[];box.deliver(old['event_id'],lambda c,p:calls.append(c)or{'status':'sent'},now=120);self.assertEqual(calls,[])
  with self.assertRaises(ValueError):self.arm(box,self.snapshot())
 def test_new_terminal_and_activation_tamper_are_bound(self):
  box=self.box();self.arm(box,self.snapshot());e=box.publish(self.snapshot('completed'));seen=[]
  box.deliver(e['event_id'],lambda c,p:seen.append(c)or{'status':'sent'},now=120);self.assertEqual(seen,['qq','pm'])
  path=self.root/'lifecycle-activation.json';value=json.loads(path.read_bytes());value['authorized_goal_id']='OTHER';path.write_text(json.dumps(value))
  with self.assertRaises(ValueError):self.box().deliver(e['event_id'],lambda c,p:seen.append('BAD'),now=130)
  self.assertNotIn('BAD',seen)
 def test_no_ack_poll_or_task_progress_is_eligible(self):
  box=self.box();self.arm(box,self.snapshot())
  for state in ('running','queued','preparing','ack','observation_timeout'):
   self.assertIsNone(box.publish(self.snapshot(state)))

 def test_existing_cli_arm_uses_current_locked_source_digest_and_hold_host(self):
  import importlib.util,hashlib
  spec=importlib.util.spec_from_file_location('lifecycle_arm_entry',ROOT/'bin/goal-entry.py');entry=importlib.util.module_from_spec(spec)
  with patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,'synthetic\n','')):spec.loader.exec_module(entry)
  (self.root/'.collab-durable').mkdir();raw=json.dumps({'goals':{'SYNTHETIC_GOAL':self.snapshot(decision='old-source')}}).encode();(self.root/'.collab-durable/store.json').write_bytes(raw)
  host={'synthetic':False,'model_release':False,'runtime_root':str(ROOT)};(self.root/'host.json').write_text(json.dumps(host))
  argv=['goal-entry.py','--state',str(self.root),'--existing-state','lifecycle','arm','--authorized-goal-id','SYNTHETIC_GOAL','--approval-reference','SYNTHETIC OWNER','--expected-source-sha256','b'*64]
  with patch('framework.runtime_guard.verify_runtime',return_value={'synthetic_test_seam':True}),patch('sys.argv',argv),self.assertRaises(ValueError):entry.main()
  self.assertFalse((self.root/'lifecycle-activation.json').exists())
  host['synthetic']=True;(self.root/'host.json').write_text(json.dumps(host));argv[-1]=hashlib.sha256(raw).hexdigest()
  with patch('framework.runtime_guard.verify_runtime',return_value={'synthetic_test_seam':True}),patch('sys.argv',argv),self.assertRaises(ValueError):entry.main()
  host['synthetic']=False;(self.root/'host.json').write_text(json.dumps(host))
  import io
  with patch('framework.runtime_guard.verify_runtime',return_value={'synthetic_test_seam':True})as gate,patch('sys.argv',argv),patch('sys.stdout',new_callable=io.StringIO):entry.main()
  self.assertEqual(gate.call_count,1);activation=json.loads((self.root/'lifecycle-activation.json').read_bytes());self.assertIn(['SYNTHETIC_GOAL','old-source'],activation['excluded_source_identities']);self.assertEqual((self.root/'.collab-durable/store.json').read_bytes(),raw)
  box=self.box();event=box.publish(self.snapshot(decision='old-source'));seen=[];box.deliver(event['event_id'],lambda c,p:seen.append(c)or{'status':'sent'});self.assertEqual(seen,[])

 def test_actual_synthetic_service_cannot_mint_real_eligible_event(self):
  sys.path.insert(0,str(ROOT/'tests'))
  with patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,'synthetic\n','')):
   from framework.goal_service import GoalService
   from test_goal_loop_maintenance import Lead,Backend
  self.arm(self.box(),self.snapshot())
  with patch('framework.app_service.read_git_head',return_value='synthetic'),patch('framework.app_service.process_start_tip',return_value='synthetic'):
   service=GoalService(self.root,test_factory=lambda a:(Lead(a,('pass',)),Backend(a)))
  self.addCleanup(service.server.server_close)
  e=service.lifecycle.publish(self.snapshot('completed'));seen=[];service.lifecycle.deliver(e['event_id'],lambda c,p:seen.append(c)or{'status':'sent'},now=120)
  self.assertEqual(e['runtime']['execution_class'],'synthetic');self.assertEqual(seen,[])

if __name__=='__main__':unittest.main(verbosity=2)
