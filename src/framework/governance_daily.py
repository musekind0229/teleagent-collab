"""Bounded governance observation and one daily summary; no model or scheduler.
Only explicit rule paths and curated aggregate inputs; no chat/state/config scan.
"""
from pathlib import Path
from datetime import datetime,timedelta,timezone,date
import argparse,hashlib,json,re
from framework.persist_lock import persist_lock,atomic_write_json
PROFILES=('default','project-manager','worker','watchdog')
SKILLS=('agent-delivery-management','teleagent-collab','collab-execution-evidence-lessons')
ZONE=timezone(timedelta(hours=8));DAILY_ID='collab-v2-comprehensive-daily'
METRIC_PROFILES=(*PROFILES,'sentinel')
def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
def text(v,limit=200):
 s=' '.join(str(v).split())[:limit]
 s=re.sub(r'(?i)MEDIA\s*:', 'MEDIA：',s).replace('[[','［［').replace(']]','］］')
 s=re.sub(r'\b[A-Za-z]:[\\/]\S+|/(?:Users|home)/\S+','[local-path]',s)
 if re.search(r'(?i)(api[_-]?key|access[_-]?token|password|bearer)\s*[:=]',s):return '[redacted unsafe field]'
 return s

def rule_targets(home):
 home=Path(home);out=[]
 for profile in PROFILES:
  root=home if profile=='default'else home/'profiles'/profile
  out.append({'profile':profile,'kind':'SOUL','path':str(root/'SOUL.md')})
  out.extend({'profile':profile,'kind':name,'path':str(root/'skills/autonomous-ai-agents'/name/'SKILL.md')}for name in SKILLS)
 return out

def observe_rules(targets,baseline):
 approved=isinstance(baseline,dict)and bool(baseline.get('user_approval_reference'))and baseline.get('approved')is True
 hashes=baseline.get('hashes',{})if approved else{};rows=[]
 for t in targets:
  p=Path(t['path']);row={**t,'sha256':None,'state':'missing'}
  try:
   if any(x.is_symlink()or getattr(x.lstat(),'st_file_attributes',0)&0x400 for x in[p,*p.parents]if x.exists()):raise ValueError('reparse refused')
   if p.is_file():
    if p.stat().st_size>2000000:raise ValueError('rule file over bound')
    sha=hashlib.sha256(p.read_bytes()).hexdigest();expected=hashes.get(str(p));row.update(sha256=sha,state='unapproved'if not approved or not expected else'match'if sha==expected else'drift')
  except(OSError,ValueError)as exc:row.update(state='blocked',reason=type(exc).__name__)
  rows.append(row)
 shared={name:len({r['sha256']for r in rows if r['kind']==name and r['sha256']})==1 and sum(r['kind']==name and r['sha256']is not None for r in rows)==len(PROFILES)for name in SKILLS}
 return {'approved_baseline_status':'approved'if approved else'unapproved','rows':rows,'missing':sum(r['state']=='missing'for r in rows),'drift':sum(r['state']=='drift'for r in rows),'all_approved_match':approved and bool(rows)and all(r['state']=='match'for r in rows),'shared_skill_equal':shared,'SOUL_policy':'role-specific individual approved baselines, not forced equal hashes'}

def validate_ledger(ledger,previous=None):
 issues=ledger.get('issues',[]);errors=[];ids=set();names=set()
 for issue in issues:
  iid=issue.get('issue_id');aliases=issue.get('aliases',[])
  if not iid or iid in ids or any(n in names for n in[iid,*aliases]):errors.append('duplicate/renamed identity: '+str(iid))
  ids.add(iid);names.update([iid,*aliases])
  if not issue.get('owner'):errors.append('missing owner: '+str(iid))
  rounds=issue.get('rounds',[])
  if issue.get('round_count_unknown'):
   if rounds or not issue.get('evidence'):errors.append('unknown history must retain evidence without fabricated rounds')
  for n,r in enumerate(rounds,1):
   if r.get('number')!=n or not r.get('evidence')or not r.get('result'):errors.append('noncontiguous/unproven round: '+str(iid))
   if n>2:
    a=r.get('additional_approval')or{}
    if not a.get('by')or not a.get('evidence')or a.get('approved_round')!=n:errors.append('additional round lacks exact upper approval: '+str(iid))
    proof=a.get('evidence')
    if isinstance(proof,dict)and not re.fullmatch('[0-9a-f]{64}',str(proof.get('sha256',''))):errors.append('invalid approval evidence hash: '+str(iid))
 if previous:
  current={i['issue_id']:i for i in issues}
  for old in previous.get('issues',[]):
   now=current.get(old['issue_id'])
   if now is None:errors.append('historical issue removed/renamed: '+old['issue_id']);continue
   for idx,r in enumerate(old.get('rounds',[])):
    if idx>=len(now.get('rounds',[])):errors.append('round history shortened: '+old['issue_id']);break
    new=now['rounds'][idx]
    if r!=new and not(r.get('result')=='in_progress'and all(new.get(k)==v for k,v in r.items()if k!='result')):errors.append('historical round rewritten: '+old['issue_id'])
 return errors

