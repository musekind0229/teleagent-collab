#!/usr/bin/env python3
"""v0.3 stage2-k1: job_end must not fail when arts_ok and finish=stop.

Reproduces the P2 gap: lead verdict=fail, arts_ok=True, missing=[], finish=stop
must close as contract success. Lead opinion is advisory, not the contract.

No live TeleAgent. Inprocess backend + glue mocks inject the semantic.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent
REPO = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from charter import load_charter  # noqa: E402
from completion import (  # noqa: E402
    finish_is_successful,
    is_success_allowed,
    job_end_contract_close,
    snapshot_artifacts,
)
from execution_backend import (  # noqa: E402
    artifact_review,
    run_file_job_via_public_api,
)

CLOSED = REPO / "jobs/examples/hello-inprocess-closed-loop.charter.yaml"


def _always_fail_lead(request, schema=None):
    return {
        "application_id": request["application_id"],
        "context_summary": request["context_summary"],
        "verdict": "fail",
        "reason": "stub: lead fail despite complete artifacts",
    }


class TestJobEndContractKernel(unittest.TestCase):
    def test_arts_ok_finish_stop_lead_fail_is_success(self):
        recs = [{"path": "/ws/p2-st-reinstall-evidence.txt", "exists": True, "sha256": "abc123"}]
        close = job_end_contract_close(
            artifacts_ok=True,
            missing=[],
            finish="stop",
            lead_verdict="fail",
            session_id="ses_f659c3418ffe3npIsc0317HCKs",
            run_id="ses_f659c3418ffe3npIsc0317HCKs",
            artifact_records=recs,
        )
        self.assertTrue(close["ok"], close)
        self.assertEqual(close["state"], "ok")
        self.assertNotEqual(close["state"], "fail")
        self.assertTrue(close["lead_advisory"])
        self.assertEqual(close["reason"], "ok_lead_advisory")
        self.assertEqual(close["session_id"], "ses_f659c3418ffe3npIsc0317HCKs")
        self.assertEqual(close["run_id"], "ses_f659c3418ffe3npIsc0317HCKs")
        self.assertEqual(close["finish"], "stop")
        self.assertEqual(close["missing"], [])
        self.assertTrue(close["artifact_records"][0]["sha256"])
        self.assertEqual(close["error"], "")

    def test_equivalent_successful_finish_values(self):
        for fin in ("stop", "complete", "completed", "Stop"):
            close = job_end_contract_close(
                artifacts_ok=True, missing=[], finish=fin, lead_verdict="fail"
            )
            self.assertTrue(close["ok"], fin)
            self.assertTrue(finish_is_successful(fin), fin)

    def test_missing_artifacts_still_fail(self):
        close = job_end_contract_close(
            artifacts_ok=False,
            missing=["evidence.txt"],
            finish="stop",
            lead_verdict="fail",
        )
        self.assertFalse(close["ok"])
        self.assertEqual(close["state"], "fail")
        self.assertEqual(close["reason"], "artifacts_incomplete")

    def test_finish_error_still_fail(self):
        close = job_end_contract_close(
            artifacts_ok=True, missing=[], finish="error", lead_verdict="pass"
        )
        self.assertFalse(close["ok"])
        self.assertIn("finish_not_successful", close["reason"])

    def test_fingerprint_change_still_fail(self):
        close = job_end_contract_close(
            artifacts_ok=True,
            missing=[],
            finish="stop",
            lead_verdict="fail",
            fingerprint_ok=False,
        )
        self.assertFalse(close["ok"])
        self.assertEqual(close["reason"], "artifact_fingerprint_changed")

    def test_is_success_allowed_p1_compat_without_finish(self):
        ok, _ = is_success_allowed(
            state="ok", artifacts_ok=True, force_lead_review=True, lead_verdict="fail"
        )
        self.assertFalse(ok)
        ok2, why = is_success_allowed(
            state="ok",
            artifacts_ok=True,
            force_lead_review=True,
            lead_verdict="fail",
            finish="stop",
        )
        self.assertTrue(ok2)
        self.assertEqual(why, "ok_lead_advisory")


class TestInprocessArtsOkFinishStop(unittest.TestCase):
    def test_inprocess_collect_lead_fail_job_end_is_success(self):
        charter = load_charter(CLOSED)
        with tempfile.TemporaryDirectory() as td:
            collect = run_file_job_via_public_api(workdir=td, charter=charter)
            self.assertTrue(collect["ok"], collect)
            self.assertEqual(collect.get("finish"), "stop")
            self.assertTrue(collect.get("run_observation", {}).get("finish_successful"))
            self.assertTrue(collect.get("artifacts"))
            self.assertTrue(collect.get("run_id"))

            review = artifact_review(
                charter=charter,
                workdir=td,
                collect=collect,
                decision_fn=_always_fail_lead,
                run_id=str(collect.get("run_id") or ""),
            )
            self.assertEqual(review.get("verdict"), "fail")
            packet = review.get("packet") or {}
            self.assertTrue(packet.get("artifacts_complete"), packet)
            self.assertEqual(packet.get("missing") or [], [])

            recs = snapshot_artifacts(collect["artifacts"])
            close = job_end_contract_close(
                artifacts_ok=True,
                missing=list(packet.get("missing") or []),
                finish=collect.get("finish"),
                lead_verdict=review.get("verdict"),
                fingerprint_ok=True,
                lead_binding_ok=True,
                session_id=str(collect.get("run_id") or ""),
                run_id=str(collect.get("run_id") or ""),
                artifact_records=[r.to_dict() for r in recs],
            )
            self.assertTrue(close["ok"], close)
            self.assertEqual(close["state"], "ok")
            self.assertNotEqual(close["state"], "fail")
            self.assertTrue(close["lead_advisory"])
            self.assertEqual(close["session_id"], collect["run_id"])
            self.assertEqual(close["finish"], "stop")
            self.assertTrue(close["artifact_records"][0].get("sha256"))
            self.assertTrue(close["artifact_records"][0].get("path"))

            status = {
                "ok": close["ok"],
                "state": close["state"],
                "session_id": close["session_id"],
                "run_id": close["run_id"],
                "finish": close["finish"],
                "artifacts": collect["artifacts"],
                "artifact_records": close["artifact_records"],
                "grok_review_decision": review.get("verdict"),
                "lead_advisory": {
                    "verdict": review.get("verdict"),
                    "reason": review.get("reason"),
                },
                "error": close["error"],
            }
            self.assertTrue(status["ok"])
            self.assertNotEqual(status["state"], "fail")
            self.assertTrue(status["session_id"])
            self.assertEqual(status["finish"], "stop")
            self.assertTrue(status["artifact_records"][0]["sha256"])
            self.assertEqual(status["grok_review_decision"], "fail")
            self.assertEqual(status["error"], "")


class TestGlueJobEndLeadFailNotContractFail(unittest.TestCase):
    def test_glue_run_job_arts_ok_finish_stop_lead_fail(self):
        import glue

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            collab = root / "collab"
            collab.mkdir()
            ws = root / "ws"
            ws.mkdir()
            art = ws / "p2-st-reinstall-evidence.txt"
            art.write_text("sha=deadbeef\nexts=ok\nHTTP/1.1 200 OK\n", encoding="utf-8")
            sid = "ses_s2k1_inproc"

            def fake_call(method, path, body=None, extra_headers=None):
                if method == "POST" and path == "/session":
                    return 200, {"id": sid}
                if method == "POST" and str(path).endswith("/prompt_async"):
                    return 200, {}
                if method == "GET" and path == "/permission":
                    return 200, []
                return 200, {}

            def fake_obs(call, session_id, fetch_messages=False, **kwargs):
                return {
                    "busy": False,
                    "finish": "stop",
                    "finish_successful": True,
                    "cancelled": False,
                    "errored": False,
                    "assistant_error": "",
                    "activity": "idle",
                    "session_id": sid,
                }

            def fake_lead(req, schema=None, cwd=None, timeout_sec=180, adapter=None):
                body = {
                    "application_id": req["application_id"],
                    "context_summary": req["context_summary"],
                    "verdict": "fail",
                    "reason": "lead still fail despite artifacts",
                }
                return json.dumps(body), body

            with (
                patch.object(glue, "COLLAB", collab),
                patch.object(glue, "call", fake_call),
                patch.object(glue, "call_lead_request", fake_lead),
                patch(
                    "teleagent_adapter.run_observe.fetch_run_observation",
                    fake_obs,
                ),
            ):
                result = glue.run_job(
                    "s2k1",
                    "write evidence and stop",
                    [str(art)],
                    force_lead_review=True,
                    timeout_sec=30,
                    charter={
                        "goal": "write evidence",
                        "must": ["ws"],
                        "must_not": ["secrets"],
                        "done_when": {"artifacts": ["p2-st-reinstall-evidence.txt"]},
                        "acceptance": "evidence exists",
                        "force_lead_review": True,
                        "lead_review_steps": ["job_end"],
                    },
                    workspace=ws,
                )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result.get("state"), "ok")
            self.assertNotEqual(result.get("state"), "fail")
            self.assertNotIn("lead verdict=fail", str(result.get("error") or ""))
            self.assertEqual(result.get("session_id"), sid)
            self.assertEqual(result.get("run_id"), sid)
            self.assertEqual(result.get("finish"), "stop")
            self.assertTrue(result.get("artifacts"))
            recs = result.get("artifact_records") or []
            self.assertTrue(recs, result)
            self.assertTrue(recs[0].get("sha256") or recs[0].get("path"), recs)
            self.assertEqual(result.get("grok_review_decision"), "fail")
            close = result.get("job_end_contract") or {}
            self.assertTrue(close.get("ok"), close)
            self.assertTrue(close.get("lead_advisory") or result.get("lead_advisory"), result)
            self.assertTrue(
                any("advisory" in str(n) for n in (result.get("notes") or [])),
                result.get("notes"),
            )
            status_path = collab / "status-s2k1.json"
            self.assertTrue(status_path.is_file(), status_path)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertTrue(status["ok"], status)
            self.assertEqual(status["state"], "ok")
            self.assertEqual(status["session_id"], sid)
            self.assertEqual(status.get("finish"), "stop")
            self.assertTrue(status.get("artifact_records"))
            self.assertEqual(status.get("grok_review_decision"), "fail")


if __name__ == "__main__":
    raise SystemExit(unittest.main(verbosity=2))
