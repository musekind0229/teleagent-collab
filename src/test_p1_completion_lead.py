#!/usr/bin/env python3
"""P1 tests (条2 + 条3) — all *simulated* unless noted. No live TeleAgent / Grok required."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from completion import (
    ReworkBudget,
    artifacts_all_present,
    build_acceptance_packet,
    confirm_artifacts_for_lead_approve,
    is_success_allowed,
    snapshot_artifacts,
)
from lead_adapter import (
    LeadDecisionError,
    build_lead_request,
    get_lead_adapter,
    lead_permission_response_schema,
    lead_review_response_schema,
    pin_lead_response_schema,
    validate_lead_decision,
)
from lead_adapter.inprocess import InProcessLeadAdapter
from lead_adapter.grok_cli import GrokCliLeadAdapter
from lead_adapter.schema import format_lead_request_prompt, unwrap_structured
from scheduler import ParallelScheduler, JobState

SIMULATED = True


class TestCompletionCriteria(unittest.TestCase):
    def test_all_artifacts_required_not_any(self):
        with tempfile.TemporaryDirectory() as d:
            p1 = Path(d) / "a.txt"
            p2 = Path(d) / "b.txt"
            p1.write_text("x", encoding="utf-8")
            recs = snapshot_artifacts([str(p1), str(p2)])
            self.assertTrue(recs[0].exists)
            self.assertFalse(recs[1].exists)
            self.assertFalse(artifacts_all_present(recs))

    def test_timeout_cancel_error_not_success(self):
        for state in ("timeout", "cancelled", "fail", "error"):
            ok, _ = is_success_allowed(state=state, artifacts_ok=True)
            self.assertFalse(ok, state)
        ok, _ = is_success_allowed(state="ok", artifacts_ok=False)
        self.assertFalse(ok)
        ok, _ = is_success_allowed(
            state="ok", artifacts_ok=True, force_lead_review=True, lead_verdict="fail"
        )
        self.assertFalse(ok)
        ok, _ = is_success_allowed(
            state="ok", artifacts_ok=True, force_lead_review=True, lead_verdict="pass"
        )
        self.assertTrue(ok)

    def test_rework_budget_cannot_reset_wall(self):
        b = ReworkBudget.start(5, max_reworks=1)
        wall = b.wall_deadline
        self.assertTrue(b.consume_rework())
        self.assertEqual(b.wall_deadline, wall)
        self.assertFalse(b.consume_rework())  # max 1
        # clamp never past wall
        self.assertLessEqual(b.clamp_subdeadline(9999), wall)

    def test_acceptance_packet_and_fingerprint_gate(self):
        with tempfile.TemporaryDirectory() as d:
            art = Path(d) / "out.txt"
            art.write_text("hello", encoding="utf-8")
            packet = build_acceptance_packet(
                job_name="t",
                goal="g",
                acceptance_criteria={"artifacts": [str(art)]},
                expected_artifacts=[str(art)],
                execution_result={"finish": "stop"},
                error="",
                tool_records=[{"tool": "write"}],
                api_replies=[{"id": "1", "reply": "once"}],
            )
            self.assertTrue(packet["artifacts_complete"])
            self.assertEqual(len(packet["tool_records"]), 1)
            ok, gate = confirm_artifacts_for_lead_approve(packet)
            self.assertTrue(ok, gate)
            art.write_text("mutated", encoding="utf-8")
            ok2, gate2 = confirm_artifacts_for_lead_approve(packet)
            self.assertFalse(ok2)
            self.assertTrue(gate2["diffs"])


class TestLeadAdapter(unittest.TestCase):
    def test_validate_binds_application_id(self):
        req = build_lead_request(
            kind="permission",
            goal="stay in ws",
            authorized_scope=["ws"],
            prohibitions=["secrets"],
            acceptance_criteria={"artifacts": ["a.txt"]},
            current_application={"tool": "bash"},
        )
        good = {
            "application_id": req["application_id"],
            "context_summary": req["context_summary"],
            "decision": "once",
            "reason": "ok",
        }
        out = validate_lead_decision(json.dumps(good), good, request=req, kind="permission")
        self.assertEqual(out["decision"], "once")

        bad = {"decision": "once", "reason": "no id"}
        with self.assertRaises(LeadDecisionError) as cm:
            validate_lead_decision(json.dumps(bad), bad, request=req, kind="permission")
        self.assertEqual(cm.exception.code, "application_id_mismatch")

    def test_timeout_envelope(self):
        req = build_lead_request(
            kind="review",
            goal="g",
            authorized_scope=[],
            prohibitions=[],
            acceptance_criteria={},
            current_application={},
        )
        with self.assertRaises(LeadDecisionError) as cm:
            validate_lead_decision(
                "TIMEOUT",
                {"_lead_status": "timeout"},
                request=req,
                kind="review",
            )
        self.assertEqual(cm.exception.code, "timeout")

    def test_inprocess_adapter_no_spawn(self):
        with tempfile.TemporaryDirectory() as d:
            def decide_fn(request, schema):
                return {
                    "application_id": request["application_id"],
                    "context_summary": request["context_summary"],
                    "decision": "reject",
                    "reason": "codex-as-lead",
                }

            ad = InProcessLeadAdapter(exchange_dir=d, decision_fn=decide_fn)
            req = build_lead_request(
                kind="permission",
                goal="g",
                authorized_scope=["ws"],
                prohibitions=["x"],
                acceptance_criteria={},
                current_application={"path": "/tmp/x"},
            )
            raw, parsed = ad.decide(req, schema={"type": "object"}, cwd=d, timeout_sec=5)
            self.assertEqual(parsed["decision"], "reject")
            self.assertEqual(parsed["application_id"], req["application_id"])
            # pending archived
            self.assertFalse((Path(d) / "pending" / f"{req['application_id']}.json").exists())

    def test_inprocess_file_protocol(self):
        with tempfile.TemporaryDirectory() as d:
            ad = InProcessLeadAdapter(exchange_dir=d, poll_interval=0.05)
            req = build_lead_request(
                kind="review",
                goal="g",
                authorized_scope=[],
                prohibitions=[],
                acceptance_criteria={},
                current_application={"arts": []},
            )

            def writer():
                time.sleep(0.1)
                ad.submit_decision(
                    req["application_id"],
                    {
                        "application_id": req["application_id"],
                        "context_summary": req["context_summary"],
                        "verdict": "fail",
                        "reason": "incomplete",
                    },
                )

            import threading

            threading.Thread(target=writer, daemon=True).start()
            raw, parsed = ad.decide(req, schema={"type": "object"}, cwd=d, timeout_sec=3)
            self.assertEqual(parsed["verdict"], "fail")

    def test_factory_and_grok_is_one_backend(self):
        ad = get_lead_adapter("grok_cli")
        self.assertIsInstance(ad, GrokCliLeadAdapter)
        ad2 = get_lead_adapter("inprocess", exchange_dir=tempfile.mkdtemp())
        self.assertIsInstance(ad2, InProcessLeadAdapter)

    def test_grok_cli_no_disallowed_tools_retry(self):
        """Source must not contain the deleted degradation retry."""
        src = Path(__file__).resolve().parent / "glue.py"
        text = src.read_text(encoding="utf-8")
        self.assertNotIn("retry without disallowed-tools", text)
        self.assertNotIn("disallowed-tools if tool names invalid", text)
        grok_src = (Path(__file__).resolve().parent / "lead_adapter" / "grok_cli.py").read_text()
        self.assertIn("NO retry that drops", grok_src)
        self.assertIn("--disallowed-tools", grok_src)


class TestSchedulerForceLeadAndTimeout(unittest.TestCase):
    def test_timeout_not_success_with_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                max_parallel=1,
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                dry_run=True,
                persist=False,
                stop_when_idle=True,
                idle_min=1,
                idle_max=1,
                busy_min=1,
                busy_max=1,
            )
            charter = {
                "name": "to",
                "goal": "timeout case",
                "must": ["ws"],
                "must_not": ["secrets"],
                "allow_secret_globs": [],
                "allow_paths": [],
                "allow_keys": [],
                "done_when": {"artifacts": ["out.txt"]},
                "timeout_sec": 2,  # short wall; we backdate started_at
            }
            job = sched.enqueue_charter(charter)
            sched.try_start_queued()
            # artifacts exist from dry start
            self.assertTrue(Path(job.expected_artifacts[0]).exists())
            job.started_at = time.time() - 10  # force wall exceeded
            sched.refresh_job_status(job)
            self.assertEqual(job.state, JobState.TIMEOUT)
            self.assertFalse((job.result or {}).get("ok"))

    def test_force_lead_review_in_parallel_dry(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            decisions = []

            def fake_lead(prompt, schema, cwd):
                decisions.append(prompt[:80])
                # permission-style; scheduler maps once→pass for review
                return json.dumps({"decision": "once", "reason": "ok"}), {
                    "decision": "once",
                    "reason": "ok",
                }

            sched = ParallelScheduler(
                max_parallel=2,
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                dry_run=True,
                call_lead_fn=fake_lead,
                stop_when_idle=True,
                idle_min=1,
                idle_max=1,
            )
            for i in range(2):
                sched.enqueue_charter(
                    {
                        "name": f"flr-{i}",
                        "goal": f"force lead {i}",
                        "must": ["ws"],
                        "must_not": ["secrets"],
                        "allow_secret_globs": [],
                        "allow_paths": [],
                        "allow_keys": [],
                        "done_when": {"artifacts": [f"out-{i}.txt"]},
                        "timeout_sec": 60,
                        "force_lead_review": True,
                    }
                )
            summary = sched.run(sleep=False, max_ticks=30)
            sched.shutdown()
            self.assertGreaterEqual(len(decisions), 2)  # review per job
            for info in summary["jobs"].values():
                self.assertTrue(info.get("ok"), info)
                self.assertEqual(
                    (info.get("result") or {}).get("lead_review_verdict"), "pass"
                )


class TestLiveGrokEchoAndUnwrap(unittest.TestCase):
    def _req(self, kind="review"):
        return build_lead_request(
            kind=kind,
            goal="stay in ws",
            authorized_scope=["ws"],
            prohibitions=["secrets"],
            acceptance_criteria={"artifacts": ["a.txt"]},
            current_application={"tool": "write"},
        )

    def test_pin_lead_response_schema_sets_const(self):
        req = self._req("review")
        schema = lead_review_response_schema()
        pinned = pin_lead_response_schema(schema, req)
        self.assertIsNot(pinned, schema)
        self.assertEqual(
            pinned["properties"]["application_id"],
            {"type": "string", "const": req["application_id"]},
        )
        self.assertEqual(
            pinned["properties"]["context_summary"],
            {"type": "string", "const": req["context_summary"]},
        )
        self.assertIn("context_summary", pinned["required"])
        self.assertIn("application_id", pinned["required"])
        self.assertIn("verdict", pinned["required"])
        self.assertEqual(schema["properties"]["application_id"], {"type": "string"})
        perm = pin_lead_response_schema(lead_permission_response_schema(), req)
        self.assertEqual(perm["properties"]["application_id"]["const"], req["application_id"])
        self.assertIn("context_summary", perm["required"])

    def test_empty_or_missing_application_id_mismatch_review_and_permission(self):
        for kind, field, ok_val in (
            ("review", "verdict", "pass"),
            ("permission", "decision", "once"),
        ):
            req = self._req(kind)
            missing = {field: ok_val, "reason": "no id"}
            with self.assertRaises(LeadDecisionError) as cm:
                validate_lead_decision(json.dumps(missing), missing, request=req, kind=kind)
            self.assertEqual(cm.exception.code, "application_id_mismatch")

            empty = {
                "application_id": "",
                "context_summary": req["context_summary"],
                field: ok_val,
                "reason": "empty id",
            }
            with self.assertRaises(LeadDecisionError) as cm:
                validate_lead_decision(json.dumps(empty), empty, request=req, kind=kind)
            self.assertEqual(cm.exception.code, "application_id_mismatch")

            req_empty = dict(req)
            req_empty["application_id"] = ""
            with self.assertRaises(LeadDecisionError) as cm:
                validate_lead_decision(json.dumps(empty), empty, request=req_empty, kind=kind)
            self.assertEqual(cm.exception.code, "application_id_mismatch")

    def test_exact_echo_accepted(self):
        for kind, field, ok_val in (
            ("review", "verdict", "pass"),
            ("permission", "decision", "once"),
        ):
            req = self._req(kind)
            good = {
                "application_id": req["application_id"],
                "context_summary": req["context_summary"],
                field: ok_val,
                "reason": "exact echo",
            }
            out = validate_lead_decision(json.dumps(good), good, request=req, kind=kind)
            self.assertEqual(out["application_id"], req["application_id"])
            self.assertEqual(out["context_summary"], req["context_summary"])
            self.assertEqual(out[field], ok_val)

    def test_rewritten_context_summary_mismatch(self):
        req = self._req("review")
        req = dict(req)
        req["context_summary"] = "line1\nline2"
        rewritten = {
            "application_id": req["application_id"],
            "context_summary": "line1 line2 (looks fine)",
            "verdict": "pass",
            "reason": "rewrote summary",
        }
        with self.assertRaises(LeadDecisionError) as cm:
            validate_lead_decision(json.dumps(rewritten), rewritten, request=req, kind="review")
        self.assertEqual(cm.exception.code, "context_summary_mismatch")

    def test_nested_structured_output_unwrap_validates(self):
        req = self._req("review")
        inner = {
            "application_id": req["application_id"],
            "context_summary": req["context_summary"],
            "verdict": "pass",
            "reason": "nested ok",
        }
        envelopes = [
            {"structuredOutput": inner},
            {"structuredOutput": json.dumps(inner)},
            {"response": {"structuredOutput": inner}},
            {"output": {"content": inner}},
            {"data": {"result": inner}},
            {"message": {"content": inner}},
        ]
        for env in envelopes:
            unwrapped = unwrap_structured(env)
            self.assertIsNotNone(unwrapped, env)
            out = validate_lead_decision("", env, request=req, kind="review")
            self.assertEqual(out["verdict"], "pass", env)
            self.assertEqual(out["application_id"], req["application_id"], env)

        missing_id = {"structuredOutput": {"verdict": "pass", "reason": "no id"}}
        with self.assertRaises(LeadDecisionError) as cm:
            validate_lead_decision("", missing_id, request=req, kind="review")
        self.assertEqual(cm.exception.code, "application_id_mismatch")

    def test_prompt_demands_verbatim_echo(self):
        req = self._req("review")
        prompt = format_lead_request_prompt(req)
        self.assertIn("byte-for-byte", prompt)
        self.assertIn("MUST be copied verbatim", prompt)
        self.assertIn(f'"application_id":{json.dumps(req["application_id"])}', prompt)
        self.assertIn(f'"context_summary":{json.dumps(req["context_summary"])}', prompt)
        self.assertNotIn('"application_id":"..."', prompt)
        self.assertNotIn('"context_summary":"..."', prompt)

    def test_grok_cli_json_schema_is_const_pinned(self):
        req = self._req("review")
        inner = {
            "application_id": req["application_id"],
            "context_summary": req["context_summary"],
            "verdict": "pass",
            "reason": "ok",
        }
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            idx = cmd.index("--json-schema")
            captured["schema"] = json.loads(cmd[idx + 1])

            class P:
                returncode = 0
                stdout = json.dumps({"structuredOutput": inner})
                stderr = ""

            return P()

        ad = GrokCliLeadAdapter(bin_path="/bin/echo")
        with mock.patch("lead_adapter.grok_cli.subprocess.run", fake_run):
            ad.decide(req, schema=lead_review_response_schema(), cwd="/tmp")
        self.assertEqual(
            captured["schema"]["properties"]["application_id"]["const"],
            req["application_id"],
        )
        self.assertEqual(
            captured["schema"]["properties"]["context_summary"]["const"],
            req["context_summary"],
        )
        self.assertIn("context_summary", captured["schema"]["required"])

    def test_call_lead_request_pins_schema(self):
        import glue

        req = self._req("review")
        captured = {}

        class Fake:
            def decide(self, request, *, schema, cwd, timeout_sec=180):
                captured["schema"] = schema
                body = {
                    "application_id": request["application_id"],
                    "context_summary": request["context_summary"],
                    "verdict": "pass",
                    "reason": "ok",
                }
                return json.dumps(body), body

        glue.call_lead_request(
            req, schema=lead_review_response_schema(), cwd="/tmp", adapter=Fake()
        )
        self.assertEqual(
            captured["schema"]["properties"]["application_id"]["const"],
            req["application_id"],
        )
        self.assertEqual(
            captured["schema"]["properties"]["context_summary"]["const"],
            req["context_summary"],
        )


class TestGlueThisRoundFinish(unittest.TestCase):
    def test_successful_finish_values(self):
        from glue import assistant_finish_successful

        self.assertTrue(assistant_finish_successful("stop"))
        self.assertTrue(assistant_finish_successful("complete"))
        self.assertTrue(assistant_finish_successful("completed"))
        self.assertFalse(assistant_finish_successful(None))
        self.assertFalse(assistant_finish_successful(""))
        self.assertFalse(assistant_finish_successful("error"))
        self.assertFalse(assistant_finish_successful("cancelled"))

    def test_run_job_waits_for_successful_finish_before_lead_review(self):
        text = (Path(__file__).resolve().parent / "glue.py").read_text(encoding="utf-8")
        self.assertIn("assistant_finish_successful", text)
        self.assertNotIn("proceed to review only if forced", text)
        self.assertIn("do not fall back to last_assistant", text)


class TestGlueNoLooseSuccess(unittest.TestCase):
    def test_no_or_true_success_branch(self):
        text = (Path(__file__).resolve().parent / "glue.py").read_text(encoding="utf-8")
        self.assertNotIn("fin == \"stop\" or True", text)
        self.assertNotIn("or True):", text)
        self.assertNotIn("accepted after wall timeout", text)
        self.assertNotIn("early accept: hard_rule_allowlisted", text)

    def test_cut_no_or_arts_shortcut(self):
        c2 = (Path(__file__).resolve().parent / "cut2.py").read_text(encoding="utf-8")
        self.assertNotIn("fin == \"stop\" or arts", c2)
        c3 = (Path(__file__).resolve().parent / "cut3.py").read_text(encoding="utf-8")
        self.assertNotIn("deadline = max(deadline, time.time() + 240)", c3)


if __name__ == "__main__":
    raise SystemExit(unittest.main(verbosity=2))
