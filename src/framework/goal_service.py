"""Same-store continuity host; recording adapters are TEST injection only."""
import threading,time,copy,math

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
 def __init__(self,authority,bin_path,*,task_permissions=False):
  super().__init__(bin_path=bin_path);self._trusted_dispatch_hook=authority
  if task_permissions:
   self.configure_caller_permissions(allowed_operations=['read','write','command'],native_mode='skip_permissions');env=dict(self._base_env());env['AGY_AUTO_APPROVE']='1';self._base_environ=env;self._environ=dict(env)
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
 def __init__(self,root,*,lead_bin='grok',native_bin='agy',release=False,task_permissions=False,test_factory=None,planning_timeout_sec=180.0,lead_max_turns=1):
  self.lead_max_turns=validate_lead_max_turns(lead_max_turns)
  self.planning_timeout_sec=planning_timeout(planning_timeout_sec)
  self.root=Path(root);self.app=CollabApplication(self.root);self.authority=ResourceAuthority(self.app.layer)
  if test_factory is None:
   lead=Lead(self.authority,lead_bin,planning_timeout_sec=self.planning_timeout_sec,lead_max_turns=self.lead_max_turns);backend=Native(self.authority,native_bin,task_permissions=task_permissions)
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
