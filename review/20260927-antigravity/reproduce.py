"""Offline review probes. Only temporary files/processes; never invokes agy or cmdkey.

Updated 2026-09-27 after Codex P1 fixes: asserts improved outcomes (no pipe
deadlock, acceptance fails closed, account reservation / busy lease, observe
timeout).
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend
from execution_backend.run_job_wire import run_antigravity_charter
from execution_backend.agy_account_pool import (
    AccountPoolError,
    prepare_antigravity_environ_from_pool,
    release_account_lease,
    load_pool,
)

ENV = {k: v for k, v in os.environ.items() if not k.startswith(("AGY_", "COLLAB_AGY_"))}
real_popen = subprocess.Popen
results = {}


def factory(code):
    def spawn(argv, **kwargs):
        return real_popen([sys.executable, "-c", code], **kwargs)

    return spawn


with tempfile.TemporaryDirectory(prefix="collab-review-") as td:
    root = Path(td)
    lock_dir = root / "locks"
    lock_dir.mkdir()
    ENV = {**ENV, "COLLAB_AGY_LOCK_DIR": str(lock_dir)}

    # Large JSON must exit without a parent-side manual communicate drain.
    be = AntigravityCliExecutionBackend(environ=ENV, timeout_sec=8, poll_sec=0.05)
    code = "import json; print(json.dumps({'status':'ok','response':'x'*200000}))"
    with patch("execution_backend.antigravity_cli_v1.subprocess.Popen", factory(code)):
        run = be.start_run(title="large output", directory=str(root))
    deadline = time.time() + 5
    obs = be.observe_run(run["run_id"])
    while obs.get("busy") and time.time() < deadline:
        time.sleep(0.05)
        obs = be.observe_run(run["run_id"])
    collected = be.collect_result(run["run_id"])
    results["pipe_deadlock"] = {
        "busy_after_observe": bool(obs.get("busy")),
        "ok": bool(collected.get("ok")),
        "response_bytes": len(collected.get("response") or ""),
        "improved": (not obs.get("busy")) and bool(collected.get("ok")) and len(collected.get("response") or "") >= 200000,
    }

    # Observe-only callers enforce the configured wall deadline.
    be = AntigravityCliExecutionBackend(environ=ENV, timeout_sec=0.1, poll_sec=0.05)
    with patch(
        "execution_backend.antigravity_cli_v1.subprocess.Popen",
        factory("import time; time.sleep(30)"),
    ):
        run = be.start_run(title="deadline", directory=str(root))
    time.sleep(0.35)
    obs = be.observe_run(run["run_id"])
    results["observe_timeout"] = {
        "timeout_sec": 0.1,
        "elapsed_sec": 0.35,
        "busy": bool(obs.get("busy")),
        "improved": not bool(obs.get("busy")),
    }
    be.cancel(run["run_id"])

    # Review requested + WRONG content must not return ok=true.
    code = (
        "from pathlib import Path; import json; "
        "Path('answer.txt').write_text('WRONG'); "
        "print(json.dumps({'status':'ok','response':'done'}))"
    )
    charter = {
        "name": "review-probe",
        "goal": "Write answer.txt",
        "done_when": {"artifacts": ["answer.txt"]},
        "acceptance": "answer.txt must contain exactly RIGHT",
        "force_lead_review": True,
        "timeout_sec": 5,
    }
    with patch("execution_backend.antigravity_cli_v1.subprocess.Popen", factory(code)):
        r = run_antigravity_charter(charter=charter, workdir=root, environ=ENV)
    results["review_ignored"] = {
        "force_lead_review": True,
        "actual_content": (root / "answer.txt").read_text(encoding="utf-8"),
        "ok": bool(r.get("ok")),
        "state": r.get("state"),
        "improved": (not r.get("ok")) and r.get("state") != "ok",
    }

    # Separate callers cannot simultaneously reserve the same HOME.
    pool = root / "pool.json"
    home_a = root / "homeA"
    home_a.mkdir()
    pool.write_text(
        json.dumps({"accounts": [{"id": "A", "home": str(home_a), "state": "available"}]}),
        encoding="utf-8",
    )
    with patch("execution_backend.agy_account_pool.clear_windows_antigravity_keyring"):
        a = prepare_antigravity_environ_from_pool(pool, base_environ=ENV, persist=True)
        second_err = None
        second_profile = None
        try:
            b = prepare_antigravity_environ_from_pool(pool, base_environ=ENV, persist=True)
            second_profile = b.get("agy_profile")
        except AccountPoolError as e:
            second_err = str(e)
        pool_state = json.loads(pool.read_text(encoding="utf-8"))["accounts"][0]["state"]
        release_account_lease(load_pool(pool), "A", persist=True)
    results["no_account_reservation"] = {
        "first": a["agy_profile"],
        "second": second_profile,
        "second_error": second_err,
        "pool_state_after_first": pool_state,
        "improved": second_err is not None and pool_state == "busy",
    }

print(json.dumps(results, indent=2))
