"""Exact-run review recovery: synthetic only, subprocess/connect forbidden."""
import copy,json,sys,time,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_goal_loop_maintenance import Tests as Base,Lead,Backend
from framework.goal_service import ContinuityCoordinator,ResourceAuthority
from framework.app_service import CollabApplication,LeadAdapterPlanner

class Tests(unittest.TestCase):
 setUp=Base.setUp
 make=Base.make
 drive=Base.drive
 def blocked(self):
  app,a,l,b,gid=self.make(answers=('partial',));out,s=self.drive(app,gid)
  self.assertEqual(out['state'],'awaiting_owner');return app,a,l,b,gid,s['tasks'][0],s['pending_decisions'][0]
 def grant_review(self,app,gid,t,d,*,key='synthetic-review',lead_limit=2,trusted=True):
  from framework.review_recovery import review_binding
  binding=review_binding(app.layer,gid,t['task_id'],d['decision_id'])
  now=time.time();grant={'goal_id':gid,'grant_id':key,'reason':'synthetic owner review only','decision_id':d['decision_id'],'review_only':binding,'resource':{'package_id':key,'native_limit':2,'lead_limit':lead_limit,'original_epoch':now,'checkpoint_epoch':now+300}}
  return app.layer.continuity_authorize(gid,decision_id=d['decision_id'],grant=grant,trusted=trusted),grant
 def test_review_pass_same_run_no_native_collect_and_unlock_dependency(self):
  app,a,l,b,gid,t,d=self.blocked();old=copy.deepcopy(t['result']);rid=t['run_id'];col=b.collections;starts=b.starts
  app.layer.add_child_task(gid,task={'task_id':'synthetic-test-dependent','title':'test existing UI','status':'queued','depends_on':[t['task_id']],'inputs':{'instruction':'synthetic dependency test'},'expected_artifacts':['RESULT.md']})
  l.answers=['pass'];self.grant_review(app,gid,t,d)
  # Exactly one tick is review-only; dependent dispatch happens on a later tick.
  app.coordinator.process_goal(gid);s=app.layer.get_goal(gid)['goal'];done=s['tasks'][0]
  self.assertEqual(done['status'],'succeeded');self.assertEqual(done['run_id'],rid)
  self.assertEqual(b.starts,starts);self.assertEqual(b.collections,col);self.assertEqual(s['tasks'][1]['status'],'queued')
  self.assertEqual(done['attempt_history'][0]['result'],old)
  self.drive(app,gid);self.assertEqual(app.layer.get_goal(gid)['goal']['state'],'completed')
 def test_wrong_decision_native_hash_or_artifact_hash_refused(self):
  app,a,l,b,gid,t,d=self.blocked()
  from framework.review_recovery import review_binding
  binding=review_binding(app.layer,gid,t['task_id'],d['decision_id']);now=time.time()
  grant={'goal_id':gid,'grant_id':'bad','reason':'synthetic','review_only':binding,'resource':{'package_id':'bad','native_limit':0,'lead_limit':1,'original_epoch':now,'checkpoint_epoch':now+60}}
  for key in ('native_sha256','workspace_sha256','decision_id','run_id'):
   bad=copy.deepcopy(grant);bad['review_only'][key]='wrong'
   with self.subTest(key=key),self.assertRaises(ValueError):app.layer.continuity_authorize(gid,decision_id=d['decision_id'],grant=bad,trusted=True)
  Path(t['result']['artifacts'][0]).write_text('CHANGED')
  with self.assertRaises(ValueError):app.layer.continuity_authorize(gid,decision_id=d['decision_id'],grant=grant,trusted=True)
  self.assertEqual(len(l.actions),1)
 def test_review_again_no_verdict_stops_preserves_old_accounting(self):
  app,a,l,b,gid,t,d=self.blocked();before=a.status(gid);self.grant_review(app,gid,t,d);out,s=self.drive(app,gid)
  self.assertEqual(out['state'],'awaiting_owner');self.assertEqual(b.starts,1);self.assertEqual(b.collections,1);self.assertEqual(len(l.actions),2)
  self.assertEqual(s['resource_packages'][next(iter(before['packages']))],next(iter(before['packages'].values())))
  self.assertEqual(s['tasks'][0]['attempt_history'][0]['result'],t['result'])
 def test_review_fail_goes_to_existing_rework_not_pass(self):
  app,a,l,b,gid,t,d=self.blocked();l.answers=['fail'];self.grant_review(app,gid,t,d)
  app.coordinator.process_goal(gid);s=app.layer.get_goal(gid)['goal'];self.assertEqual(s['tasks'][0]['status'],'queued');self.assertEqual(b.starts,1);self.assertEqual(b.collections,1)
  self.assertEqual(s['tasks'][0]['result']['review']['status'],'failed')
 def test_untrusted_and_lead_budget_zero_refused_without_lead(self):
  app,a,l,b,gid,t,d=self.blocked()
  with self.assertRaises(ValueError):self.grant_review(app,gid,t,d,trusted=False)
  with self.assertRaises(ValueError):self.grant_review(app,gid,t,d,lead_limit=0)
  self.assertEqual(len(l.actions),1)
 def test_duplicate_authorization_and_restart_do_not_wash_history(self):
  app,a,l,b,gid,t,d=self.blocked();_,grant=self.grant_review(app,gid,t,d);saved=copy.deepcopy(app.layer.get_goal(gid)['goal'])
  self.assertTrue(app.layer.continuity_authorize(gid,decision_id=d['decision_id'],grant=grant,trusted=True)['duplicate'])
  reopened=CollabApplication(self.root);a2=ResourceAuthority(reopened.layer);l2=Lead(a2,('pass',));b2=Backend(a2)
  reopened.coordinator=ContinuityCoordinator(reopened.layer,planner=LeadAdapterPlanner(l2,cwd=self.root),backend=b2,workspaces_root=self.root/'workspaces');b2._trusted_dispatch_hook=a2
  self.drive(reopened,gid);self.assertEqual(b2.starts,0);self.assertEqual(b2.collections,0);self.assertEqual(len(l2.actions),1)
  self.drive(reopened,gid);self.assertEqual(len(l2.actions),1);self.assertEqual(reopened.layer.get_goal(gid)['goal']['tasks'][0]['attempt_history'][0]['result'],t['result'])
 def test_mutation_after_authorization_blocks_before_lead(self):
  app,a,l,b,gid,t,d=self.blocked();self.grant_review(app,gid,t,d);Path(t['result']['artifacts'][0]).write_text('CHANGED AFTER GRANT')
  out,s=self.drive(app,gid);self.assertEqual(out['state'],'awaiting_owner');self.assertEqual(len(l.actions),1);self.assertEqual(b.starts,1)
 def test_material_dedup_is_lossless_self_contained(self):
  from framework.review_material_codec import pack_material,unpack_material
  text='synthetic full source\n'*1000
  payload={'diff':text,'source_packet':{'provided_blocks':[{'code':text}]},'complete_delta':{'paths':[{'edits':[{'before_hex':'','after_hex':text.encode().hex()}]}]},'rules':['scope intact'],'review_status':'complete'}
  packed=pack_material(payload);self.assertEqual(unpack_material(packed),payload)
  self.assertLess(len(json.dumps(packed)),len(json.dumps(payload)))

 def test_legacy_continue_requeues_native_while_review_grant_keeps_run(self):
  app,a,l,b,gid,t,d=self.blocked();now=time.time()
  grant={'goal_id':gid,'grant_id':'legacy','reason':'synthetic legacy continuation','resource':{'package_id':'legacy','native_limit':1,'lead_limit':2,'original_epoch':now,'checkpoint_epoch':now+60}}
  app.layer.continuity_authorize(gid,decision_id=d['decision_id'],grant=grant,trusted=True)
  row=app.layer.get_goal(gid)['goal']['tasks'][0];self.assertEqual(row['status'],'queued');self.assertNotIn('run_id',row)
 def test_inflight_review_restart_requires_owner_without_new_lead(self):
  app,a,l,b,gid,t,d=self.blocked();self.grant_review(app,gid,t,d)
  with app.layer._rmw():
   row=app.layer.goals[gid]['tasks'][0];row['review_recovery']['status']='inflight';row['status']='review';app.layer._persist_unlocked()
  out,s=self.drive(app,gid);self.assertEqual(out['state'],'awaiting_owner');self.assertEqual(len(l.actions),1);self.assertEqual(b.starts,1)
 def test_exhausted_review_budget_before_lead(self):
  app,a,l,b,gid,t,d=self.blocked();self.grant_review(app,gid,t,d,lead_limit=1)
  from test_goal_loop_maintenance import receipt
  receipt(a,'lead',{'goal_id':gid})
  out,s=self.drive(app,gid);self.assertEqual(out['state'],'awaiting_owner');self.assertEqual(len(l.actions),1)
 def test_material_missing_blocks_still_prevents_paid_review(self):
  app,a,l,b,gid,t,d=self.blocked();self.grant_review(app,gid,t,d)
  # Change contract only in this synthetic test; missing required blocks gate
  # must remain authoritative even though native evidence is valid.
  with app.layer._rmw():
   acc=app.layer.goals[gid]['goal']['acceptance'];acc['review_materials_required']=True;acc['required_blocks']=[];app.layer._persist_unlocked()
  out,s=self.drive(app,gid);self.assertEqual(len(l.actions),1);self.assertNotEqual(s['tasks'][0]['status'],'succeeded')

 def test_actual_review_gate_packs_full_source_and_complete_delta_losslessly(self):
  app,a,l,b,gid=self.make(answers=('partial',))
  with app.layer._rmw():
   acc=app.layer.goals[gid]['goal']['acceptance'];acc.update(auto_review_materials=True,complete_delta_required=True,material_capacity_bytes=200000,delta_capacity_bytes=400000);app.layer._persist_unlocked()
  original=b._start_run_locked
  def with_source(**kw):
   ret=original(**kw);(Path(kw['root'])/'RESULT.md').write_text('SYNTHETIC SOURCE CONTEXT\n'*600);return ret
  b._start_run_locked=with_source
  out,s=self.drive(app,gid);t=s['tasks'][0];d=s['pending_decisions'][0]
  l.answers=['pass'];self.grant_review(app,gid,t,d);app.coordinator.process_goal(gid)
  from framework.review_material_codec import unpack_material
  packed=l.actions[-1];self.assertEqual(packed['encoding'],'collab-review-content-v1');expanded=unpack_material(packed)
  for field in ('diff','source_packet','complete_delta','scope_rule','changed_files','out_of_scope_changes'):
   self.assertEqual(expanded[field],l.actions[0][field])
  self.assertIn('owner_review_recovery',expanded)
  metrics={'synthetic_only':True,'before_bytes':len(json.dumps(expanded,ensure_ascii=False).encode()),'after_bytes':len(json.dumps(packed,ensure_ascii=False).encode()),'lossless_roundtrip':True,'no_actual_portal_wire_read':True}
  self.assertLess(metrics['after_bytes'],metrics['before_bytes'])
  (ROOT.parent/'review-recovery-evidence/MATERIAL-METRICS.json').write_text(json.dumps(metrics,indent=2))

 def test_recovery_material_gate_refuses_without_lead_even_after_grant(self):
  app,a,l,b,gid=self.make(answers=('partial',))
  with app.layer._rmw():
   app.layer.goals[gid]['goal']['acceptance']['auto_review_materials']=True;app.layer._persist_unlocked()
  out,s=self.drive(app,gid);t=s['tasks'][0];self.grant_review(app,gid,t,s['pending_decisions'][0])
  with patch('framework.agy_review.build_snapshot_material_packet',return_value={'is_blocked':True,'blocked_reason':'synthetic source unavailable','missing_blocks':[{'path':'RESULT.md'}]}):out,s=self.drive(app,gid)
  self.assertEqual(len(l.actions),1);self.assertNotEqual(s['tasks'][0]['status'],'succeeded');self.assertTrue(s['tasks'][0]['result']['material_blocked'])
 def test_result_modified_during_lead_does_not_apply_pass(self):
  app,a,l,b,gid,t,d=self.blocked();self.grant_review(app,gid,t,d);l.answers=['pass'];original=l.decide
  def change_during_review(request,**kw):
   answer=original(request,**kw);Path(t['result']['artifacts'][0]).write_text('synthetic concurrent mutation');return answer
  l.decide=change_during_review
  out,s=self.drive(app,gid);self.assertEqual(out['state'],'awaiting_owner');self.assertNotEqual(s['tasks'][0]['status'],'succeeded');self.assertEqual(len(l.actions),2)

del Base
if __name__=='__main__':unittest.main(verbosity=2)
