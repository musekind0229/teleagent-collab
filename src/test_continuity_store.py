import unittest,tempfile,time,copy,concurrent.futures,os,psutil
from pathlib import Path
from framework.goal_service import GoalService
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'tests');self.root=Path(self.tmp.name);self.service=GoalService(self.root);self.layer=self.service.app.layer
  # No service thread/models; actual application API creates durable goal.
  result=self.service.app.submit({'title':'fixture','goal':'synthetic','boundaries':{'must':[],'must_not':['no private']},'acceptance':{'artifacts':[],'lead_selects_artifacts':True,'text':'synthetic only'},'idempotency_key':'one','budget':{'wall_sec':100,'max_reworks':1}});self.gid=result['goal_id'];now=time.time();self.grant={'goal_id':self.gid,'grant_id':'initial','reason':'test only','resource':{'package_id':'initial','original_epoch':now,'checkpoint_epoch':now+100,'native_limit':1,'lead_limit':1}};self.layer.continuity_authorize(self.gid,decision_id='',grant=self.grant,trusted=True)
 def tearDown(self):self.service.server.server_close();self.tmp.cleanup()
 def test_no_authorization_cross_goal_and_pending_required(self):
  for grant,trusted in [(self.grant,False),({**self.grant,'goal_id':'other'},True),({**self.grant,'grant_id':'not-pending','resource':{**self.grant['resource'],'package_id':'new'}},True)]:
   with self.assertRaises(ValueError):self.layer.continuity_authorize(self.gid,decision_id='',grant=grant,trusted=trusted)
 def test_concurrent_same_store_cap(self):
  def take(i):
   try:return self.layer.continuity_claim(self.gid,kind='native',binding={'task':i})
   except ValueError:return None
  with concurrent.futures.ThreadPoolExecutor(max_workers=6)as pool:values=list(pool.map(take,range(6)))
  self.assertEqual(sum(x is not None for x in values),1)
 def test_receipts_no_spawn_release_and_actual_once(self):
  k=self.layer.continuity_claim(self.gid,kind='native',binding={});self.layer.continuity_receipt(self.gid,k)
  with self.assertRaises(ValueError):self.layer.continuity_receipt(self.gid,k)
  k=self.layer.continuity_claim(self.gid,kind='native',binding={});self.layer.continuity_receipt(self.gid,k,pid=os.getpid(),birth=psutil.Process().create_time())
  with self.assertRaises(ValueError):self.layer.continuity_receipt(self.gid,k,pid=os.getpid(),birth=psutil.Process().create_time())
  self.assertEqual(self.service.authority.status(self.gid)['actual_native'],1)
 def test_durable_decision_idempotence_and_explicit_increment(self):
  before=self.layer.get_goal(self.gid)['goal'];d=self.layer.continuity_block(self.gid,reason='ordinary resource checkpoint')['decision'];self.assertEqual(d,self.layer.continuity_block(self.gid,reason='ordinary resource checkpoint')['decision']);now=time.time();g={'goal_id':self.gid,'grant_id':'increment','reason':'evidence-based adjustment','resource':{'package_id':'increment','original_epoch':now,'checkpoint_epoch':now+90,'native_limit':2,'lead_limit':3}}
  self.layer.continuity_authorize(self.gid,decision_id=d['decision_id'],grant=g,trusted=True);self.assertTrue(self.layer.continuity_authorize(self.gid,decision_id=d['decision_id'],grant=g,trusted=True)['duplicate']);after=self.layer.get_goal(self.gid)['goal'];self.assertEqual(after['created_at'],before['created_at']);self.assertEqual(after['resource_packages']['initial'],before['resource_packages']['initial']);self.assertEqual(len(after['resolved_decisions']),1)
 def test_explicit_value_termination_retains_accounting(self):
  d=self.layer.continuity_block(self.gid,reason='ordinary choice')['decision'];before=self.service.authority.status(self.gid)
  self.layer.continuity_end(self.gid,decision_id=d['decision_id'],reason='not worth additional cost',alternative='retain partial output for later',trusted=True)
  self.assertEqual(self.service.app.coordinator.process_goal(self.gid)['state'],'failed');self.assertEqual(self.service.authority.status(self.gid),before)
  with self.assertRaises(ValueError):self.layer.continuity_claim(self.gid,kind='native',binding={})
 def test_plan_error_escalates_instead_of_killing_goal(self):
  def broken(snapshot):raise ValueError('synthetic planner resource feasibility feedback')
  self.service.app.coordinator.planner.plan=broken
  result=self.service.app.coordinator.process_goal(self.gid);self.assertEqual(result['state'],'awaiting_owner');self.assertEqual(self.layer.get_goal(self.gid)['goal']['state'],'blocked')
 def test_checkpoint_blocks_not_terminal_and_expired_grant_rejected(self):
  with self.layer._rmw():self.layer.goals[self.gid]['resource_packages']['initial']['resource']['checkpoint_epoch']=time.time()-1;self.layer._persist_unlocked()
  result=self.service.app.coordinator.process_goal(self.gid);self.assertEqual(result['state'],'awaiting_owner');snap=self.layer.get_goal(self.gid)['goal'];self.assertEqual(snap['state'],'blocked');self.assertTrue(snap['pending_decisions'])
  g=copy.deepcopy(self.grant);g.update(grant_id='expired');g['resource'].update(package_id='expired',original_epoch=time.time()-50,checkpoint_epoch=time.time()-1)
  with self.assertRaises(ValueError):self.layer.continuity_authorize(self.gid,decision_id=snap['pending_decisions'][0]['decision_id'],grant=g,trusted=True)
if __name__=='__main__':unittest.main(verbosity=2)
