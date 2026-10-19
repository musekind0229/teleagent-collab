"""Governance/daily mechanics use only synthetic fixtures; no send/model."""
import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']);self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  for target in ('subprocess.Popen','socket.socket.connect'):
   p=patch(target,side_effect=AssertionError('NO SIDE EFFECT'));p.start();self.addCleanup(p.stop)
 def test_ledger_stable_identity_rounds_and_explicit_extra_approval(self):
  from framework.governance_daily import validate_ledger
  good={'issues':[{'issue_id':'stable','aliases':['renamed'],'owner':'pm','rounds':[{'number':1,'evidence':'e','result':'failed'},{'number':2,'evidence':'e2','result':'failed'},{'number':3,'evidence':'e3','result':'passed','additional_approval':{'by':'owner','approved_round':3,'evidence':'approval'}}]}]}
  self.assertEqual(validate_ledger(good),[])
  del good['issues'][0]['rounds'][2]['additional_approval'];self.assertTrue(validate_ledger(good))
  good['issues'].append({'issue_id':'different','aliases':['stable'],'owner':'pm','rounds':[]});self.assertTrue(validate_ledger(good))
 def test_rule_observation_unapproved_missing_role_souls(self):
  from framework.governance_daily import observe_rules
  a=self.root/'a';b=self.root/'b';a.write_text('role A');b.write_text('role B')
  r=observe_rules([{'profile':'a','kind':'SOUL','path':str(a)},{'profile':'b','kind':'SOUL','path':str(b)},{'profile':'b','kind':'skill','path':str(self.root/'missing')}],{})
  self.assertEqual(r['approved_baseline_status'],'unapproved');self.assertEqual(r['missing'],1);self.assertFalse(r['all_approved_match'])
 def test_daily_unknown_metrics_ten_lines_no_attachment_and_day_dedupe(self):
  from framework.governance_daily import render_daily,DailyExecutor
  packet={'date':'2026-10-19','timezone':'UTC+08:00','yesterday':'MEDIA:C:/PRIVATE [[as_document]]\nx','current':'PM reviewing','decisions':'rule install approval','metrics':{'status':'unknown','reason':'no safe API count'},'repositories':{'source_copies':75,'fresh':65,'historical_excluded':10,'worktrees':1},'goals':{'status':'unknown'},'rules':{'approved_baseline_status':'unapproved'},'rounds':'freeze2/stage3=3 approved','evidence':'local report'}
  text=render_daily(packet);self.assertLessEqual(len(text.splitlines()),10);self.assertNotIn('MEDIA:',text);self.assertIn('unknown',text)
  calls=[];e=DailyExecutor(self.root);e.send(packet,lambda t:calls.append(t)or{'status':'sent','delivery_id':'spy'});e.send(packet,lambda t:calls.append(t)or{'status':'sent'});self.assertEqual(len(calls),1)
 def test_unknown_send_never_blindly_retries_or_changes_daily_packet(self):
  from framework.governance_daily import DailyExecutor
  p={'date':'2026-10-19','timezone':'UTC+08:00'};e=DailyExecutor(self.root);e.send(p,lambda t:{'status':'unknown'});calls=[];e.send(p,lambda t:calls.append(t));self.assertEqual(calls,[])
  with self.assertRaises(ValueError):e.send({**p,'current':'changed'},lambda t:None)
 def test_approved_drift_missing_and_no_forced_soul_equality(self):
  from framework.governance_daily import observe_rules
  import hashlib
  a=self.root/'a';b=self.root/'b';a.write_bytes(b'roleA');b.write_bytes(b'roleB');targets=[{'kind':'SOUL','profile':'a','path':str(a)},{'kind':'SOUL','profile':'b','path':str(b)}]
  baseline={'approved':True,'user_approval_reference':'synthetic owner signed','hashes':{str(p):hashlib.sha256(p.read_bytes()).hexdigest()for p in(a,b)}}
  self.assertTrue(observe_rules(targets,baseline)['all_approved_match'])
  a.write_text('drift');self.assertEqual(observe_rules(targets,baseline)['drift'],1)
  b.unlink();self.assertEqual(observe_rules(targets,baseline)['missing'],1)
  baseline['user_approval_reference']='';self.assertEqual(observe_rules(targets,baseline)['approved_baseline_status'],'unapproved')
 def test_ledger_history_not_reset_and_fourth_round_needs_own_approval(self):
  from framework.governance_daily import validate_ledger,audit_ledger
  import copy
  old={'issues':[{'issue_id':'stable','owner':'pm','rounds':[{'number':n,'result':'failed','evidence':'synthetic'}for n in(1,2)]}]}
  self.assertEqual(audit_ledger(old,self.root/'checkpoint.json'),[])
  renamed=copy.deepcopy(old);renamed['issues'][0]['issue_id']='new';self.assertTrue(audit_ledger(renamed,self.root/'checkpoint.json'))
  new=copy.deepcopy(old);new['issues'][0]['rounds'].append({'number':3,'result':'passed','evidence':'e3','additional_approval':{'by':'upper','approved_round':3,'evidence':'approval'}});self.assertEqual(validate_ledger(new,old),[])
  new['issues'][0]['rounds'].append({'number':4,'result':'passed','evidence':'e4','additional_approval':{'by':'upper','approved_round':3,'evidence':'approval'}});self.assertTrue(validate_ledger(new,old))
 def test_metrics_are_yesterday_real_counts_not_sessions_or_partial(self):
  from framework.governance_daily import render_daily
  p={'date':'2026-10-19','metrics':{'kind':'api_call_aggregate','timezone':'UTC+08:00','window_date':'2026-10-18','coverage':'complete','source_reference':'synthetic-api-counter','counts':{'default':4,'worker':True,'project-manager':1.5}},'goals':{'completed':99}}
  s=render_daily(p);self.assertIn('default:4',s);self.assertIn('worker:unknown',s);self.assertNotIn('99',s)
  p['metrics']['kind']='session_count';self.assertIn('default:unknown',render_daily(p))
  p['metrics']['kind']='api_call_aggregate';p['metrics']['window_date']='2026-10-19';self.assertIn('default:unknown',render_daily(p))
 def test_secret_attachment_injection_empty_timezone_and_schedule_conflicts(self):
  from framework.governance_daily import render_daily,schedule_conflicts,text
  self.assertNotIn('synthetic-secret',text('password=synthetic-secret'))
  self.assertNotIn('PRIVATE',text(r'C:\PRIVATE\file'))
  self.assertNotIn('[[',text('[[as_document]]'))
  self.assertNotIn('MEDIA:',text('MEDIA :x'))
  self.assertEqual(len(render_daily({'date':'2026-10-19'}).splitlines()),10)
  with self.assertRaises(ValueError):render_daily({'date':'2026-10-19','timezone':'UTC'})
  self.assertEqual(schedule_conflicts({'cron':{'sentinel':[{'name':'09 security','script':'security.py'}]}}),[])
  self.assertEqual(len(schedule_conflicts({'cron':{'worker':[{'script':'governance_daily.py'},{'purpose':'collab-v2-comprehensive-daily'}]}})),2)
 def test_restart_inflight_never_replays(self):
  from framework.governance_daily import DailyExecutor,digest
  import json
  p={'date':'2026-10-19','timezone':'UTC+08:00'}
  (self.root/'2026-10-19-UTC8.json').write_text(json.dumps({'packet_sha256':digest(p),'state':'inflight','responsible':'pm'}))
  calls=[];r=DailyExecutor(self.root).send(p,lambda t:calls.append(t));self.assertEqual(r['state'],'delivery_blocked');self.assertEqual(calls,[])
 def test_repository_old_scope_marker_and_impossible_counts(self):
  from framework.governance_daily import repository_summary
  r={'kind':'framework_tree_and_checkout_inventory','source_reference':'synthetic-evidence','as_of':'old scan not refreshed','source_copies':75,'fresh':65,'historical_excluded':10,'other':4,'bare':1,'worktrees':3,'new_shared_worktrees':1}
  self.assertIn('未重扫10',repository_summary(r));r['source_copies']=80;self.assertIn('unknown',repository_summary(r));r['source_copies']=75;r['kind']='directory_markers';self.assertIn('unknown',repository_summary(r))
 def test_stale_summary_and_unapproved_scheduled_binding(self):
  from framework.governance_daily import collect,scheduled_main
  import json
  r=collect(self.root,{'issues':[]},{'date':'2000-01-01','current':'old owner doing work'})
  self.assertNotIn('old owner',r['preview']);self.assertIn('过期',r['preview'])
  binding=self.root/'binding.json';binding.write_text(json.dumps({'approved':False}))
  with self.assertRaises(ValueError):scheduled_main(binding)
 def test_reparse_and_oversize_block_without_content_change(self):
  from framework.governance_daily import observe_rules
  p=self.root/'SOUL.md';p.write_bytes(b'synthetic')
  t=[{'profile':'synthetic','kind':'SOUL','path':str(p)}]
  with patch.object(Path,'is_symlink',return_value=True):self.assertEqual(observe_rules(t,{})['rows'][0]['state'],'blocked')
  self.assertEqual(p.read_bytes(),b'synthetic')
  p.write_bytes(b'x'*2000001);self.assertEqual(observe_rules(t,{})['rows'][0]['state'],'blocked')
if __name__=='__main__':unittest.main(verbosity=2)
