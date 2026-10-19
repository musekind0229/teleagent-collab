"""Lifecycle notifications: synthetic snapshots/senders; no network/model/QQ."""
import json,sys,tempfile,os,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']);self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  for target in ('subprocess.Popen','socket.socket.connect'):
   p=patch(target,side_effect=AssertionError('NO EXTERNAL SIDE EFFECT'));p.start();self.addCleanup(p.stop)
 def snapshot(self,status='completed'):
  return {'goal_id':'synthetic_goal','state':status,'goal':{'title':'Synthetic\nuser text','private':'DO NOT EXPORT'},'pending_decisions':[],'tasks':[{'task_id':'synthetic_task','run_id':'synthetic_run','status':'succeeded','result':{'backend':'synthetic','response':'PRIVATE RAW THOUGHT','usage':{'input_tokens':3},'review':{'status':'passed'},'artifacts':['PRIVATE LIST']}}],'resource_packages':{}}
 def outbox(self):
  from framework.lifecycle_events import LifecycleOutbox
  return LifecycleOutbox(self.root,runtime={'head':'a'*40,'runtime_root':'synthetic runtime'})
 def test_auto_event_stable_restart_and_whitelist_no_task_or_ack_event(self):
  b=self.outbox();self.assertIsNone(b.publish(self.snapshot('running')))
  event=b.publish(self.snapshot());self.assertEqual(event['event_id'],self.outbox().publish(self.snapshot())['event_id'])
  self.assertNotIn('PRIVATE',json.dumps(event));self.assertNotIn('DO NOT EXPORT',json.dumps(event))
  self.assertEqual(len(list((self.root/'lifecycle-events').glob('*.json'))),1)
 def test_consumers_independent_three_lines_queued_not_consumed(self):
  b=self.outbox();e=b.publish(self.snapshot());seen=[]
  def sender(channel,payload):
   seen.append((channel,payload));return {'status':'sent','delivery_id':'qq-1'}if channel=='qq'else{'status':'queued','delivery_id':'pm-1'}
  b.deliver(e['event_id'],sender,now=100)
  self.assertEqual(len(next(p for c,p in seen if c=='qq').splitlines()),3)
  s=b.status(e['event_id']);self.assertEqual(s['qq']['state'],'sent');self.assertEqual(s['pm']['state'],'queued');self.assertNotEqual(s['pm']['state'],'consumed')
  b.deliver(e['event_id'],sender,now=101);self.assertEqual(sum(c=='qq'for c,p in seen),1)
 def test_busy_two_same_rejections_stop_new_evidence_resume_same_event(self):
  b=self.outbox();e=b.publish(self.snapshot());calls=[]
  def busy(c,p):calls.append(c);return {'status':'target_busy','delivery_id':str(len(calls))}
  b.deliver(e['event_id'],busy,now=100);b.deliver(e['event_id'],busy,now=140)
  b.deliver(e['event_id'],busy,now=300);self.assertEqual(calls.count('pm'),2)
  self.assertEqual(b.status(e['event_id'])['pm']['state'],'delivery_blocked')
  b.resume(e['event_id'],'pm',evidence='canonical owner changed from busy to idle',now=400)
  b.deliver(e['event_id'],busy,now=400);self.assertEqual(calls.count('pm'),3)
 def test_exact_receipt_report_owner_consumption_and_unapproved_no_execution(self):
  b=self.outbox();e=b.publish(self.snapshot())
  with self.assertRaises(ValueError):b.consume(e['event_id'],'pm',packet_sha256='wrong',receipt_id='r')
  b.consume(e['event_id'],'pm',packet_sha256=e['packet_sha256'],receipt_id='read-and-hash-checked')
  b.report(e['event_id'],packet_sha256=e['packet_sha256'],report={'conclusion':'healthy_no_change','evidence':['synthetic test passed'],'proposal':'none'},receipt_id='substantive-report')
  self.assertEqual(b.status(e['event_id'])['owner']['state'],'pending')
  with self.assertRaises(ValueError):b.consume(e['event_id'],'owner',packet_sha256=e['packet_sha256'],receipt_id='owner-read')
  report_sha=b.status(e['event_id'])['report_sha256'];b.consume(e['event_id'],'owner',packet_sha256=report_sha,receipt_id='owner-read-report')
  self.assertEqual(b.status(e['event_id'])['owner']['state'],'consumed');self.assertFalse(b.status(e['event_id'])['maintenance_authorized'])
 def test_unknown_exit_not_business_failure_and_pending_identity_changes(self):
  b=self.outbox();s=self.snapshot('running');self.assertIsNone(b.publish(s,host_exited=True))
  self.assertTrue((self.root/'lifecycle-health.json').exists());s['pending_decisions']=[{'continuity':True,'decision_id':'decision1','task_id':'synthetic_task'}]
  e=b.publish(s);s['pending_decisions'][0]['decision_id']='decision2';self.assertNotEqual(e['event_id'],b.publish(s)['event_id']);self.assertEqual(e['state'],'awaiting_owner')
 def test_partial_persist_rebuilds_delivery_and_inflight_never_resends(self):
  b=self.outbox();s=self.snapshot();e=b.publish(s);p=b.receipts/(e['event_id']+'.json');p.unlink();self.outbox().publish(s)
  self.assertEqual(b.status(e['event_id'])['pm']['state'],'pending')
  row=b.status(e['event_id']);row['pm']['state']='inflight';b._save(e['event_id'],row);self.outbox().recover_interrupted()
  self.assertEqual(b.status(e['event_id'])['pm']['state'],'delivery_blocked')
  calls=[];b.deliver(e['event_id'],lambda c,p:calls.append(c)or{'status':'sent'},now=100);self.assertNotIn('pm',calls)
 def test_queued_expiry_wrong_hash_and_out_of_order_report(self):
  b=self.outbox();e=b.publish(self.snapshot());sender=lambda c,p:{'status':'queued','delivery_id':'synthetic'}
  b.deliver(e['event_id'],sender,now=100);b.deliver(e['event_id'],sender,now=100+b.window+1)
  self.assertEqual(b.status(e['event_id'])['pm']['state'],'delivery_blocked')
  with self.assertRaises(ValueError):b.report(e['event_id'],packet_sha256=e['packet_sha256'],report={'conclusion':'healthy_no_change','evidence':['x']},receipt_id='r')
  p=b.root/(e['event_id']+'.json');obj=json.loads(p.read_bytes());obj['goal_id']='wrong';p.write_text(json.dumps(obj))
  with self.assertRaises(ValueError):b.status(e['event_id'])
 def test_persistent_store_callback_no_manual_wait_required(self):
  import subprocess
  with patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,'synthetic\n','')):
   from framework.durable_api import DurableLayer
  layer=DurableLayer.open(self.root,use_cache=False);box=self.outbox();layer._lifecycle_publisher=box
  snap=self.snapshot()
  with layer._rmw():layer.goals[snap['goal_id']]=snap;layer._persist_unlocked()
  self.assertEqual(len(list(box.root.glob('*.json'))),1)
  layer.persist();self.assertEqual(len(list(box.root.glob('*.json'))),1)
 def test_approved_sender_no_route_never_spawns_and_argv_is_supported(self):
  from framework.lifecycle_transport import HermesLifecycleSender
  import subprocess
  route=self.root/'route.json';sender=HermesLifecycleSender(route)
  self.assertEqual(sender('qq','one\ntwo\nthree')['status'],'route_unbound')
  route.write_text(json.dumps({'approved':True,'hermes_executable':sys.executable,'qq':{'profile':'synthetic','target':'qq:SYNTHETIC'},'pm':{'profile':'project-manager'}}))
  with patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,json.dumps({'success':True,'message_id':'spy-id'}),''))as run:
   self.assertEqual(sender('qq','one\ntwo\nthree')['status'],'sent');argv=run.call_args.args[0];self.assertIn('--json',argv);self.assertEqual(run.call_args.kwargs['input'],'one\ntwo\nthree');self.assertNotIn('shell',run.call_args.kwargs)
  with patch('subprocess.run',return_value=subprocess.CompletedProcess([],1,'target_busy','')):
   self.assertEqual(sender('pm',{'event_id':'synthetic'})['status'],'target_busy')

 def test_busy_pm_does_not_block_qq_or_business_persistence(self):
  import threading
  b=self.outbox();e=b.publish(self.snapshot());entered=threading.Event();release=threading.Event()
  def pm(c,p):entered.set();release.wait(3);return {'status':'target_busy'}
  thread=threading.Thread(target=lambda:b.deliver(e['event_id'],pm,channels=('pm',),now=100));thread.start()
  try:
   self.assertTrue(entered.wait(1));seen=[];b.deliver(e['event_id'],lambda c,p:seen.append(c)or{'status':'sent','delivery_id':'qq-independent'},channels=('qq',),now=100)
   self.assertEqual(seen,['qq']);self.assertEqual(b.status(e['event_id'])['qq']['state'],'sent')
   newer=self.snapshot('failed');self.assertIsNotNone(b.publish(newer))
  finally:release.set();thread.join(3)
 def test_result_return_cannot_overwrite_consumer_read_receipt(self):
  b=self.outbox();e=b.publish(self.snapshot())
  def sender(c,p):
   b.consume(e['event_id'],'pm',packet_sha256=e['packet_sha256'],receipt_id='synchronous-read')
   return {'status':'settled','delivery_id':'transport-only'}
  b.deliver(e['event_id'],sender,channels=('pm',),now=100);self.assertEqual(b.status(e['event_id'])['pm']['state'],'consumed')

 def test_goal_title_cannot_inject_attachments_or_extra_lines(self):
  b=self.outbox();s=self.snapshot();s['goal']['title']='x\nMEDIA:C:/private.txt [[as_document]]';e=b.publish(s);seen=[]
  b.deliver(e['event_id'],lambda c,p:seen.append(p)or{'status':'sent'},channels=('qq',),now=100)
  self.assertEqual(len(seen[0].splitlines()),3);self.assertNotIn('MEDIA:',seen[0]);self.assertNotIn('[[',seen[0])

if __name__=='__main__':unittest.main(verbosity=2)
