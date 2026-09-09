import copy
import json
import tempfile
import time
import unittest
from pathlib import Path

from win_collab.core import Engine, Store, contained, validate_charter


def charter():
    return {'goal':'Write result', 'must':['Stay in workdir'], 'must_not':['Read secrets'],
            'artifacts':['a.txt','b.txt'], 'acceptance':'Both files contain verified output',
            'timeout_sec':60,'max_redos':1}


class FakeClient:
    base='http://127.0.0.1:4397'
    def __init__(self):
        self.pending=[]; self.questions=[]; self.status={}; self.messages={}; self.calls=[]
        self.fail_reply=False; self.ack_policy=True; self.abort_stays_busy=False; self.count=0

    def call(self, method, path, body=None, workspace=None):
        self.calls.append((method,path,copy.deepcopy(body),workspace))
        if path=='/session' and method=='POST':
            self.count+=1; sid='ses-'+str(self.count)
            self.status[sid]={'type':'busy'}; self.messages[sid]=[]
            return {'id':sid,'permission':body.get('permission') if self.ack_policy else None}
        if path.endswith('/prompt_async'):
            sid=path.split('/')[2]
            self.messages[sid].append({'info':{'role':'user'}})
            self.status[sid]={'type':'busy'}
            return None
        if path=='/permission': return copy.deepcopy(self.pending)
        if path=='/question': return copy.deepcopy(self.questions)
        if path=='/session/status': return copy.deepcopy(self.status)
        if path.endswith('/message'): return copy.deepcopy(self.messages[path.split('/')[2]])
        if path.startswith('/permission/') and path.endswith('/reply'):
            if self.fail_reply: raise RuntimeError('HTTP 500')
            pid=path.split('/')[2]
            self.pending=[p for p in self.pending if p['id']!=pid]
            return True
        if path.endswith('/abort'):
            if not self.abort_stays_busy:
                self.status[path.split('/')[2]]={'type':'idle'}
            return True
        raise AssertionError((method,path))


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name))
        self.client=FakeClient()
        self.engine=Engine(self.store,self.client)

    def tearDown(self):
        self.store.db.close(); self.tmp.cleanup()

    def start(self):
        j=self.engine.submit(charter()); self.engine.tick(); return self.store.get(j['id'])

    def rescan(self, job):
        with self.store.transaction():
            j=self.store.get(job['id']);j['next_scan']=0;self.store.save(j)
        self.engine.tick()

    def pending(self,j,pid='p1',path='a.txt'):
        self.client.pending.append({'id':pid,'sessionID':j['session_id'],'permission':'edit','patterns':[str(Path(j['workspace'])/path)],'metadata':{'diff':'example'}})
        self.rescan(j)
        return self.store.inbox()[0]

    def answer(self,p,choice='once',**extra):
        return self.engine.decide({'request_id':p['request_id'],'context_hash':p['context_hash'],
                                  'decision':choice,'reason':'Independently checked task scope',**extra})

    def deliver(self,j,all_files=True,parts=None):
        for name in ('a.txt','b.txt') if all_files else ('a.txt',):
            (Path(j['workspace'])/name).write_text('verified\n')
        self.client.status[j['session_id']]={'type':'idle'}
        self.client.messages[j['session_id']].append(
            {'info':{'role':'assistant','finish':'stop'},'parts':parts or []})
        self.rescan(j)
        return self.store.inbox()[0]

    def test_foreign_permissions_are_never_touched(self):
        j=self.start()
        self.client.pending=[{'id':'foreign','sessionID':'other','patterns':['.env']}]
        self.rescan(j)
        self.assertFalse(self.store.inbox())
        self.assertFalse(any('/permission/foreign/' in c[1] for c in self.client.calls))

    def test_empty_scans_do_not_call_lead(self):
        j=self.start();self.rescan(j)
        self.assertEqual(self.store.get(j['id'])['lead_requests'],0)

    def test_request_id_dedupe_and_serial_same_path(self):
        j=self.start();p=self.pending(j)
        self.rescan(j);self.assertEqual(len(self.store.inbox()),1)
        self.answer(p)
        p2=self.pending(j,'p2')
        self.assertNotEqual(p['request_id'],p2['request_id'])
        self.assertEqual(p2['charter'],j['charter'])
        self.answer(p2)

    def test_malformed_or_stale_decision_never_grants(self):
        j=self.start();p=self.pending(j)
        with self.assertRaises(ValueError):self.answer(p,'always')
        with self.assertRaises(ValueError):self.answer({**p,'context_hash':'old'})
        self.assertEqual(len(self.client.pending),1)

    def test_failed_reply_not_marked_handled(self):
        j=self.start();p=self.pending(j);self.client.fail_reply=True
        with self.assertRaises(RuntimeError):self.answer(p)
        self.assertFalse(self.store.get(j['id'])['handled'])
        self.assertEqual(len(self.store.inbox()),1)

    def test_requires_all_artifacts_and_explicit_review(self):
        j=self.start();p=self.deliver(j,False)
        self.assertEqual(self.store.get(j['id'])['state'],'awaiting_review')
        with self.assertRaises(ValueError):self.answer(p,'pass')

    def test_acceptance_binds_hash_and_idle(self):
        j=self.start();p=self.deliver(j)
        (Path(j['workspace'])/'a.txt').write_text('changed')
        with self.assertRaises(ValueError):self.answer(p,'pass')
        (Path(j['workspace'])/'a.txt').write_text('verified\n')
        self.client.status[j['session_id']]={'type':'busy'}
        with self.assertRaises(ValueError):self.answer(p,'pass')
        self.client.status[j['session_id']]={'type':'idle'}
        self.answer(p,'pass');self.assertEqual(self.store.get(j['id'])['state'],'passed')

    def test_redo_uses_same_approval_and_deadline(self):
        j=self.start();p=self.deliver(j);deadline=j['deadline']
        self.answer(p,'fail')
        j=self.store.get(j['id']);self.assertEqual(j['deadline'],deadline)
        self.pending(j,'redo')
        self.assertFalse(any(c[1]=='/permission/redo/reply' for c in self.client.calls))
        prompts=[c[2] for c in self.client.calls if c[1].endswith('/prompt_async')]
        self.assertEqual(len(prompts),2)
        self.assertNotEqual(prompts[0]['queryID'],prompts[1]['queryID'])

    def test_timeout_cannot_be_success_even_with_artifacts(self):
        j=self.start();(Path(j['workspace'])/'a.txt').write_text('partial')
        with self.store.transaction():
            j['deadline']=time.time()-1;self.store.save(j)
        self.engine.tick()
        self.assertEqual(self.store.get(j['id'])['state'],'timed_out')
        self.assertTrue(any(c[1].endswith('/abort') for c in self.client.calls))

    def test_restart_keeps_sessions_and_inbox(self):
        j=self.start();p=self.pending(j)
        self.store.db.close();self.store=Store(self.tmp.name)
        self.engine=Engine(self.store,self.client);self.engine.tick()
        self.assertEqual(self.client.count,1)
        self.assertEqual(self.store.inbox()[0]['request_id'],p['request_id'])

    def test_ambiguous_start_is_not_replayed(self):
        j=self.engine.submit(charter())
        self.engine.start_journal(j,{'stage':'creating'})
        self.engine.tick()
        self.assertEqual(self.client.count,0)
        self.assertEqual(self.store.get(j['id'])['state'],'failed')

    def test_three_slots_and_isolated_workspaces(self):
        jobs=[self.engine.submit(charter()) for _ in range(4)]
        self.engine.tick()
        self.assertEqual(self.client.count,3)
        self.assertEqual(len({j['workspace'] for j in jobs}),4)
        self.assertEqual(self.store.get(jobs[3]['id'])['state'],'queued')

    def test_unacknowledged_policy_never_dispatches(self):
        self.client.ack_policy=False;j=self.start()
        self.assertEqual(j['state'],'failed')
        self.assertFalse(any(c[1].endswith('/prompt_async') for c in self.client.calls))

    def test_path_traversal_drive_ads_rejected(self):
        for path in ('../other/a.txt','C:/secret','a.txt:secret','a\\b','/absolute'):
            with self.subTest(path=path),self.assertRaises(ValueError):contained(self.tmp.name,path)

    def test_all_permission_patterns_reach_lead(self):
        j=self.start();p=self.pending(j)
        self.assertIn('metadata',p['payload'])
        self.assertIn('patterns',p['payload'])

    def test_secret_prefilter_never_lead(self):
        j=self.start();self.client.pending=[{'id':'secret','sessionID':j['session_id'],'patterns':['C:/x/.env']}]
        self.rescan(j)
        self.assertFalse(self.store.inbox())
        self.assertEqual(self.store.get(j['id'])['handled'],['secret'])

    def test_external_directory_escape_is_rejected_before_lead(self):
        j=self.start()
        parent=str(Path(j['workspace']).parent).replace('\\','/')
        self.client.pending=[{'id':'escape','sessionID':j['session_id'],
                              'permission':'external_directory','patterns':[parent+'/*']}]
        self.rescan(j)
        self.assertFalse(self.store.inbox())
        self.assertEqual(self.store.get(j['id'])['handled'],['escape'])
        self.assertTrue(any(c[1]=='/permission/escape/reply' and c[2]=={'reply':'reject'}
                            for c in self.client.calls))

    def test_completed_forbidden_tool_prevents_acceptance(self):
        c=charter();c['forbidden_tools']=['powershell']
        j=self.engine.submit(c);self.engine.tick();j=self.store.get(j['id'])
        tool={'type':'tool','tool':'powershell','state':{'status':'completed','input':{'command':'Get-Location'}}}
        p=self.deliver(j,parts=[tool])
        self.assertEqual(p['payload']['policy_violations'],[{'tool':'powershell','status':'completed'}])
        with self.assertRaisesRegex(ValueError,'forbidden tools'):self.answer(p,'pass')

    def test_minimum_permission_approvals_prevents_zero_approval_pass(self):
        c=charter();c['min_approved_permissions']=1
        j=self.engine.submit(c);self.engine.tick();j=self.store.get(j['id'])
        p=self.deliver(j)
        self.assertEqual(p['payload']['approved_permissions'],0)
        with self.assertRaisesRegex(ValueError,'Required permission approvals'):self.answer(p,'pass')

    def test_once_decision_counts_approved_permission(self):
        c=charter();c['min_approved_permissions']=1
        j=self.engine.submit(c);self.engine.tick();j=self.store.get(j['id'])
        p=self.pending(j);self.answer(p,'once')
        self.assertEqual(self.store.get(j['id'])['approved_permissions'],1)

    def test_cancel_invalidates_outstanding_decisions(self):
        j=self.start();p=self.pending(j);self.engine.cancel(j['id'])
        with self.assertRaises(ValueError):self.answer(p)

    def test_abort_ack_does_not_finish_until_remote_is_idle(self):
        j=self.start();self.client.abort_stays_busy=True
        result=self.engine.cancel(j['id'])
        self.assertEqual(result['state'],'stopping')
        aborts=[c for c in self.client.calls if c[1].endswith('/abort')]
        self.assertEqual(len(aborts),1)
        self.client.status[j['session_id']]={'type':'idle'}
        self.engine.tick()
        self.assertEqual(self.store.get(j['id'])['state'],'cancelled')
        self.assertEqual(len([c for c in self.client.calls if c[1].endswith('/abort')]),1)

    def test_same_port_new_backend_cannot_receive_old_decision(self):
        self.client.instance_id='first'
        j=self.start();p=self.pending(j)
        self.client.instance_id='second'
        with self.assertRaises(ValueError):self.answer(p)
        with self.assertRaises(ValueError):self.engine.cancel(j['id'])
        self.assertEqual(len(self.client.pending),1)

    def test_question_handled_elsewhere_does_not_stick(self):
        j=self.start()
        self.client.questions=[{'id':'q1','sessionID':j['session_id'],'questions':[]}]
        self.rescan(j);self.assertEqual(len(self.store.inbox()),1)
        self.client.questions=[]
        self.rescan(j);self.assertEqual(self.store.inbox(),[])
        self.assertEqual(self.store.get(j['id'])['state'],'running')


if __name__=='__main__':unittest.main()
