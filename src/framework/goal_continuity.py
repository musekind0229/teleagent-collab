"""Goal continuity policy: pure transitions for existing durable integration.
No model calls, storage migration, or automatic budget multiplication.
"""
from __future__ import annotations
import copy
from typing import Any,Mapping

class ContinuityRefused(ValueError):pass

def require_owner(snapshot:Mapping[str,Any],*,reason:str,decision_id:str)->dict[str,Any]:
    if not reason or not decision_id:raise ContinuityRefused('explicit reason/decision required')
    out=copy.deepcopy(dict(snapshot))
    if out.get('state')in ('completed','cancelled'):raise ContinuityRefused('cannot suspend a closed goal')
    pending=out.setdefault('continuity_decisions',[])
    existing=next((d for d in pending if d['decision_id']==decision_id),None)
    if existing:
        if existing['reason']!=reason:raise ContinuityRefused('decision identity conflict')
        return out
    pending.append({'decision_id':decision_id,'reason':reason,'state':'awaiting_authorized_owner','goal_id':out['goal_id']})
    out['continuity_state']='awaiting_authorized_owner'
    return out

def apply_continuation(snapshot:Mapping[str,Any],*,decision_id:str,grant:Mapping[str,Any],trusted:bool=False)->dict[str,Any]:
    if trusted is not True:raise ContinuityRefused('trusted owner authorization required')
    if grant.get('goal_id')!=snapshot.get('goal_id'):raise ContinuityRefused('cross-goal continuation')
    if not grant.get('grant_id')or not grant.get('reason'):raise ContinuityRefused('grant identity/reason required')
    out=copy.deepcopy(dict(snapshot));history=out.setdefault('continuation_grants',[])
    prior=next((x for x in history if x['grant_id']==grant['grant_id']),None)
    record={'decision_id':decision_id,**copy.deepcopy(dict(grant))}
    if prior:
        if prior!=record:raise ContinuityRefused('continuation grant conflict')
        return out
    decision=next((d for d in out.get('continuity_decisions',[])if d['decision_id']==decision_id),None)
    if not decision or decision['state']!='awaiting_authorized_owner':raise ContinuityRefused('no pending continuation decision')
    if grant.get('action')not in ('continue','terminate'):raise ContinuityRefused('explicit continue/terminate decision required')
    if grant['action']=='continue':
        resource=grant.get('resource')
        if not isinstance(resource,dict)or not resource.get('package_id'):raise ContinuityRefused('explicit package required')
        if resource['checkpoint_epoch']<=resource['original_epoch']:raise ContinuityRefused('invalid continuation window')
        # ResourceAuthority owns actual admission/claims. No mutation of historical
        # package, task failures, original created_at or rework counters here.
        out['active_resource_package']=copy.deepcopy(resource)
        out['continuity_state']='authorized_to_continue'
    else:
        out['continuity_state']='explicitly_terminated_by_owner'
    decision.update(state='resolved',grant_id=grant['grant_id']);history.append(record)
    return out
