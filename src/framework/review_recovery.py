"""Explicit owner-authorized review of an exact already-collected native run.
No model calls here. Authorization and coordinator intent use the existing store.
"""
import copy,hashlib,json
from pathlib import Path
from framework import agy_review

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def _task(snapshot,task_id):
 task=next((t for t in snapshot.get('tasks',[])if t['task_id']==task_id),None)
 if not task:raise ValueError('unknown exact review task')
 return task

def _workspace_manifest(layer,gid,task):
 root=Path(layer.persist_root)/'workspaces'/gid/task['task_id']
 if not root.is_dir()or root.is_symlink():raise ValueError('assigned review workspace absent or symlink')
 for path in [*root.parents,root,*root.rglob('*')]:
  if path.exists()and(path.is_symlink()or getattr(path.lstat(),'st_file_attributes',0)&0x400):raise ValueError('review workspace reparse refused')
 if Path(task.get('workspace','')).resolve()!=root.resolve():raise ValueError('review workspace binding mismatch')
 # scan_workspace marks incomplete/oversized/binary unsupported bytes instead of guessing.
 scan=agy_review.scan_workspace(root)
 if '\x00scan_incomplete'in scan:raise ValueError('review workspace scan incomplete')
 for row in scan.values():
  if 'raw_hex'not in row:raise ValueError('review snapshot bytes incomplete')
 manifest={p:{'sha256':r['sha256'],'bytes':r['size']}for p,r in sorted(scan.items())}
 for name in task.get('expected_artifacts',[]):
  if name not in manifest:raise ValueError('expected review artifact missing: '+name)
 return manifest

def review_binding(layer,gid,task_id,decision_id):
 snapshot=layer.get_goal(gid)['goal'];task=_task(snapshot,task_id)
 decision=next((d for d in snapshot.get('pending_decisions',[])if d.get('continuity')and d['decision_id']==decision_id and d.get('task_id')==task_id),None)
 if not decision:raise ValueError('exact pending continuity review decision required')
 result=task.get('result')or{};native=result.get('native_result')
 if task.get('status')!='failed'or not(result.get('review_timeout')or result.get('review_no_verdict'))or not isinstance(native,dict)or not native.get('ok'):raise ValueError('only retained native success with owner-stopped no-verdict is review-recoverable')
 rid=str(task.get('run_id')or'')
 if not rid or str(native.get('run_id')or'')!=rid:raise ValueError('native run identity mismatch')
 if any(t.get('status')in('running','review','awaiting_decision','cancel_requested')for t in snapshot['tasks']):raise ValueError('in-flight task prevents review recovery')
 if any(c.get('state')=='reserved'for p in snapshot.get('resource_packages',{}).values()for c in p.get('claims',[])):raise ValueError('unsettled resource reservation prevents review recovery')
 manifest=_workspace_manifest(layer,gid,task)
 baseline=agy_review.load_baseline(Path(layer.persist_root)/'workspaces',gid,task_id)
 if baseline is None:raise ValueError('original review baseline absent')
 return {'store_root':str(Path(layer.persist_root).resolve()),'goal_id':gid,'task_id':task_id,'decision_id':decision_id,'run_id':rid,'native_sha256':digest(native),'result_sha256':digest(result),'baseline_sha256':digest(baseline),'contract_sha256':digest({'goal':{k:v for k,v in snapshot['goal'].items()if k!='budget'},'task_inputs':task.get('inputs'),'done_when':task.get('done_when'),'expected_artifacts':task.get('expected_artifacts')}),'workspace_sha256':digest(manifest),'artifacts':manifest}

def validate_review_grant(layer,gid,decision_id,grant):
 supplied=grant.get('review_only')
 if not isinstance(supplied,dict):raise ValueError('exact review-only binding required')
 actual=review_binding(layer,gid,supplied.get('task_id'),decision_id)
 if supplied!=actual:raise ValueError('review-only evidence/decision binding mismatch')
 if grant.get('resource',{}).get('lead_limit',0)<1:raise ValueError('review-only grant requires new lead budget')
 return actual

def validate_authorized_snapshot(layer,gid,task,record):
 binding=record['binding'];result=task.get('result')or{}
 snapshot=layer.get_goal(gid)['goal']
 contract={'goal':{k:v for k,v in snapshot['goal'].items()if k!='budget'},'task_inputs':task.get('inputs'),'done_when':task.get('done_when'),'expected_artifacts':task.get('expected_artifacts')}
 if digest(contract)!=binding['contract_sha256']:raise ValueError('review acceptance/scope contract changed after grant')
 if task.get('run_id')!=binding['run_id']or digest(result)!=binding['result_sha256']or digest(result.get('native_result'))!=binding['native_sha256']:raise ValueError('retained native/result evidence changed after grant')
 if digest(_workspace_manifest(layer,gid,task))!=binding['workspace_sha256']:raise ValueError('artifact bytes changed after review grant')
 baseline=agy_review.load_baseline(Path(layer.persist_root)/'workspaces',gid,task['task_id'])
 if digest(baseline)!=binding['baseline_sha256']:raise ValueError('review baseline changed after grant')
 return copy.deepcopy(result['native_result'])

