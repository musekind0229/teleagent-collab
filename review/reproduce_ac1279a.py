"""Behavioral probes for astra P1 fixes (cancel abort, status gate, restore contract, unbound lead).

Usage:
  python review/reproduce_ac1279a.py [PATH_TO_SRC]

Default SRC is repo src/ next to this file. After fixes, all four probes assert
*correct* behavior (no longer asserting the old defects).
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

SRC = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from scheduler import JobState, ParallelScheduler  # noqa: E402
from state_store import StateStore  # noqa: E402

out = {}
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)

    def make(name, **kwargs):
        kwargs.setdefault("persist", False)
        return ParallelScheduler(
            workspaces_root=root / name / "ws",
            runs_root=root / name / "runs",
            **kwargs,
        )

    charter = {
        "name": "probe",
        "goal": "write exact OK",
        "must": ["only workdir"],
        "must_not": ["no network"],
        "done_when": {"artifacts": ["out.txt"]},
        "force_lead_review": True,
        "timeout_sec": 300,
    }

    # 1) cancel must call abort; failed abort ⇒ not cancel_effected
    calls = []

    def transport_fail(method, path, *args, **kwargs):
        calls.append((method, path))
        return 500, {"error": "status endpoint unavailable"}

    s = make("cancel", teleagent_call=transport_fail)
    j = s.enqueue_charter(charter)
    j.session_id = "session-probe"
    j.state = JobState.RUNNING
    s.request_cancel(j.job_id)
    result = s.effect_cancel(j.job_id)
    abort_calls = [c for c in calls if c[0] == "POST" and str(c[1]).endswith("/abort")]
    out["cancel_abort_required"] = {
        "result": {
            "cancel_effected": result.get("cancel_effected"),
            "cancel_requested": result.get("cancel_requested"),
            "stop_pending_confirm": result.get("stop_pending_confirm"),
            "state": result.get("state"),
        },
        "abort_calls": abort_calls,
        "transport_calls": list(calls),
    }
    assert abort_calls, "expected POST /session/{id}/abort"
    assert not result.get("cancel_effected"), "abort HTTP 500 must not mark cancel_effected"
    assert result.get("stop_pending_confirm") or result.get("state") == "cancel_requested"
    s.shutdown()
    calls.clear()

    # 1b) abort 200 ⇒ cancel_effected
    def transport_ok_abort(method, path, *args, **kwargs):
        calls.append((method, path))
        if method == "POST" and str(path).endswith("/abort"):
            return 200, {"ok": True}
        return 200, {}

    s = make("cancel_ok", teleagent_call=transport_ok_abort)
    j = s.enqueue_charter(charter)
    j.session_id = "session-probe"
    j.state = JobState.RUNNING
    s.request_cancel(j.job_id)
    result = s.effect_cancel(j.job_id)
    out["cancel_abort_ok"] = {
        "cancel_effected": result.get("cancel_effected"),
        "state": result.get("state"),
        "calls": list(calls),
    }
    assert result.get("cancel_effected") is True
    assert j.state == JobState.CANCELLED
    s.shutdown()
    calls.clear()

    # 2) status HTTP 500 + wrong content must NOT ok=True
    def transport_status_500(method, path, *args, **kwargs):
        calls.append((method, path))
        return 500, {"error": "status endpoint unavailable"}

    s = make("status", teleagent_call=transport_status_500)
    j = s.enqueue_charter({**charter, "force_lead_review": False})
    j.session_id = "session-probe"
    j.state = JobState.RUNNING
    j.started_at = time.time()
    Path(j.expected_artifacts[0]).write_text("wrong content")
    s.refresh_job_status(j)
    out["status_http_500_rejected"] = {
        "state": j.state.value,
        "ok": (j.result or {}).get("ok"),
        "calls": list(calls),
    }
    assert j.state != JobState.DONE
    assert not (j.result or {}).get("ok")
    s.shutdown()
    calls.clear()

    # 3) restore keeps full contract
    store = StateStore(root=root / "store")
    s = make("restore", state_store=store, persist=True, teleagent_call=transport_status_500)
    j = s.enqueue_charter(charter)
    j.state = JobState.RUNNING
    j.session_id = "session-probe"
    j.started_at = time.time()
    s._persist_job(j)
    s2 = make("restore2", state_store=StateStore(root=root / "store"), persist=True)
    plan = s2.restore_from_store()
    r = s2.jobs[j.job_id]
    out["restore_keeps_contract"] = {
        "charter_must_not": r.charter.get("must_not"),
        "artifacts": r.expected_artifacts,
        "force_lead_review": r.force_lead_review,
        "blocked": plan.get("blocked"),
        "goal": r.charter.get("goal"),
    }
    assert r.expected_artifacts, "expected_artifacts must restore"
    assert r.force_lead_review is True
    assert r.charter.get("must_not") == ["no network"]
    assert r.charter.get("must") == ["only workdir"]
    s.shutdown()
    s2.shutdown()

    # 4) unbound lead (no application_id) rejected in production (dry_run=False)
    def unbound_lead(*args):
        return '{"verdict":"pass","reason":"unbound"}', {"verdict": "pass", "reason": "unbound"}

    def transport_idle(method, path, *args, **kwargs):
        calls.append((method, path))
        if method == "GET" and path == "/session/status":
            return 200, {"session-probe": {"type": "idle"}}
        if method == "GET" and "/message" in str(path):
            return 200, [{"info": {"role": "assistant", "finish": "stop"}}]
        if method == "POST" and str(path).endswith("/abort"):
            return 200, {}
        return 200, {}

    s = make("unbound", call_lead_fn=unbound_lead, teleagent_call=transport_idle, dry_run=False)
    j = s.enqueue_charter(charter)
    j.session_id = "session-probe"
    j.state = JobState.RUNNING
    j.started_at = time.time()
    Path(j.expected_artifacts[0]).write_text("OK")
    s.refresh_job_status(j)
    out["unbound_lead_rejected"] = {
        "state": j.state.value,
        "ok": (j.result or {}).get("ok"),
        "lead_review_verdict": (j.result or {}).get("lead_review_verdict"),
    }
    assert j.state != JobState.DONE or not (j.result or {}).get("ok")
    assert (j.result or {}).get("lead_review_verdict") != "pass" or j.state == JobState.FAIL
    s.shutdown()

print(json.dumps(out, ensure_ascii=False, indent=2))
print("ALL PROBES PASSED (correct behavior)", file=sys.stderr)
