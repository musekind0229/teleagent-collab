"""Same-store continuity host; recording adapters are TEST injection only."""
import threading,time,copy,math

def native_timeout(value):
 """Finite positive per-native-run deadline, not a package checkpoint."""
 if isinstance(value,bool):raise ValueError('native timeout must be finite and > 0 seconds')
 value=float(value)
 if not math.isfinite(value)or value<=0:raise ValueError('native timeout must be finite and > 0 seconds')
 return value

def planning_timeout(value):
 """Local planning deadline only; never grants resources or releases models."""
 if isinstance(value,bool):raise ValueError('planning timeout must be finite, > 0 and <= 300 seconds')
 value=float(value)
 if not math.isfinite(value) or not 0 < value <= 300:raise ValueError('planning timeout must be finite, > 0 and <= 300 seconds')
 return value
from pathlib import Path
from framework.app_service import CollabApplication,AppCoordinator,CollabHttpServer,_Handler,CoordinatorLoop,LeadAdapterPlanner,budget_block_reason
from framework.durable_api import DurableLayer
from lead_adapter.grok_cli import GrokCliLeadAdapter
from lead_adapter.grok_cli import lead_max_turns as validate_lead_max_turns
from lead_adapter.cancel import trusted_spawn_admission
from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend

class ResourceAuthority:
 def __init__(self,layer):self.layer=layer
 def resolve(self,b):
  app=b.get('application_id','');gid=b.get('goal_id');tid=b.get('task_id')
  if b.get('workspace'):gid=Path(b['workspace']).parent.name;tid=Path(b['workspace']).name
  if app.startswith('plan_goal_'):gid=app[5:]
  if gid and not tid:
   with self.layer._rmw():
    if gid in self.layer.goals:return gid
  with self.layer._rmw():
   for key,s in self.layer.goals.items():
    if gid and gid!=key:continue
    if tid and not any(t['task_id']==tid for t in s.get('tasks',[])):continue
    if gid or tid:return key
  raise ValueError('unknown service dispatch binding')
 def reserve(self,kind,binding):
  gid=self.resolve(binding)
  return gid,self.layer.continuity_claim(gid,kind=kind,binding=binding)
 def actual(self,key,**kw):self.layer.continuity_receipt(*key,**kw)
 def no_spawn(self,key):self.layer.continuity_receipt(*key)
 def status(self,gid):
  s=self.layer.get_goal(gid)['goal'];packages=s.get('resource_packages',{});active=packages.get(s.get('active_resource_package'))
  return {'active':active,'packages':packages,'actual_native':sum(p['actual_native']for p in packages.values()),'actual_lead':sum(p['actual_lead']for p in packages.values())}

class Lead:
 name='grok_cli'
 def __init__(self,authority,bin_path,*,planning_timeout_sec=180.0,lead_max_turns=1):
  from framework.lead_evidence import EvidenceAdapter
  self.planning_timeout_sec=planning_timeout(planning_timeout_sec)
  self.authority=authority;self.adapter=EvidenceAdapter(GrokCliLeadAdapter(bin_path=bin_path,tool_free=True,max_turns=lead_max_turns,caller_evidence_root=Path(authority.layer.persist_root)/'lead-caller-evidence'),Path(authority.layer.persist_root)/'lead-wire-evidence')
 def decide(self,request,**kwargs):
  req=copy.deepcopy(request);b={k:req.get(k)for k in ('application_id','context_summary','kind')};b['task_id']=(req.get('extra')or{}).get('current_task',{}).get('task_id');gid=self.authority.resolve(b);s=self.authority.status(gid);p=s['active']
  req.setdefault('extra',{})['resource_package']={'active':p,'prior_packages':[x for x in s['packages'].values()if x is not p],'rule':'choose useful multi-task graph within remaining dispatch/review resources; ordinary rework or owner decision, no automatic goal termination'}
  snapshot=self.authority.layer.get_goal(gid)['goal']
  if req.get('kind')=='plan':
   remaining=p['resource']['checkpoint_epoch']-time.time()
   if remaining<=0:raise ValueError('planning resource checkpoint reached; explicit continuation required')
   kwargs['timeout_sec']=min(self.planning_timeout_sec,remaining)
  req.setdefault('extra',{})['continuation_context']={'latest_grants':snapshot.get('continuation_grants',[])[-3:],'resolved_decisions':snapshot.get('resolved_decisions',[])[-3:],'rule':'use owner reason/evidence to adjust; ordinary follow-up does not erase prior failure or consume a new planning goal'}
  with trusted_spawn_admission(self.authority,b):return self.adapter.decide(req,**kwargs)
