import unittest,tempfile,json,copy,subprocess
from pathlib import Path
from unittest.mock import patch
from framework.planning_contract import project_plan
from framework.app_service import build_planning_request,planning_response_schema,validate_plan,LeadAdapterPlanner,AppError
from lead_adapter.grok_cli import GrokCliLeadAdapter
from lead_adapter.schema import format_lead_request_prompt
W=Path(__file__).resolve().parents[1]
class Tests(unittest.TestCase):
 def snap(self,native=3,lead=5):
  return {'goal_id':'goal_fixture','created_at':100,'goal':{'desired_outcome':'synthetic only','acceptance':{'artifacts':[]},'boundaries':{},'budget':{'wall_sec':2331,'max_reworks':3}},'active_resource_package':'p','resource_packages':{'p':{'resource':{'package_id':'p','original_epoch':631,'checkpoint_epoch':2431,'native_limit':native,'lead_limit':lead},'claims':[],'actual_native':0,'actual_lead':0}}}
 def test_resource_cap_and_time_units_no_store_mutation(self):
  s=self.snap();before=copy.deepcopy(s)
  with patch('framework.planning_contract.time.time',return_value=640):r=build_planning_request(s)
  self.assertEqual(r['max_tasks'],3);self.assertEqual(r['budget']['wall_sec'],1791);self.assertEqual(r['extra']['time_budget_semantics']['goal_schedule_offset_sec'],2331);self.assertEqual(r['extra']['time_budget_semantics']['package_window_sec'],1800);self.assertEqual(s,before)
 def test_pending_and_no_spawn_accounting(self):
  s=self.snap();s['resource_packages']['p']['claims']=[{'kind':'native','state':'reserved'},{'kind':'lead','state':'no_spawn'},{'kind':'lead','state':'spawned'}]
  self.assertEqual(build_planning_request(s)['max_tasks'],2)
 def test_reject_five_tasks_not_force_single(self):
  r=build_planning_request(self.snap());rows=[{'task_key':str(i),'title':'test','instruction':'synthetic','depends_on':[],'artifacts':[]}for i in range(5)]
  p={'application_id':r['application_id'],'context_summary':r['context_summary'],'summary':'test','tasks':rows}
  with self.assertRaisesRegex(AppError,'1..3'):validate_plan(p,r)
  p['tasks']=rows[:2];self.assertEqual(len(validate_plan(p,r)['tasks']),2)
 def test_no_call_when_impossible(self):
  class Never:
   def decide(*a,**kw):raise AssertionError('must not spend lead')
  with self.assertRaisesRegex(AppError,'resource_plan_infeasible'):LeadAdapterPlanner(Never(),cwd=W).plan(self.snap(native=0))
 def test_prompt_phase_clear(self):
  p=format_lead_request_prompt(build_planning_request(self.snap()));self.assertIn('not a failed business review',p);self.assertIn('minimum_native_starts_per_task',p);self.assertIn('directory scans',p)
 def test_exact_original_stderr_replay_and_caller_parameters(self):
  # Synthetic envelope with the exact observed stderr; not a provider replay.
  req=build_planning_request(self.snap());text={'stdout':json.dumps({'stopReason':'cancelled','structuredOutput':None,'structuredOutputError':'model did not produce structured output','num_turns':1}),'stderr':'Error: max turns reached\n'}
  with tempfile.TemporaryDirectory(dir=W/'tests')as d:
   adapter=GrokCliLeadAdapter(bin_path='NOT_EXECUTED',tool_free=True,caller_evidence_root=d)
   def fake(cmd,**kw):
    self.assertEqual(cmd[cmd.index('--tools')+1],'');self.assertIn('--no-subagents',cmd);self.assertIn('--disable-web-search',cmd);self.assertEqual(cmd[cmd.index('--max-turns')+1],'1');self.assertEqual(kw['timeout'],180)
    self.assertEqual(Path(cmd[2]).read_text(encoding='utf-8'),format_lead_request_prompt(req));return subprocess.CompletedProcess(cmd,1,text['stdout'],text['stderr'])
   with patch('lead_adapter.grok_cli.run_cancellable',side_effect=fake)as runner:raw,p=adapter.decide(req,schema=planning_response_schema(),cwd=str(W),timeout_sec=180)
   self.assertEqual(runner.call_count,1);assert isinstance(p,dict);self.assertTrue(p['incomplete']);self.assertEqual(p['failure_stage'],'lead_cli_turn_limit');self.assertIn('not a business review verdict',p['reason']);self.assertFalse(p['raw_evidence']['caller_observation']['timeout_exception']);self.assertEqual(p['raw_evidence']['capture_status'],'not_recorded');self.assertTrue(list(Path(d).glob('call-*/CALLER.json')))
 def test_schema_cap_sent(self):
  class Capture:
   def decide(inner,r,**kw):
    self.assertEqual(r['max_tasks'],3);self.assertEqual(kw['schema']['properties']['tasks']['maxItems'],3)
    return '',{'application_id':r['application_id'],'context_summary':r['context_summary'],'summary':'fixture','tasks':[{'task_key':'one','title':'one','instruction':'fixture','depends_on':[],'artifacts':[]}]}
  self.assertEqual(len(LeadAdapterPlanner(Capture(),cwd=W).plan(self.snap())['tasks']),1)
if __name__=='__main__':unittest.main(verbosity=2)
