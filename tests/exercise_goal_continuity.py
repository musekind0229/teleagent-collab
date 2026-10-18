"""Physical product path with adapters injected ONLY from this test module."""
from pathlib import Path
import sys,time,json,subprocess,tempfile,os,hashlib,psutil
W=Path(__file__).resolve().parents[1];sys.path.insert(0,str(W/'src'))
from framework.goal_service import GoalService
from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend

def spawn(authority,kind,binding):
 key=authority.reserve(kind,binding)
 try:p=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(.15);print("SYNTHETIC_ONLY")'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 except OSError:authority.no_spawn(key);raise
 authority.actual(key,pid=p.pid,birth=psutil.Process(p.pid).create_time());p.communicate(timeout=5)
 return key
class TestLead:
 name='synthetic_recorded_not_model'
 def __init__(self,a):self.a=a
 def decide(self,r,**kwargs):
  b={k:r.get(k)for k in ('application_id','context_summary','kind')};b['task_id']=(r.get('extra')or{}).get('current_task',{}).get('task_id');spawn(self.a,'lead',b)
  if r['kind']=='plan':v={'application_id':r['application_id'],'context_summary':r['context_summary'],'summary':'synthetic plan','tasks':[{'task_key':'one','title':'Synthetic artifact','instruction':'Fictional only','depends_on':[],'artifacts':['RESULT.md']}]}
  else:v={'application_id':r['application_id'],'context_summary':r['context_summary'],'verdict':'pass','reason':'synthetic recorded review'}
  return json.dumps(v),v
class TestBackend(AntigravityCliExecutionBackend):
 def __init__(self,a,*,fail_initial=False):super().__init__(bin_path=sys.executable,environ={'AGY_AUTO_APPROVE':'1'});self.a=a;self.fail_initial=fail_initial;self.rows={};self.configure_caller_permissions(allowed_operations=['read','write','command'],native_mode='skip_permissions')
 def _start_run_locked(self,**kw):
  if self.fail_initial and self.a.status(Path(kw['root']).parent.name)['active']['resource']['package_id'].startswith('initial_'):raise ValueError('synthetic ordinary initial dispatch failure; test injection only')
  root=Path(kw['root']);key=spawn(self.a,'native',{'goal_id':root.parent.name,'task_id':root.name});(root/'RESULT.md').write_text('SYNTHETIC ARTIFACT ONLY\n');rid='recorded_'+key[1][:12];self.rows[rid]={'ok':True,'backend':self.backend_id,'run_id':rid,'workspace':str(root),'artifacts':[str(root/'RESULT.md')],'response':'synthetic recorded result','warnings':['NOT_MODEL'],'state':'completed'};return {},{'ok':True,'backend':self.backend_id,'run_id':rid,'state':'running'}
 def observe_run(self,r):return {'ok':True,'state':'completed','busy':False,'activity':'idle','finish':'success'}
 def collect_result(self,r):return self.rows[r]
 def close(self):pass

def main():
 E=W/'tests'/('continuity-evidence-'+time.strftime('%Y%m%d-%H%M%S'));E.mkdir();S=E/'state';service=GoalService(S,test_factory=lambda a:(TestLead(a),TestBackend(a,fail_initial=True)));endpoint=service.start();(S/'host.json').write_text(json.dumps({'endpoint':endpoint,'pid':os.getpid(),'birth':psutil.Process().create_time(),'synthetic':True,'model_release':False}));commands=[]
 def cli(*args,success=True):
  p=subprocess.run([sys.executable,'-B',str(W/'bin/goal-entry.py'),'--state',str(S),*args],cwd=W,capture_output=True,timeout=15);commands.append({'args':args,'rc':p.returncode,'stdout':p.stdout.decode(),'stderr':p.stderr.decode()});assert(p.returncode==0)==success,commands[-1];return json.loads(p.stdout)
 def poll(gid,target):
  end=time.time()+15
  while time.time()<end:
   v=cli('status','--goal-id',gid)
   if v['state']==target:return v
   time.sleep(.1)
  raise AssertionError(v)
 try:
  opened=cli('open','--goal','Fictional same-store continuation','--must','recorded artifact','--must-not','no private data or models','--key','one-goal','--native-limit','1','--lead-limit','2','--window-sec','90');gid=opened['goal_id'];blocked=poll(gid,'awaiting_owner');decision=blocked['pending_decisions'][0]['decision_id'];assert len(blocked['tasks'])==1
  raw_before=service.app.layer.get_goal(gid)['goal'];assert raw_before['tasks'][0]['status']=='failed';created=raw_before['created_at'];history_len=len(raw_before['history']);store=S/'.collab-durable/store.json';same_store=store.resolve()
  cli('consume','--goal-id',gid)
  # HTTP request cannot masquerade as a trusted local grant.
  import urllib.request,urllib.error
  try:urllib.request.urlopen(urllib.request.Request(endpoint+'/v1/continuity/'+gid+'/continue',data=b'{}',method='POST'),timeout=5);raise AssertionError('untrusted grant admitted')
  except urllib.error.HTTPError as exc:assert exc.code==403
  grantargs=['continue','--goal-id',gid,'--decision-id',decision,'--grant-id','explicit-increment-1','--reason','Owner bounded native/review allowance after ordinary block','--native-limit','1','--lead-limit','1','--window-sec','90']
  cli(*grantargs);cli(*grantargs)
  done=poll(gid,'completed');assert done['resources']['actual_native']==1 and done['resources']['actual_lead']==2,done
  final=service.app.layer.get_goal(gid)['goal'];assert final['created_at']==created and len(final['history'])>history_len;assert final['tasks'][0]['attempt_history'][-1]['status']=='failed';assert len(final['resource_packages'])==2
  cli('report','--goal-id',gid);cli('consume','--goal-id',gid)
  assert same_store==store.resolve();assert len(service.app.layer.list_goals()['goals'])==1
  result={'state':'PASS_SAME_STORE_BLOCK_DECIDE_INCREMENT_CONTINUE_DELIVER','goal_id':gid,'same_store':str(store),'no_successor_copy':True,'goal_count':1,'old_failure_preserved':True,'packages':2,'synthetic_native':1,'synthetic_lead':2,'model_calls':0,'untrusted_HTTP_refused':True,'repeat_grant_no_extra_calls':True,'report_consume':True};(E/'RESULT.json').write_text(json.dumps(result,indent=2))
 finally:service.close();(E/'COMMANDS.json').write_text(json.dumps(commands,indent=2))
 print(json.dumps({'evidence':str(E),'result':result},indent=2))
if __name__=='__main__':main()
