"""Independent round-3 edge probes (safe expectations after fix).

Asserts:
- error / unrecognized status dicts must NOT confirm cancel (activity=unknown)
- previous-turn finish=stop must NOT complete when a newer user has no reply

Usage:
  python3 review/review_edge_cases.py [PATH_TO_SRC]
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

SRC = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from scheduler import ParallelScheduler, JobState  # noqa: E402

out = {}
charter = {
    "name": "edge",
    "goal": "latest turn output must be NEW",
    "must": ["workspace only"],
    "must_not": ["no secrets"],
    "done_when": {"artifacts": ["out.txt"]},
    "timeout_sec": 120,
    "force_lead_review": False,
}

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    for label, status in [
        ("error_dict", {"error": "backend not ready"}),
        ("invalid_session_entry", {"s": {"type": "unrecognized-state"}}),
    ]:
        calls = []

        def transport(method, path, *a, status=status, **kw):
            calls.append([method, path])
            return (202, {"accepted": True}) if method == "POST" else (200, status)

        s = ParallelScheduler(
            workspaces_root=root / label / "ws",
            runs_root=root / label / "runs",
            persist=False,
            teleagent_call=transport,
        )
        j = s.enqueue_charter(charter)
        j.session_id = "s"
        j.state = JobState.RUNNING
        s.request_cancel(j.job_id)
        r = s.effect_cancel(j.job_id)
        out[label] = {
            "state": j.state.value,
            "cancel_effected": r["cancel_effected"],
            "stop_pending_confirm": r.get("stop_pending_confirm"),
            "calls": calls,
        }
        assert r.get("cancel_effected") is False, label
        assert j.state != JobState.CANCELLED, label
        assert r.get("stop_pending_confirm"), label
        s.shutdown()

    msgs = [
        {"info": {"id": "u-old", "role": "user"}},
        {"info": {"id": "a-old", "role": "assistant", "parentID": "u-old", "finish": "stop"}},
        {"info": {"id": "u-new", "role": "user"}},
    ]

    def transport(method, path, *a, **kw):
        return (200, {}) if path == "/session/status" else (200, msgs)

    s = ParallelScheduler(
        workspaces_root=root / "turn" / "ws",
        runs_root=root / "turn" / "runs",
        persist=False,
        teleagent_call=transport,
    )
    j = s.enqueue_charter(charter)
    j.session_id = "s"
    j.state = JobState.RUNNING
    j.started_at = time.time()
    j.dispatch_user_message_id = "u-old"  # prior dispatch; newer user u-new has no reply
    Path(j.expected_artifacts[0]).write_text("OLD")
    s.refresh_job_status(j)
    out["previous_turn_finish_rejected"] = {
        "state": j.state.value,
        "ok": (j.result or {}).get("ok"),
        "notes_tail": (j.notes or [])[-2:],
    }
    assert j.state != JobState.DONE
    assert not (j.result or {}).get("ok")
    s.shutdown()

    # Positive control: latest user has matching assistant stop → may DONE
    msgs_ok = [
        {"info": {"id": "u-old", "role": "user"}},
        {"info": {"id": "a-old", "role": "assistant", "parentID": "u-old", "finish": "stop"}},
        {"info": {"id": "u-new", "role": "user"}},
        {"info": {"id": "a-new", "role": "assistant", "parentID": "u-new", "finish": "stop"}},
    ]

    def transport_ok(method, path, *a, **kw):
        return (200, {}) if path == "/session/status" else (200, msgs_ok)

    s = ParallelScheduler(
        workspaces_root=root / "turn_ok" / "ws",
        runs_root=root / "turn_ok" / "runs",
        persist=False,
        teleagent_call=transport_ok,
    )
    j = s.enqueue_charter(charter)
    j.session_id = "s"
    j.state = JobState.RUNNING
    j.started_at = time.time()
    j.dispatch_user_message_id = "u-new"
    Path(j.expected_artifacts[0]).write_text("NEW")
    s.refresh_job_status(j)
    out["this_turn_finish_accepted"] = {
        "state": j.state.value,
        "ok": (j.result or {}).get("ok"),
    }
    assert j.state == JobState.DONE
    assert (j.result or {}).get("ok") is True
    s.shutdown()

print(json.dumps(out, indent=2))
print("ALL EDGE PROBES PASSED (safe expectations)")
