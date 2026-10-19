"""Physical interpreter/CLI, synthetic filesystem and network/process deny.
No production route or config and no actual transport/model.
"""
import os,sys,tempfile,json,unittest,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
CHILD=r'''
import sys,runpy,types,json,os
from pathlib import Path
root=Path(sys.argv.pop(1));mode=sys.argv.pop(1);sys.path.insert(0,str(root/'src'))
def guard(event,args):
 if event in('socket.connect','socket.__new__','subprocess.Popen','os.system'):raise AssertionError('NO NETWORK/PROCESS')
 if event=='open'and isinstance(args[0],(str,bytes)):
  p=str(args[0]).lower().replace('\\','/')
  if any(t in p for t in('/.env','/config.yaml','/memories/','/sessions/','/states/','/raw/')):raise AssertionError('NO PRIVATE SOURCE')
sys.addaudithook(guard)
if mode.endswith('spy'):
 m=types.ModuleType('framework.runtime_guard')
 def verify():
  with open(os.environ['SPY_GUARD'],'a')as f:f.write('guard invoked\n')
 m.verify_runtime=verify;sys.modules[m.__name__]=m
 n=types.ModuleType('framework.lifecycle_transport')
 class Sender:
  def __init__(self,path):assert Path(path).name=='synthetic-route.json'
  def __call__(self,channel,body):
   assert channel=='qq'and len(body.splitlines())<=10 and 'MEDIA:'not in body
   with open(os.environ['SPY_LOG'],'a')as f:f.write('spy-only\n')
   return {'status':'sent','delivery_id':'SYNTHETIC'}
 n.HermesLifecycleSender=Sender;sys.modules[n.__name__]=n
if mode=='scheduled-spy':
 from framework.governance_daily import scheduled_main
 raise SystemExit(scheduled_main(sys.argv[1]))
runpy.run_module('framework.governance_daily',run_name='__main__')
'''
class Physical(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']);self.addCleanup(self.tmp.cleanup);self.p=Path(self.tmp.name);self.home=self.p/'synthetic-home';self.home.mkdir()
  self.ledger=self.p/'ledger.json';self.ledger.write_text(json.dumps({'issues':[{'issue_id':'synthetic','owner':'pm','rounds':[]}]}))
  self.input=self.p/'input.json';self.input.write_text('{}');self.output=self.p/'report.json';self.cron=self.p/'cron.json';self.cron.write_text(json.dumps({'cron':{'worker':[{'purpose':'collab-v2-comprehensive-daily','id':'synthetic-job'}]}}))
 def runcli(self,mode='preview',extra=()):
  argv=[sys.executable,'-B','-c',CHILD,str(ROOT),mode,'--profile-home-root',str(self.home),'--ledger',str(self.ledger),'--ledger-checkpoint',str(self.p/'checkpoint.json'),'--input',str(self.input),'--output',str(self.output),'--cron-metadata',str(self.cron),*extra]
  return subprocess.run(argv,capture_output=True,text=True,env={**os.environ,'PYTHONIOENCODING':'utf-8','SPY_LOG':str(self.p/'spy.log'),'SPY_GUARD':str(self.p/'guard.log')},timeout=30)
 def test_actual_preview_empty_rules_no_private_source(self):
  r=self.runcli();self.assertEqual(r.returncode,0,r.stderr);o=json.loads(self.output.read_bytes());self.assertEqual(len(o['preview'].splitlines()),10);self.assertEqual(o['rule_observation']['missing'],16);self.assertFalse((self.p/'spy.log').exists())
 def test_actual_send_cli_synthetic_guard_and_sender_restart_dedupe(self):
  args=['--send','--route-file',str(self.p/'synthetic-route.json'),'--receipt-root',str(self.p/'receipts')]
  for _ in range(2):
   r=self.runcli('spy',args);self.assertEqual(r.returncode,0,r.stderr)
  self.assertEqual((self.p/'spy.log').read_text().splitlines(),['spy-only']);self.assertEqual(len((self.p/'guard.log').read_text().splitlines()),2)
  data=json.loads(self.output.with_suffix('.delivery.json').read_bytes());self.assertEqual(data['delivery_id'],'SYNTHETIC')
 def test_real_runtime_send_gate_refuses_unregistered_worktree(self):
  r=self.runcli(extra=['--send']);self.assertNotEqual(r.returncode,0);self.assertFalse((self.p/'spy.log').exists())
 def test_duplicate_scheduler_and_renamed_ledger_block(self):
  self.assertEqual(self.runcli().returncode,0)
  self.ledger.write_text(json.dumps({'issues':[{'issue_id':'renamed','owner':'pm','rounds':[]}]}));r=self.runcli();self.assertEqual(r.returncode,2,r.stderr);self.assertTrue(json.loads(self.output.read_bytes())['ledger_errors'])
  self.ledger.write_text(json.dumps({'issues':[{'issue_id':'synthetic','owner':'pm','rounds':[]}]}));self.cron.write_text(json.dumps({'cron':{'worker':[{'purpose':'collab-v2-comprehensive-daily'},{'script':'governance_daily.py'}]}}))
  r=self.runcli('spy',['--send','--route-file',str(self.p/'synthetic-route.json'),'--receipt-root',str(self.p/'receipts')]);self.assertNotEqual(r.returncode,0);self.assertFalse((self.p/'spy.log').exists())
 def test_actual_no_agent_launcher_binding_through_same_send_main(self):
  binding={'approved':True,'daily_id':'collab-v2-comprehensive-daily','timezone':'UTC+08:00','profile-home-root':str(self.home),'ledger':str(self.ledger),'ledger-checkpoint':str(self.p/'checkpoint.json'),'input':str(self.input),'output':str(self.output),'cron-metadata':str(self.cron),'receipt-root':str(self.p/'receipts'),'route-file':str(self.p/'synthetic-route.json')}
  bp=self.p/'binding.json';bp.write_text(json.dumps(binding))
  for _ in range(2):
   r=subprocess.run([sys.executable,'-B','-c',CHILD,str(ROOT),'scheduled-spy',str(bp)],capture_output=True,text=True,env={**os.environ,'PYTHONIOENCODING':'utf-8','SPY_LOG':str(self.p/'spy.log'),'SPY_GUARD':str(self.p/'guard.log')},timeout=30)
   self.assertEqual(r.returncode,0,r.stderr)
  self.assertEqual((self.p/'spy.log').read_text().splitlines(),['spy-only'])
 def test_alias_transfer_after_separate_cli_process_keeps_original_budget(self):
  import copy
  old={'issues':[{'issue_id':'synthetic','owner':'pm','aliases':['same-known-problem'],'rounds':[{'number':n,'result':'failed','evidence':'synthetic'}for n in(1,2)]}]}
  self.ledger.write_text(json.dumps(old));r=self.runcli();self.assertEqual(r.returncode,0,r.stderr)
  good=copy.deepcopy(old);good['issues'][0]['aliases'].append('new-evidenced-alias');self.ledger.write_text(json.dumps(good));r=self.runcli();self.assertEqual(r.returncode,0,r.stderr)
  checkpoint=(self.p/'checkpoint.json').read_bytes();bad=copy.deepcopy(good);bad['issues'][0]['aliases'].remove('same-known-problem');bad['issues'].append({'issue_id':'narrow','owner':'worker','aliases':['same-known-problem'],'rounds':[]})
  self.ledger.write_text(json.dumps(bad));r=self.runcli();self.assertEqual(r.returncode,2,r.stderr);self.assertEqual((self.p/'checkpoint.json').read_bytes(),checkpoint);self.assertIn('BLOCKED',json.loads(self.output.read_bytes())['packet']['rounds'])
if __name__=='__main__':unittest.main(verbosity=2)