def schedule_conflicts(metadata):
 rows=[]
 for profile,jobs in metadata.get('cron',metadata).items():
  if not isinstance(jobs,list):continue
  for job in jobs:
   if job.get('enabled')is False:continue
   if job.get('purpose')==DAILY_ID or Path(job.get('script')or'').name in('collab_governance_daily.py','governance_daily.py')or DAILY_ID in(job.get('name')or''):
    rows.append({'profile':profile,'id':job.get('id'),'script':job.get('script'),'reason':'same comprehensive daily executor'})
 return rows

def repository_summary(repos):
 fields=('source_copies','fresh','historical_excluded','other','bare','worktrees','new_shared_worktrees')
 valid=repos.get('kind')=='framework_tree_and_checkout_inventory'and bool(repos.get('source_reference'))and bool(repos.get('as_of'))and all(type(repos.get(k))is int and repos[k]>=0 for k in fields)
 if not valid or repos['source_copies']!=repos['fresh']+repos['historical_excluded']:return 'unknown（无有效tree+checkout计数；不以目录/marker代替）'
 return text(f"历史source={repos['source_copies']}=已核{repos['fresh']}+未重扫{repos['historical_excluded']}；其他{repos['other']}/bare{repos['bare']}；共享worktree新增{repos['new_shared_worktrees']}/现{repos['worktrees']}；asof={repos['as_of']}（非全库新重扫）")

def render_daily(p):
 day=date.fromisoformat(p['date']);tz=p.get('timezone','UTC+08:00')
 if tz!='UTC+08:00':raise ValueError('daily timezone must be explicit UTC+08:00')
 metrics=p.get('metrics')or{};counts=metrics.get('counts')or{}
 measured=metrics.get('kind')=='api_call_aggregate'and metrics.get('window_date')==(day-timedelta(days=1)).isoformat()and metrics.get('timezone')==tz and metrics.get('coverage')=='complete'and bool(metrics.get('source_reference'))
 calls='/'.join(profile+':'+str(counts[profile])if measured and type(counts.get(profile))is int and counts[profile]>=0 else profile+':unknown'for profile in METRIC_PROFILES)
 repos=p.get('repositories')or{};goals=p.get('goals')or{};rules=p.get('rules')or{}
 goal_verified=goals.get('kind')=='goal_outcome_aggregate'and goals.get('window_date')==(day-timedelta(days=1)).isoformat()and goals.get('timezone')==tz and goals.get('coverage')=='complete'and bool(goals.get('source_reference'))
 goal_counts='/'.join(str(goals[k])if goal_verified and type(goals.get(k))is int and goals[k]>=0 else'unknown'for k in('completed','failed'))
 return '\n'.join([
  f'综合日报 {day.isoformat()} 09:00 {tz}',
  '昨天：'+text(p.get('yesterday','unknown（无获准工程摘要）')),
  '当前/负责人：'+text(p.get('current','unknown（需责任人更新）')),
  '需决定：'+text(p.get('decisions','unknown（未提供经核决定项）')),
  'API调用：'+calls+'；口径='+('完整聚合'if measured else'unknown：未获安全完整API聚合，非session数'),
  '仓库：'+repository_summary(repos),
  '真实goal完成/失败：'+goal_counts+'（昨日UTC+08；仅经核完整聚合）',
  '规则：'+text('baseline='+str(rules.get('approved_baseline_status','unknown'))+' missing='+str(rules.get('missing','unknown'))+' drift='+str(rules.get('drift','unknown'))+' shared-diff='+str(sum(v is False for v in rules.get('shared_skill_equal',{}).values()))+'；SOUL按角色'),
  '轮数/阻塞owner：'+text(p.get('rounds','unknown'),4000),
  '证据/时效：'+text(p.get('evidence','unknown'))])

class DailyExecutor:
 def __init__(self,root):self.root=Path(root)
 def send(self,packet,sender):
  rendered=render_daily(packet);identity=packet['date']+'-UTC8';sha=digest(packet);path=self.root/(identity+'.json')
  with persist_lock(self.root):
   if path.exists():
    row=json.loads(path.read_bytes())
    if row['packet_sha256']!=sha:raise ValueError('daily identity already bound to different packet')
    if row['state']=='inflight':
     row.update(state='delivery_blocked',reason='prior send in progress or effect unknown; reconcile, no blind duplicate');atomic_write_json(path,row)
    return row
   row={'daily_id':DAILY_ID,'date':packet['date'],'timezone':'UTC+08:00','packet_sha256':sha,'state':'inflight','responsible':'project-manager','recovery':'unknown external send must be reconciled; never blindly duplicate'};atomic_write_json(path,row)
  try:r=sender(rendered)
  except Exception:r={'status':'unknown'}
  row.update(state='sent'if isinstance(r,dict)and r.get('status')=='sent'else'delivery_blocked',delivery_id=r.get('delivery_id')if isinstance(r,dict)else None)
  with persist_lock(self.root):atomic_write_json(path,row)
  return row

