"""Single-store durable owner continuation and actual resource accounting."""
import copy,time,uuid,math
from typing import Any
class ContinuityStore:
 goals: dict[str,Any]
 _rmw: Any
 _append_history: Any
 _touch: Any
 _persist_unlocked: Any
 def continuity_block(self,gid,*,reason,task_id=''):
  with self._rmw():
   s=self.goals[gid]
   if reason=='Explicit initial package required' and s.get('resource_packages'):return {'ok':True,'goal_id':gid,'state':s['state'],'action':'initial_grant_race_resolved'}
   if task_id and not any(t['task_id']==task_id and t['status']=='failed'for t in s.get('tasks',[])):return {'ok':True,'goal_id':gid,'state':s['state'],'action':'stale_block_ignored'}
   prior=next((d for d in s.get('pending_decisions',[])if d.get('continuity')),None)
   if prior:return {'ok':True,'goal_id':gid,'decision':dict(prior),'duplicate':True}
   category='checkpoint'
   lower=str(reason).lower()
   if any(word in lower for word in ('resource exhausted','checkpoint reached','time checkpoint','package limit','plan_infeasible','quota','budget exhausted')):category='escalate_over_budget'
   elif any(word in lower for word in ('permission','authorization','authentication','credential','login','not_authorized')):category='escalate_insufficient_auth'
   d={'decision_id':'continuity_'+uuid.uuid4().hex,'kind':category,'task_id':task_id,'reason':reason,'return_to_upper':True,'continuity':True,'status':'pending'}
   previous_state=s.get('state');s['state']='blocked';s['continuity_state']='awaiting_authorized_owner'
   s.setdefault('pending_decisions',[]).append(d);self._append_history(s,'continuity_block',prior_goal_state=previous_state,**d);self._touch(s);self._persist_unlocked();return {'ok':True,'goal_id':gid,'state':'awaiting_owner','decision':dict(d)}
 def continuity_authorize(self,gid,*,decision_id,grant,trusted=False):
  if trusted is not True or grant.get('goal_id')!=gid or not grant.get('grant_id')or not grant.get('reason'):raise ValueError('trusted exact owner grant required')
  grant=copy.deepcopy(grant)
  if grant.get('decision_id',decision_id)!=decision_id:raise ValueError('cross-decision grant')
  grant['decision_id']=decision_id
  r=grant.get('resource',{})
  if any(type(r.get(k))is not int or r[k]<0 for k in ('native_limit','lead_limit')):raise ValueError('invalid limits')
  if any(type(r.get(k))not in(int,float)or not math.isfinite(r[k])for k in ('original_epoch','checkpoint_epoch')):raise ValueError('invalid epoch')
  if not isinstance(r.get('package_id'),str)or not r['package_id'] or not isinstance(grant['grant_id'],str)or not isinstance(grant['reason'],str):raise ValueError('explicit package/grant/reason strings required')
  if r['checkpoint_epoch']<=max(time.time(),r['original_epoch'])or not r.get('package_id'):raise ValueError('expired package')
  with self._rmw():
   s=self.goals[gid];grants=s.setdefault('continuation_grants',[]);prior=next((x for x in grants if x['grant_id']==grant['grant_id']),None)
   if prior:
    if prior!=grant:raise ValueError('grant identity conflict')
    return {'ok':True,'goal_id':gid,'duplicate':True}
   packages=s.setdefault('resource_packages',{})
   if r['package_id']in packages:raise ValueError('package identity reused')
   if decision_id:
    d=next((d for d in s.get('pending_decisions',[])if d['decision_id']==decision_id and d.get('continuity')),None)
    if not d:raise ValueError('no pending owner decision')
    if any(t['status']=='running'for t in s.get('tasks',[])):raise ValueError('active worker cannot change package')
    s['pending_decisions']=[x for x in s['pending_decisions']if x['decision_id']!=decision_id];s.setdefault('resolved_decisions',[]).append({**d,'verdict':'continue','grant_id':grant['grant_id']})
    t=next((t for t in s.get('tasks',[])if t['task_id']==d.get('task_id')),None)
    if t and t['status']=='failed':
     t.setdefault('attempt_history',[]).append(copy.deepcopy({k:v for k,v in t.items()if k!='attempt_history'}))
     # Only this exact trusted decision task receives owner input. Existing
     # instruction_with_feedback renders reason verbatim into the final prompt.
     t.setdefault('rework_feedback',[]).append({'reason':d['reason']+'\n[owner_feedback]\n'+grant['reason']+'\n[/owner_feedback]','checkpoint_reason':d['reason'],'owner_feedback':grant['reason'],'source':'explicit_continuation','prior_result':copy.deepcopy(t.get('result'))})
     t.update(status='queued',result=None)
     for k in ('run_id','native_handle','dispatch_claim','progress','run_started_at'):t.pop(k,None)
   elif packages:raise ValueError('increment requires pending owner decision')
   packages[r['package_id']]={'resource':copy.deepcopy(r),'claims':[],'actual_native':0,'actual_lead':0};s['active_resource_package']=r['package_id'];grants.append(copy.deepcopy(grant))
   budget=s['goal'].setdefault('budget',{});s['continuity_state']='authorized_to_continue';budget['wall_sec']=r['checkpoint_epoch']-s['created_at'];budget['max_reworks']=max((int(t.get('rework_count')or 0)for t in s.get('tasks',[])),default=0)+r['native_limit'];s['state']='running'if s.get('tasks')else'queued'
   self._append_history(s,'explicit_continuation',grant_id=grant['grant_id'],decision_id=decision_id,resource=r,reason=grant['reason']);self._touch(s);self._persist_unlocked();return {'ok':True,'goal_id':gid,'package_id':r['package_id']}
 def continuity_end(self,gid,*,decision_id,reason,alternative,trusted=False):
  if trusted is not True or not reason or not alternative:raise ValueError('trusted value/cost decision and alternative required')
  with self._rmw():
   s=self.goals[gid];d=next((x for x in s.get('pending_decisions',[])if x['decision_id']==decision_id and x.get('continuity')),None)
   if not d:raise ValueError('no pending owner decision')
   if any(t['status']=='running'for t in s.get('tasks',[])):raise ValueError('cannot end while worker active')
   s['pending_decisions']=[x for x in s['pending_decisions']if x['decision_id']!=decision_id];s.setdefault('resolved_decisions',[]).append({**d,'verdict':'terminate','reason':reason,'alternative':alternative});s['state']='failed';s['continuity_state']='explicitly_terminated_by_owner';s['owner_termination']={'reason':reason,'alternative':alternative}
   self._append_history(s,'explicit_owner_termination',decision_id=decision_id,reason=reason,alternative=alternative);self._touch(s);self._persist_unlocked();return {'ok':True,'goal_id':gid,'state':'failed','owner_termination':s['owner_termination']}
 def continuity_claim(self,gid,*,kind,binding):
  if kind not in ('native','lead'):raise ValueError('invalid kind')
  with self._rmw():
   s=self.goals[gid]
   if s.get('state')in('completed','failed','cancelled'):raise ValueError('closed goal cannot dispatch')
   if any(d.get('continuity')for d in s.get('pending_decisions',[])):raise ValueError('awaiting owner')
   p=s.get('resource_packages',{}).get(s.get('active_resource_package'))
   if not p:raise ValueError('HOLD: no explicit resource package')
   r=p['resource']
   if time.time()>=r['checkpoint_epoch']:raise ValueError('checkpoint reached')
   if sum(c['kind']==kind and c['state']!='no_spawn'for c in p['claims'])>=r[kind+'_limit']:raise ValueError('resource exhausted')
   key=uuid.uuid4().hex;p['claims'].append({'claim':key,'kind':kind,'binding':binding,'state':'reserved'});self._persist_unlocked();return key
 def continuity_receipt(self,gid,claim,*,pid=None,birth=None):
  import psutil
  if pid is not None:
   if type(pid)is not int or not isinstance(birth,(int,float)):raise ValueError('PID identity fields invalid')
   if abs(psutil.Process(pid).create_time()-birth)>.01:raise ValueError('PID identity mismatch')
  with self._rmw():
   s=self.goals[gid];p,c=next((p,c)for p in s['resource_packages'].values()for c in p['claims']if c['claim']==claim)
   if c['state']!='reserved':raise ValueError('receipt replay')
   c['state']='spawned'if pid is not None else'no_spawn'
   if pid is not None:c.update(pid=pid,birth=birth);p['actual_'+c['kind']]+=1
   self._persist_unlocked()
