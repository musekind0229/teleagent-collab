"""Synthetic final-prompt material visibility. No installed CLI or network."""
import copy,hashlib,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src')]
from framework.agy_review import build_complete_delta,build_snapshot_material_packet
from framework.review_material_codec import pack_material
from lead_adapter.schema import build_lead_request,format_lead_request_prompt
from lead_adapter.grok_cli import GrokCliLeadAdapter
# The app module captures a Git tip at import; supply synthetic engineering
# metadata instead of relaxing the independent process deny guard.
with patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,'synthetic\n','')):
 import framework.app_service
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']);self.addCleanup(self.tmp.cleanup)
  self.guard=patch('subprocess.Popen',side_effect=AssertionError('NO PROCESSES'));self.guard.start();self.addCleanup(self.guard.stop)
  self.net=patch('socket.socket.connect',side_effect=AssertionError('NO NETWORK'));self.net.start();self.addCleanup(self.net.stop)
  for target in ('framework.app_service.read_git_head','framework.app_service.process_start_tip'):
   p=patch(target,return_value='synthetic');p.start();self.addCleanup(p.stop)
 def payload(self):
  self.old=('// 原始 CRLF\r\n'+''.join('const a%d = "旧值";\r\n'%i for i in range(900))).encode()
  self.new=self.old.replace('旧值'.encode(),'新值'.encode())+b'export const finalMarker = true;\r\n'
  def row(b):return {'raw_hex':b.hex(),'sha256':hashlib.sha256(b).hexdigest()}
  self.current={'src/big.js':row(self.new),'src/new.js':row('export const 名称="新文件";\n'.encode()),'src/dep.js':row(b'export const dep = 1;\n')}
  base={'src/big.js':row(self.old),'src/dep.js':self.current['src/dep.js'],'deleted.js':row('删除\n'.encode())}
  delta=build_complete_delta(baseline=base,current=self.current,binding={'task_id':'synthetic'},evidence_root=self.tmp.name,capacity_bytes=900000)
  return {'source_packet':build_snapshot_material_packet(current=self.current,capacity_limit_bytes=500000),'complete_delta':delta,'diff':'summary only','diff_truncated':True,'review_status':'complete_alternative_delta','scope_rule':'KEEP SCOPE','unreviewed_dependencies':[],'changed_files':[{'path':p}for p in ('src/big.js','src/new.js','deleted.js')],'owner_review_recovery':{'original_failure':'KEEP FAILURE'}}
 def request(self,p):return build_lead_request(kind='review',goal='synthetic',authorized_scope=['KEEP SCOPE'],prohibitions=['NO TOOLS'],acceptance_criteria={'text':'KEEP ACCEPTANCE'},current_application={'goal_id':'synthetic'},extra={'worker_request':pack_material(p),'current_task':{'expected_artifacts':['src/big.js','src/new.js']},'sibling_tasks':[{'task_id':'dependency','expected_artifacts':['src/dep.js']}]})
 def test_final_prompt_contains_direct_exact_endpoints_and_context(self):
  p=self.payload();prompt=format_lead_request_prompt(self.request(p),tool_free=True)
  for path,side,raw in [('src/big.js','AFTER',self.new),('src/big.js','BEFORE',self.old),('src/new.js','BEFORE',b''),('deleted.js','AFTER',b'')]:
   header='FILE '+json.dumps(path,ensure_ascii=False)+' '+side+' UTF8_BYTES '+str(len(raw))+'\n'
   self.assertIn(header,prompt);start=prompt.index(header)+len(header);self.assertEqual(prompt[start:start+len(raw.decode())].encode(),raw)
  for value in ('KEEP SCOPE','KEEP ACCEPTANCE','KEEP FAILURE','dependency','summary only','"diff_truncated": true','Summary diff is truncated','src/dep.js') :self.assertIn(value,prompt)
  self.assertEqual(json.loads(prompt.split('Full request JSON:\n',1)[1])['extra']['worker_request'],self.request(p)['extra']['worker_request'])
 def test_wrong_hash_missing_and_insufficient_capacity_rejected_before_dispatch(self):
  p=self.payload()
  variants=[]
  bad=copy.deepcopy(p);bad['source_packet']['provided_blocks'][0]['sha256']='0'*64;variants.append(bad)
  bad=copy.deepcopy(p);bad['source_packet']['provided_blocks']=[x for x in bad['source_packet']['provided_blocks']if x['path']!='src/big.js'];variants.append(bad)
  bad=copy.deepcopy(p);bad['complete_delta']['paths'][0]['before']['sha256']='0'*64;variants.append(bad)
  bad=copy.deepcopy(p);bad['source_packet']['is_blocked']=True;variants.append(bad)
  bad=copy.deepcopy(p);bad['source_packet']['capacity_limit_bytes']=1;variants.append(bad)
  bad=copy.deepcopy(p);del bad['complete_delta'];variants.append(bad)
  for bad in variants:
   with self.subTest(case=len(str(bad))),patch('lead_adapter.grok_cli.run_cancellable',side_effect=AssertionError('NO DISPATCH')),self.assertRaises(ValueError):GrokCliLeadAdapter(bin_path='NEVER',tool_free=True).decide(self.request(bad),schema={},cwd=self.tmp.name)
 def test_legacy_permission_and_material_free_review_not_reinterpreted(self):
  req=self.request({});self.assertNotIn('READABLE VERIFIED',format_lead_request_prompt(req))
  req['kind']='permission';req['extra']['worker_request']=pack_material(self.payload());self.assertNotIn('READABLE VERIFIED',format_lead_request_prompt(req))
 def test_actual_written_prompt_with_real_summary_budget_truncation(self):
  from framework.agy_review import change_set,review_payload
  p=self.payload();current=copy.deepcopy(self.current)
  for row in current.values():row['text']=bytes.fromhex(row['raw_hex']).decode()
  # Added-file summary really exhausts the actual summary budget.
  with patch('framework.agy_review.MAX_DIFF_TOTAL',100):changes=change_set({},current,list(current))
  self.assertTrue(changes['diff_truncated']);self.assertTrue(changes['truncation_metrics'])
  delta=build_complete_delta(baseline={},current=current,binding={'task_id':'synthetic'},evidence_root=self.tmp.name,capacity_bytes=900000)
  payload=review_payload(changes=changes,worker_response='KEEP FAILURE',rework_round=0,max_reworks=1,source_packet=p['source_packet'])
  payload.update(complete_delta=delta,review_status='complete_alternative_delta')
  req=self.request(payload);observed={}
  # Delete every sidecar: final display must not fetch these local files.
  for f in Path(self.tmp.name).glob('*.bin'):f.unlink()
  def fake(cmd,**kw):
   prompt=Path(cmd[2]).read_bytes();observed.update(prompt_bytes=len(prompt),argv=cmd,schema=json.loads(cmd[cmd.index('--json-schema')+1]))
   evidence=ROOT.parent/'material-visibility-evidence'
   (evidence/'SYNTHETIC-FINAL-PROMPT.txt').write_bytes(prompt)
   (evidence/'SYNTHETIC-ARGV-SCHEMA.json').write_text(json.dumps({'argv':cmd,'pinned_schema':observed['schema'],'prompt_sha256':hashlib.sha256(prompt).hexdigest(),'synthetic_only':True},indent=2))
   self.assertIn(self.new,prompt);self.assertIn(b'Summary diff is truncated',prompt)
   self.assertIn(b'"diff_truncated": true',prompt);self.assertIn('名称'.encode(),prompt)
   self.assertEqual(observed['schema']['properties']['application_id']['const'],req['application_id'])
   return subprocess.CompletedProcess(cmd,0,json.dumps({'stopReason':'end_turn','structuredOutput':{'application_id':req['application_id'],'context_summary':req['context_summary'],'review_status':'review_blocked','incomplete':True,'reason':'SYNTHETIC ONLY'}}),'')
  from lead_adapter.schema import lead_review_response_schema
  with patch('lead_adapter.grok_cli.run_cancellable',side_effect=fake)as run:
   GrokCliLeadAdapter(bin_path='NEVER',tool_free=True,max_turns=8).decide(req,schema=lead_review_response_schema(),cwd=self.tmp.name)
  self.assertEqual(run.call_count,1);self.assertEqual(observed['argv'][observed['argv'].index('--max-turns')+1],'8')
  (ROOT.parent/'material-visibility-evidence/FINAL-PROMPT-METRICS.json').write_text(json.dumps({'synthetic_only':True,'prompt_bytes':observed['prompt_bytes'],'source_bytes':len(self.new),'summary_truncated':True,'direct_body_visible':True,'schema_pinned':True,'dispatch_calls':1},indent=2))
 def test_multiple_edits_reconstruct_before_exactly_without_files(self):
  from framework.review_material_codec import readable_material
  for old,new in [(b'a\nb\nc\nd\ne\n',b'FIRST\na\nc\nD\ne\nLAST'),(b'a\r\nb\r\nc',b'a\r\nB\r\nc'),(b'old',b'new'),(b'',b''),(b'\n\n',b'\n')]:
   def row(b):return {'raw_hex':b.hex(),'sha256':hashlib.sha256(b).hexdigest()}
   current={'file.js':row(new)};delta=build_complete_delta(baseline={'file.js':row(old)},current=current,binding={'task_id':'synthetic'},evidence_root=self.tmp.name)
   payload={'source_packet':build_snapshot_material_packet(current=current),'complete_delta':delta,'changed_files':[{'path':'file.js'}]if old!=new else[]}
   text=readable_material(pack_material(payload),expected_artifacts=['file.js'])
   for side,raw in ([('BEFORE',old),('AFTER',new)]if old!=new else[('CURRENT',new)]):
    header='FILE "file.js" '+side+' UTF8_BYTES '+str(len(raw))+'\n';i=text.index(header)+len(header);self.assertEqual(text[i:i+len(raw.decode())].encode(),raw)
 def test_missing_declared_artifact_dependency_and_delta_capacity_block(self):
  p=self.payload();r=self.request(p);r['extra']['current_task']['expected_artifacts'].append('missing.js')
  with self.assertRaises(ValueError):format_lead_request_prompt(r)
  p['source_packet']['unreviewed_dependencies']=[{'path':'dep','reason':'missing'}]
  with self.assertRaises(ValueError):format_lead_request_prompt(self.request(p))
  p=self.payload();p['complete_delta']['capacity_bytes']=1
  with self.assertRaises(ValueError):format_lead_request_prompt(self.request(p))
 def test_coordinator_readability_gate_refuses_before_lead(self):
  sys.path.insert(0,str(ROOT/'tests'))
  from test_review_recovery import Tests as Fixture
  fixture=Fixture();fixture.setUp();self.addCleanup(fixture.doCleanups)
  app,a,lead,backend,gid=fixture.make(answers=('partial',))
  with app.layer._rmw():
   app.layer.goals[gid]['goal']['acceptance'].update(auto_review_materials=True,complete_delta_required=True);app.layer._persist_unlocked()
  with patch('framework.review_material_codec.readable_material',side_effect=ValueError('synthetic readability cap')):out,s=fixture.drive(app,gid)
  self.assertEqual(len(lead.actions),0);self.assertEqual(a.status(gid)['actual_lead'],1) # planning only, zero review dispatches
  self.assertTrue(s['tasks'][0]['result']['material_blocked']);self.assertEqual(backend.starts,1)

if __name__=='__main__':unittest.main(verbosity=2)
