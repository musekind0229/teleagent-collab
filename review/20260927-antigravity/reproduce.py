"""Offline review probes. Only temporary files/processes; never invokes agy or cmdkey."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend
from execution_backend.run_job_wire import run_antigravity_charter
from execution_backend.agy_account_pool import prepare_antigravity_environ_from_pool

ENV = {k: v for k, v in os.environ.items() if not k.startswith(('AGY_', 'COLLAB_AGY_'))}
real_popen = subprocess.Popen
results = {}

def factory(code):
    def spawn(argv, **kwargs):
        return real_popen([sys.executable, '-c', code], **kwargs)
    return spawn

with tempfile.TemporaryDirectory(prefix='collab-review-') as td:
    root = Path(td)
    # A process that emits a large JSON result cannot exit until its pipe is read.
    be = AntigravityCliExecutionBackend(environ=ENV, timeout_sec=1)
    code = "import json; print(json.dumps({'status':'ok','response':'x'*200000}))"
    with patch('execution_backend.antigravity_cli_v1.subprocess.Popen', factory(code)):
        run = be.start_run(title='large output', directory=str(root))
    time.sleep(.5)
    before = be.observe_run(run['run_id'])
    rec = be._runs[run['run_id']]
    out, err = rec['proc'].communicate(timeout=5)
    results['pipe_deadlock'] = {'busy_before_drain': before['busy'], 'bytes_after_manual_drain': len(out), 'exit_after_drain': rec['proc'].returncode}

    # Observe-only callers do not enforce the configured wall deadline.
    be = AntigravityCliExecutionBackend(environ=ENV, timeout_sec=.1)
    with patch('execution_backend.antigravity_cli_v1.subprocess.Popen', factory('import time; time.sleep(30)')):
        run = be.start_run(title='deadline', directory=str(root))
    time.sleep(.3)
    results['observe_timeout'] = {'timeout_sec': .1, 'elapsed_sec': .3, 'busy': be.observe_run(run['run_id'])['busy']}
    be.cancel(run['run_id'])

    # Review requested, but wrong content still receives success.
    code = "from pathlib import Path; import json; Path('answer.txt').write_text('WRONG'); print(json.dumps({'status':'ok','response':'done'}))"
    charter = {'name':'review-probe','goal':'Write answer.txt', 'done_when':{'artifacts':['answer.txt']}, 'acceptance':'answer.txt must contain exactly RIGHT', 'force_lead_review':True, 'timeout_sec':5}
    with patch('execution_backend.antigravity_cli_v1.subprocess.Popen', factory(code)):
        r = run_antigravity_charter(charter=charter, workdir=root, environ=ENV)
    results['review_ignored'] = {'force_lead_review':True,'actual_content':(root/'answer.txt').read_text(),'ok':r['ok'],'state':r['state']}

    # Separate callers can simultaneously reserve the same supposedly serial profile.
    pool = root/'pool.json'
    pool.write_text(json.dumps({'accounts':[{'id':'A','home':str(root/'homeA'),'state':'available'}]}))
    with patch('execution_backend.agy_account_pool.clear_windows_antigravity_keyring'):
        a = prepare_antigravity_environ_from_pool(pool, base_environ=ENV)
        b = prepare_antigravity_environ_from_pool(pool, base_environ=ENV)
    results['no_account_reservation'] = {'first':a['agy_profile'],'second':b['agy_profile'],'pool_state':json.loads(pool.read_text())['accounts'][0]['state']}
print(json.dumps(results, indent=2))
