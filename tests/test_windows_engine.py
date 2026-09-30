import copy
import hashlib
import json
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from desktop_lock_isolation import install_desktop_lock_isolation
from execution_backend.windows_supervised_v1 import WindowsSupervisedExecutionBackend
from win_collab.core import (
    ArtifactContaminatedError,
    Engine,
    Store,
    contained,
    digest,
    external_directory_scope,
    validate_charter,
)


def charter():
    return {'goal':'Write result', 'must':['Stay in workdir'], 'must_not':['Read secrets'],
            'artifacts':['a.txt','b.txt'], 'acceptance':'Both files contain verified output',
            'timeout_sec':60,'max_redos':1}


def install_charter(source):
    source = Path(source)
    return {
        'goal': 'Install a pinned test MSI after a separate lead action approval',
        'must': ['Prepare an exact action request before execution'],
        'must_not': ['Use system tools before approval', 'Use the network'],
        'artifacts': ['install-result.json'],
        'acceptance': 'The result records the bounded execution outcome',
        'timeout_sec': 120,
        'max_lead_requests': 4,
        'max_redos': 0,
        'task_kind': 'system_install',
        'action_request_artifact': 'system-action-request.json',
        'system_action': {
            'type': 'msi_install',
            'elevation': 'runas',
            'package': {
                'source': str(source.resolve()),
                'filename': 'test-package.msi',
                'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            },
            'arguments': ['/qn', '/norestart', 'INSTALLPRINTER=N'],
            'allowed_effects': ['install_files', 'service_change', 'firewall_change'],
        },
        'user_authorized_effects': ['service_change', 'firewall_change'],
        'rollback': 'Uninstall the test product and remove its service and firewall rules.',
    }


class FakeClient:
    base='http://127.0.0.1:4397'
    def __init__(self):
        self.pending=[]; self.questions=[]; self.status={}; self.messages={}; self.calls=[]
        self.fail_reply=False; self.fail_prompt=False; self.ack_policy=True
        self.abort_stays_busy=False; self.count=0

    def call(self, method, path, body=None, workspace=None):
        self.calls.append((method,path,copy.deepcopy(body),workspace))
        if path=='/session' and method=='POST':
            self.count+=1; sid='ses-'+str(self.count)
            self.status[sid]={'type':'busy'}; self.messages[sid]=[]
            return {'id':sid,'permission':body.get('permission') if self.ack_policy else None}
        if path.endswith('/prompt_async'):
            if self.fail_prompt: raise RuntimeError('ambiguous prompt failure')
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
        install_desktop_lock_isolation(self)
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

    def test_file_task_prompt_requires_relative_artifact_names(self):
        j=self.start()
        text=self.engine.prompt(j)['parts'][0]['text']
        self.assertIn('relative names listed in CHARTER.artifacts', text)
        self.assertIn('Do not retype or invent absolute directory paths', text)
        self.assertIn(j['workspace'], text)

    def test_foreign_permissions_are_never_touched(self):
        j=self.start()
        self.client.pending=[{'id':'foreign','sessionID':'other','patterns':['.env']}]
        self.rescan(j)
        self.assertFalse(self.store.inbox())
        self.assertFalse(any('/permission/foreign/' in c[1] for c in self.client.calls))

    def test_empty_scans_do_not_call_lead(self):
        j=self.start();self.rescan(j)
        self.assertEqual(self.store.get(j['id'])['lead_requests'],0)

    def test_worker_error_keeps_safe_diagnostics_only(self):
        j=self.start()
        self.client.status[j['session_id']]={'type':'idle'}
        self.client.messages[j['session_id']].append({'info':{
            'role':'assistant','finish':'error','error':{
                'name':'APIError','data':{'statusCode':401,'message':'token=secret-value; 40108 invalid token'},
                'headers':{'authorization':'secret-value'},
            }}})
        self.rescan(j)
        failed=self.store.get(j['id'])
        self.assertEqual(failed['state'],'failed')
        self.assertIn('name=APIError',failed['error'])
        self.assertIn('statusCode=401',failed['error'])
        self.assertIn('message=token=<redacted>; 40108 invalid token',failed['error'])
        self.assertNotIn('secret-value',failed['error'])

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

    def test_hash_pinned_repository_input_can_receive_once_approval(self):
        source=Path(self.tmp.name)/'approved-input.json';source.write_text('{"value":1}',encoding='utf-8')
        c=charter();c['external_inputs']=[{
            'path':str(source.resolve()),
            'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        }];c['min_approved_permissions']=1
        with mock.patch('win_collab.core.REPO',Path(self.tmp.name)):
            j=self.engine.submit(c)
        self.engine.tick();j=self.store.get(j['id'])
        copy=j['charter']['external_inputs'][0]['path']
        self.client.pending=[{'id':'external-input','sessionID':j['session_id'],
                              'permission':'external_directory','patterns':[str(Path(copy).parent/'*')],
                              'metadata':{'filepath':copy}}]
        self.rescan(j)
        p=self.store.inbox()[0]
        self.answer(p,'once')
        self.assertEqual(self.store.get(j['id'])['approved_permissions'],1)

    def test_changed_external_input_cannot_be_approved(self):
        source=Path(self.tmp.name)/'approved-input.json';source.write_text('first',encoding='utf-8')
        c=charter();c['external_inputs']=[{
            'path':str(source.resolve()),
            'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        }]
        with mock.patch('win_collab.core.REPO',Path(self.tmp.name)):
            j=self.engine.submit(c)
        self.engine.tick();j=self.store.get(j['id']);source.write_text('changed',encoding='utf-8')
        self.client.pending=[{'id':'changed-input','sessionID':j['session_id'],
                              'permission':'external_directory','patterns':[str(source.parent/'*')],
                              'metadata':{'filepath':str(source.resolve())}}]
        self.rescan(j)
        self.assertFalse(self.store.inbox())
        self.assertEqual(self.store.get(j['id'])['handled'],['changed-input'])

    def test_completed_forbidden_tool_prevents_acceptance(self):
        c=charter();c['forbidden_tools']=['powershell']
        j=self.engine.submit(c);self.engine.tick();j=self.store.get(j['id'])
        tool={'type':'tool','tool':'powershell','state':{'status':'completed','input':{'command':'Get-Location'}}}
        p=self.deliver(j,parts=[tool])
        self.assertEqual(p['payload']['policy_violations'],[{'tool':'powershell','status':'completed'}])
        with self.assertRaisesRegex(ValueError,'Policy violations'):self.answer(p,'pass')

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

    def prepare_install(self):
        source=Path(self.tmp.name)/'source.msi';source.write_bytes(b'fixed-msi-fixture')
        with mock.patch('win_collab.core.REPO',Path(self.tmp.name)):
            j=self.engine.submit(install_charter(source))
        self.engine.tick();j=self.store.get(j['id'])
        proposal={
            'type':'msi_install',
            'elevation':'runas',
            'package':{'filename':'test-package.msi',
                       'sha256':hashlib.sha256(source.read_bytes()).hexdigest()},
            'arguments':['/qn','/norestart','INSTALLPRINTER=N'],
            'allowed_effects':['install_files','service_change','firewall_change'],
        }
        (Path(j['workspace'])/'system-action-request.json').write_text(
            json.dumps(proposal),encoding='utf-8')
        self.client.status[j['session_id']]={'type':'idle'}
        self.client.messages[j['session_id']].append(
            {'info':{'role':'assistant','finish':'stop'},'parts':[
                {'type':'tool','tool':'write','state':{'status':'completed','input':{}}}
            ]})
        with mock.patch('win_collab.core.REPO',Path(self.tmp.name)):
            self.rescan(j)
        return source,self.store.get(j['id']),self.store.inbox()[0]

    def test_system_action_approval_stages_package_then_resumes_worker(self):
        source,j,p=self.prepare_install()
        self.assertEqual(p['kind'],'system_action')
        self.assertFalse((Path(j['workspace'])/'test-package.msi').exists())
        with mock.patch('win_collab.core.REPO',Path(self.tmp.name)):
            result=self.answer(p,'approve')
        staged=Path(j['workspace'])/'test-package.msi'
        self.assertEqual(staged.read_bytes(),source.read_bytes())
        self.assertTrue(result['action_dispatched'])
        prompts=[c for c in self.client.calls if c[1].endswith('/prompt_async')]
        self.assertEqual(len(prompts),2)
        execute_text=prompts[-1][2]['parts'][0]['text']
        self.assertIn('APPROVED_ACTION',execute_text)
        self.assertIn('CHARTER:',execute_text)
        self.assertIn(j['charter']['goal'],execute_text)
        self.assertNotIn(str(source),execute_text)

    def test_system_install_review_requires_bound_runas_invocation(self):
        _source,j,p=self.prepare_install()
        with mock.patch('win_collab.core.REPO',Path(self.tmp.name)):
            self.answer(p,'approve')
        (Path(j['workspace'])/'install-result.json').write_text('{}',encoding='utf-8')
        self.client.status[j['session_id']]={'type':'idle'}
        self.client.messages[j['session_id']].append(
            {'info':{'role':'assistant','finish':'stop'},'parts':[
                {'type':'tool','tool':'powershell','state':{
                    'status':'completed','input':{'command':'Start-Process msiexec.exe -Wait -PassThru'}}}
            ]})
        self.rescan(j);review=self.store.inbox()[0]
        self.assertTrue(review['payload']['policy_violations'])
        with self.assertRaisesRegex(ValueError,'Policy violations'):
            self.answer(review,'pass')

    def test_changed_system_action_request_invalidates_approval(self):
        _source,j,p=self.prepare_install()
        request=Path(j['workspace'])/'system-action-request.json'
        request.write_text(request.read_text()+' ',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'changed after review'):
            self.answer(p,'approve')
        self.assertFalse((Path(j['workspace'])/'test-package.msi').exists())

    def test_changed_msi_invalidates_action_approval(self):
        source,j,p=self.prepare_install();source.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'MSI package changed'):
            self.answer(p,'approve')
        self.assertFalse((Path(j['workspace'])/'test-package.msi').exists())

    def test_ambiguous_system_action_dispatch_is_not_replayed(self):
        _source,j,p=self.prepare_install();self.client.fail_prompt=True
        with mock.patch('win_collab.core.REPO',Path(self.tmp.name)),self.assertRaisesRegex(
                RuntimeError,'ambiguous prompt failure'):
            self.answer(p,'approve')
        self.client.fail_prompt=False
        with self.assertRaisesRegex(ValueError,'refusing automatic replay'):
            self.answer(p,'approve')
        action_prompts=[c for c in self.client.calls
                        if c[1].endswith('/prompt_async') and
                        'APPROVED_ACTION' in c[2]['parts'][0]['text']]
        self.assertEqual(len(action_prompts),1)

    def test_system_tool_before_action_approval_fails_job(self):
        source=Path(self.tmp.name)/'source.msi';source.write_bytes(b'fixed-msi-fixture')
        with mock.patch('win_collab.core.REPO',Path(self.tmp.name)):
            j=self.engine.submit(install_charter(source))
        self.engine.tick();j=self.store.get(j['id'])
        self.client.status[j['session_id']]={'type':'idle'}
        self.client.messages[j['session_id']].append(
            {'info':{'role':'assistant','finish':'stop'},'parts':[
                {'type':'tool','tool':'powershell','state':{'status':'completed','input':{}}}
            ]})
        self.rescan(j)
        self.assertEqual(self.store.get(j['id'])['state'],'failed')
        self.assertFalse((Path(j['workspace'])/'test-package.msi').exists())

    def test_snapshot_contamination_blocks_pass_until_opt_out(self):
        j=self.start()
        dirty='hello decision\n\nAI生成\n'+('\u200b\u200d'*3)
        (Path(j['workspace'])/'a.txt').write_text(dirty, encoding='utf-8')
        (Path(j['workspace'])/'b.txt').write_text('verified\n', encoding='utf-8')
        self.client.status[j['session_id']]={'type':'idle'}
        self.client.messages[j['session_id']].append(
            {'info':{'role':'assistant','finish':'stop'},'parts':[]})
        self.rescan(j)
        p=self.store.inbox()[0]
        dirty_scan=p['payload']['artifacts']['a.txt']['contamination']
        clean_scan=p['payload']['artifacts']['b.txt']['contamination']
        self.assertTrue(dirty_scan['contaminated'])
        self.assertEqual(dirty_scan['aigc_marks']['AI生成'], 1)
        self.assertEqual(dirty_scan['invisible']['U+200B'], 3)
        self.assertEqual(dirty_scan['invisible']['U+200D'], 3)
        self.assertFalse(clean_scan['contaminated'])
        self.assertEqual(clean_scan['encoding'], 'utf-8')
        with self.assertRaises(ArtifactContaminatedError) as cm:
            self.answer(p, 'pass')
        self.assertIsInstance(cm.exception, ValueError)
        self.assertTrue(issubclass(ArtifactContaminatedError, ValueError))
        self.assertIn('Artifact content contaminated', str(cm.exception))
        self.assertIn('CONTAMINATED a.txt', cm.exception.summary)
        self.assertEqual(cm.exception.findings['a.txt']['aigc_marks']['AI生成'], 1)
        self.assertIn('b.txt', cm.exception.findings)
        self.assertEqual(self.store.get(j['id'])['state'], 'awaiting_review')
        self.answer(p, 'fail')
        redone=self.store.get(j['id'])
        self.assertEqual(redone['state'], 'running')
        self.assertEqual(redone['redos'], 1)

    def test_allow_aigc_marks_permits_contaminated_pass(self):
        c=charter(); c['allow_aigc_marks']=True
        j=self.engine.submit(c); self.engine.tick(); j=self.store.get(j['id'])
        dirty='AI 生成\n'+'\u200b'
        for name in ('a.txt', 'b.txt'):
            (Path(j['workspace'])/name).write_text(dirty, encoding='utf-8')
        self.client.status[j['session_id']]={'type':'idle'}
        self.client.messages[j['session_id']].append(
            {'info':{'role':'assistant','finish':'stop'},'parts':[]})
        self.rescan(j)
        p=self.store.inbox()[0]
        self.assertTrue(p['payload']['artifacts']['a.txt']['contamination']['contaminated'])
        self.answer(p, 'pass')
        self.assertEqual(self.store.get(j['id'])['state'], 'passed')

    def test_allow_aigc_marks_must_be_boolean(self):
        c=charter(); c['allow_aigc_marks']='true'
        with self.assertRaisesRegex(ValueError, 'allow_aigc_marks'):
            validate_charter(c)
        c['allow_aigc_marks']=True
        self.assertIs(validate_charter(c)['allow_aigc_marks'], True)
        bom=self.start()
        payload=b'\xef\xbb\xbfverified\n'
        for name in ('a.txt', 'b.txt'):
            (Path(bom['workspace'])/name).write_bytes(payload)
        self.client.status[bom['session_id']]={'type':'idle'}
        self.client.messages[bom['session_id']].append(
            {'info':{'role':'assistant','finish':'stop'},'parts':[]})
        self.rescan(bom)
        review=self.store.inbox()[0]
        self.assertFalse(review['payload']['artifacts']['a.txt']['contamination']['contaminated'])
        self.assertEqual(review['payload']['artifacts']['a.txt']['contamination']['encoding'], 'utf-8-bom')
        self.answer(review, 'pass')
        self.assertEqual(self.store.get(bom['id'])['state'], 'passed')

    def _events(self, job, kind):
        rows = self.store.db.execute(
            'SELECT data FROM events WHERE job_id=? AND kind=? ORDER BY seq',
            (job['id'], kind),
        ).fetchall()
        return [json.loads(row['data']) for row in rows]

    def _pinned_charter(self, name, text):
        source = Path(self.tmp.name) / name
        source.write_text(text, encoding='utf-8')
        body = charter()
        digest_hex = hashlib.sha256(source.read_bytes()).hexdigest()
        body['external_inputs'] = [{'path': str(source.resolve()), 'sha256': digest_hex}]
        return body, source, digest_hex

    def test_submit_isolates_external_inputs_and_rewrites_charter(self):
        first, source_a, sha_a = self._pinned_charter('ext-input.txt', 'alpha\n')
        source_b = Path(self.tmp.name) / 'notes.txt'
        source_b.write_text('beta\n', encoding='utf-8')
        sha_b = hashlib.sha256(source_b.read_bytes()).hexdigest()
        first['external_inputs'].append({'path': str(source_b.resolve()), 'sha256': sha_b})
        original = copy.deepcopy(first)
        with mock.patch('win_collab.core.REPO', Path(self.tmp.name)):
            job = self.engine.submit(first)
        stored = self.store.get(job['id'])
        pins = stored['charter']['external_inputs']
        self.assertEqual([item['sha256'] for item in pins], [sha_a, sha_b])
        self.assertEqual(stored['charter']['external_inputs_source'], [
            {'path': str(source_a.resolve()), 'sha256': sha_a},
            {'path': str(source_b.resolve()), 'sha256': sha_b},
        ])
        self.assertEqual(stored['charter_hash'], digest(stored['charter']))
        self.assertNotEqual(stored['charter_hash'], digest(original))
        for index, (pin, source, expected) in enumerate((
                (pins[0], source_a, sha_a), (pins[1], source_b, sha_b))):
            copy_path = Path(pin['path'])
            self.assertEqual(copy_path.name, source.name)
            self.assertEqual(copy_path.parent.name, str(index))
            self.assertEqual(copy_path.parent.parent.name, job['id'])
            self.assertEqual(copy_path.parent.parent.parent.name, 'external-inputs')
            self.assertEqual(hashlib.sha256(copy_path.read_bytes()).hexdigest(), expected)
            self.assertNotEqual(copy_path.resolve(), source.resolve())
        text = self.engine.prompt(stored)['parts'][0]['text']
        self.assertIn('Read external inputs ONLY at the exact copy paths listed in CHARTER.external_inputs', text)
        self.assertIn('not paths mentioned elsewhere', text)
        self.assertIn(pins[0]['path'], text)
        self.assertNotIn('external_inputs_source', text)
        self.assertNotIn(str(source_a.resolve()), text)

    def test_external_input_copy_hash_mismatch_fails_submit_and_leaves_no_dir(self):
        body, _source, _sha = self._pinned_charter('ext-input.txt', 'pinned\n')
        with mock.patch('win_collab.core.REPO', Path(self.tmp.name)), \
                mock.patch('win_collab.core.file_sha256', return_value='0' * 64), \
                self.assertRaisesRegex(ValueError, 'copy hash mismatch'):
            self.engine.submit(body)
        self.assertEqual(self.store.jobs(), [])
        root = Path(self.store.home) / 'external-inputs'
        if root.exists():
            self.assertEqual(list(root.iterdir()), [])

    def test_hard_reject_accepts_copy_path_and_rejects_original(self):
        body, source, _sha = self._pinned_charter('ext-input.txt', 'pinned\n')
        with mock.patch('win_collab.core.REPO', Path(self.tmp.name)):
            job = self.engine.submit(body)
        self.engine.tick()
        job = self.store.get(job['id'])
        copy = job['charter']['external_inputs'][0]['path']
        original = job['charter']['external_inputs_source'][0]['path']
        self.assertEqual(original, str(source.resolve()))
        self.client.pending = [{
            'id': 'original-path', 'sessionID': job['session_id'],
            'permission': 'external_directory', 'patterns': [str(source.parent / '*')],
            'metadata': {'filepath': original},
        }]
        self.rescan(job)
        self.assertFalse(self.store.inbox())
        self.assertEqual(self.store.get(job['id'])['handled'], ['original-path'])
        self.client.pending = [{
            'id': 'copy-path', 'sessionID': job['session_id'],
            'permission': 'external_directory', 'patterns': [str(Path(copy).parent / '*')],
            'metadata': {'filepath': copy},
        }]
        self.rescan(job)
        pending = self.store.inbox()[0]
        self.assertEqual(pending['payload']['metadata']['filepath'], copy)
        self.assertNotIn('scope', pending['payload'])
        self.answer(pending, 'once')
        self.assertEqual(self.store.get(job['id'])['approved_permissions'], 1)

    def test_external_inputs_cleaned_on_pass_and_sweep_is_idempotent(self):
        body, _source, _sha = self._pinned_charter('ext-input.txt', 'pinned\n')
        with mock.patch('win_collab.core.REPO', Path(self.tmp.name)):
            job = self.engine.submit(body)
        self.engine.tick()
        job = self.store.get(job['id'])
        copy = Path(job['charter']['external_inputs'][0]['path'])
        job_dir = copy.parent.parent
        self.assertTrue(copy.is_file())
        review = self.deliver(job)
        self.answer(review, 'pass')
        done = self.store.get(job['id'])
        self.assertEqual(done['state'], 'passed')
        self.assertFalse(job_dir.exists())
        self.assertEqual(len(self._events(done, 'external_inputs_cleaned')), 1)
        self.engine.tick()
        self.assertEqual(len(self._events(done, 'external_inputs_cleaned')), 1)
        job_dir.mkdir(parents=True)
        (job_dir / 'leftover.txt').write_text('x', encoding='utf-8')
        self.engine.tick()
        self.assertFalse(job_dir.exists())
        self.assertEqual(len(self._events(done, 'external_inputs_cleaned')), 2)
        self.engine.tick()
        self.assertEqual(len(self._events(done, 'external_inputs_cleaned')), 2)

    def test_external_inputs_cleaned_on_fail_and_cancel_without_following_links(self):
        body, _source, _sha = self._pinned_charter('ext-input.txt', 'pinned\n')
        with mock.patch('win_collab.core.REPO', Path(self.tmp.name)):
            job = self.engine.submit(body)
        self.engine.tick()
        job = self.store.get(job['id'])
        copy = Path(job['charter']['external_inputs'][0]['path'])
        outside = Path(self.tmp.name) / 'outside.txt'
        outside.write_text('secret', encoding='utf-8')
        (copy.parent / 'link.txt').symlink_to(outside)
        self.client.status[job['session_id']] = {'type': 'idle'}
        self.client.messages[job['session_id']].append(
            {'info': {'role': 'assistant', 'finish': 'error', 'error': {'message': 'boom'}}})
        self.rescan(job)
        failed = self.store.get(job['id'])
        self.assertEqual(failed['state'], 'failed')
        self.assertFalse(copy.parent.parent.exists())
        self.assertEqual(outside.read_text(encoding='utf-8'), 'secret')
        self.assertTrue(self._events(failed, 'external_inputs_cleaned'))

        body, _source, _sha = self._pinned_charter('other-input.txt', 'other\n')
        with mock.patch('win_collab.core.REPO', Path(self.tmp.name)):
            queued = self.engine.submit(body)
        queued = self.store.get(queued['id'])
        copy = Path(queued['charter']['external_inputs'][0]['path'])
        job_dir = copy.parent.parent
        sentinel = Path(self.tmp.name) / 'sentinel-target'
        sentinel.mkdir()
        (sentinel / 'keep.txt').write_text('keep', encoding='utf-8')
        backup = Path(self.tmp.name) / 'backup-inputs'
        job_dir.rename(backup)
        job_dir.symlink_to(sentinel, target_is_directory=True)
        self.engine.cancel(queued['id'])
        cancelled = self.store.get(queued['id'])
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertFalse(job_dir.is_symlink())
        self.assertEqual((sentinel / 'keep.txt').read_text(encoding='utf-8'), 'keep')
        self.assertTrue((backup / '0' / 'other-input.txt').is_file())
        self.assertTrue(self._events(cancelled, 'external_inputs_cleaned'))

    def test_permission_scope_only_pinned_for_copy_dir(self):
        body, _source, _sha = self._pinned_charter('ext-input.txt', 'pinned\n')
        with mock.patch('win_collab.core.REPO', Path(self.tmp.name)):
            job = self.engine.submit(body)
        self.engine.tick()
        job = self.store.get(job['id'])
        copy = job['charter']['external_inputs'][0]['path']
        pattern = str(Path(copy).parent / '*')
        self.client.pending = [{
            'id': 'scoped', 'sessionID': job['session_id'],
            'permission': 'external_directory', 'patterns': [pattern],
            'metadata': {'filepath': copy},
        }]
        self.rescan(job)
        packet = json.loads(self.store.db.execute(
            'SELECT data FROM requests WHERE job_id=?', (job['id'],)).fetchone()['data'])
        self.assertNotIn('scope', packet)
        self.assertNotIn('scope', packet['payload'])
        backend = WindowsSupervisedExecutionBackend(state_dir=self.tmp.name, client=self.client)
        code, actions = backend.list_pending_actions()
        self.assertEqual(code, 200)
        self.assertEqual(len(actions), 1)
        self.assertNotIn('scope', actions[0]['payload'])
        self.assertEqual(digest(actions[0]['payload']), digest(self.client.pending[0]))
        scope = actions[0]['scope']
        self.assertEqual(len(scope), 1)
        self.assertEqual(scope[0]['pattern'], pattern)
        self.assertEqual(scope[0]['files'], ['ext-input.txt'])
        self.assertTrue(scope[0]['only_pinned'])
        self.assertFalse(scope[0]['truncated'])
        (Path(copy).parent / 'extra.txt').write_text('extra', encoding='utf-8')
        _code, again = backend.list_pending_actions()
        widened = again[0]['scope'][0]
        self.assertFalse(widened['only_pinned'])
        self.assertEqual(sorted(widened['files']), ['ext-input.txt', 'extra.txt'])
        self.assertEqual(digest(again[0]['payload']), digest(self.client.pending[0]))

    def test_external_directory_scope_bounds_double_star(self):
        root = Path(self.tmp.name) / 'tree'
        nested = root / 'sub'
        nested.mkdir(parents=True)
        for index in range(60):
            (nested / f'f{index}.txt').write_text('x', encoding='utf-8')
        scope = external_directory_scope(
            {'permission': 'external_directory', 'patterns': [str(root) + '/**']},
            [],
        )
        self.assertEqual(len(scope), 1)
        self.assertTrue(scope[0]['truncated'])
        self.assertEqual(len(scope[0]['files']), 50)
        self.assertFalse(scope[0]['only_pinned'])
        self.assertIsNone(external_directory_scope({'permission': 'edit', 'patterns': ['*']}, []))

    def test_single_star_scope_counts_nested_files(self):
        root = Path(self.tmp.name) / 'star'
        (root / 'sub').mkdir(parents=True)
        pinned = root / 'only.txt'
        pinned.write_text('p', encoding='utf-8')
        (root / 'sub' / 'hidden.txt').write_text('h', encoding='utf-8')
        scope = external_directory_scope(
            {'permission': 'external_directory', 'patterns': [str(root) + '/*']},
            [{'path': str(pinned), 'sha256': '0' * 64}],
        )
        self.assertFalse(scope[0]['only_pinned'])
        self.assertEqual(sorted(scope[0]['files']), ['only.txt', 'sub/hidden.txt'])

    def test_system_install_requires_explicit_sensitive_effect_authorization(self):
        source=Path(self.tmp.name)/'source.msi';source.write_bytes(b'fixed-msi-fixture')
        c=install_charter(source);c['user_authorized_effects']=[]
        with mock.patch('win_collab.core.REPO',Path(self.tmp.name)),self.assertRaisesRegex(
                ValueError,'explicit user_authorized_effects'):
            self.engine.submit(c)


if __name__=='__main__':unittest.main()
