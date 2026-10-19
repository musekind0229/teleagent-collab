"""Physical goal-entry HTTP/CLI with synthetic adapters and strict side-effect guards."""
from pathlib import Path
import importlib.util,json,os,socket,subprocess,sys,tempfile,threading,time,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests'))
from framework.goal_service import GoalService
from test_goal_loop_maintenance import Lead,Backend

GUARD='''import sys,socket
PORT=__PORT__
def audit(event,args):
 if event.startswith(('subprocess.','os.system','os.spawn','os.exec')):raise OSError('CHILD MODELS/PROCESSES FORBIDDEN')
 if event=='socket.connect':
  addr=args[1]
  if not isinstance(addr,tuple)or addr[:2]!=('127.0.0.1',PORT):raise RuntimeError('REMOTE NETWORK FORBIDDEN')
 if event=='open'and isinstance(args[0],str):
  name=args[0].replace(chr(92),'/').rsplit('/',1)[-1].lower()
  if name in ('.env','auth.json','config.yaml','credentials.json'):raise RuntimeError('PRIVATE CONFIG FORBIDDEN')
sys.addaudithook(audit)
'''
class Tests(unittest.TestCase):
 def test_physical_open_wait_report_and_exact_owner_receipt(self):
  commands=[]
  with tempfile.TemporaryDirectory(dir=ROOT,prefix='synthetic-handoff-')as td:
   base=Path(td);state=base/'state';guard=base/'guard';guard.mkdir()
   with patch('framework.app_service.read_git_head',return_value='68050f2'),patch('framework.app_service.process_start_tip',return_value='68050f2'):
    service=GoalService(state,test_factory=lambda a:(Lead(a,('fail','pass')),Backend(a)))
    # Existing actual HTTP handler, but coordinator progresses only under this test.
    thread=threading.Thread(target=service.server.serve_forever,daemon=True);thread.start();service.thread=thread
    endpoint='http://127.0.0.1:'+str(service.server.server_port)
    (state/'host.json').write_text(json.dumps({'endpoint':endpoint,'synthetic':True,'model_release':False}))
    (guard/'sitecustomize.py').write_text(GUARD.replace('__PORT__',str(service.server.server_port)))
    raw_popen=subprocess.Popen;raw_connect=socket.socket.connect
    def allowed_popen(argv,*a,**kw):
     if argv[:3]!=[sys.executable,'-B',str(ROOT/'bin/goal-entry.py')]:raise AssertionError('NOT OWNED CLI')
     return raw_popen(argv,*a,**kw)
    def allowed_connect(sock,address):
     if address[:2]!=('127.0.0.1',service.server.server_port):raise AssertionError('REMOTE NETWORK')
     return raw_connect(sock,address)
    env={k:os.environ[k]for k in ('SYSTEMROOT','WINDIR')if k in os.environ};env.update(PYTHONPATH=str(guard),PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1',TMPDIR=str(base),TEMP=str(base),TMP=str(base))
    def cli(*args,rc=0):
     p=subprocess.run([sys.executable,'-B',str(ROOT/'bin/goal-entry.py'),'--state',str(state),*args],env=env,cwd=ROOT,capture_output=True,text=True,timeout=30)
     commands.append({'args':args,'rc':p.returncode,'stdout':p.stdout,'stderr':p.stderr});self.assertEqual(p.returncode,rc,commands[-1]);return json.loads(p.stdout)
    try:
     with patch('subprocess.Popen',side_effect=allowed_popen),patch('socket.socket.connect',new=allowed_connect):
      opened=cli('open','--goal','Synthetic only','--must','SYNTHETIC ARTIFACT','--key','physical-test','--native-limit','3','--lead-limit','5','--window-sec','90');gid=opened['goal_id']
      waiting=cli('wait','--goal-id',gid,'--timeout-sec','.01',rc=3);self.assertEqual(waiting['state'],'observation_timeout')
      self.assertEqual(service.authority.status(gid)['actual_native'],0)
      for _ in range(8):service.app.coordinator.process_goal(gid)
      status=cli('status','--goal-id',gid);self.assertEqual(status['state'],'completed')
      event=cli('wait','--goal-id',gid,'--timeout-sec','.1');again=cli('wait','--goal-id',gid,'--timeout-sec','.1')
      self.assertEqual(event['event_id'],again['event_id']);self.assertEqual(event['delivery'],'prepared');self.assertFalse((state/'owner-consumption').exists())
      packet=json.loads(Path(event['packet']).read_bytes());self.assertEqual(packet['consumer'],'product_owner');self.assertIn('report',packet)
      args=('consume','--goal-id',gid,'--event-id',event['event_id'],'--packet-sha256',event['packet_sha256'],'--receipt-id','synthetic-explicit-owner-receipt')
      ack=cli(*args);self.assertEqual(ack,cli(*args));self.assertFalse(ack['business_acceptance'])
      cli('consume','--goal-id',gid,'--event-id',event['event_id'],'--packet-sha256','incorrect','--receipt-id','wrong',rc=2)
      self.assertEqual(service.authority.status(gid)['actual_native'],2);self.assertEqual(service.authority.status(gid)['actual_lead'],3)
      backend=service.app.coordinator.backend;backend.failures=['permission denied']
      other=cli('open','--goal','Synthetic escalation only','--must','SYNTHETIC ARTIFACT','--key','physical-escalation','--native-limit','1','--lead-limit','2','--window-sec','90')['goal_id']
      for _ in range(8):service.app.coordinator.process_goal(other)
      raised=cli('wait','--goal-id',other,'--timeout-sec','.1',rc=4)
      self.assertEqual(raised['state'],'awaiting_owner');self.assertEqual(raised['delivery'],'prepared')
      self.assertEqual(service.authority.status(other)['actual_native'],1);self.assertEqual(service.authority.status(other)['actual_lead'],1)
      raised_packet=json.loads(Path(raised['packet']).read_bytes());self.assertTrue(raised_packet['status']['pending_decisions'][0]['return_to_upper'])
      self.assertFalse((state/'owner-consumption'/(raised['event_id']+'.json')).exists())
    finally:
     service.close()
     (Path(os.environ.get('COLLAB_TEST_EVIDENCE_DIR',str(base)))/'WORKER-PHYSICAL-COMMANDS.json').write_text(json.dumps(commands,indent=2))
if __name__=='__main__':unittest.main(verbosity=2)
