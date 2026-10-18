"""Single direct entry: same store, explicit owner continuation, no successor copy."""
from pathlib import Path
import sys,argparse,json,time,os,urllib.request,urllib.error,hashlib
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from framework.durable_api import DurableLayer
from framework.goal_service import GoalService,planning_timeout
from lead_adapter.grok_cli import lead_max_turns

def save(p,x):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(x,ensure_ascii=False));os.replace(tmp,p)
def call(endpoint,method,path,body=None):
 from urllib.parse import urlsplit
 parsed=urlsplit(endpoint)
 if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.username or parsed.password or parsed.path not in ('','/')or parsed.query or parsed.fragment:raise ValueError('only exact loopback service endpoint is allowed')
 class NoRedirect(urllib.request.HTTPRedirectHandler):
  def redirect_request(self,*args,**kwargs):raise ValueError('service redirects refused')
 req=urllib.request.Request(endpoint.rstrip('/')+path,data=json.dumps(body).encode()if body is not None else None,method=method,headers={'Content-Type':'application/json'});opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
 try:
  with opener.open(req,timeout=10)as r:return json.loads(r.read())
 except urllib.error.HTTPError as exc:raise ValueError('HTTP '+str(exc.code)+': '+exc.read(4000).decode())from exc
def wait_for_owner(state,endpoint,goal_id,*,timeout_sec=300,poll_sec=.2):
 """Bounded observer only: never plans, grants, dispatches, or marks received.

 Run under the PRODUCT OWNER's existing tracked terminal completion channel.
 A busy/unknown/observation timeout is not permission to resubmit native.
 """
 import math
 from framework.persist_lock import persist_lock,atomic_write_json
 if not math.isfinite(timeout_sec)or timeout_sec<=0 or not math.isfinite(poll_sec)or poll_sec<=0:raise ValueError('positive finite observation window required')
 def durable_terminal():
  # An exited host is not a goal outcome. Use ONLY an existing same-store
  # terminal/owner record; a still-running or unreadable store stays unknown.
  if not(Path(state)/'.collab-durable/store.json').is_file():return None
  layer=DurableLayer.open(state,use_cache=False);got=layer.get_goal(goal_id)
  if not got.get('ok'):return None
  goal=got['goal'];pending=goal.get('pending_decisions',[])
  status='awaiting_owner'if any(d.get('continuity')for d in pending)else goal['state']
  if status not in ('completed','failed','cancelled','awaiting_owner'):return None
  return {'goal_id':goal_id,'state':status,'pending_decisions':pending,'tasks':goal.get('tasks',[]),'history':goal.get('history',[]),'source':'existing_same_store','service_reachable':False,'business_acceptance':False},layer.get_report(goal_id)
 deadline=time.monotonic()+timeout_sec
 while True:
  fallback_report=None
  try:result=call(endpoint,'GET','/v1/continuity/'+goal_id)
  except Exception as exc:
   try:fallback=durable_terminal()
   except Exception:fallback=None
   if fallback is None:return {'goal_id':goal_id,'state':'unknown','error':str(exc),'resume_same_goal':True,'business_acceptance':False}
   result,fallback_report=fallback
  if result.get('goal_id')!=goal_id:raise ValueError('response goal identity mismatch')
  if result.get('state')in ('completed','failed','cancelled','awaiting_owner'):
   decisions=result.get('pending_decisions')or[]
   event=next((d.get('decision_id')for d in decisions if d.get('continuity')),None)if result['state']=='awaiting_owner'else result['state']
   if not event:return {'goal_id':goal_id,'state':'unknown','reason':'owner event identity absent','resume_same_goal':True,'business_acceptance':False}
   event_id=hashlib.sha256(json.dumps([goal_id,event]).encode()).hexdigest()
   outbox=Path(state)/'owner-handoff';outbox.mkdir(parents=True,exist_ok=True);path=outbox/(event_id+'.json')
   with persist_lock(outbox):
    if path.exists():
     packet=json.loads(path.read_bytes())
     if packet.get('goal_id')!=goal_id or packet.get('event_id')!=event_id or packet.get('event')!=event:raise ValueError('existing handoff packet identity mismatch')
    else:
     report=fallback_report if fallback_report is not None else call(endpoint,'GET','/v1/requests/'+goal_id+'/report')
     packet={'goal_id':goal_id,'event':event,'event_id':event_id,'consumer':'product_owner','delivery':'prepared','business_acceptance':False,'status':result,'report':report}
     atomic_write_json(path,packet)
   digest=hashlib.sha256(path.read_bytes()).hexdigest()
   return {'goal_id':goal_id,'state':result['state'],'event_id':event_id,'packet':str(path.resolve()),'packet_sha256':digest,'consumer':'product_owner','delivery':'prepared','business_acceptance':False,'next':'product owner reads packet then explicitly consumes exact event; queued/busy is not receipt'}
  if time.monotonic()>=deadline:return {'goal_id':goal_id,'state':'observation_timeout','resume_same_goal':True,'business_acceptance':False}
  time.sleep(min(poll_sec,max(0,deadline-time.monotonic())))

