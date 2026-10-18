"""Pure projections: package allowance is not cumulative goal scheduling time."""
import copy,time

def project_plan(request,snapshot,*,now=None):
 req=copy.deepcopy(request)
 if req.get('kind')!='plan':return req
 packages=snapshot.get('resource_packages',{});p=packages.get(snapshot.get('active_resource_package'))
 if not p:return req
 now=time.time()if now is None else now;r=p['resource'];claims=p.get('claims',[])
 remaining={kind:max(0,r[kind+'_limit']-sum(c['kind']==kind and c['state']!='no_spawn'for c in claims))for kind in ('native','lead')}
 # Called before this planning process reserves a lead: one call is still due.
 cap=min(int(req.get('max_tasks',8)),remaining['native'],max(0,remaining['lead']-1))
 req['max_tasks']=cap
 extra=req.setdefault('extra',{})
 extra['planning_contract']={'phase':'planning_only','tools':'none; no workspace scan or execution','review':'runtime invokes a separate lead review after each native task; do not add a task merely to obtain lead review','failure_history':'review_blocked on a planning call means incomplete planning transport/output, not a rejected business implementation; no implementation or review exists when task_count is zero','accounting':{'remaining_native_starts':remaining['native'],'remaining_lead_calls_before_this_plan':remaining['lead'],'this_plan_lead_calls':1,'minimum_native_starts_per_task':1,'minimum_lead_reviews_per_task':1,'max_feasible_tasks':cap,'rework':'each native rework and lead re-review consumes additional allowance; limits are ceilings, not targets'},'output':'return concise task instructions and dependency/artifact choices, not implementation file contents; choose your own useful split within the cap'}
 cumulative=req.setdefault('budget',{}).get('wall_sec')
 extra['time_budget_semantics']={'goal_schedule_offset_sec':cumulative,'goal_schedule_offset_is_not_new_allowance':True,'package_id':r['package_id'],'package_original_epoch':r['original_epoch'],'package_checkpoint_epoch':r['checkpoint_epoch'],'package_window_sec':r['checkpoint_epoch']-r['original_epoch'],'remaining_package_sec':max(0,r['checkpoint_epoch']-now),'checkpoint_not_goal_termination':True,'authority':'only package reserve authorizes dispatch; no budget projection extends its epoch'}
 req['budget']['wall_sec']=extra['time_budget_semantics']['remaining_package_sec']
 return req
