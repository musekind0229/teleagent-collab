"""Single direct entry: same store, explicit owner continuation, no successor copy."""
from pathlib import Path
import sys,argparse,json,time,os,urllib.request,urllib.error,hashlib
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from framework.durable_api import DurableLayer
from framework.goal_service import GoalService

def save(p,x):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(x,ensure_ascii=False));os.replace(tmp,p)
def call(endpoint,method,path,body=None):
 req=urllib.request.Request(endpoint+path,data=json.dumps(body).encode()if body is not None else None,method=method,headers={'Content-Type':'application/json'});opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 try:
  with opener.open(req,timeout=10)as r:return json.loads(r.read())
 except urllib.error.HTTPError as exc:raise ValueError('HTTP '+str(exc.code)+': '+exc.read(4000).decode())from exc
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state',required=True);sub=p.add_subparsers(dest='cmd',required=True)
 serve=sub.add_parser('serve');serve.add_argument('--restart-settled',type=float);serve.add_argument('--model-release',action='store_true');serve.add_argument('--task-workspace-permissions',action='store_true');serve.add_argument('--lead-bin',default='grok');serve.add_argument('--native-bin',default='agy')
 op=sub.add_parser('open');op.add_argument('--goal',required=True);op.add_argument('--must',action='append',default=[]);op.add_argument('--must-not',action='append',default=[]);op.add_argument('--key',required=True);op.add_argument('--native-limit',type=int,default=0);op.add_argument('--lead-limit',type=int,default=0);op.add_argument('--window-sec',type=int,default=1800)
 for name in ('status','report','consume'):q=sub.add_parser(name);q.add_argument('--goal-id',required=True)
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
  service=GoalService(state,lead_bin=a.lead_bin,native_bin=a.native_bin,release=a.model_release,task_permissions=a.task_workspace_permissions);endpoint=service.start();save(state/'host.json',{'endpoint':endpoint,'pid':os.getpid(),'birth':psutil.Process().create_time(),'model_release':a.model_release,'synthetic':False});print(json.dumps({'state':'serving'if a.model_release else'HOLD','endpoint':endpoint}),flush=True)
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
 elif a.cmd=='consume':
  result=call(endpoint,'GET','/v1/continuity/'+a.goal_id)
  if result['state']not in ('completed','failed','cancelled','awaiting_owner'):raise ValueError('not terminal or owner handoff')
  key=(result['pending_decisions'][0]['decision_id']if result['state']=='awaiting_owner'else result['state']);receipt=state/'owner-consumption'/(a.goal_id+'_'+key+'.json');record={'goal_id':a.goal_id,'event':key,'owner':'product_owner','decision':'received','business_acceptance':False}
  if not receipt.exists():save(receipt,record)
  result=record
 else:
  import psutil
  if abs(psutil.Process(host['pid']).create_time()-host['birth'])>.01:raise ValueError('host identity mismatch')
  save(state/'stop.json',{'requested':True});result={'stop_requested':True}
 print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':
 try:main()
 except Exception as exc:print(json.dumps({'state':'blocked','error':str(exc),'new_model_calls':'not inferred; read authoritative same-store package claims'}));sys.exit(2)