def consume_owner_packet(state,goal_id,*,event_id,packet_sha256,receipt_id):
 """Explicit trusted LOCAL product-owner acknowledgment, not business approval."""
 from framework.persist_lock import persist_lock,atomic_write_json
 if len(event_id)!=64 or any(c not in '0123456789abcdef'for c in event_id):raise ValueError('exact event id required')
 if not receipt_id or not packet_sha256:raise ValueError('actual owner receipt and exact packet digest required')
 root=Path(state)/'owner-handoff';path=root/(event_id+'.json')
 with persist_lock(root):
  packet=json.loads(path.read_bytes())
  if packet.get('goal_id')!=goal_id or packet.get('event_id')!=event_id or hashlib.sha256(path.read_bytes()).hexdigest()!=packet_sha256:raise ValueError('owner receipt binding mismatch')
  receipt=Path(state)/'owner-consumption'/(event_id+'.json')
  record={'goal_id':goal_id,'event_id':event_id,'packet_sha256':packet_sha256,'receipt_id':receipt_id,'owner':'product_owner','decision':'received','business_acceptance':False}
  if receipt.exists():
   if json.loads(receipt.read_bytes())!=record:raise ValueError('owner receipt identity conflict')
  else:atomic_write_json(receipt,record)
 return record

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state',required=True);sub=p.add_subparsers(dest='cmd',required=True)
 serve=sub.add_parser('serve');serve.add_argument('--restart-settled',type=float);serve.add_argument('--model-release',action='store_true');serve.add_argument('--task-workspace-permissions',action='store_true');serve.add_argument('--lead-bin',default='grok');serve.add_argument('--native-bin',default='agy');serve.add_argument('--planning-timeout-sec',type=planning_timeout,default=180,help='planning only: finite seconds > 0 and <= 300 (default: 180); capped by active resource checkpoint, does not release models')
 serve.add_argument('--lead-max-turns',type=lead_max_turns,default=1,help='tool-free plan/review internal CLI turns: 1 or 2 (default: 1); permission stays 1; not a resource grant')
 op=sub.add_parser('open');op.add_argument('--goal',required=True);op.add_argument('--must',action='append',default=[]);op.add_argument('--must-not',action='append',default=[]);op.add_argument('--key',required=True);op.add_argument('--native-limit',type=int,default=0);op.add_argument('--lead-limit',type=int,default=0);op.add_argument('--window-sec',type=int,default=1800)
 for name in ('status','report','consume','wait'):
  q=sub.add_parser(name);q.add_argument('--goal-id',required=True)
  if name=='wait':q.add_argument('--timeout-sec',type=float,default=300)
  if name=='consume':
   q.add_argument('--event-id',required=True);q.add_argument('--packet-sha256',required=True);q.add_argument('--receipt-id',required=True)
 cont=sub.add_parser('continue');cont.add_argument('--goal-id',required=True);cont.add_argument('--decision-id',required=True);cont.add_argument('--grant-id',required=True);cont.add_argument('--reason',required=True);cont.add_argument('--native-limit',type=int,required=True);cont.add_argument('--lead-limit',type=int,required=True);cont.add_argument('--window-sec',type=int,required=True)
 end=sub.add_parser('end');end.add_argument('--goal-id',required=True);end.add_argument('--decision-id',required=True);end.add_argument('--reason',required=True);end.add_argument('--alternative',required=True)
 release=sub.add_parser('release-models');release.add_argument('--reason',required=True)
 sub.add_parser('stop');a=p.parse_args();state=Path(os.path.abspath(a.state))
 if not state.is_relative_to(ROOT):raise ValueError('state must be a new directory within this isolated checkout')
 for ancestor in [*reversed(state.parents),state]:
  if ancestor.exists()and(ancestor.is_symlink()or getattr(ancestor.lstat(),'st_file_attributes',0)&0x400):raise ValueError('state reparse refused')
 if a.cmd=='serve':
  state.mkdir(parents=True,exist_ok=True)
  import msvcrt
  lease=(state/'host-owner.lock').open('a+b');lease.seek(0);lease.write(b'0');lease.flush();lease.seek(0)
  try:msvcrt.locking(lease.fileno(),msvcrt.LK_NBLCK,1)
  except OSError:raise ValueError('host already owned by another process')
  if(state/'host.json').exists():
   if a.restart_settled is None:raise ValueError('host already configured; explicit --restart-settled OLD_BIRTH required')
   if a.model_release:raise ValueError('settled restart must begin HOLD; later release-models and explicit continuation required')
   from framework.host_restart import archive_settled_host
   archive_settled_host(state,expected_epoch=a.restart_settled,write_json=save)
  import psutil
  service=GoalService(state,lead_bin=a.lead_bin,native_bin=a.native_bin,release=a.model_release,task_permissions=a.task_workspace_permissions,planning_timeout_sec=a.planning_timeout_sec,lead_max_turns=a.lead_max_turns);endpoint=service.start();save(state/'host.json',{'endpoint':endpoint,'pid':os.getpid(),'birth':psutil.Process().create_time(),'model_release':a.model_release,'synthetic':False,'planning_timeout_sec':a.planning_timeout_sec,'lead_max_turns':a.lead_max_turns});print(json.dumps({'state':'serving'if a.model_release else'HOLD','endpoint':endpoint}),flush=True)
  try:
   while not(state/'stop.json').exists():
    if not service.server.released and(state/'host-release.json').exists():
     release=json.loads((state/'host-release.json').read_bytes());identity=json.loads((state/'host.json').read_bytes())
     if release.get('pid')==os.getpid()and release.get('birth')==identity['birth']:
      service.server.released=True;identity['model_release']=True;save(state/'host.json',identity);service.loop.start()
    time.sleep(.2)
  finally:service.close();save(state/'settled.json',{'thread_alive':service.thread.is_alive(),'state':'settled'})
  return
 host=json.loads((state/'host.json').read_bytes());layer=DurableLayer.open(state,use_cache=False);endpoint=host['endpoint']
 if a.cmd=='open':
  body={'title':'Direct goal','goal':a.goal,'boundaries':{'must':[],'must_not':a.must_not},'acceptance':{'artifacts':[],'lead_selects_artifacts':True,'text':'\n'.join(a.must),'auto_review_materials':True,'complete_delta_required':True,'material_capacity_bytes':250000,'delta_capacity_bytes':500000},'budget':{'max_reworks':max(1,a.native_limit),'wall_sec':a.window_sec},'idempotency_key':a.key}
  result=call(endpoint,'POST','/v1/requests',body);gid=result['goal_id'];snap=layer.get_goal(gid)['goal']
  if not snap.get('resource_packages'):
   if not host.get('model_release')and not host.get('synthetic'):result.update(state='HOLD',reason='host model release absent; goal retained, no models called')
   else:
    d=next((d for d in snap.get('pending_decisions',[])if d.get('continuity')),None);now=time.time();grant={'goal_id':gid,'grant_id':'initial_'+hashlib.sha256(a.key.encode()).hexdigest(),'reason':'product owner initial bounded package','resource':{'package_id':'initial_'+gid,'original_epoch':now,'checkpoint_epoch':now+a.window_sec,'native_limit':a.native_limit,'lead_limit':a.lead_limit}};result['authorization']=layer.continuity_authorize(gid,decision_id=d['decision_id']if d else'',grant=grant,trusted=True)
 elif a.cmd=='end':result=layer.continuity_end(a.goal_id,decision_id=a.decision_id,reason=a.reason,alternative=a.alternative,trusted=True)
 elif a.cmd=='release-models':
  import psutil
  if abs(psutil.Process(host['pid']).create_time()-host['birth'])>.01:raise ValueError('host identity mismatch')
  save(state/'host-release.json',{'pid':host['pid'],'birth':host['birth'],'reason':a.reason,'authorized_by':'local_product_owner'});result={'release_requested':True,'reason':a.reason,'model_invocation_not_inferred':True}
 elif a.cmd=='continue':
  if not host.get('model_release')and not host.get('synthetic'):raise ValueError('HOLD: explicit host release absent')
  now=time.time();grant={'goal_id':a.goal_id,'grant_id':a.grant_id,'reason':a.reason,'resource':{'package_id':a.grant_id,'original_epoch':now,'checkpoint_epoch':now+a.window_sec,'native_limit':a.native_limit,'lead_limit':a.lead_limit}}
  prior=next((g for g in layer.get_goal(a.goal_id)['goal'].get('continuation_grants',[])if g['grant_id']==a.grant_id),None)
  if prior:
   r=prior['resource']
   if prior['reason']!=a.reason or r['native_limit']!=a.native_limit or r['lead_limit']!=a.lead_limit or r['checkpoint_epoch']-r['original_epoch']!=a.window_sec:raise ValueError('continuation id reused with different terms')
   grant=prior
  result=layer.continuity_authorize(a.goal_id,decision_id=a.decision_id,grant=grant,trusted=True)
 elif a.cmd in ('status','report'):
  result=call(endpoint,'GET','/v1/continuity/'+a.goal_id)
  if a.cmd=='report':result['report']=call(endpoint,'GET','/v1/requests/'+a.goal_id+'/report')
 elif a.cmd=='wait':result=wait_for_owner(state,endpoint,a.goal_id,timeout_sec=a.timeout_sec)
 elif a.cmd=='consume':
  result=consume_owner_packet(state,a.goal_id,event_id=a.event_id,packet_sha256=a.packet_sha256,receipt_id=a.receipt_id)
 else:
  import psutil
  if abs(psutil.Process(host['pid']).create_time()-host['birth'])>.01:raise ValueError('host identity mismatch')
  save(state/'stop.json',{'requested':True});result={'stop_requested':True}
 print(json.dumps(result,ensure_ascii=False))
 if a.cmd=='wait':
  code={'completed':0,'failed':2,'cancelled':2,'unknown':3,'observation_timeout':3,'awaiting_owner':4}.get(result.get('state'),3)
  if code:raise SystemExit(code)
if __name__=='__main__':
 try:main()
 except Exception as exc:print(json.dumps({'state':'blocked','error':str(exc),'new_model_calls':'not inferred; read authoritative same-store package claims'}));sys.exit(2)
