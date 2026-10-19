"""Durable same-goal lifecycle facts with independent delivery/consumption.
No model, route discovery, raw evidence traversal or maintenance execution here.
"""
from pathlib import Path,PurePosixPath
import copy,hashlib,json,re,time,math
from framework.persist_lock import persist_lock,atomic_write_json

def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def line(value):
 text=' '.join(str(value).split())[:160]
 return re.sub(r'(?i)MEDIA\s*:', 'MEDIA：',text).replace('[[','［［').replace(']]','］］')
def task_evidence(task):
 """Only producer-backed structured fields; never open a product artifact."""
 result=task.get('result')if isinstance(task.get('result'),dict)else{}
 native=result.get('native_result')if isinstance(result.get('native_result'),dict)else{}
 receipt=result.get('session_receipt',native.get('session_receipt'))
 tid,rid=task.get('task_id'),task.get('run_id')
 review=result.get('review')if isinstance(result.get('review'),dict)else{}
 usage=result.get('usage')if isinstance(result.get('usage'),dict)else native.get('usage')
 numbers={k:v for k,v in (usage.items()if isinstance(usage,dict)else[])if k in('input_tokens','output_tokens','total_tokens')and type(v)in(int,float)and math.isfinite(v)and v>=0}
 out={'task_id':tid,'run_id':rid,'status':task.get('status'),'backend':result.get('backend',native.get('backend')),'review':{k:review[k]for k in('status','source','code','attempts')if k in review},'usage':numbers,'usage_available':bool(numbers),'manifest':[],'manifest_available':False,'execution_receipt_status':'unknown_missing','observed_run_count':None,'result_class':{k:result[k]for k in('ok','review_no_verdict','review_timeout','material_blocked')if type(result.get(k))is bool}}
 if not isinstance(receipt,dict):return out
 if receipt.get('source')!='backend_observed_process_and_files' or not tid or not rid or receipt.get('task_id')!=tid or receipt.get('run_id')!=rid:
  out['execution_receipt_status']='rejected_identity_or_source';return out
 if type(receipt.get('returncode'))is not int or type(receipt.get('fresh_session'))is not bool:
  out['execution_receipt_status']='rejected_execution_fields';return out
 outputs=receipt.get('outputs')
 if not isinstance(outputs,list)or len(outputs)>1000:
  out['execution_receipt_status']='rejected_manifest_shape_or_bound';return out
 manifest=[];seen=set()
 for row in outputs:
  if not isinstance(row,dict):out['execution_receipt_status']='rejected_manifest_row';return out
  path=row.get('relative');sha=row.get('sha256');size=row.get('bytes')
  if not isinstance(path,str)or not path or len(path)>2048 or '\\' in path or ':'in path or PurePosixPath(path).is_absolute()or '..'in PurePosixPath(path).parts or path in seen or not isinstance(sha,str)or not re.fullmatch('[0-9a-f]{64}',sha)or type(size)is not int or size<0:
   out['execution_receipt_status']='rejected_manifest_identity';return out
  seen.add(path);manifest.append({'path':path,'bytes':size,'sha256':sha})
 selected={k:receipt[k]for k in('source','task_id','run_id')}
 if type(receipt.get('fresh_session'))is bool:selected['fresh_session']=receipt['fresh_session']
 if type(receipt.get('returncode'))is int:selected['returncode']=receipt['returncode']
 out.update(session_receipt=selected,execution_receipt_status='backend_observed_bound',manifest=manifest,manifest_available=True,observed_run_count=1)
 return out

