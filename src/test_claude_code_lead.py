"""Claude Code lead adapter: fake claude executable only, no real model calls, no credentials."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from lead_adapter import get_lead_adapter
from lead_adapter.cancel import CancelScope, use_scope
from lead_adapter.claude_code import DEFAULT_MODEL, ClaudeBinNotFound, ClaudeCodeLeadAdapter, build_command, resolve_claude_bin
from lead_adapter.schema import (
    LeadDecisionError,
    build_lead_request,
    lead_permission_response_schema,
    lead_review_response_schema,
    validate_lead_decision,
)

FAKE = r'''
import json, os, sys, time
rec = os.environ.get("FAKE_CLAUDE_RECORD")
data = sys.stdin.read()
if rec:
    with open(rec, "w", encoding="utf-8") as f:
        json.dump({"argv": sys.argv[1:], "stdin": data, "cwd": os.getcwd()}, f)
mode = os.environ.get("FAKE_CLAUDE_MODE", "structured")
payload = json.loads(os.environ.get("FAKE_CLAUDE_PAYLOAD", "{}"))
def out(obj, rc=0):
    sys.stdout.write(json.dumps(obj)); sys.stdout.flush(); sys.exit(rc)
if mode == "structured":
    a = sys.argv[1:]
    used = os.environ.get("FAKE_CLAUDE_USED") or (a[a.index("--model") + 1] if "--model" in a else "default-model")
    out({"type": "result", "subtype": "success", "is_error": False, "result": "done", "structured_output": payload,
         "total_cost_usd": 0.0012, "num_turns": 1, "modelUsage": {used: {"costUSD": 0.0012}}})
if mode == "result_text":
    out({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(payload)})
if mode == "result_prose":
    out({"type": "result", "subtype": "success", "is_error": False, "result": "Sure! " + json.dumps(payload)})
if mode == "not_logged_in":
    out({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in \u00b7 Please run /login"}, 1)
if mode == "is_error_rc0":
    out({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom", "structured_output": payload})
if mode == "nonzero_valid_json":
    out({"type": "result", "subtype": "success", "is_error": False, "structured_output": payload}, 3)
if mode == "garbage":
    sys.stdout.write("this is not json"); sys.exit(0)
if mode == "empty":
    sys.exit(0)
if mode == "secret_stderr":
    sys.stderr.write("fatal: token sk-SYNTHSECRET0123456789abcdef rejected"); sys.exit(2)
if mode == "sleep":
    time.sleep(30)
'''


def _write_fake(d: Path) -> str:
    p = d / "claude"
    p.write_text("#!" + sys.executable + "\n" + FAKE, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


def _req(kind: str = "permission") -> dict:
    return build_lead_request(
        kind=kind,
        goal="SYNTH goal",
        authorized_scope=["ws"],
        prohibitions=[],
        acceptance_criteria={},
        current_application={"id": "p1"},
    )


def _ok_payload(req: dict, **over) -> dict:
    p = {"application_id": req["application_id"], "context_summary": req["context_summary"]}
    if req.get("kind") == "review":
        p.update({"verdict": "pass", "reason": "ok"})
    else:
        p.update({"decision": "once", "reason": "ok"})
    p.update(over)
    return p


@unittest.skipIf(sys.platform == "win32", "posix shebang fake binary")
class ClaudeLeadTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        self.bin = _write_fake(self.dir)
        self.rec = self.dir / "rec.json"
        self.env = mock.patch.dict(os.environ, {"FAKE_CLAUDE_RECORD": str(self.rec)}, clear=False)
        self.env.start()
        for k in ("COLLAB_CLAUDE_LEAD_BIN", "COLLAB_CLAUDE_LEAD_MODEL", "FAKE_CLAUDE_MODE", "FAKE_CLAUDE_PAYLOAD"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()
        self.td.cleanup()

    def _run(self, mode, req=None, payload=None, schema=None, timeout=20, **kw):
        req = req or _req()
        os.environ["FAKE_CLAUDE_MODE"] = mode
        os.environ["FAKE_CLAUDE_PAYLOAD"] = json.dumps(payload if payload is not None else _ok_payload(req))
        schema = schema or (lead_review_response_schema() if req.get("kind") == "review" else lead_permission_response_schema())
        ad = ClaudeCodeLeadAdapter(bin_path=self.bin, **kw)
        return req, ad.decide(req, schema=schema, cwd=str(self.dir), timeout_sec=timeout)

    def _status(self, parsed):
        return (parsed or {}).get("_lead_status")

    # --- success paths -----------------------------------------------------
    def test_structured_output_permission_validates(self):
        req, (raw, parsed) = self._run("structured")
        self.assertIsNone(self._status(parsed), parsed)
        self.assertEqual(parsed["decision"], "once")
        validate_lead_decision(raw, parsed, request=req, kind="permission")

    def test_review_structured_output_validates(self):
        req = _req("review")
        req, (raw, parsed) = self._run("structured", req=req)
        self.assertEqual(parsed["verdict"], "pass")
        validate_lead_decision(raw, parsed, request=req, kind="review")

    def test_result_text_json_fallback(self):
        req, (raw, parsed) = self._run("result_text")
        self.assertEqual(parsed.get("decision"), "once", parsed)

    # --- argv: read-only, non-interactive, schema pinned, prompt on stdin --
    def test_argv_is_read_only_and_pins_schema(self):
        req, _ = self._run("structured")
        r = json.loads(self.rec.read_text(encoding="utf-8"))
        a = r["argv"]
        self.assertIn("-p", a)
        self.assertEqual(a[a.index("--output-format") + 1], "json")
        self.assertEqual(a[a.index("--tools") + 1], "")
        self.assertEqual(a[a.index("--permission-mode") + 1], "plan")
        self.assertEqual(a[a.index("--permission-prompts") + 1], "none")
        self.assertIn("--no-session-persistence", a)
        self.assertIn("--strict-mcp-config", a)
        dis = a[a.index("--disallowedTools") + 1]
        for t in ("Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch"):
            self.assertIn(t, dis)
        for bad in ("--dangerously-skip-permissions", "bypassPermissions", "acceptEdits", "--add-dir"):
            self.assertNotIn(bad, a)
        pinned = json.loads(a[a.index("--json-schema") + 1])
        self.assertEqual(pinned["properties"]["application_id"]["const"], req["application_id"])
        self.assertIn(req["application_id"], r["stdin"])  # prompt via stdin, not argv
        self.assertEqual(os.path.realpath(r["cwd"]), os.path.realpath(str(self.dir)))
        self.assertEqual(a[a.index("--model") + 1], DEFAULT_MODEL)

    def test_default_model_is_pinned_full_id(self):
        self.assertEqual(DEFAULT_MODEL, "claude-haiku-5-5")
        self.assertRegex(DEFAULT_MODEL, r"^claude-[a-z]+-\d")  # full ID, not an alias

    def test_last_run_records_model_used_and_cost(self):
        ad = ClaudeCodeLeadAdapter(bin_path=self.bin)
        req = _req()
        os.environ["FAKE_CLAUDE_MODE"] = "structured"
        os.environ["FAKE_CLAUDE_PAYLOAD"] = json.dumps(_ok_payload(req))
        ad.decide(req, schema=lead_permission_response_schema(), cwd=str(self.dir))
        lr = ad.last_run
        self.assertEqual(lr["model_requested"], DEFAULT_MODEL)
        self.assertEqual(lr["models_used"], [DEFAULT_MODEL])
        self.assertFalse(lr["model_mismatch"])
        self.assertAlmostEqual(lr["cost_usd"], 0.0012)
        self.assertEqual(ad.doctor_hint()["last_run"], lr)

    def test_model_mismatch_is_flagged_not_hidden(self):
        os.environ["FAKE_CLAUDE_USED"] = "claude-sonnet-9-9"
        try:
            ad = ClaudeCodeLeadAdapter(bin_path=self.bin)
            req = _req()
            os.environ["FAKE_CLAUDE_MODE"] = "structured"
            os.environ["FAKE_CLAUDE_PAYLOAD"] = json.dumps(_ok_payload(req))
            raw, parsed = ad.decide(req, schema=lead_permission_response_schema(), cwd=str(self.dir))
        finally:
            os.environ.pop("FAKE_CLAUDE_USED", None)
        self.assertEqual(parsed["decision"], "once")
        self.assertTrue(ad.last_run["model_mismatch"])
        self.assertEqual(ad.last_run["models_used"], ["claude-sonnet-9-9"])

    def test_model_flag_from_kwarg_and_env(self):
        self._run("structured", model="SYNTH-model-a")
        a = json.loads(self.rec.read_text())["argv"]
        self.assertEqual(a[a.index("--model") + 1], "SYNTH-model-a")
        os.environ["COLLAB_CLAUDE_LEAD_MODEL"] = "SYNTH-model-b"
        self._run("structured")
        a = json.loads(self.rec.read_text())["argv"]
        self.assertEqual(a[a.index("--model") + 1], "SYNTH-model-b")

    def test_build_command_never_has_bypass(self):
        a = build_command("/x/claude", schema={"type": "object"}, model="claude-haiku-5-5")
        self.assertEqual(a[0], "/x/claude")
        self.assertNotIn("--dangerously-skip-permissions", a)

    # --- failures: never fake pass ----------------------------------------
    def _assert_fails(self, parsed, raw, req, status):
        self.assertEqual(self._status(parsed), status, parsed)
        self.assertNotIn((parsed or {}).get("decision"), ("once", "always"))
        self.assertNotIn((parsed or {}).get("verdict"), ("pass",))
        with self.assertRaises(LeadDecisionError):
            validate_lead_decision(raw, parsed, request=req, kind=req.get("kind"))

    def test_missing_binary_is_call_failed(self):
        req = _req()
        ad = ClaudeCodeLeadAdapter(bin_path=str(self.dir / "nope"))
        raw, parsed = ad.decide(req, schema=lead_permission_response_schema(), cwd=str(self.dir))
        self._assert_fails(parsed, raw, req, "call_failed")
        self.assertIn("not found", parsed["error"])

    def test_resolve_bin_env_then_path(self):
        self.assertEqual(resolve_claude_bin(env={"COLLAB_CLAUDE_LEAD_BIN": self.bin}, which=lambda n: None), self.bin)
        self.assertEqual(resolve_claude_bin(env={}, which=lambda n: self.bin if n == "claude" else None), self.bin)
        with self.assertRaises(ClaudeBinNotFound):
            resolve_claude_bin(env={"COLLAB_LEAD_BIN": self.bin}, which=lambda n: None)  # grok's var is not used

    def test_not_logged_in_is_call_failed_with_code(self):
        req, (raw, parsed) = self._run("not_logged_in")
        self._assert_fails(parsed, raw, req, "call_failed")
        self.assertEqual(parsed.get("lead_error_code"), "not_logged_in")

    def test_is_error_with_rc0_is_call_failed(self):
        req, (raw, parsed) = self._run("is_error_rc0")
        self._assert_fails(parsed, raw, req, "call_failed")

    def test_nonzero_exit_never_a_decision_even_with_valid_json(self):
        req, (raw, parsed) = self._run("nonzero_valid_json")
        self._assert_fails(parsed, raw, req, "call_failed")
        self.assertEqual(parsed.get("returncode"), 3)

    def test_unparseable_output_is_error(self):
        for mode in ("garbage", "empty", "result_prose"):
            with self.subTest(mode=mode):
                req, (raw, parsed) = self._run(mode)
                self._assert_fails(parsed, raw, req, "error")

    def test_application_id_mismatch_is_error(self):
        req = _req()
        req, (raw, parsed) = self._run("structured", req=req, payload=_ok_payload(req, application_id="other"))
        self._assert_fails(parsed, raw, req, "error")
        self.assertEqual(parsed.get("lead_error_code"), "application_id_mismatch")

    def test_schema_invalid_decision_is_error(self):
        req = _req()
        req, (raw, parsed) = self._run("structured", req=req, payload=_ok_payload(req, decision="maybe"))
        self._assert_fails(parsed, raw, req, "error")

    def test_review_invalid_verdict_is_error(self):
        req = _req("review")
        req, (raw, parsed) = self._run("structured", req=req, payload=_ok_payload(req, verdict="sure"))
        self._assert_fails(parsed, raw, req, "error")

    def test_timeout_kills_and_reports_timeout(self):
        t0 = time.monotonic()
        req, (raw, parsed) = self._run("sleep", timeout=1.0)
        self.assertLess(time.monotonic() - t0, 10)
        self._assert_fails(parsed, raw, req, "timeout")

    def test_cancel_scope_stops_lead(self):
        scope = CancelScope()
        with use_scope(scope):
            scope.cancel("SYNTH goal cancelled")
            req, (raw, parsed) = self._run("sleep", timeout=20)
        self._assert_fails(parsed, raw, req, "call_failed")
        self.assertIn("stopped", parsed["error"])

    def test_secret_in_stderr_is_redacted(self):
        req, (raw, parsed) = self._run("secret_stderr")
        self._assert_fails(parsed, raw, req, "call_failed")
        self.assertNotIn("SYNTHSECRET0123456789abcdef", json.dumps(parsed) + raw)

    def test_uses_run_cancellable_not_subprocess_run(self):
        src = (Path(__file__).resolve().parent / "lead_adapter" / "claude_code.py").read_text(encoding="utf-8")
        self.assertIn("run_cancellable(", src)
        self.assertNotIn("subprocess.run(", src)

    # --- registry ----------------------------------------------------------
    def test_registry_passes_bin_path(self):
        ad = get_lead_adapter("claude_code", bin_path=self.bin)
        self.assertIsInstance(ad, ClaudeCodeLeadAdapter)
        self.assertEqual(ad.doctor_hint().get("bin_path"), self.bin)
        self.assertFalse(ad.doctor_hint().get("fake_pass"))


if __name__ == "__main__":
    unittest.main()
