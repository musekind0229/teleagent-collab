"""Read-only upstream behavioral probes; fake transport, temporary files only."""
import json
import sys
import tempfile
import time
from pathlib import Path
if len(sys.argv) != 2:
    raise SystemExit('Usage: python reproduce_ac1279a.py PATH_TO_AC1279A_SRC')
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from scheduler import ParallelScheduler, JobState
from state_store import StateStore

out = {}
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    calls = []
    def transport(method, path, *args, **kwargs):
        calls.append((method, path))
        return 500, {'error': 'status endpoint unavailable'}
    def make(name, **kwargs):
        return ParallelScheduler(workspaces_root=root/name/'ws', runs_root=root/name/'runs',
                                 teleagent_call=transport, persist=False, **kwargs)
    charter = {'name':'probe', 'goal':'write exact OK', 'must':['only workdir'],
               'must_not':['no network'], 'done_when':{'artifacts':['out.txt']},
               'force_lead_review':True, 'timeout_sec':300}
    s = make('cancel')
    j = s.enqueue_charter(charter)
    j.session_id = 'session-probe'; j.state = JobState.RUNNING
    s.request_cancel(j.job_id); result = s.effect_cancel(j.job_id)
    out['cancel_without_abort'] = {'result':result, 'transport_calls':list(calls)}
    assert result['cancel_effected'] and not calls
    s.shutdown()

    s = make('status')
    j = s.enqueue_charter({**charter, 'force_lead_review':False})
    j.session_id='session-probe'; j.state=JobState.RUNNING; j.started_at=time.time()
    Path(j.expected_artifacts[0]).write_text('wrong content')
    s.refresh_job_status(j)
    out['status_http_500_accepted'] = {'state':j.state.value, 'ok':j.result.get('ok'), 'calls':list(calls)}
    assert j.state == JobState.DONE
    s.shutdown(); calls.clear()

    store = StateStore(root=root/'store')
    s = make('restore', state_store=store)
    s.persist=True
    j = s.enqueue_charter(charter)
    j.state=JobState.RUNNING; j.session_id='session-probe'; j.started_at=time.time()
    s._persist_job(j)
    s2 = make('restore', state_store=StateStore(root=root/'store'))
    s2.restore_from_store()
    r = s2.jobs[j.job_id]
    out['restore_loses_contract'] = {'charter':r.charter, 'artifacts':r.expected_artifacts,
                                    'force_lead_review':r.force_lead_review}
    assert not r.expected_artifacts and not r.force_lead_review and not r.charter['must_not']
    s.shutdown(); s2.shutdown()

    def unbound_lead(*args):
        return '{"verdict":"pass","reason":"unbound"}', {'verdict':'pass','reason':'unbound'}
    s = make('unbound', call_lead_fn=unbound_lead)
    s._teleagent_call = lambda *args, **kwargs: (200, {})
    j=s.enqueue_charter(charter)
    j.session_id='session-probe'; j.state=JobState.RUNNING; j.started_at=time.time()
    Path(j.expected_artifacts[0]).write_text('OK')
    s.refresh_job_status(j)
    out['unbound_lead_accepted']={'state':j.state.value, 'ok':j.result.get('ok')}
    assert j.state == JobState.DONE
    s.shutdown()
print(json.dumps(out, ensure_ascii=False, indent=2))
