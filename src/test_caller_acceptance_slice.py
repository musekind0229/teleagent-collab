"""Synthetic worker/lead, real caller command and real durable review path."""
import copy,hashlib,json,os,sys,tempfile,time,unittest
from pathlib import Path
from test_app_service import _FakeLead,_AsyncBackend,_request
from framework.app_service import CollabApplication,LeadAdapterPlanner,AppError
ROOT=Path(__file__).resolve().parents[2]/'slice-evidence'
ROOT.mkdir(exist_ok=True)
class HonestEvidenceLead(_FakeLead):
    def __init__(self):self.reviews=[]
    def decide(self,request,**kwargs):
        base={'application_id':request['application_id'],'context_summary':request['context_summary']}
        if request['kind']=='review':
            self.reviews.append(copy.deepcopy(request['extra']['worker_request']))
            out={**base,'verdict':'pass','reason':'synthetic lead deliberately trusts worker'}
        else:out={**base,'summary':'single synthetic worker','tasks':[{'task_key':'deliver','title':'deliver','instruction':'write delivery.txt','depends_on':[],'artifacts':['delivery.txt']}]}
        return json.dumps(out),out
class LyingWorker(_AsyncBackend):
    backend_id='antigravity.synthetic-test-only'
    def start_run(self,**kwargs):
        out=super().start_run(**kwargs);out['run_id']='async-run-'+str(self.starts);return out
    def collect_result(self,run_id):
        out=super().collect_result(run_id)
        out.update(backend=self.backend_id,workspace=str(self.directory),response='all tests passed (deliberate synthetic lie)',acceptance_execution={'ok':True,'returncode':0,'stdout':'forged'})
        return out