def process_review(coordinator,gid,task):
 """One persisted intent => at most one new lead review, never native collect."""
 layer=coordinator.layer;record=task['review_recovery'];key=record['grant_id']
 if record['status']=='inflight':
  layer.finish_task(gid,task['task_id'],succeeded=False,close_goal=False,result=task['result'])
  return layer.continuity_block(gid,task_id=task['task_id'],reason='Review recovery interrupted/unknown; explicit owner decision required, no automatic lead retry')
 try:native=validate_authorized_snapshot(layer,gid,task,record)
 except ValueError as exc:
  with layer._rmw():
   snapshot=layer.goals[gid];row=_task(snapshot,task['task_id']);row['review_recovery']['status']='rejected';layer._touch(snapshot);layer._persist_unlocked()
  return layer.continuity_block(gid,task_id=task['task_id'],reason=str(exc))
 # Recheck package admission BEFORE changing task state or invoking a lead.
 package=coordinator.backend._trusted_dispatch_hook.status(gid)['active']
 import time
 if not package or time.time()>=package['resource']['checkpoint_epoch']or sum(c['kind']=='lead'and c['state']!='no_spawn'for c in package['claims'])>=package['resource']['lead_limit']:
  return layer.continuity_block(gid,task_id=task['task_id'],reason='Review recovery resource exhausted or checkpoint reached')
 with layer._rmw():
  snapshot=layer.goals[gid];row=_task(snapshot,task['task_id'])
  if row['review_recovery']['status']!='authorized':return {'ok':True,'action':'review_recovery_already_claimed'}
  # Original failure is immutable history, not overwritten by new review outcome.
  row.setdefault('attempt_history',[]).append(copy.deepcopy({k:v for k,v in row.items()if k!='attempt_history'}))
  row['review_recovery']['status']='inflight';row['status']='review';layer._touch(snapshot);layer._persist_unlocked()
 snap=layer.get_goal(gid)['goal'];current=_task(snap,task['task_id'])
 try:
  # Use the existing full material/scope/lead/rework gate. Do not collect again,
  # replay native, or run the old timeout duplicate fence against this new grant.
  requeued,result=coordinator._agy_post_run_review(snap,current,native)
  if requeued is None:
   if digest(_workspace_manifest(layer,gid,current))!=record['binding']['workspace_sha256']:
    result=copy.deepcopy(task['result']);result.update(ok=False,error='Artifact changed during owner review; verdict not applied')
    layer.finish_task(gid,task['task_id'],succeeded=False,close_goal=False,result=result)
    requeued=layer.continuity_block(gid,task_id=task['task_id'],reason=result['error'])
   else:
    layer.finish_task(gid,task['task_id'],succeeded=bool(result.get('ok')),close_goal=False,result=result)
    layer.reconcile_goal_progress(gid)
  outcome=requeued or {'ok':True,'goal_id':gid,'action':'review_recovery_finished'}
 except Exception as exc:
  # Unknown lead execution is consumed, never automatically paid again.
  layer.finish_task(gid,task['task_id'],succeeded=False,close_goal=False,result=task['result'])
  outcome=layer.continuity_block(gid,task_id=task['task_id'],reason='Review recovery exception; owner must investigate: '+type(exc).__name__)
 with layer._rmw():
  snapshot=layer.goals[gid];row=_task(snapshot,task['task_id']);row['review_recovery'].update(status='settled',outcome=copy.deepcopy(outcome));layer._touch(snapshot);layer._persist_unlocked()
 return outcome


def backup_existing_state(state,*,expected_store_sha256,backup,expected_birth):
 """Called only under the existing host-owner lock, BEFORE settled restart.
 Writes a NEW byte-preserving backup, never touches an existing backup.
 """
 import shutil
 from framework.host_restart import validate_settled
 state=Path(state).resolve()
 if not expected_store_sha256 or not backup:raise ValueError('external same-store restart requires exact store digest and new backup path')
 target=Path(backup).absolute()
 if target.exists()or target.resolve().is_relative_to(state)or state.is_relative_to(target.resolve()):raise ValueError('backup must be new and disjoint from state')
 for p in [*target.parents,target]:
  if p.exists()and(p.is_symlink()or getattr(p.lstat(),'st_file_attributes',0)&0x400):raise ValueError('backup reparse refused')
 host,raw=validate_settled(state)
 if host['birth']!=expected_birth or hashlib.sha256(raw).hexdigest()!=expected_store_sha256:raise ValueError('settled host/store binding mismatch')
 for p in state.rglob('*'):
  if p.is_symlink()or getattr(p.lstat(),'st_file_attributes',0)&0x400:raise ValueError('state backup reparse refused')
 shutil.copytree(state,target,ignore=shutil.ignore_patterns('host-owner.lock','.collab-persist.lock'))
 if (target/'.collab-durable/store.json').read_bytes()!=raw:raise ValueError('state backup verification failed')
 if (state/'.collab-durable/store.json').read_bytes()!=raw:raise ValueError('same-store changed during backup')
 (target/'REVIEW-RECOVERY-BACKUP.json').write_text(json.dumps({'store_sha256':expected_store_sha256,'old_host':host,'source':str(state),'rollback':'stop candidate and verify no active runs/writers, then restore this complete state snapshot; never merge store rows','models_released':False},indent=2),encoding='utf-8')
 return {'backup':str(target),'store_sha256':expected_store_sha256}
