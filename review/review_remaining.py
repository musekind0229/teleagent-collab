"""Independent boundary probes after astra round-2 fixes.

Asserts *safe* expectations (not the old defect shapes):
- message 500 / cancelled / malformed status must not ok
- abort 202 while busy must not cancel_effected
- redirect sink receives zero Authorization / signature headers

Usage:
  python review/review_remaining.py [PATH_TO_SRC]
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SRC = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from scheduler import ParallelScheduler, JobState  # noqa: E402
from teleagent_adapter.linux_local_v1 import LinuxLocalV1Adapter  # noqa: E402
from teleagent_adapter.base import AdapterError  # noqa: E402

results = {}
charter = {
    "name": "probe",
    "goal": "write OK",
    "must": ["only workspace"],
    "must_not": ["no secrets"],
    "done_when": {"artifacts": ["out.txt"]},
    "force_lead_review": True,
    "timeout_sec": 300,
}

with tempfile.TemporaryDirectory() as d:
    root = Path(d)

    def lead(prompt, schema, cwd):
        req = json.loads(prompt)["_lead_request"]
        response = {
            "application_id": req["application_id"],
            "context_summary": req["context_summary"],
            "verdict": "pass",
            "reason": "test approval with correct binding",
        }
        return json.dumps(response), response

    for label, force, status, messages in [
        ("force_review_message_500", True, (200, {}), (500, {"error": "unavailable"})),
        (
            "force_review_cancelled",
            True,
            (200, {}),
            (200, [{"info": {"role": "assistant", "finish": "cancelled"}}]),
        ),
        (
            "malformed_status",
            False,
            (200, "not a status object"),
            (200, [{"info": {"role": "assistant", "finish": "stop"}}]),
        ),
    ]:
        def transport(method, path, *args, status=status, messages=messages, **kwargs):
            return status if path == "/session/status" else messages

        s = ParallelScheduler(
            workspaces_root=root / label / "ws",
            runs_root=root / label / "runs",
            persist=False,
            dry_run=False,
            teleagent_call=transport,
            call_lead_fn=lead,
        )
        j = s.enqueue_charter({**charter, "force_lead_review": force})
        j.session_id = "probe-session"
        j.state = JobState.RUNNING
        j.started_at = time.time()
        Path(j.expected_artifacts[0]).write_text("OK")
        s.refresh_job_status(j)
        results[label] = {
            "state": j.state.value,
            "ok": (j.result or {}).get("ok"),
            "finish": (j.result or {}).get("finish"),
        }
        assert j.state != JobState.DONE, label
        assert not (j.result or {}).get("ok"), label
        s.shutdown()

    calls = []

    def abort_transport(method, path, *args, **kwargs):
        calls.append([method, path])
        if path.endswith("/abort"):
            return 202, {"accepted": True}
        return 200, {"probe-session": {"type": "busy"}}

    s = ParallelScheduler(
        workspaces_root=root / "cancel" / "ws",
        runs_root=root / "cancel" / "runs",
        persist=False,
        teleagent_call=abort_transport,
    )
    j = s.enqueue_charter(charter)
    j.session_id = "probe-session"
    j.state = JobState.RUNNING
    s.request_cancel(j.job_id)
    r = s.effect_cancel(j.job_id)
    results["abort_202_without_stop_confirmation"] = {
        "state": j.state.value,
        "cancel_effected": r["cancel_effected"],
        "stop_pending_confirm": r.get("stop_pending_confirm"),
        "calls": calls,
    }
    assert j.state != JobState.CANCELLED
    assert not r.get("cancel_effected")
    assert r.get("stop_pending_confirm")
    assert any(p.endswith("/abort") for _, p in calls)
    assert any(p == "/session/status" for _, p in calls)
    s.shutdown()

    received = []

    class Sink(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(
                {
                    "authorization_present": bool(self.headers.get("Authorization")),
                    "signature_present": bool(self.headers.get("X-SA-Signature")),
                }
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.2:{sink.server_port}/sink")
            self.end_headers()

        def log_message(self, *args):
            pass

    sink = ThreadingHTTPServer(("127.0.0.2", 0), Sink)
    source = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    for server in (source, sink):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        adapter = LinuxLocalV1Adapter(
            base_url=f"http://127.0.0.1:{source.server_port}",
            find_creds_fn=lambda: ("synthetic-user", "synthetic-password", "synthetic-key"),
        )
        try:
            adapter.call("GET", "/probe", timeout=3)
            outcome = "returned"
        except AdapterError:
            outcome = "AdapterError"
        except Exception as e:
            outcome = type(e).__name__
        results["redirect_rejected_before_credentials_sent"] = {
            "outcome": outcome,
            "sink_received": list(received),
        }
        assert outcome == "AdapterError"
        assert received == []
    finally:
        for server in (source, sink):
            server.shutdown()
            server.server_close()

print(json.dumps(results, indent=2))
print("ALL BOUNDARY PROBES PASSED (safe expectations)")