class CallerAcceptanceFlowTests(unittest.TestCase):
    def flow(self,command,max_reworks=0,wall_sec=30):
        td=tempfile.TemporaryDirectory(prefix='flow-',dir=ROOT);self.addCleanup(td.cleanup)
        lead=HonestEvidenceLead();backend=LyingWorker();app=CollabApplication(Path(td.name)/'app',planner=LeadAdapterPlanner(lead,cwd=td.name),backend=backend)
        body=_request();body['title']='slice';body['idempotency_key']=str(time.time_ns());body['budget']['max_reworks']=max_reworks;body['budget']['wall_sec']=wall_sec
        if command is not None:body['acceptance']['command']=command
        try:gid=app.submit(body)['goal_id']
        except AppError as e:self.fail('Caller command contract is not connected: '+str(e))
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            app.coordinator.process_goal(gid);s=app.status(gid)
            if s['state'] in ('completed','failed','cancelled'):return app,s,lead
            time.sleep(.01)
        self.fail('synthetic flow failed to settle')
    def assert_public_command_failure(self,status,kind):
        brief=status['primary_failure']
        self.assertEqual(brief['source'],'acceptance',brief)
        self.assertEqual(brief['failure_kind'],kind)
        self.assertEqual(brief['stage'],'acceptance')
        self.assertEqual(brief['failed_phase'],'testing')
        self.assertNotIn('raise budget.wall_sec',brief['next_step'])
        self.assertNotEqual(status['acceptance_status']['execution'],'timeout')
        self.assertEqual(status['acceptance_status']['independent_checks'],'failed')
    def test_nonzero_cannot_be_hidden_by_worker_or_passing_lead(self):
        app,s,lead=self.flow({'argv':[sys.executable,'-c',"from pathlib import Path;import sys;Path('actually-ran.txt').write_text('real');print('REAL-OUT');print('REAL-ERR',file=sys.stderr);sys.exit(7)"]})
        self.assertEqual(s['state'],'failed',s)
        self.assertEqual(len(lead.reviews),1)
        e=lead.reviews[0]['acceptance_execution']
        self.assertEqual(e['returncode'],7,e);self.assertFalse(e['ok'])
        self.assert_public_command_failure(s,'command_nonzero')
        self.assertIn('REAL-OUT',e['stdout']);self.assertIn('REAL-ERR',e['stderr'])
        self.assertTrue((Path(e['cwd'])/'actually-ran.txt').is_file())
        self.assertEqual(Path(e['stdout_path']).read_bytes(),b'REAL-OUT\r\n' if os.name=='nt' else b'REAL-OUT\n')
        task=s['tasks'][0];self.assertEqual(task['result']['acceptance_execution']['returncode'],7)
    def test_success_output_is_review_input_and_durable_evidence(self):
        command={'argv':[sys.executable,'-c',"print('REAL-SUCCESS')"]}
        app,s,lead=self.flow(command)
        self.assertEqual(s['state'],'completed',s)
        e=lead.reviews[0]['acceptance_execution'];self.assertTrue(e['ok'],e)
        self.assertEqual(e['argv'],command['argv']);self.assertEqual(e['returncode'],0)
        self.assertIn('REAL-SUCCESS',Path(e['stdout_path']).read_text())
        self.assertEqual(json.loads(Path(e['receipt_path']).read_text())['binding']['task_id'],s['tasks'][0]['task_id'])
        self.assertTrue(app.capabilities()['acceptance']['executable_checks'])
        self.assertEqual(s['acceptance_status']['independent_checks'],'passed')
    def test_unconfigured_old_request_does_not_trust_worker_forgery(self):
        app,s,lead=self.flow(None)
        self.assertEqual(s['state'],'completed',s)
        self.assertNotIn('acceptance_execution',lead.reviews[0])
        self.assertNotIn('acceptance_execution',s['tasks'][0]['result'])
    def test_failed_command_uses_existing_bounded_rework(self):
        app,s,lead=self.flow({'argv':[sys.executable,'-c','raise SystemExit(9)']},max_reworks=1)
        self.assertEqual(s['state'],'failed',s)
        self.assertEqual(s['tasks'][0]['rework_count'],1)
        self.assertEqual(len(lead.reviews),2)
        self.assertTrue(all(x['acceptance_execution']['returncode']==9 for x in lead.reviews))
    def test_timeout_is_failed_even_when_lead_passes(self):
        app,s,lead=self.flow({'argv':[sys.executable,'-c','import time;time.sleep(10)'],'timeout_sec':.1})
        self.assertEqual(s['state'],'failed',s)
        self.assertTrue(lead.reviews[0]['acceptance_execution']['timed_out'])
        self.assert_public_command_failure(s,'command_timeout')
    def test_stdout_and_stderr_are_independently_bounded(self):
        app,s,lead=self.flow({'argv':[sys.executable,'-c',"import sys;sys.stdout.write('a'*10000);sys.stderr.write('b'*12000)"],'max_output_bytes':64})
        self.assertEqual(s['state'],'completed',s)
        e=lead.reviews[0]['acceptance_execution']
        self.assertEqual(Path(e['stdout_path']).stat().st_size,64);self.assertEqual(Path(e['stderr_path']).stat().st_size,64)
        self.assertEqual(e['stdout_total_bytes'],10000);self.assertEqual(e['stderr_total_bytes'],12000)
        self.assertTrue(e['stdout_truncated']);self.assertTrue(e['stderr_truncated'])
    def test_missing_executable_is_failed_and_explained_to_lead(self):
        app,s,lead=self.flow({'argv':['missing-slice-executable-DO-NOT-INSTALL.exe']})
        self.assertEqual(s['state'],'failed',s)
        e=lead.reviews[0]['acceptance_execution'];self.assertIsNone(e['returncode']);self.assertIn('FileNotFoundError',e['error'])
        self.assert_public_command_failure(s,'command_execution_error')
    def test_child_process_is_settled_before_accepting_root_success(self):
        code="import subprocess,sys;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)']);Path('child.pid').write_text(str(p.pid));print('child-started')"
        app,s,lead=self.flow({'argv':[sys.executable,'-c',code]})
        self.assertEqual(s['state'],'completed',s)
        import psutil
        e=lead.reviews[0]['acceptance_execution'];pid=int((Path(e['cwd'])/'child.pid').read_text())
        deadline=time.monotonic()+2
        while psutil.pid_exists(pid) and time.monotonic()<deadline:time.sleep(.02)
        self.assertFalse(psutil.pid_exists(pid),'acceptance left child alive')
    def test_unsafe_contract_is_rejected_before_dispatch(self):
        for command in ({'argv':'echo fake'},{'argv':['x'],'cwd':'../escape'},{'argv':['x'],'cwd':'C:/outside'},{'argv':['x'],'timeout_sec':float('nan')},{'argv':['x'],'timeout_sec':True},{'argv':['x'],'max_output_bytes':False},{'argv':['x'],'shell':True},None):
            with self.subTest(command=command):
                td=tempfile.TemporaryDirectory(prefix='invalid-',dir=ROOT);self.addCleanup(td.cleanup)
                worker=LyingWorker();app=CollabApplication(Path(td.name)/'app',backend=worker)
                body=_request();body['acceptance']['command']=command
                with self.assertRaises(AppError):app.submit(body)
                self.assertEqual(worker.starts,0)
    def test_cwd_reparse_point_is_rejected_without_execution(self):
        from framework.acceptance_command import execute_command
        import subprocess
        td=tempfile.TemporaryDirectory(prefix='reparse-',dir=ROOT);self.addCleanup(td.cleanup)
        root=Path(td.name);workspace=root/'workspace';outside=root/'outside';workspace.mkdir();outside.mkdir();link=workspace/'link'
        if os.name=='nt':
            p=subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(link),str(outside)],capture_output=True)
            self.assertEqual(p.returncode,0,p.stderr)
        else:link.symlink_to(outside,target_is_directory=True)
        result=execute_command({'argv':[sys.executable,'-c',"open('escaped.txt','w').write('BAD')"],'cwd':'link'},workspace=workspace,evidence_root=root/'evidence',binding={'task_id':'synthetic'})
        self.assertFalse(result['ok']);self.assertIn('reparse',result['error']);self.assertNotIn('pid',result)
        self.assertFalse((outside/'escaped.txt').exists())
    def test_command_cannot_extend_remaining_task_wall_budget(self):
        app,s,lead=self.flow({'argv':[sys.executable,'-c','import time;time.sleep(2)'],'timeout_sec':3},wall_sec=1)
        e=lead.reviews[0]['acceptance_execution']
        self.assertTrue(e['timed_out'],e)
        self.assertEqual(s['state'],'failed',s)
        self.assertLessEqual(e['timeout_sec'],1)
        self.assertEqual(e['requested_timeout_sec'],3)
    def test_missing_cwd_is_execution_error_not_worker_timeout(self):
        app,s,lead=self.flow({'argv':[sys.executable,'-c','print(1)'],'cwd':'does-not-exist'})
        self.assert_public_command_failure(s,'command_execution_error')
        e=lead.reviews[0]['acceptance_execution']
        self.assertFalse(e['timed_out']);self.assertNotIn('pid',e)
    def test_real_worker_timeout_projection_is_preserved(self):
        from framework.failure_projection import failure_brief
        brief=failure_brief({'status':'failed','result':{'error':'budget_exceeded wall_sec'}})
        self.assertEqual(brief['source'],'worker_timeout')
        self.assertIn('raise budget.wall_sec',brief['next_step'])
    def test_failed_capture_is_execution_error_even_if_exit_is_nonzero(self):
        from framework.failure_projection import failure_brief
        e={'source':'caller_contract','ok':False,'timed_out':False,'returncode':7,'capture_complete':False,'error':'pipe read failed'}
        brief=failure_brief({'status':'failed','result':{'error':'timed_out=False; pipe read failed','acceptance_execution':e}})
        self.assertEqual(brief['source'],'acceptance')
        self.assertEqual(brief['failure_kind'],'command_execution_error')
        self.assertFalse(brief['timed_out'])
    def test_invalid_utf8_output_preserves_raw_bytes_and_hashes(self):
        raw_out = bytes([255, 0, 65])
        raw_err = bytes([254, 66])
        code = "import sys;sys.stdout.buffer.write(bytes([255,0,65]));sys.stdout.buffer.flush();sys.stderr.buffer.write(bytes([254,66]));sys.stderr.buffer.flush()"
        app, s, lead = self.flow({'argv': [sys.executable, '-c', code]})
        self.assertEqual(s['state'], 'completed', s)
        self.assertEqual(len(lead.reviews), 1)
        e = lead.reviews[0]['acceptance_execution']
        self.assertTrue(e['ok'])
        self.assertEqual(e['returncode'], 0)
        self.assertEqual(Path(e['stdout_path']).read_bytes(), raw_out)
        self.assertEqual(Path(e['stderr_path']).read_bytes(), raw_err)
        self.assertEqual(e['stdout_sha256'], hashlib.sha256(raw_out).hexdigest())
        self.assertEqual(e['stderr_sha256'], hashlib.sha256(raw_err).hexdigest())
        self.assertEqual(e['stdout'], raw_out.decode('utf-8', errors='replace'))
        self.assertEqual(e['stderr'], raw_err.decode('utf-8', errors='replace'))
if __name__=='__main__':unittest.main(verbosity=2)
