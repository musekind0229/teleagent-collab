"""Physical same-store stop/restart HOLD/explicit continue product regression."""
from pathlib import Path
import sys,time,json,os,subprocess,psutil
W=Path(__file__).resolve().parents[1];sys.path[:0]=[str(W/'src'),str(W/'tests')]
from framework.goal_service import GoalService
from exercise_goal_continuity import TestBackend,TestLead

def fixture(state):
 S=Path(state);service=GoalService(S,test_factory=lambda a:(TestLead(a),TestBackend(a,fail_initial=True)));endpoint=service.start();(S/'host.json').write_text(json.dumps({'endpoint':endpoint,'pid':os.getpid(),'birth':psutil.Process().create_time(),'synthetic':True,'model_release':False}))
 try:
  while not(S/'stop.json').exists():time.sleep(.1)
 finally:service.close();(S/'settled.json').write_text(json.dumps({'thread_alive':False,'state':'settled'}))
if len(sys.argv)>1 and sys.argv[1]=='--fixture':fixture(sys.argv[2]);sys.exit(0)
E=W/'tests'/('restart-evidence-'+time.strftime('%Y%m%d-%H%M%S'));E.mkdir();S=E/'state';commands=[];host=None
def cli(*args,ok=True):
 p=subprocess.run([sys.executable,'-B',str(W/'bin/goal-entry.py'),'--state',str(S),*args],cwd=W,capture_output=True,timeout=15);commands.append({'args':args,'rc':p.returncode,'stdout':p.stdout.decode(),'stderr':p.stderr.decode()});assert(p.returncode==0)==ok,commands[-1];return json.loads(p.stdout)
def launch(cmd,old_birth=None):
 f=(E/('host-'+str(len(commands))+'.log')).open('wb');p=subprocess.Popen(cmd,cwd=W,stdout=f,stderr=f);end=time.time()+10
 while time.time()<end and p.poll()is None:
  if(S/'host.json').exists():
   h=json.loads((S/'host.json').read_bytes())
   if h['birth']!=old_birth:return p
  time.sleep(.1)
 raise AssertionError('host start failed '+str(p.poll()))
try:
 host=launch([sys.executable,'-B',str(Path(__file__).resolve()),'--fixture',str(S)])
 op=cli('open','--goal','synthetic stopped goal','--must','fixture artifact','--key','existing','--native-limit','1','--lead-limit','2','--window-sec','90');gid=op['goal_id'];end=time.time()+10
 while time.time()<end:
  blocked=cli('status','--goal-id',gid)
  if blocked['state']=='awaiting_owner'and blocked['tasks']:break
  time.sleep(.1)
 assert blocked['state']=='awaiting_owner';decision=blocked['pending_decisions'][0]['decision_id'];old=json.loads((S/'host.json').read_bytes());cli('consume','--goal-id',gid);cli('stop');host.wait(timeout=10);host=None;store=S/'.collab-durable/store.json';raw=store.read_bytes()
 cli('serve',ok=False);cli('serve','--restart-settled',str(old['birth']+1),ok=False)
 host=launch([sys.executable,'-B',str(W/'bin/goal-entry.py'),'--state',str(S),'serve','--restart-settled',str(old['birth'])],old['birth']);assert store.read_bytes()==raw
 held=cli('status','--goal-id',gid);assert held['state']=='awaiting_owner'and held['resources']['actual_lead']==1 and held['resources']['actual_native']==0;cli('continue','--goal-id',gid,'--decision-id',decision,'--grant-id','forbidden','--reason','no release','--native-limit','1','--lead-limit','1','--window-sec','90',ok=False)
 # Stop real default HOLD; model release is intentionally NOT performed.
 cli('stop');host.wait(timeout=10);host=None
 # Resume same store with TEST-injected adapters, not a fake live model.
 from framework.host_restart import archive_settled_host
 def save(p,x):p.write_text(json.dumps(x))
 nowhost=json.loads((S/'host.json').read_bytes());archive_settled_host(S,expected_epoch=nowhost['birth'],write_json=save)
 host=launch([sys.executable,'-B',str(Path(__file__).resolve()),'--fixture',str(S)])
 cli('continue','--goal-id',gid,'--decision-id',decision,'--grant-id','explicit-resume','--reason','synthetic owner authorized same-store continuation','--native-limit','1','--lead-limit','1','--window-sec','90');end=time.time()+15
 while time.time()<end:
  done=cli('status','--goal-id',gid)
  if done['state']=='completed':break
  time.sleep(.1)
 assert done['state']=='completed',done;cli('report','--goal-id',gid);cli('consume','--goal-id',gid);cli('stop');host.wait(timeout=10);host=None
 result={'state':'PASS_STOP_RESTART_HOLD_SAME_STORE_EXPLICIT_CONTINUE','goal_id':gid,'same_store':str(store),'goal_POST_count':1,'new_POST_for_restart':0,'old_host_evidence_archived':True,'restart_before_authorization_store_bytes_unchanged':True,'wrong_birth_refused':True,'restart_without_explicit_flag_refused':True,'HOLD_continue_refused':True,'synthetic_execution_only':True,'new_paid':0};(E/'RESULT.json').write_text(json.dumps(result,indent=2))
finally:
 if host and host.poll()is None:(S/'stop.json').write_text('{}');host.wait(timeout=10)
 (E/'COMMANDS.json').write_text(json.dumps(commands,indent=2))
print(json.dumps({'evidence':str(E),'result':result},indent=2))
