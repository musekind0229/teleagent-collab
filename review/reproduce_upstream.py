"""Read-only reproductions against upstream aa6a9b4 logic; no TeleAgent calls."""
import json
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from scheduler import ParallelScheduler, JobState

with tempfile.TemporaryDirectory() as temp:
    scheduler=ParallelScheduler(workspaces_root=Path(temp)/'work',runs_root=Path(temp)/'runs',
        teleagent_call=lambda *a,**k:(200,{}),session_busy_fn=lambda *a:False)
    charter={'name':'proof','goal':'Deliver two files','must':[],'must_not':[],
             'allow_paths':[],'done_when':{'artifacts':['a.txt','b.txt']},'force_lead_review':True}
    job=scheduler.enqueue_charter(charter)
    job.state=JobState.RUNNING;job.session_id='ours';job.started_at=time.time()
    (job.workdir/'a.txt').write_text('Only one file exists')
    scheduler.refresh_job_status(job)
    partial={'state':job.state.value,'ok':job.result.get('ok'),
             'required':2,'present':len(job.result.get('artifacts',[])),
             'force_lead_review':job.force_lead_review,'lead_calls':scheduler.stats.lead_calls}
    job.state=JobState.RUNNING
    scheduler.dry_run=True
    sibling=Path(str(job.workdir)+'-other')/'outside.py'
    outside=scheduler.handle_one_permission({'id':'p1','sessionID':'ours','permission':'edit','path':str(sibling)})
    print(json.dumps({'partial_delivery_accepted':partial,'sibling_path_approval':outside},indent=2))
    scheduler._executor.shutdown()
