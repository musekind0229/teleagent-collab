#!/usr/bin/env python3
"""DeepSeek harness LeadAdapter tests — simulated; 真 harness 未接线验收."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parent
REPO = _SRC.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from lead_adapter import (
    DeepSeekHarnessLeadAdapter,
    LeadDecisionError,
    build_lead_request,
    get_lead_adapter,
    lead_permission_response_schema,
    lead_review_response_schema,
    pin_lead_response_schema,
    validate_lead_decision,
)
from lead_adapter.schema import format_lead_request_prompt

SIMULATED = True
WRAPPER = REPO / "bin" / "run-deepseek-lead.py"


def _req(kind: str = "permission"):
    return build_lead_request(
        kind=kind,
        goal="stay in ws",
        authorized_scope=["ws"],
        prohibitions=["secrets"],
        acceptance_criteria={"artifacts": ["a.txt"]},
        current_application={"tool": "write"},
    )


def _ok_permission(req: dict) -> dict:
    return {
        "application_id": req["application_id"],
        "context_summary": req["context_summary"],
        "decision": "once",
        "reason": "in scope",
    }


def _ok_review(req: dict) -> dict:
    return {
        "application_id": req["application_id"],
        "context_summary": req["context_summary"],
        "verdict": "pass",
        "reason": "artifacts present",
    }


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class TestFactory(unittest.TestCase):
    def test_kind_and_aliases(self):
        for kind in ("deepseek_harness", "deepseek", "deepseek-harness"):
            ad = get_lead_adapter(kind, bin_path="/bin/true")
            self.assertIsInstance(ad, DeepSeekHarnessLeadAdapter)
            self.assertEqual(ad.name, "deepseek_harness")

    def test_env_kind(self):
        with mock.patch.dict(os.environ, {"COLLAB_LEAD_ADAPTER": "deepseek"}, clear=False):
            ad = get_lead_adapter()
            self.assertIsInstance(ad, DeepSeekHarnessLeadAdapter)

    def test_doctor_hint_unwired_no_fake_pass(self):
        ad = get_lead_adapter("deepseek_harness", bin_path=str(WRAPPER))
        hint = ad.doctor_hint()
        self.assertEqual(hint["status"], "unwired")
        self.assertFalse(hint["fake_pass"])
        self.assertFalse(hint["wired"])
        self.assertIn("未接线验收", hint["reason"])

    def test_bin_precedence(self):
        with mock.patch.dict(
            os.environ,
            {
                "COLLAB_LEAD_BIN": "/tmp/grok-lead",
                "COLLAB_DEEPSEEK_LEAD_BIN": "/tmp/deepseek-wrapper",
            },
            clear=False,
        ):
            ad = DeepSeekHarnessLeadAdapter()
            self.assertEqual(ad.bin_path, "/tmp/deepseek-wrapper")
            ad2 = DeepSeekHarnessLeadAdapter(bin_path="/tmp/explicit")
            self.assertEqual(ad2.bin_path, "/tmp/explicit")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COLLAB_LEAD_BIN", None)
            os.environ.pop("COLLAB_DEEPSEEK_LEAD_BIN", None)
            ad3 = DeepSeekHarnessLeadAdapter()
            self.assertTrue(ad3.bin_path.endswith("bin/run-deepseek-lead.py"))


class TestDeepSeekDecideSim(unittest.TestCase):
    def test_legal_once_binds_and_validates(self):
        req = _req("permission")
        body = _ok_permission(req)
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")
            return _Proc(stdout=json.dumps(body))

        ad = DeepSeekHarnessLeadAdapter(bin_path="/bin/echo")
        with mock.patch("lead_adapter.deepseek_harness.subprocess.run", fake_run):
            raw, parsed = ad.decide(
                req, schema=lead_permission_response_schema(), cwd="/tmp"
            )
        self.assertEqual(parsed["decision"], "once")
        out = validate_lead_decision(raw, parsed, request=req, kind="permission")
        self.assertEqual(out["decision"], "once")
        self.assertEqual(out["application_id"], req["application_id"])
        self.assertEqual(out["context_summary"], req["context_summary"])
        envelope = json.loads(captured["input"])
        self.assertEqual(envelope["request"]["application_id"], req["application_id"])
        self.assertEqual(
            envelope["schema"]["properties"]["application_id"]["const"],
            req["application_id"],
        )
        self.assertEqual(
            envelope["schema"]["properties"]["context_summary"]["const"],
            req["context_summary"],
        )
        self.assertIn("context_summary", envelope["schema"]["required"])
        self.assertTrue(envelope["constraints"]["no_retry_without_constraints"])

    def test_legal_pass_review(self):
        req = _req("review")
        body = _ok_review(req)

        def fake_run(cmd, **kwargs):
            return _Proc(stdout=json.dumps(body))

        ad = DeepSeekHarnessLeadAdapter(bin_path="/bin/echo")
        with mock.patch("lead_adapter.deepseek_harness.subprocess.run", fake_run):
            raw, parsed = ad.decide(
                req, schema=lead_review_response_schema(), cwd="/tmp"
            )
        out = validate_lead_decision(raw, parsed, request=req, kind="review")
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual(out["application_id"], req["application_id"])

    def test_illegal_json_fail_closed(self):
        req = _req("permission")

        def fake_run(cmd, **kwargs):
            return _Proc(stdout="not-json {{{", returncode=0)

        ad = DeepSeekHarnessLeadAdapter(bin_path="/bin/echo")
        with mock.patch("lead_adapter.deepseek_harness.subprocess.run", fake_run):
            raw, parsed = ad.decide(
                req, schema=lead_permission_response_schema(), cwd="/tmp"
            )
        self.assertIsNone(parsed)
        with self.assertRaises(LeadDecisionError) as cm:
            validate_lead_decision(raw, parsed, request=req, kind="permission")
        self.assertEqual(cm.exception.code, "illegal_json")

    def test_timeout_fail_closed(self):
        req = _req("review")

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

        ad = DeepSeekHarnessLeadAdapter(bin_path="/bin/echo")
        with mock.patch("lead_adapter.deepseek_harness.subprocess.run", fake_run):
            raw, parsed = ad.decide(
                req, schema=lead_review_response_schema(), cwd="/tmp", timeout_sec=1
            )
        self.assertEqual(parsed.get("_lead_status"), "timeout")
        self.assertNotEqual(parsed.get("verdict"), "pass")
        self.assertNotEqual(parsed.get("decision"), "once")
        with self.assertRaises(LeadDecisionError) as cm:
            validate_lead_decision(raw, parsed, request=req, kind="review")
        self.assertEqual(cm.exception.code, "timeout")

    def test_spawn_failed_fail_closed(self):
        req = _req("permission")

        def fake_run(cmd, **kwargs):
            raise OSError("no such file")

        ad = DeepSeekHarnessLeadAdapter(bin_path="/no/such/deepseek-wrapper")
        with mock.patch("lead_adapter.deepseek_harness.subprocess.run", fake_run):
            raw, parsed = ad.decide(
                req, schema=lead_permission_response_schema(), cwd="/tmp"
            )
        self.assertEqual(parsed.get("_lead_status"), "call_failed")
        self.assertNotIn(parsed.get("decision"), ("once", "pass"))
        with self.assertRaises(LeadDecisionError) as cm:
            validate_lead_decision(raw, parsed, request=req, kind="permission")
        self.assertEqual(cm.exception.code, "call_failed")

    def test_nonzero_exit_no_stdout_call_failed(self):
        req = _req("permission")

        def fake_run(cmd, **kwargs):
            return _Proc(stdout="", stderr="harness exploded", returncode=2)

        ad = DeepSeekHarnessLeadAdapter(bin_path="/bin/echo")
        with mock.patch("lead_adapter.deepseek_harness.subprocess.run", fake_run):
            raw, parsed = ad.decide(
                req, schema=lead_permission_response_schema(), cwd="/tmp"
            )
        self.assertEqual(parsed.get("_lead_status"), "call_failed")
        with self.assertRaises(LeadDecisionError) as cm:
            validate_lead_decision(raw, parsed, request=req, kind="permission")
        self.assertEqual(cm.exception.code, "call_failed")

    def test_application_id_mismatch_fail_closed(self):
        req = _req("permission")
        body = _ok_permission(req)
        body["application_id"] = "app_other_id"

        def fake_run(cmd, **kwargs):
            return _Proc(stdout=json.dumps(body))

        ad = DeepSeekHarnessLeadAdapter(bin_path="/bin/echo")
        with mock.patch("lead_adapter.deepseek_harness.subprocess.run", fake_run):
            raw, parsed = ad.decide(
                req, schema=lead_permission_response_schema(), cwd="/tmp"
            )
        with self.assertRaises(LeadDecisionError) as cm:
            validate_lead_decision(raw, parsed, request=req, kind="permission")
        self.assertEqual(cm.exception.code, "application_id_mismatch")

    def test_context_summary_mismatch_fail_closed(self):
        req = _req("review")
        body = _ok_review(req)
        body["context_summary"] = "rewritten summary"

        def fake_run(cmd, **kwargs):
            return _Proc(stdout=json.dumps(body))

        ad = DeepSeekHarnessLeadAdapter(bin_path="/bin/echo")
        with mock.patch("lead_adapter.deepseek_harness.subprocess.run", fake_run):
            raw, parsed = ad.decide(
                req, schema=lead_review_response_schema(), cwd="/tmp"
            )
        with self.assertRaises(LeadDecisionError) as cm:
            validate_lead_decision(raw, parsed, request=req, kind="review")
        self.assertEqual(cm.exception.code, "context_summary_mismatch")

    def test_pin_schema_matches_helper(self):
        req = _req("review")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["input"] = kwargs.get("input")
            return _Proc(stdout=json.dumps(_ok_review(req)))

        ad = DeepSeekHarnessLeadAdapter(bin_path="/bin/echo")
        schema = lead_review_response_schema()
        with mock.patch("lead_adapter.deepseek_harness.subprocess.run", fake_run):
            ad.decide(req, schema=schema, cwd="/tmp")
        expected = pin_lead_response_schema(schema, req)
        got = json.loads(captured["input"])["schema"]
        self.assertEqual(
            got["properties"]["application_id"], expected["properties"]["application_id"]
        )
        self.assertEqual(
            got["properties"]["context_summary"],
            expected["properties"]["context_summary"],
        )

    def test_file_io_mode_writes_request_file(self):
        req = _req("permission")
        body = _ok_permission(req)
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")
            idx = cmd.index("--request-file")
            captured["path"] = cmd[idx + 1]
            captured["exists_during_run"] = os.path.isfile(captured["path"])
            captured["payload"] = Path(captured["path"]).read_text(encoding="utf-8")
            return _Proc(stdout=json.dumps(body))

        ad = DeepSeekHarnessLeadAdapter(bin_path="/bin/echo", io_mode="file")
        with mock.patch("lead_adapter.deepseek_harness.subprocess.run", fake_run):
            raw, parsed = ad.decide(
                req, schema=lead_permission_response_schema(), cwd="/tmp"
            )
        self.assertIsNone(captured["input"])
        self.assertTrue(captured["exists_during_run"])
        self.assertFalse(os.path.exists(captured["path"]))
        envelope = json.loads(captured["payload"])
        self.assertEqual(envelope["request"]["application_id"], req["application_id"])
        out = validate_lead_decision(raw, parsed, request=req, kind="permission")
        self.assertEqual(out["decision"], "once")

    def test_prompt_is_self_contained(self):
        req = _req("permission")
        prompt = format_lead_request_prompt(req)
        self.assertIn(req["application_id"], prompt)
        self.assertIn(req["context_summary"], prompt)
        self.assertIn("once|reject|deny_job|demand_safe_path", prompt)

    def test_no_constraint_retry_in_source(self):
        src = (_SRC / "lead_adapter" / "deepseek_harness.py").read_text(encoding="utf-8")
        self.assertIn("NO retry that drops constraints", src)
        self.assertNotIn("retry without disallowed-tools", src)
        self.assertNotIn("api.deepseek.com", src)
        wrapper = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("真 harness 未接线验收", wrapper)
        self.assertNotIn("api.deepseek.com", wrapper.lower())


class TestExampleWrapperFailClosed(unittest.TestCase):
    def test_wrapper_exits_nonzero_no_once_pass(self):
        req = _req("permission")
        envelope = {
            "protocol": "collab-lead-v1",
            "adapter": "deepseek_harness",
            "request": req,
            "schema": pin_lead_response_schema(lead_permission_response_schema(), req),
            "cwd": "/tmp",
            "prompt": format_lead_request_prompt(req),
        }
        proc = subprocess.run(
            [sys.executable, str(WRAPPER)],
            input=json.dumps(envelope),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual((proc.stdout or "").strip(), "")
        self.assertNotIn('"decision": "once"', proc.stdout)
        self.assertNotIn('"verdict": "pass"', proc.stdout)
        self.assertIn("未接线验收", proc.stderr)
        self.assertIn(req["application_id"], proc.stderr)

    def test_wrapper_still_fail_closed_if_harness_bin_set(self):
        req = _req("review")
        env = os.environ.copy()
        env["COLLAB_DEEPSEEK_HARNESS_BIN"] = "/usr/bin/true"
        proc = subprocess.run(
            [sys.executable, str(WRAPPER)],
            input=json.dumps({"request": req}),
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual((proc.stdout or "").strip(), "")
        self.assertIn("not wired", proc.stderr)

    def test_adapter_default_wrapper_fail_closed(self):
        """Unmocked spawn of in-tree wrapper must not invent once/pass."""
        req = _req("permission")
        ad = DeepSeekHarnessLeadAdapter(bin_path=str(WRAPPER))
        raw, parsed = ad.decide(
            req, schema=lead_permission_response_schema(), cwd="/tmp", timeout_sec=10
        )
        self.assertEqual(parsed.get("_lead_status"), "call_failed")
        self.assertNotIn(parsed.get("decision"), ("once", "pass"))
        with self.assertRaises(LeadDecisionError) as cm:
            validate_lead_decision(raw, parsed, request=req, kind="permission")
        self.assertEqual(cm.exception.code, "call_failed")


class TestGluePinsWithDeepSeek(unittest.TestCase):
    def test_call_lead_request_pins_schema(self):
        import glue

        req = _req("review")
        captured = {}

        class Fake:
            name = "deepseek_harness"

            def decide(self, request, *, schema, cwd, timeout_sec=180):
                captured["schema"] = schema
                body = _ok_review(request)
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


if __name__ == "__main__":
    raise SystemExit(unittest.main(verbosity=2))
