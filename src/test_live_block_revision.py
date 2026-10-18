import unittest,tempfile,json,time,copy,os
from pathlib import Path
from unittest.mock import patch
from framework.app_service import _lead_failure_of,LeadAdapterPlanner,AppError
from framework.lead_evidence import EvidenceAdapter
from framework.host_restart import validate_settled
from framework.goal_service import GoalService
class Tests(unittest.TestCase):
 def test_reason_not_collapsed_to_review_blocked(self):
  self.assertEqual(_lead_failure_of({'_lead_status':'review_blocked','reason':'lead structured output is None; unreviewed'}),('review_blocked','lead structured output is None; unreviewed'))
 def test_incomplete_plan_stays_failed_evidence_retained(self):
  class Adapter:
   name='synthetic'
   def decide(self,*args,**kwargs):return 'synthetic incomplete raw',{'_lead_status':'review_blocked','reason':'synthetic provider returned null structure','structuredOutput':None,'raw_evidence':{'capture_complete':True,'stdout_hex':b'fixture'.hex()}}
  with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'tests')as root:
   planner=LeadAdapterPlanner(EvidenceAdapter(Adapter(),Path(root)/'evidence'),cwd=root)
   with self.assertRaisesRegex(AppError,'synthetic provider returned null structure'):planner.plan({'goal_id':'goal_fixture','goal':{'desired_outcome':'fixture','acceptance':{'artifacts':[]},'boundaries':{},'budget':{}}})
   folder=next((Path(root)/'evidence').iterdir());self.assertTrue((folder/'REQUEST.json').exists());self.assertEqual(json.loads((folder/'RETURN.json').read_bytes())['parsed']['structuredOutput'],None)
 def test_planner_block_not_budget_classification(self):
  with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'tests')as root:
   service=GoalService(root);self.addCleanup(service.server.server_close);r=service.app.submit({'title':'fixture','goal':'fixture','acceptance':{'artifacts':[],'lead_selects_artifacts':True,'text':'test'},'idempotency_key':'one'});d=service.app.layer.continuity_block(r['goal_id'],reason='planner failed [lead_unavailable]: lead planning failed [review_blocked]')['decision'];self.assertEqual(d['kind'],'checkpoint')
 def test_restart_rejects_live_identity(self):
  import psutil
  with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'tests')as root:
   p=Path(root);(p/'host.json').write_text(json.dumps({'pid':os.getpid(),'birth':psutil.Process().create_time()}));(p/'settled.json').write_text(json.dumps({'thread_alive':False}))
   with self.assertRaisesRegex(ValueError,'still alive'):validate_settled(p)
if __name__=='__main__':unittest.main(verbosity=2)