def collect(home,ledger,inputs,*,baseline=None):
 now=datetime.now(ZONE);day=now.date().isoformat();source_day=inputs.get('date');fresh=source_day==day
 rules=observe_rules(rule_targets(home),baseline or{});errors=validate_ledger(ledger)
 rounds='; '.join(i['issue_id']+':'+('unknown'if i.get('round_count_unknown')else str(len(i.get('rounds',[]))))+'['+text(i.get('status','unknown'),85)+(' extra-approved='+','.join(str(r['number'])for r in i.get('rounds',[])if r.get('additional_approval'))if any(r.get('additional_approval')for r in i.get('rounds',[]))else'')+']/'+str(i.get('owner','unknown'))for i in ledger.get('issues',[]))
 if errors:rounds='BLOCKED '+','.join(errors)
 packet={k:inputs[k]for k in('metrics','repositories','goals','evidence')if k in inputs}
 for key in('yesterday','current','decisions'):
  packet[key]=inputs.get(key,'unknown（PM未提供安全摘要）')if fresh else'unknown（摘要缺失或过期；project-manager更新）'
 packet['evidence']=str(packet.get('evidence','no safe summary'))+'; source_date='+str(source_day or'unknown')+'; freshness='+('current-day'if fresh else'stale-or-unknown')
 packet.update(date=day,timezone='UTC+08:00',rules={k:rules[k]for k in('approved_baseline_status','missing','drift','shared_skill_equal')},rounds=rounds)
 return {'packet':packet,'preview':render_daily(packet),'rule_observation':rules,'ledger_errors':errors,'collected_at':now.isoformat(),'window':'previous local calendar day for completion summaries; calls require explicit safe aggregate window_date/coverage, not inferred from sessions'}
def audit_ledger(ledger,path):
 path=Path(path)
 with persist_lock(path.parent):
  previous=json.loads(path.read_bytes())if path.exists()else None
  errors=validate_ledger(ledger,previous)
  if not errors:atomic_write_json(path,ledger)
  return errors

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--profile-home-root',required=True);p.add_argument('--ledger',required=True);p.add_argument('--input',required=True);p.add_argument('--output',required=True);p.add_argument('--approved-baseline');p.add_argument('--cron-metadata');p.add_argument('--ledger-checkpoint',required=True);p.add_argument('--send',action='store_true');p.add_argument('--receipt-root');p.add_argument('--route-file');a=p.parse_args()
 ledger=json.loads(Path(a.ledger).read_bytes());inputs=json.loads(Path(a.input).read_bytes());baseline=json.loads(Path(a.approved_baseline).read_bytes())if a.approved_baseline else{}
 if a.send:
  from framework.runtime_guard import verify_runtime
  verify_runtime()
  if not a.receipt_root or not a.route_file or not a.cron_metadata:raise ValueError('send requires approved route, receipts and reviewed scheduler metadata')
 result=collect(a.profile_home_root,ledger,inputs,baseline=baseline)
 result['ledger_errors']=audit_ledger(ledger,a.ledger_checkpoint)
 if result['ledger_errors']:
  result['packet']['rounds']='BLOCKED: ledger history mismatch; owner project-manager';result['preview']=render_daily(result['packet'])
 if a.cron_metadata:result['schedule_conflicts']=schedule_conflicts(json.loads(Path(a.cron_metadata).read_bytes()))
 atomic_write_json(Path(a.output),result);print(result['preview'])
 if a.send:
  if result['ledger_errors']or len(result.get('schedule_conflicts',[]))>1:raise ValueError('ledger or duplicate daily scheduler blocked')
  if result['packet']['date']!=datetime.now(ZONE).date().isoformat():raise ValueError('refuse stale/backdated daily send')
  from framework.lifecycle_transport import HermesLifecycleSender
  sender=HermesLifecycleSender(a.route_file);receipt=DailyExecutor(a.receipt_root).send(result['packet'],lambda body:sender('qq',body))
  atomic_write_json(Path(a.output).with_suffix('.delivery.json'),receipt)
  return 0 if receipt['state']=='sent'else 2
 return 2 if result['ledger_errors']else 0
def scheduled_main(binding_file):
 """No-agent launcher binding is local, PM-approved, never contains targets."""
 binding=json.loads(Path(binding_file).read_bytes())
 if binding.get('approved')is not True or binding.get('daily_id')!=DAILY_ID or binding.get('timezone')!='UTC+08:00':raise ValueError('daily scheduler binding unapproved')
 argv=[]
 for key in('profile-home-root','ledger','ledger-checkpoint','input','output','cron-metadata','receipt-root','route-file'):
  value=binding.get(key)
  if not isinstance(value,str)or not Path(value).is_absolute():raise ValueError('missing absolute daily binding path: '+key)
  argv.extend(['--'+key,value])
 if binding.get('approved-baseline'):argv.extend(['--approved-baseline',binding['approved-baseline']])
 import sys
 old=sys.argv
 try:sys.argv=[old[0],*argv,'--send'];return main()
 finally:sys.argv=old

if __name__=='__main__':raise SystemExit(main())
