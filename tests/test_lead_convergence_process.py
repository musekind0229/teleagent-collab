"""Physical owned Python CLI surrogate; installed grok is NEVER executed."""
import json,os,socket,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from lead_adapter.grok_cli import GrokCliLeadAdapter
from lead_adapter.cancel import trusted_spawn_admission
from lead_adapter.schema import build_lead_request,lead_review_response_schema,validate_lead_decision
from framework.app_service import CollabApplication
from framework.goal_service import ResourceAuthority
CHILD='''import sys,json,time,argparse
from pathlib import Path
time.sleep(.15)
p=argparse.ArgumentParser();p.add_argument('--prompt-file');p.add_argument('--cwd');p.add_argument('--max-turns',type=int);p.add_argument('--output-format');p.add_argument('--json-schema');p.add_argument('--disallowed-tools');p.add_argument('--tools');p.add_argument('--disable-web-search',action='store_true');p.add_argument('--no-subagents',action='store_true');a=p.parse_args()
text=Path(a.prompt_file).read_text(encoding='utf-8');req=json.loads(text.split('Full request JSON:\\n',1)[1]);schema=json.loads(a.json_schema)
assert a.max_turns==8 and a.tools=='' and a.disable_web_search and a.no_subagents
assert schema['properties']['application_id']['const']==req['application_id']
assert 'No tools are available'in text and 'review_blocked'in text
Path(a.cwd,'SYNTHETIC-OBSERVED.json').write_text(json.dumps({'argv':sys.argv[1:],'prompt':text,'schema':schema}))
obj={'application_id':req['application_id'],'context_summary':req['context_summary'],'verdict':'pass','reason':'SYNTHETIC CLI SURROGATE ONLY'}
print(json.dumps({'stopReason':'end_turn','structuredOutput':obj,'num_turns':3}))
'''
GUARD='''import sys
def audit(event,args):
 if event.startswith(('subprocess.','os.system','os.spawn','os.exec'))or event=='socket.connect':raise OSError('CHILD SIDE EFFECT FORBIDDEN')
 if event=='open'and isinstance(args[0],str)and args[0].replace(chr(92),'/').rsplit('/',1)[-1].lower()in('.env','auth.json','settings.json','config.yaml'):raise OSError('PRIVATE CONFIG FORBIDDEN')
sys.addaudithook(audit)
'''
class Tests(unittest.TestCase):
 def test_real_subprocess_argv_prompt_schema_one_dispatch_three_reported_turns(self):
  with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR',str(ROOT)))as td:
   root=Path(td);fake=root/'synthetic_cli.py';fake.write_text(CHILD);guard=root/'guard';guard.mkdir();(guard/'sitecustomize.py').write_text(GUARD)
   app=CollabApplication(root/'store');authority=ResourceAuthority(app.layer)
   with patch('framework.app_service.read_git_head',return_value='synthetic'),patch('framework.app_service.process_start_tip',return_value='synthetic'):
    gid=app.submit({'idempotency_key':'metered','title':'Synthetic','goal':'synthetic only','acceptance':{'artifacts':['RESULT.md']}})['goal_id']
   now=time.time();app.layer.continuity_authorize(gid,decision_id='',grant={'goal_id':gid,'grant_id':'synthetic','reason':'synthetic process budget','resource':{'package_id':'synthetic','native_limit':1,'lead_limit':2,'original_epoch':now,'checkpoint_epoch':now+60}},trusted=True)
   req=build_lead_request(kind='review',goal='synthetic supplied material',authorized_scope=[],prohibitions=['no tools'],acceptance_criteria={},current_application={'goal_id':gid})
   adapter=GrokCliLeadAdapter(bin_path='SYNTHETIC_NEVER_EXECUTED',max_turns=8,tool_free=True)
   real=subprocess.Popen;seen=[]
   def owned(cmd,**kw):
    if cmd[0]!='SYNTHETIC_NEVER_EXECUTED':raise AssertionError('non-owned spawn')
    seen.append(list(cmd));env={k:os.environ[k]for k in ('SYSTEMROOT','WINDIR')if k in os.environ};env.update(PYTHONPATH=str(guard),PYTHONIOENCODING='utf-8')
    return real([sys.executable,'-B',str(fake),*cmd[1:]],env=env,**kw)
   with patch('subprocess.Popen',side_effect=owned),patch('socket.socket.connect',side_effect=AssertionError('NO CONNECT')),trusted_spawn_admission(authority,{'goal_id':gid}):
    raw,parsed=adapter.decide(req,schema=lead_review_response_schema(),cwd=str(root),timeout_sec=10)
   self.assertEqual(len(seen),1);self.assertEqual(authority.status(gid)['actual_lead'],1);self.assertEqual(authority.status(gid)['actual_native'],0)
   self.assertEqual(parsed['raw_evidence']['caller_observation']['reported_internal_turns'],3)
   self.assertEqual(parsed['raw_evidence']['caller_observation']['effective_internal_turn_limit'],8)
   self.assertEqual(validate_lead_decision(raw,parsed,request=req)['verdict'],'pass')
   observed=json.loads((root/'SYNTHETIC-OBSERVED.json').read_bytes());self.assertEqual(observed['argv'],seen[0][1:])
   E=ROOT.parent/'lead-convergence-evidence';(E/'PHYSICAL-ARGV-PROMPT-SCHEMA.json').write_text(json.dumps({'synthetic_only':True,'argv':seen[0],'observed':observed,'actual_lead_dispatches':1,'reported_internal_turns':3,'internal_turn_limit':8},indent=2))
 def test_goal_entry_cli_eight_loads_hold_and_preserves_default_one(self):
  with tempfile.TemporaryDirectory(dir=ROOT,prefix='synthetic-convergence-')as td:
   root=Path(td);guard=root/'guard';guard.mkdir();(guard/'sitecustomize.py').write_text(GUARD)
   env={k:os.environ[k]for k in ('SYSTEMROOT','WINDIR')if k in os.environ};env.update(PYTHONPATH=str(guard),PYTHONIOENCODING='utf-8')
   for turns in (None,8):
    state=root/('state-'+str(turns));argv=[sys.executable,'-B',str(ROOT/'bin/goal-entry.py'),'--state',str(state),'serve','--native-bin',sys.executable,'--lead-bin','NEVER_EXECUTE']
    if turns is not None:argv+=['--lead-max-turns',str(turns)]
    proc=subprocess.Popen(argv,env=env,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
     deadline=time.monotonic()+10;host={}
     while time.monotonic()<deadline:
      if proc.poll()is not None:break
      try:host=json.loads((state/'host.json').read_bytes())
      except(FileNotFoundError,ValueError):pass
      if host.get('pid')==proc.pid:break
      time.sleep(.05)
     self.assertEqual(host.get('pid'),proc.pid);self.assertEqual(host['lead_max_turns'],turns or 1);self.assertFalse(host['model_release'])
     (state/'stop.json').write_text('{}');out,err=proc.communicate(timeout=10);self.assertEqual(proc.returncode,0,out+err)
     self.assertFalse(json.loads((state/'settled.json').read_bytes())['thread_alive'])
    finally:
     if proc.poll()is None:proc.terminate();proc.communicate(timeout=10)

if __name__=='__main__':unittest.main(verbosity=2)
