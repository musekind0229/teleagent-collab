"""Owned physical process/restart and lifecycle CLI, all inputs synthetic."""
import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
GUARD='''import sys
def audit(event,args):
 if event.startswith(('subprocess.','os.system','os.spawn','os.exec'))or event in('socket.connect','socket.getaddrinfo'):raise OSError('NO PROCESS OR NETWORK')
 if event=='open'and isinstance(args[0],str)and args[0].replace(chr(92),'/').rsplit('/',1)[-1].lower()in('.env','auth.json','settings.json','config.yaml','credentials.json'):raise OSError('NO PRIVATE CONFIG')
sys.addaudithook(audit)
'''
CHILD='''import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1]);from framework.lifecycle_events import LifecycleOutbox
root=Path(sys.argv[2]);box=LifecycleOutbox(root,runtime={'head':'a'*40,'runtime_root':'synthetic'})
s={'goal_id':'synthetic_goal','state':'completed','goal':{'title':'synthetic'},'tasks':[],'pending_decisions':[]}
e=box.publish(s)
if sys.argv[3]=='prepare':
 box.deliver(e['event_id'],lambda c,p:{'status':'sent','delivery_id':'spy-qq'}if c=='qq'else{'status':'queued','delivery_id':'spy-bot'},now=100)
else:
 assert box.status(e['event_id'])['qq']['state']=='sent'
 box.consume(e['event_id'],'pm',packet_sha256=e['packet_sha256'],receipt_id='synthetic-physical-read')
 box.report(e['event_id'],packet_sha256=e['packet_sha256'],report={'conclusion':'healthy_no_change','evidence':['owned child read exact packet'],'proposal':'none'},receipt_id='synthetic-physical-report')
 box.consume(e['event_id'],'owner',packet_sha256=box.status(e['event_id'])['report_sha256'],receipt_id='synthetic-owner-read')
print(json.dumps({'event_id':e['event_id'],'packet_sha256':e['packet_sha256'],'status':box.status(e['event_id'])}))
'''
class Tests(unittest.TestCase):
 def test_owned_process_restarts_then_cli_reads_same_exact_event(self):
  with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])as td:
   root=Path(td);guard=root/'guard';guard.mkdir();(guard/'sitecustomize.py').write_text(GUARD);script=root/'synthetic.py';script.write_text(CHILD)
   env={k:os.environ[k]for k in('SYSTEMROOT','WINDIR')if k in os.environ};env.update(PYTHONPATH=str(guard),PYTHONIOENCODING='utf-8',TMPDIR=td,TEMP=td,TMP=td)
   rows=[]
   for mode in('prepare','restart'):
    p=subprocess.run([sys.executable,'-B',str(script),str(ROOT/'src'),str(root/'state'),mode],env=env,capture_output=True,text=True,timeout=20)
    self.assertEqual(p.returncode,0,p.stderr);rows.append(json.loads(p.stdout))
   self.assertEqual(rows[0]['event_id'],rows[1]['event_id']);self.assertEqual(rows[1]['status']['owner']['state'],'consumed');self.assertFalse(rows[1]['status']['maintenance_authorized'])
   state=root/'state';(state/'.collab-durable').mkdir();(state/'.collab-durable/store.json').write_text('{}');(state/'host.json').write_text('{}')
   argv=[sys.executable,'-B',str(ROOT/'bin/goal-entry.py'),'--state',str(state),'--existing-state','lifecycle','status','--event-id',rows[0]['event_id']]
   p=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=20);self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(json.loads(p.stdout)['pm']['state'],'consumed')
if __name__=='__main__':unittest.main(verbosity=2)
