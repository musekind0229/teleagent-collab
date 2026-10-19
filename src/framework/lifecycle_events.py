"""Durable same-goal lifecycle facts with independent delivery/consumption.
No model, route discovery, raw evidence traversal or maintenance execution here.
"""
from pathlib import Path
import copy,hashlib,json,re,time
from framework.persist_lock import persist_lock,atomic_write_json

def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def line(value):
 text=' '.join(str(value).split())[:160]
 return re.sub(r'(?i)MEDIA\s*:', 'MEDIA：',text).replace('[[','［［').replace(']]','］］')
class LifecycleOutbox:
 def __init__(self,state,*,runtime=None):
  self.state=Path(state);self.root=self.state/'lifecycle-events';self.receipts=self.state/'lifecycle-delivery';self.runtime=runtime or {};self.window=86400
 def _event(self,event_id):
  if not isinstance(event_id,str)or len(event_id)!=64 or any(x not in'0123456789abcdef'for x in event_id):raise ValueError('invalid event id')
  obj=json.loads((self.root/(event_id+'.json')).read_bytes());body={k:v for k,v in obj.items()if k!='packet_sha256'}
  if obj['event_id']!=event_id or digest(body)!=obj['packet_sha256']:raise ValueError('lifecycle packet hash mismatch')
  return obj
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
    result=t.get('result')or{};review=result.get('review')or{};usage=result.get('usage')or{}
    tasks.append({'task_id':t.get('task_id'),'run_id':t.get('run_id'),'status':t.get('status'),'backend':result.get('backend'),'attempt_count':len(t.get('attempt_history',[]))+1,'rework_count':t.get('rework_count',0),'review':{k:review[k]for k in('status','source','code','attempts')if k in review},'usage':{k:v for k,v in usage.items()if k in('input_tokens','output_tokens','total_tokens')and type(v)in(int,float)},'usage_available':bool(usage),'manifest':[{k:a[k]for k in('path','sha256','bytes')if k in a}for a in t.get('artifact_manifest',[])if isinstance(a,dict)]})
   packages=[]
   for p in snapshot.get('resource_packages',{}).values():
    summary={k:p[k]for k in('package_id','actual_native','actual_lead')if k in p}
    summary['claims']=[{k:c[k]for k in('claim_id','kind','state','pid','birth')if k in c}for c in p.get('claims',[])if isinstance(c,dict)]
    packages.append(summary)
   obj={'schema':'collab-lifecycle-v1','goal_id':gid,'event':identity,'event_id':event_id,'state':status,'title':line((snapshot.get('goal')or{}).get('title')or gid),'decision_id':identity if pending else None,'runtime':{k:self.runtime[k]for k in('head','runtime_root','branch')if k in self.runtime},'tasks':tasks,'packages':packages,'business_acceptance':False,'owner':'product_owner','maintenance_owner':'project-manager','maintenance_authorized':False}
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
  # Never turn an interrupted send into pending: its external effect is unknown.
  for path in self.root.glob('*.json'):
   with persist_lock(self.receipts):
    s=self.status(path.stem)
    for c in('qq','pm','owner'):
     if s[c]['state']=='inflight':s[c].update(state='delivery_blocked',reason='interrupted send effect unknown',responsible='product_owner',recovery='reconcile external delivery before explicit resume')
    self._save(path.stem,s)
