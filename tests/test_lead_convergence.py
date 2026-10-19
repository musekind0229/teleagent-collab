"""Convergence settings, final argv/prompt/schema; fake subprocess, no external effects."""
import copy,importlib.util,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from lead_adapter.grok_cli import GrokCliLeadAdapter,lead_max_turns
from lead_adapter.schema import build_lead_request,lead_review_response_schema,lead_permission_response_schema,validate_lead_decision,LeadDecisionError,pin_lead_response_schema
from framework.app_service import build_planning_request,planning_response_schema,LeadAdapterPlanner
import test_planning_turn_revision as planning_fixture

class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR',str(ROOT)));self.addCleanup(self.tmp.cleanup);self.dir=Path(self.tmp.name)
  for target in ('subprocess.Popen','socket.socket.connect'):
   p=patch(target,side_effect=AssertionError('NO EXTERNAL SIDE EFFECT'));p.start();self.addCleanup(p.stop)
 def request(self,kind='review'):
  return build_lead_request(kind=kind,goal='synthetic only',authorized_scope=['only task'],prohibitions=['no tools'],acceptance_criteria={'text':'synthetic acceptance'},current_application={'task_id':'synthetic_task'})
 def test_finite_integer_config_above_two_and_legacy(self):
  for v in (1,2,4,8,32,'1','2','4','8','32'):self.assertEqual(lead_max_turns(v),int(v))
  for v in (True,False,0,-1,1.0,4.0,4.5,float('inf'),float('nan'),'0','-1','4.0','04',' 4','+4',33,'33',None):
   with self.subTest(v=v),self.assertRaises(ValueError):lead_max_turns(v)
 def test_actual_argv_prompt_schema_bindings_with_config_eight(self):
  for kind in ('plan','review','permission'):
   if kind=='plan':r=build_planning_request(planning_fixture.Tests().snap());schema=planning_response_schema()
   else:r=self.request(kind);schema=lead_permission_response_schema()if kind=='permission'else lead_review_response_schema()
   original=copy.deepcopy(schema);expected=1 if kind=='permission'else 8
   adapter=GrokCliLeadAdapter(bin_path='SYNTHETIC_NEVER_EXECUTED',max_turns=8,tool_free=True,caller_evidence_root=self.dir)
   def fake(cmd,**kw):
    self.assertEqual(cmd[cmd.index('--max-turns')+1],str(expected));self.assertEqual(kw['timeout'],47.5)
    self.assertEqual(cmd[cmd.index('--tools')+1],'');self.assertIn('--no-subagents',cmd);self.assertIn('--disable-web-search',cmd)
    wire=json.loads(cmd[cmd.index('--json-schema')+1]);self.assertEqual(wire['properties']['application_id']['const'],r['application_id']);self.assertEqual(wire['properties']['context_summary']['const'],r['context_summary']);self.assertFalse(wire['additionalProperties'])
    prompt=Path(cmd[cmd.index('--prompt-file')+1]).read_text(encoding='utf-8')
    self.assertIn('No tools are available',prompt);self.assertIn('Do not wait',prompt);self.assertIn(r['application_id'],prompt)
    if kind=='review':self.assertIn('review_blocked',prompt);self.assertIn('no business verdict',prompt)
    return subprocess.CompletedProcess(cmd,0,json.dumps({'stopReason':'cancelled','structuredOutput':None,'num_turns':expected}), 'Error: max turns reached')
   with patch('lead_adapter.grok_cli.run_cancellable',side_effect=fake)as run:raw,parsed=adapter.decide(r,schema=schema,cwd=str(self.dir),timeout_sec=47.5)
   self.assertEqual(run.call_count,1);self.assertTrue(parsed['incomplete']);self.assertEqual(schema,original)
   observation=parsed['raw_evidence']['caller_observation']
   self.assertEqual(observation['max_turns'],expected);self.assertEqual(observation['reported_internal_turns'],expected)
   self.assertEqual(observation['configured_internal_turn_limit'],8);self.assertIn('one admitted CLI dispatch',observation['resource_accounting'])
 def test_non_tool_free_cannot_increase_turns(self):
  for turns in (2,4,8):
   with self.assertRaises(ValueError):GrokCliLeadAdapter(bin_path='NEVER',max_turns=turns,tool_free=False)
 def test_schema_allows_explicit_material_block_without_verdict(self):
  import jsonschema
  r=self.request();schema=pin_lead_response_schema(lead_review_response_schema(),r)
  blocked={'application_id':r['application_id'],'context_summary':r['context_summary'],'incomplete':True,'review_status':'review_blocked','reason':'required source absent'}
  jsonschema.validate(blocked,schema)
  with self.assertRaises(LeadDecisionError)as exc:validate_lead_decision(json.dumps(blocked),blocked,request=r)
  self.assertEqual(exc.exception.code,'lead_incomplete')
  with self.assertRaises(jsonschema.ValidationError):jsonschema.validate({**blocked,'verdict':'pass'},schema)
  with self.assertRaises(LeadDecisionError):validate_lead_decision('',{**blocked,'application_id':'wrong'},request=r)
  for verdict in ('pass','fail'):
   obj={'application_id':r['application_id'],'context_summary':r['context_summary'],'verdict':verdict,'reason':'synthetic concrete evidence'}
   jsonschema.validate(obj,schema);self.assertEqual(validate_lead_decision('',obj,request=r)['verdict'],verdict)
 def test_explicit_block_is_owner_stopped_not_paid_again(self):
  import test_goal_loop_maintenance as fixture
  f=fixture.Tests();f.setUp()
  try:
   app,a,l,b,gid=f.make(answers=('pass',));original=l.decide
   def blocked(r,**kw):
    if r['kind']=='plan':return original(r,**kw)
    from test_goal_loop_maintenance import receipt
    receipt(a,'lead',{'goal_id':gid,'task_id':r['extra']['current_task']['task_id']});l.actions.append(r['extra']['worker_request'])
    obj={'application_id':r['application_id'],'context_summary':r['context_summary'],'review_status':'review_blocked','incomplete':True,'reason':'synthetic material unavailable; no business verdict'}
    return json.dumps(obj),{'structuredOutput':obj,'stopReason':'end_turn'}
   l.decide=blocked;out,s=f.drive(app,gid)
   self.assertEqual(out['state'],'awaiting_owner');self.assertEqual(b.starts,1);self.assertEqual(len(l.actions),1);self.assertTrue(s['tasks'][0]['result']['review_no_verdict'])
  finally:f.doCleanups()
 def test_cli_host_propagates_eight_without_release(self):
  from framework.goal_service import GoalService
  service=GoalService(self.dir/'host',lead_bin='NEVER',native_bin=sys.executable,lead_max_turns=8)
  try:
   self.assertEqual(service.lead_max_turns,8);self.assertEqual(service.app.coordinator.planner.adapter.adapter.adapter.adapter.max_turns,8);self.assertFalse(service.server.released)
  finally:service.server.server_close()

 def test_cancelled_envelope_does_not_promote_json_pass_in_text(self):
  r=self.request();pass_obj={'application_id':r['application_id'],'context_summary':r['context_summary'],'verdict':'pass','reason':'SYNTHETIC DECOY NOT A VERDICT'}
  envelope={'stopReason':'cancelled','structuredOutput':None,'num_turns':8,'text':json.dumps(pass_obj)}
  adapter=GrokCliLeadAdapter(bin_path='NEVER_EXECUTE',max_turns=8,tool_free=True)
  with patch('lead_adapter.grok_cli.run_cancellable',return_value=subprocess.CompletedProcess([],1,json.dumps(envelope),'Error: max turns reached'))as run:
   raw,p=adapter.decide(r,schema=lead_review_response_schema(),cwd=str(self.dir))
  self.assertEqual(run.call_count,1);self.assertTrue(p['incomplete']);self.assertIsNone(p['structuredOutput']);self.assertNotIn('verdict',p)
  planner=LeadAdapterPlanner(adapter,cwd=self.dir)
  # Real coordinator no-verdict exception must win over text extraction.
  snap={'goal_id':'synthetic','goal':{'acceptance':{},'boundaries':{}},'tasks':[]};task={'task_id':'synthetic','title':'Synthetic','inputs':{'instruction':'synthetic'},'expected_artifacts':[]}
  with patch('lead_adapter.grok_cli.run_cancellable',return_value=subprocess.CompletedProcess([],1,json.dumps(envelope),'Error: max turns reached'))as run,self.assertRaises(LeadDecisionError)as err:
   planner.decide_action(snap,task,{'kind':'review','native_post_run':True,'payload':{}})
  self.assertEqual(err.exception.code,'lead_cancelled');self.assertEqual(run.call_count,1)
 def test_timeout_never_turns_into_extra_dispatch_or_extra_turns(self):
  r=self.request();adapter=GrokCliLeadAdapter(bin_path='NEVER_EXECUTE',max_turns=8,tool_free=True)
  with patch('lead_adapter.grok_cli.run_cancellable',side_effect=subprocess.TimeoutExpired('synthetic',.25))as run:
   raw,p=adapter.decide(r,schema=lead_review_response_schema(),cwd=str(self.dir),timeout_sec=.25)
  self.assertEqual(run.call_count,1);self.assertEqual(p['_lead_status'],'timeout');self.assertEqual(p['raw_evidence']['caller_observation']['max_turns'],8);self.assertEqual(p['raw_evidence']['caller_observation']['timeout_sec'],.25)
 def test_input_rejection_precedes_bin_resolution(self):
  for v in (False,0,1.5,33):
   with self.subTest(v=v),patch('lead_adapter.grok_cli.resolve_lead_bin',side_effect=AssertionError('must validate first')),self.assertRaises(ValueError):GrokCliLeadAdapter(max_turns=v,tool_free=True)
 def test_legacy_one_two_effective_argv_unchanged(self):
  r=self.request()
  for turns in (1,2):
   adapter=GrokCliLeadAdapter(bin_path='NEVER_EXECUTE',max_turns=turns,tool_free=True)
   with patch('lead_adapter.grok_cli.run_cancellable',return_value=subprocess.CompletedProcess([],1,'','synthetic error'))as run:
    adapter.decide(r,schema=lead_review_response_schema(),cwd=str(self.dir))
   self.assertEqual(run.call_count,1);cmd=run.call_args.args[0];self.assertEqual(cmd[cmd.index('--max-turns')+1],str(turns));self.assertEqual(cmd[cmd.index('--tools')+1],'')

if __name__=='__main__':unittest.main(verbosity=2)