class LifecycleOutbox:
 def __init__(self,state,*,runtime=None):
  self.state=Path(state);self.root=self.state/'lifecycle-events';self.receipts=self.state/'lifecycle-delivery';self.runtime=runtime or {};self.window=86400
 def _event(self,event_id):
  if not isinstance(event_id,str)or len(event_id)!=64 or any(x not in'0123456789abcdef'for x in event_id):raise ValueError('invalid event id')
  obj=json.loads((self.root/(event_id+'.json')).read_bytes());body={k:v for k,v in obj.items()if k!='packet_sha256'}
  if obj['event_id']!=event_id or digest(body)!=obj['packet_sha256']:raise ValueError('lifecycle packet hash mismatch')
  return obj
 def arm(self,snapshots,*,authorized_goal_id,approval_reference,source_sha256,now=None):
  """Trusted local operator only; production caller holds store persist lock.
  Capture durable source identities, not event-file timestamps. Never auto-rearm.
  """
  if not isinstance(approval_reference,str)or not approval_reference.strip()or not re.fullmatch('[0-9a-f]{64}',source_sha256 or ''):raise ValueError('exact source digest and owner approval required')
  snapshots=list(snapshots)
  if not any(s.get('goal_id')==authorized_goal_id for s in snapshots):raise ValueError('authorized goal absent from source')
  baseline=[]
  for s in snapshots:
   gid=s['goal_id']
   if s.get('state')in('completed','failed','cancelled'):baseline.append([gid,s['state']])
   for field in('pending_decisions','resolved_decisions'):
    for d in s.get(field,[]):
     if isinstance(d,dict)and d.get('decision_id'):baseline.append([gid,d['decision_id']])
  path=self.state/'lifecycle-activation.json'
  with persist_lock(self.root):
   if path.exists():raise ValueError('activation already exists; no implicit baseline reset')
   data={'schema':'collab-lifecycle-activation-v1','store_root':str(self.state.resolve()),'armed':True,'authorized_goal_id':authorized_goal_id,'approval_reference':approval_reference,'activation_epoch':time.time()if now is None else now,'source_sha256':source_sha256,'excluded_source_identities':baseline,'excluded_event_ids':[p.stem for p in self.root.glob('*.json')]}
   data['activation_id']=digest(data);atomic_write_json(path,data)
  return data
 def _activation(self):
  path=self.state/'lifecycle-activation.json'
  if not path.exists():return None
  value=json.loads(path.read_bytes())
  if not isinstance(value,dict):raise ValueError('activation record must be object')
  body={k:v for k,v in value.items()if k!='activation_id'}
  if digest(body)!=value.get('activation_id')or value.get('armed')is not True or value.get('store_root')!=str(self.state.resolve()):raise ValueError('invalid activation record')
  return value
 def _qualification(self,gid,identity,event_id):
  a=self._activation()
  if not a:return {'eligible':False,'reason':'not_armed'}
  if self.runtime.get('execution_class')!='real':return {'eligible':False,'reason':'synthetic_or_unknown_runtime'}
  if gid!=a['authorized_goal_id']:return {'eligible':False,'reason':'goal_not_authorized'}
  if [gid,identity]in a['excluded_source_identities']or event_id in a['excluded_event_ids']:return {'eligible':False,'reason':'preactivation_source_or_backlog'}
  return {'eligible':True,'activation_id':a['activation_id'],'execution_class':'real','source_identity':[gid,identity]}
 def publish(self,snapshot,*,host_exited=False):
  gid=snapshot['goal_id'];pending=[d for d in snapshot.get('pending_decisions',[])if d.get('continuity')]
  status='awaiting_owner'if pending else snapshot.get('state')
  if status not in('completed','failed','cancelled','awaiting_owner'):
   if host_exited:atomic_write_json(self.state/'lifecycle-health.json',{'goal_id':gid,'state':'unknown','owner':'product_owner','reason':'host exited without durable goal outcome','business_acceptance':False})
   return None
  identity=pending[0]['decision_id']if status=='awaiting_owner'else status
  event_id=hashlib.sha256(json.dumps([gid,identity]).encode()).hexdigest()
  with persist_lock(self.root):
   path=self.root/(event_id+'.json')
   if path.exists():
    obj=self._event(event_id)
    self._prepare_delivery(obj)
    return obj
   tasks=[]
   for t in snapshot.get('tasks',[]):
    selected=task_evidence(t)
    history=t.get('attempt_history')if isinstance(t.get('attempt_history'),list)else[]
    selected['prior_attempts']=[task_evidence(a)for a in history[-100:]if isinstance(a,dict)]
    selected['history_truncated']=len(history)>100
    selected['retained_attempt_records']=len(history)
    selected['rework_count']=t.get('rework_count',0)
    tasks.append(selected)
   packages=[]
   for p in snapshot.get('resource_packages',{}).values():
    summary={k:p[k]for k in('package_id','actual_native','actual_lead')if k in p}
    summary['claims']=[{k:c[k]for k in('claim_id','kind','state','pid','birth')if k in c}for c in p.get('claims',[])if isinstance(c,dict)]
    packages.append(summary)
   obj={'schema':'collab-lifecycle-v1','goal_id':gid,'event':identity,'event_id':event_id,'state':status,'title':line((snapshot.get('goal')or{}).get('title')or gid),'decision_id':identity if pending else None,'runtime':{k:self.runtime[k]for k in('head','runtime_root','branch','execution_class')if k in self.runtime},'tasks':tasks,'packages':packages,'business_acceptance':False,'owner':'product_owner','maintenance_owner':'project-manager','maintenance_authorized':False}
   obj['qualification']=self._qualification(gid,identity,event_id)
   obj['packet_sha256']=digest(obj);atomic_write_json(path,obj)
  self._prepare_delivery(obj)
  return obj
 def _prepare_delivery(self,obj):
  event_id=obj['event_id']
  with persist_lock(self.receipts):
   if not(self.receipts/(event_id+'.json')).exists():atomic_write_json(self.receipts/(event_id+'.json'),{'event_id':event_id,'packet_sha256':obj['packet_sha256'],'qq':{'state':'pending','attempts':[]},'pm':{'state':'pending','attempts':[]},'owner':{'state':'not_ready','attempts':[]},'maintenance_authorized':False})
 def status(self,event_id):
  event=self._event(event_id)
  row=json.loads((self.receipts/(event_id+'.json')).read_bytes())
  if row.get('event_id')!=event_id or row.get('packet_sha256')!=event['packet_sha256']:raise ValueError('delivery identity mismatch')
  return row
 def _save(self,event_id,row):atomic_write_json(self.receipts/(event_id+'.json'),row)
 def deliver(self,event_id,sender,*,now=None,channels=('qq','pm','owner')):
  now=time.time()if now is None else now;e=self._event(event_id)
  a=self._activation();q=e.get('qualification')or{}
  if not a or q.get('eligible')is not True or q.get('activation_id')!=a['activation_id']or e['goal_id']!=a['authorized_goal_id']:
   with persist_lock(self.receipts):
    s=self.status(event_id);s['qualification_blocked']={'reason':q.get('reason','missing_or_changed_activation'),'responsible':'product_owner','recovery':'do not authorize historical/synthetic packet by replay or route edits'};self._save(event_id,s)
   return
  # Independent bounded mechanical delivery. Sender is injected by the existing
  # host or approved operator drain, never a model-generated command.
  for channel in channels:
   if channel not in ('qq','pm','owner'):raise ValueError('unknown lifecycle consumer')
   with persist_lock(self.receipts):
    s=self.status(event_id);c=s[channel]
    if c['state']in('sent','consumed','not_ready','delivery_blocked','inflight'):continue
    if now-c.get('first_at',now)>self.window:
     c.update(state='delivery_blocked',reason='compensation window expired',responsible='product_owner',recovery='owner inspects receipt and explicitly resumes same event');self._save(event_id,s);continue
    if c['state']=='queued':continue
    if now<c.get('next_at',0):continue
    c.setdefault('first_at',now);c.update(state='inflight');self._save(event_id,s)
   if channel=='qq':payload='目标：'+e['title']+'\n结果：'+e['state']+'（未作用户验收）\n下一步：'+('上层决定；负责人product_owner'if e['state']=='awaiting_owner'else'查看产物与结论；负责人product_owner')
   else:payload={'protocol_document':str(Path(__file__).resolve().parents[2]/'docs/lifecycle-delivery.md'),'entrypoint':str(Path(__file__).resolve().parents[2]/'bin/goal-entry.py'),'state':str(self.state.resolve()),'event_id':event_id,'goal_id':e['goal_id'],'report':str((self.receipts/(event_id+'.report.json')).resolve())if channel=='owner'else None,'packet':str((self.root/(event_id+'.json')).resolve()),'packet_sha256':e['packet_sha256'],'consumer':channel,'report_sha256':s.get('report_sha256'),'rule':'Read and hash-check local allowlisted engineering packet; explicit consume/report receipts required; do not implement unapproved repairs.'}
   try:result=sender(channel,payload)
   except Exception:result={'status':'unknown_delivery','reason':'sender exception; remote effect unknown'}
   with persist_lock(self.receipts):
    s=self.status(event_id);c=s[channel];status=result.get('status','unknown_delivery')
    c['attempts'].append({'at':now,'status':status,'delivery_id':result.get('delivery_id')})
    if c['state']=='consumed':self._save(event_id,s);continue
    if status=='sent':c.update(state='sent',delivery_id=result.get('delivery_id'))
    elif status in('queued','settled'):c.update(state='queued',delivery_id=result.get('delivery_id'),responsible=channel,recovery='explicit actual read receipt, not transport status')
    elif status=='unknown_delivery':c.update(state='delivery_blocked',responsible='product_owner',reason=status,recovery='reconcile possible remote send before resuming; never blindly duplicate')
    else:
     same=c.get('last_status')==status;c['same_rejections']=c.get('same_rejections',0)+1 if same else 1;c['last_status']=status
     c.update(state='delivery_blocked'if c['same_rejections']>=2 else'pending',reason=status,next_at=now+min(1800,30*2**min(len(c['attempts'])-1,6)),responsible='product_owner',recovery='new authoritative channel state/route evidence required after repeated same rejection')
    self._save(event_id,s)
 def resume(self,event_id,channel,*,evidence,now=None):
  if channel not in('qq','pm','owner')or not isinstance(evidence,str)or not evidence.strip():raise ValueError('actual new evidence required')
  with persist_lock(self.receipts):
   s=self.status(event_id);c=s[channel]
   if c['state']!='delivery_blocked':raise ValueError('only explicitly blocked event can resume')
   c.setdefault('recovery_evidence',[]).append({'at':time.time()if now is None else now,'evidence':evidence});c.update(state='pending',same_rejections=0,next_at=time.time()if now is None else now);self._save(event_id,s)
 def consume(self,event_id,channel,*,packet_sha256,receipt_id):
  if channel not in('pm','owner')or not receipt_id:raise ValueError('explicit consumer receipt required')
  with persist_lock(self.receipts):
   s=self.status(event_id);expected=s.get('report_sha256')if channel=='owner'else s['packet_sha256']
   if not expected or packet_sha256!=expected:raise ValueError('consumer hash mismatch')
   if channel=='owner':
    report=json.loads((self.receipts/(event_id+'.report.json')).read_bytes())
    if digest(report)!=expected or report.get('event_id')!=event_id:raise ValueError('report bytes/identity changed')
   old=s[channel].get('receipt_id')
   if old and old!=receipt_id:raise ValueError('conflicting receipt')
   s[channel].update(state='consumed',receipt_id=receipt_id,consumed_sha256=packet_sha256);self._save(event_id,s)
 def report(self,event_id,*,packet_sha256,report,receipt_id):
  if not isinstance(report,dict)or report.get('conclusion')not in('healthy_no_change','proposed_change')or not report.get('evidence')or not receipt_id:raise ValueError('substantive bounded report required')
  allowed={k:report[k]for k in('conclusion','evidence','proposal','alternatives','scope','resources','validation','unknowns')if k in report}
  with persist_lock(self.receipts):
   s=self.status(event_id)
   if packet_sha256!=s['packet_sha256']or s['pm']['state']!='consumed':raise ValueError('PM must actually consume exact event before report')
   body={'event_id':event_id,'source_sha256':packet_sha256,'report':allowed,'receipt_id':receipt_id,'maintenance_authorized':False};sha=digest(body)
   if s.get('report_sha256'):
    if s['report_sha256']!=sha:raise ValueError('immutable report conflict')
    return
   atomic_write_json(self.receipts/(event_id+'.report.json'),body);s.update(report_sha256=sha,maintenance_authorized=False);s['owner'].update(state='pending');self._save(event_id,s)
 def recover_interrupted(self):
  # Validate immutable fact first, repair ONLY absent metadata, then classify
  # prior inflight effects. Corrupt facts/rows remain untouched for the owner.
  for path in self.root.glob('*.json'):
   try:
    event=self._event(path.stem)
    self._prepare_delivery(event)
    with persist_lock(self.receipts):
     s=self.status(path.stem)
     for c in('qq','pm','owner'):
      if s[c]['state']=='inflight':s[c].update(state='delivery_blocked',reason='interrupted send effect unknown',responsible='product_owner',recovery='reconcile external delivery before explicit resume')
     self._save(path.stem,s)
   except (OSError,ValueError,KeyError,TypeError)as exc:
    self.health(path.stem,'recovery: '+type(exc).__name__)
 def health(self,event_id,reason):
  root=self.state/'lifecycle-recovery-blocked'
  safe=event_id if re.fullmatch('[0-9a-f]{64}',event_id or '')else'unknown'
  atomic_write_json(root/(safe+'.json'),{'event_id':event_id,'state':'delivery_blocked','responsible':'product_owner','reason':reason,'recovery':'inspect corrupt/missing side evidence without overwriting or resending','business_acceptance':False})
 def reconcile(self,snapshots):
  self.recover_interrupted()
  for snapshot in snapshots:
   try:self.publish(snapshot)
   except(OSError,ValueError,KeyError,TypeError)as exc:self.health('unknown','reconcile: '+type(exc).__name__)