class Native(AntigravityCliExecutionBackend):
 def __init__(self,authority,bin_path,*,task_permissions=False,native_timeout_sec=None):
  self.native_timeout_sec=None if native_timeout_sec is None else native_timeout(native_timeout_sec)
  super().__init__(bin_path=bin_path,timeout_sec=self.native_timeout_sec);self._trusted_dispatch_hook=authority
  if task_permissions:
   self.configure_caller_permissions(allowed_operations=['read','write','command'],native_mode='skip_permissions');env=dict(self._base_env());env['AGY_AUTO_APPROVE']='1';self._base_environ=env;self._environ=dict(env)
 def start_run(self,*,title,directory,instruction='',artifacts=None,charter=None,caller_task_id=None):
  # Render, contract hash and the backend deadline must all see the SAME
  # per-run value, never the whole-goal wall/checkpoint allowance.
  if self.native_timeout_sec is None:
   return super().start_run(title=title,directory=directory,instruction=instruction,artifacts=artifacts,charter=charter,caller_task_id=caller_task_id)
  bounded=copy.deepcopy(charter)if isinstance(charter,dict)else{}
  prior=bounded.get('timeout_sec')
  bounded['timeout_sec']=self.native_timeout_sec if prior is None else min(native_timeout(prior),self.native_timeout_sec)
  return super().start_run(title=title,directory=directory,instruction=instruction,artifacts=artifacts,charter=bounded,caller_task_id=caller_task_id)
 def authorize_caller_task(self,**kwargs):
  root=Path(kwargs['directory'])
  if not root.is_absolute():return {'ok':False,'reason':'absolute assigned task workspace required'}
  if root.name!=kwargs['task_id']or not root.parent.name.startswith('goal_'):return {'ok':False,'reason':'task workspace mismatch'}
  for p in [*reversed(root.parents),root]:
   if p.exists()and(p.is_symlink()or getattr(p.lstat(),'st_file_attributes',0)&0x400):return {'ok':False,'reason':'workspace reparse refused'}
  if not root.resolve().is_relative_to(self._continuity_workspace_root):return {'ok':False,'reason':'outside assigned task workspaces'}
  return super().authorize_caller_task(**kwargs)
 def _start_run_locked(self,**kwargs):
  self._continuity_binding={'goal_id':Path(kwargs['root']).parent.name,'task_id':Path(kwargs['root']).name}
  return super()._start_run_locked(**kwargs)

