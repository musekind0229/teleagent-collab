"""Safe-expectation probe: missing turn identity must NOT DONE (fake transport)."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from scheduler import JobState, ParallelScheduler  # noqa: E402

results = []
cases = [
    (
        "no_user_rows",
        [{"info": {"role": "assistant", "id": "old", "finish": "stop", "parentID": "old-user"}}],
    ),
    (
        "latest_user_without_id",
        [
            {"info": {"role": "assistant", "id": "old", "finish": "stop", "parentID": "old-user"}},
            {"info": {"role": "user"}},
        ],
    ),
]
for label, messages in cases:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        def transport(method, path, *args, **kwargs):
            return (200, {}) if path == "/session/status" else (200, messages)

        scheduler = ParallelScheduler(
            workspaces_root=root / "ws",
            runs_root=root / "runs",
            persist=False,
            dry_run=False,
            teleagent_call=transport,
        )
        job = scheduler.enqueue_charter(
            {
                "name": "binding",
                "goal": "new output",
                "must": ["workspace only"],
                "must_not": ["no network"],
                "done_when": {"artifacts": ["out.txt"]},
                "timeout_sec": 120,
            }
        )
        job.state = JobState.RUNNING
        job.session_id = "s"
        job.started_at = time.time()
        Path(job.expected_artifacts[0]).write_text("OLD")
        scheduler.refresh_job_status(job)
        row = {"case": label, "state": job.state.value, "ok": (job.result or {}).get("ok")}
        results.append(row)
        assert job.state != JobState.DONE, row
        assert (job.result or {}).get("ok") is not True, row
        scheduler.shutdown()

print(json.dumps(results, indent=2))
print("ALL BINDING FALLBACK PROBES PASSED")
