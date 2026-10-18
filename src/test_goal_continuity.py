import unittest,copy
from framework.goal_continuity import require_owner,apply_continuation,ContinuityRefused
class Tests(unittest.TestCase):
 def setUp(self):
  self.source={'goal_id':'goal_synthetic','state':'running','created_at':100,'tasks':[{'task_id':'task_a','status':'failed','result':{'error':'fixture'}}],'old_actual':{'native':1,'lead':2}}
  self.grant={'goal_id':'goal_synthetic','grant_id':'grant_next','reason':'bounded adjustment based on evidence','action':'continue','resource':{'package_id':'NEW-EXPLICIT','original_epoch':200,'checkpoint_epoch':300,'native_limit':2,'lead_limit':3}}
 def test_continuation_same_goal_keeps_history(self):
  prior=copy.deepcopy(self.source);blocked=require_owner(self.source,reason='resource boundary',decision_id='decision_1');out=apply_continuation(blocked,decision_id='decision_1',grant=self.grant,trusted=True)
  self.assertEqual(self.source,prior);self.assertEqual(out['goal_id'],prior['goal_id']);self.assertEqual(out['tasks'],prior['tasks']);self.assertEqual(out['old_actual'],prior['old_actual']);self.assertEqual(out['created_at'],100)
 def test_idempotence(self):
  blocked=require_owner(self.source,reason='resource boundary',decision_id='decision_1');out=apply_continuation(blocked,decision_id='decision_1',grant=self.grant,trusted=True);self.assertEqual(out,apply_continuation(out,decision_id='decision_1',grant=self.grant,trusted=True))
 def test_no_self_authorization(self):
  with self.assertRaises(ContinuityRefused):apply_continuation(self.source,decision_id='decision_1',grant=self.grant)
 def test_cross_goal_refused(self):
  blocked=require_owner(self.source,reason='boundary',decision_id='decision_1')
  with self.assertRaises(ContinuityRefused):apply_continuation(blocked,decision_id='decision_1',grant={**self.grant,'goal_id':'other'},trusted=True)
if __name__=='__main__':unittest.main(verbosity=2)