class ContinuityCoordinator(AppCoordinator):
 continuity_enabled=True

 def _settled_run(self, snap, task):
  """A collected/archived run is never collected or judged a second time."""
  gid=snap.get('goal_id');rid=str(task.get('run_id')or'')
  if not rid:return None
  live=self.layer.get_goal(gid)['goal'];current=next((t for t in live.get('tasks',[])if t['task_id']==task['task_id']),{})
  archived=any(str(t.get('run_id')or'')==rid for t in current.get('attempt_history',[]))
  if archived or str(current.get('run_id')or'')!=rid or current.get('status')not in ('running','review'):
   return {'ok':True,'goal_id':gid,'task_id':task['task_id'],'state':live['state'],'action':'stale_terminal_ignored','idempotent':True}
  return None

 def _after_observe(self,snap,task,observation,*,observe_error):
  settled=self._settled_run(snap,task)
  if settled is not None:return settled
  if observation.get('state')=='unknown' or (observation.get('ok')is False and observation.get('busy')is not True):
   return {'ok':True,'goal_id':snap['goal_id'],'state':'running','action':'observation_unknown','redispatch':False}
  return super()._after_observe(snap,task,observation,observe_error=observe_error)

 def _finish_gated(self,snap,task,result):
  settled=self._settled_run(snap,task)
  if settled is not None:return settled
  if isinstance(result,dict)and str(result.get('error')or'').startswith(('backend resume failed','backend observe failed','backend observation failed')):
   # An observation failure is NOT proof that the native process exited.
   # Keep its running slot/run binding; owner continuation must not respawn it.
   return self.layer.continuity_block(snap['goal_id'],reason='Unknown native execution; preserve running owner/run and do not redispatch: '+str(result['error']))
  return super()._finish_gated(snap,task,result)

 def _park_failure(self,snap,task,result,reason):
  gid=snap['goal_id'];tid=task['task_id']
  # Dispatch admission is not a replacement for a collected native/lead result.
  if result.get('failure_phase')=='dispatch':
   live=self.layer.get_goal(gid)['goal'];current=next(t for t in live['tasks']if t['task_id']==tid)
   stored=current.get('result')or result
  else:stored=result
  self._phase(gid,task_id=tid,outcome='checkpoint',detail=reason)
  self.layer.finish_task(gid,tid,succeeded=False,close_goal=False,result=stored)
  return self.layer.continuity_block(gid,task_id=tid,reason=reason)

 def _triage_failure(self,snap,task,result):
  """Execution diagnosis by the EXISTING lead, never an empty code review.

  review/fail means 'bounded requeue with reason'; pass cannot turn a failed
  native run into success. Auth/budget/no-verdict/unknown stay owner-owned.
  """
  import hashlib,json,re
  gid=snap['goal_id'];tid=task['task_id'];error=str(result.get('error')or'Worker execution failed')
  def park(reason):return self._park_failure(snap,task,result,reason)
  lower=error.lower()
  review=result.get('review')or{}
  if (result.get('need_human') or result.get('review_timeout') or result.get('review_no_verdict') or result.get('material_blocked') or review.get('status')not in (None,'not_requested','unsupported','not_available')):
   return park(error)
  if any(x in lower for x in ('auth','permission','credential','login','quota','resource exhausted','checkpoint','package limit','not_authorized','denied')):
   return park(error)
  if result.get('state')in ('unknown','running','starting') or result.get('busy')is True:
   return park('Execution state unknown or busy; no redispatch: '+error)
  status=self.backend._trusted_dispatch_hook.status(gid);package=status['active']
  if not package:return park('Explicit resource package required')
  resource=package['resource']
  if time.time()>=resource['checkpoint_epoch']:return park('Resource time checkpoint reached')
  for kind in ('native','lead'):
   used=sum(c['kind']==kind and c['state']!='no_spawn'for c in package['claims'])
   if used>=resource[kind+'_limit']:return park('resource exhausted: '+kind+'; original failure: '+error)
  rounds=int(task.get('rework_count')or 0);limit=int(snap['goal'].get('budget',{}).get('max_reworks',1))
  if rounds>=limit:return park('rework budget exhausted; original failure: '+error)
  decider=getattr(self.planner,'decide_action',None)
  if not callable(decider):return park('Current lead cannot diagnose execution failure: '+error)
  # Stable code + content evidence, not new run ids/mtime or a new lead reason.
  def stable(v):
   text=str(v).replace(str(result.get('workspace')or'\x00'),'<workspace>')
   text=re.sub(r'\b[0-9a-f]{8,}\b','<id>',text,flags=re.I)
   return re.sub(r'\d+','<n>',text)
  category=stable(result.get('failure_reason')or result.get('code')or error)
  evidence={k:stable(result.get(k,''))for k in ('response','stdout','stderr','native_status')}
  work=Path(self._trusted_workspace_text(task)or self.workspaces_root/gid/tid)
  files={}
  for name in task.get('expected_artifacts',[]):
   p=(work/name).resolve()
   if p.is_relative_to(work.resolve()):
    try:
     if p.is_file():files[name]=hashlib.sha256(p.read_bytes()).hexdigest()if p.stat().st_size<=2_000_000 else 'unknown:artifact_over_evidence_limit'
    except OSError:files[name]='unknown:artifact_unreadable'
  evidence['artifact_hashes']=files
  signature=hashlib.sha256(json.dumps({'category':category,'evidence':evidence},sort_keys=True).encode()).hexdigest()
  with self.layer._rmw():
   goal=self.layer.goals[gid];row=next(t for t in goal['tasks']if t['task_id']==tid)
   history=row.setdefault('execution_failure_history',[])
   replay=next((h for h in history if h['attempt']==str(task.get('run_id')or task.get('dispatch_claim')or'')and h.get('status')=='deciding'),None)
   repeated=any(h['signature']==signature for h in history)
   if not replay and not repeated:
    history.append({'attempt':str(task.get('run_id')or task.get('dispatch_claim')or''),'signature':signature,'category':category,'status':'deciding','result':copy.deepcopy(result)})
    self.layer._touch(goal);self.layer._persist_unlocked()
  if replay:return park('Prior lead diagnosis interrupted/unknown; no equivalent paid retry: '+error)
  if repeated:return park('Two same failures without new evidence; return to product owner: '+error)
  action={'kind':'review','payload':{'execution_failure':True,'failure':copy.deepcopy(result),'failure_signature':signature,'resource_package':copy.deepcopy(package)},'review_hint':'Execution diagnosis ONLY, not code acceptance. fail with a concrete adjustment requests bounded native retry; blocked requests owner. pass MUST NOT accept a failed execution. No artifactless acceptance review.'}
  try:
   choice=decider(snap,task,action)
  except Exception as exc:
   with self.layer._rmw():
    goal=self.layer.goals[gid];row=next(t for t in goal['tasks']if t['task_id']==tid)
    row['execution_failure_history'][-1].update(status='unavailable',lead_error={'code':str(getattr(exc,'code',type(exc).__name__)),'message':str(exc)})
    self.layer._touch(goal);self.layer._persist_unlocked()
   return park('Lead execution diagnosis unavailable ['+str(getattr(exc,'code',type(exc).__name__))+']: '+str(exc))
  with self.layer._rmw():
   goal=self.layer.goals[gid];row=next(t for t in goal['tasks']if t['task_id']==tid)
   row['execution_failure_history'][-1].update(status='decided',choice=copy.deepcopy(choice));self.layer._touch(goal);self.layer._persist_unlocked()
  if not isinstance(choice,dict):return park('Lead returned invalid recovery decision; no equivalent retry')
  reason=str(choice.get('reason')or'').strip()
  if choice.get('verdict')!='fail' or not reason or choice.get('incomplete') or choice.get('review_status')=='review_blocked':
   return park('Lead requests owner or returned no valid recovery decision: '+reason+'; '+error)
  feedback={'reason':reason,'source':'lead_execution_diagnosis','failure_signature':signature,'prior_result':copy.deepcopy(result)}
  queued=self.layer.requeue_for_rework(gid,tid,feedback=feedback,prior_result=result if result.get('failure_phase')!='dispatch' else task.get('result'))
  if not queued.get('ok'):return park('Lead recovery could not be queued: '+str(queued))
  return {'ok':True,'goal_id':gid,'task_id':tid,'state':'running','action':'execution_rework_queued'}

 def _fail_task(self,goal_id,task_id,result):
  gid,tid=goal_id,task_id
  snap=self.layer.get_goal(gid)['goal'];task=next(t for t in snap['tasks']if t['task_id']==tid)
  pre_dispatch=bool(task.get('dispatch_claim'))and not task.get('run_id')
  if result.get('failure_phase')!='dispatch'and not pre_dispatch:return super()._fail_task(gid,tid,result)
  result={**result,'failure_phase':'dispatch'}
  with self.layer._rmw():
   goal=self.layer.goals[gid];row=next(t for t in goal['tasks']if t['task_id']==tid)
   row['dispatch_block']=copy.deepcopy(result);self.layer._touch(goal);self.layer._persist_unlocked()
  snap=self.layer.get_goal(gid)['goal'];task=next(t for t in snap['tasks']if t['task_id']==tid)
  return self._triage_failure(snap,task,result)

 def _agy_post_run_review(self,snap,task,result):
  if isinstance(result,dict)and not result.get('ok'):
   return self._triage_failure(snap,task,result),result
  return super()._agy_post_run_review(snap,task,result)

 def _budget_stop(self,gid):
  s=self.layer.get_goal(gid)['goal'];authority=self.backend._trusted_dispatch_hook;status=authority.status(gid);p=status['active']
  if not p:return self.layer.continuity_block(gid,reason='Explicit resource package required')
  active=any(t['status']=='running'for t in s.get('tasks',[]))
  if not active and (time.time()>=p['resource']['checkpoint_epoch']):return self.layer.continuity_block(gid,reason='Resource time checkpoint reached; preserve work and request explicit continuation')
  return None
 def _process_goal(self,gid):
  s=self.layer.get_goal(gid)['goal']
  if s.get('owner_termination'):return {'ok':True,'goal_id':gid,'state':'failed','owner_termination':s['owner_termination']}
  pending=next((d for d in s.get('pending_decisions',[])if d.get('continuity')),None)
  if pending:return {'ok':True,'goal_id':gid,'state':'awaiting_owner','decision':pending}
  if not s.get('resource_packages'):return self.layer.continuity_block(gid,reason='Explicit initial package required')
  outcome=super()._process_goal(gid);s=self.layer.get_goal(gid)['goal'];tasks=s.get('tasks',[])
  failed=[t for t in tasks if t['status']=='failed'];busy=any(t['status']in ('running','awaiting_decision')for t in tasks);status={t['task_id']:t['status']for t in tasks}
  ready=any(t['status']=='queued'and all(status.get(dep)=='succeeded'for dep in t.get('depends_on',[]))for t in tasks)
  if failed and not busy and not ready:
   reason=str((failed[0].get('result')or{}).get('error')or'Ordinary task blocked')
   return self.layer.continuity_block(gid,reason=reason,task_id=failed[0]['task_id'])
  return outcome

