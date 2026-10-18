"""Run deadline is independent of package checkpoint; no spawn/model access."""
import importlib.util, math, sys, unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from framework import goal_service
Native=goal_service.Native
native_timeout=lambda value:goal_service.native_timeout(value)
from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend
class Tests(unittest.TestCase):
 def test_finite_positive_without_permanent_900_cap(self):
  for v in (0.25,300,900,1200,5401):self.assertEqual(native_timeout(v),float(v))
  for v in (True,False,0,-1,float('inf'),float('-inf'),float('nan'),'bad'):
   with self.subTest(value=v),self.assertRaises((ValueError,TypeError)):native_timeout(v)
 def test_service_native_uses_explicit_run_timeout_not_goal_wall(self):
  backend=Native(object(),'never-execute',native_timeout_sec=900)
  original={'timeout_sec':5400,'goal':'synthetic','max_redos':2}
  with patch.object(AntigravityCliExecutionBackend,'start_run',return_value={'ok':False,'synthetic':True})as start,patch('subprocess.Popen',side_effect=AssertionError('NO MODELS')):
   backend.start_run(title='synthetic',directory=str(ROOT),charter=original,caller_task_id='synthetic')
  self.assertEqual(start.call_args.kwargs['charter']['timeout_sec'],900)
  self.assertEqual(original['timeout_sec'],5400)
  self.assertEqual(start.call_args.kwargs['caller_task_id'],'synthetic')
 def test_default_service_run_deadline_is_backend_compatible(self):
  backend=Native(object(),'never-execute')
  self.assertEqual(backend.timeout_sec,300)
  with patch.object(AntigravityCliExecutionBackend,'start_run',return_value={})as start:
   backend.start_run(title='synthetic',directory=str(ROOT),charter={'timeout_sec':5400})
  self.assertEqual(start.call_args.kwargs['charter']['timeout_sec'],5400)
 def test_explicit_cap_never_extends_shorter_caller_deadline(self):
  backend=Native(object(),'never-execute',native_timeout_sec=900)
  for prior,expected in ((30,30),(.25,.25),(5400,900),(None,900)):
   with self.subTest(prior=prior),patch.object(AntigravityCliExecutionBackend,'start_run',return_value={})as start:
    backend.start_run(title='synthetic',directory=str(ROOT),charter={'timeout_sec':prior})
    self.assertEqual(start.call_args.kwargs['charter']['timeout_sec'],expected)
 def test_cli_accepts_finite_fractional_and_rejects_invalid(self):
  spec=importlib.util.spec_from_file_location('goal_entry_deadline',ROOT/'bin/goal-entry.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  # argparse must validate before service construction or state writes.
  for value in ('0','-1','nan','inf'):
   with patch.object(sys,'argv',['goal-entry','--state',str(ROOT/'never-created'),'serve','--native-timeout-sec',value]),self.assertRaises(SystemExit)as exc:m.main()
   self.assertEqual(exc.exception.code,2)
 def test_actual_rendered_contract_separates_5400_package_from_900_run(self):
  import tempfile,json
  from test_synthetic_caller_fixture import fixture_backend
  from framework.app_service import worker_charter_for_task
  with tempfile.TemporaryDirectory(dir=ROOT)as td:
   workspace=Path(td)/'goal_synthetic'/'task_synthetic';workspace.mkdir(parents=True)
   backend=Native(object(),sys.executable,native_timeout_sec=900)
   backend._continuity_workspace_root=Path(td).resolve();fixture_backend(backend)
   task={'task_id':'task_synthetic','title':'Synthetic','inputs':{'instruction':'synthetic only'},'expected_artifacts':['RESULT.md']}
   goal={'budget':{'wall_sec':5400,'max_reworks':2}}
   charter=worker_charter_for_task(goal=goal,task=task)
   self.assertEqual(charter['timeout_sec'],5400)
   backend.authorize_caller_task(task_id=task['task_id'],title=task['title'],directory=str(workspace),instruction='synthetic only',artifacts=['RESULT.md'])
   with patch.object(backend,'_start_run_locked',return_value=({}, {'ok':True,'synthetic':True}))as start,patch('subprocess.Popen',side_effect=AssertionError('NO SPAWN')):
    result=backend.start_run(title=task['title'],directory=str(workspace),instruction='synthetic only',artifacts=['RESULT.md'],charter=charter,caller_task_id=task['task_id'])
   self.assertTrue(result['ok']);self.assertEqual(start.call_args.kwargs['charter']['timeout_sec'],900)
   from execution_backend.antigravity_cli_v1 import _contract_meta
   bounded={**charter,'timeout_sec':900.0}
   self.assertEqual(start.call_args.kwargs['contract_meta'],_contract_meta(bounded))
   self.assertEqual(goal['budget']['wall_sec'],5400)
   self.assertEqual(charter['timeout_sec'],5400)

 def test_goal_service_threads_run_setting_without_releasing_models(self):
  import tempfile
  with tempfile.TemporaryDirectory(dir=ROOT)as td,patch('subprocess.Popen',side_effect=AssertionError('NO SPAWN')):
   service=goal_service.GoalService(Path(td),lead_bin='never-execute',native_bin=sys.executable,native_timeout_sec=900)
   try:
    import time
    app=service.app
    with patch('framework.app_service.read_git_head',return_value='synthetic'),patch('framework.app_service.process_start_tip',return_value='synthetic'):
     gid=app.submit({'idempotency_key':'deadline','title':'Synthetic','goal':'synthetic only','acceptance':{'artifacts':['RESULT.md']}})['goal_id']
    now=time.time()
    def grant(key,window):return {'goal_id':gid,'grant_id':key,'reason':'synthetic explicit','resource':{'package_id':key,'native_limit':1,'lead_limit':1,'original_epoch':now,'checkpoint_epoch':now+window}}
    app.layer.continuity_authorize(gid,decision_id='',grant=grant('first',5400),trusted=True)
    blocked=app.layer.continuity_block(gid,reason='synthetic owner checkpoint')
    app.layer.continuity_authorize(gid,decision_id=blocked['decision']['decision_id'],grant=grant('second',6000),trusted=True)
    self.assertEqual(service.native_timeout_sec,900)
    self.assertEqual(service.app.coordinator.backend.native_timeout_sec,900)
    self.assertFalse(service.server.released)
    self.assertEqual(app.layer.get_goal(gid)['goal']['resource_packages']['first']['resource']['checkpoint_epoch'],now+5400)
    self.assertEqual(app.layer.get_goal(gid)['goal']['resource_packages']['second']['resource']['checkpoint_epoch'],now+6000)
   finally:service.server.server_close()

if __name__=='__main__':unittest.main(verbosity=2)