class Handler(_Handler):
 def do_GET(self):
  if self.path.startswith('/v1/continuity/'):
   gid=self.path.split('/')[-1];s=self.server.app.layer.get_goal(gid)
   if not s.get('ok'):return self._send(404,s)
   g=s['goal'];return self._send(200,{'goal_id':gid,'state':'awaiting_owner'if any(d.get('continuity')for d in g.get('pending_decisions',[]))else g['state'],'pending_decisions':g.get('pending_decisions',[]),'history':g.get('history',[]),'tasks':g.get('tasks',[]),'resources':self.server.authority.status(gid),'models_invoked':not self.server.synthetic and bool(self.server.authority.status(gid)['actual_native']or self.server.authority.status(gid)['actual_lead']),'mode':'synthetic_test_injection'if self.server.synthetic else'real_adapters','business_acceptance':False})
  return super().do_GET()
 def do_POST(self):
  if '/continue'in self.path:return self._send(403,{'error':'owner grants only via trusted local continuation CLI'})
  if self.path!='/v1/requests':return self._send(403,{'state':'blocked','error':'only target submit is public; owner continuation uses trusted local CLI'})
  return super().do_POST()

class GoalService:
 def __init__(self,root,*,lead_bin='grok',native_bin='agy',release=False,task_permissions=False,test_factory=None,planning_timeout_sec=180.0,lead_max_turns=1,native_timeout_sec=None):
  self.native_timeout_sec=None if native_timeout_sec is None else native_timeout(native_timeout_sec)
  self.lead_max_turns=validate_lead_max_turns(lead_max_turns)
  self.planning_timeout_sec=planning_timeout(planning_timeout_sec)
  self.root=Path(root);self.app=CollabApplication(self.root);self.authority=ResourceAuthority(self.app.layer)
  if test_factory is None:
   lead=Lead(self.authority,lead_bin,planning_timeout_sec=self.planning_timeout_sec,lead_max_turns=self.lead_max_turns);backend=Native(self.authority,native_bin,task_permissions=task_permissions,native_timeout_sec=self.native_timeout_sec)
  else:lead,backend=test_factory(self.authority)
  from framework.lead_evidence import EvidenceAdapter
  lead=EvidenceAdapter(lead,self.root/'lead-evidence')
  planner=LeadAdapterPlanner(lead,cwd=self.root)
  self.app.coordinator=ContinuityCoordinator(self.app.layer,planner=planner,backend=backend,workspaces_root=self.root/'workspaces')
  backend._trusted_dispatch_hook=self.authority;backend._continuity_workspace_root=(self.root/'workspaces').resolve()
  self.server=CollabHttpServer(('127.0.0.1',0),self.app);self.server.RequestHandlerClass=Handler;self.server.authority=self.authority;self.server.released=release or test_factory is not None;self.server.synthetic=test_factory is not None;self.loop=CoordinatorLoop(self.app,interval=.05)
 def start(self):
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
  if self.server.released:self.loop.start()
  return 'http://127.0.0.1:'+str(self.server.server_port)
 def close(self):self.loop.stop();self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)
