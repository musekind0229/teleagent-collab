#!/usr/bin/env python3
"""DeepSeek harness LeadAdapter tests — simulated; live dsh skipped without bin+key."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import stat
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

_FAKE_DSH = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

argv_path = os.environ.get("COLLAB_FAKE_DSH_ARGV", "")
if argv_path:
    Path(argv_path).write_text(json.dumps(sys.argv), encoding="utf-8")
stdin_path = os.environ.get("COLLAB_FAKE_DSH_STDIN", "")
task = sys.stdin.read()
if stdin_path:
    Path(stdin_path).write_text(task, encoding="utf-8")
sys.stdout.write(os.environ.get("COLLAB_FAKE_DSH_STDOUT", ""))
sys.stderr.write(os.environ.get("COLLAB_FAKE_DSH_STDERR", ""))
raise SystemExit(int(os.environ.get("COLLAB_FAKE_DSH_EXIT", "0")))
'''


def _load_wrapper():
    spec = importlib.util.spec_from_file_location("run_deepseek_lead", WRAPPER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _envelope(req: dict, kind: str = "permission") -> dict:
    schema = (
        lead_permission_response_schema()
        if kind == "permission"
        else lead_review_response_schema()
    )
    return {
        "protocol": "collab-lead-v1",
        "adapter": "deepseek_harness",
        "request": req,
        "schema": pin_lead_response_schema(schema, req),
        "cwd": "/tmp",
        "prompt": format_lead_request_prompt(req),
        "timeout_sec": 30,
    }


def _live_dsh_ready() -> bool:
    spec = (os.environ.get("COLLAB_DEEPSEEK_HARNESS_BIN") or "").strip()
    has_bin = bool(spec) or bool(shutil.which("dsh"))
    has_key = bool((os.environ.get("DEEPSEEK_API_KEY") or "").strip())
    return has_bin and has_key


def _fail_closed_env() -> dict:
    env = os.environ.copy()
    env.pop("COLLAB_DEEPSEEK_HARNESS_BIN", None)
    env.pop("DEEPSEEK_API_KEY", None)
    return env


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

    def test_doctor_hint_no_fake_pass(self):
        ad = get_lead_adapter("deepseek_harness", bin_path=str(WRAPPER))
        hint = ad.doctor_hint()
        self.assertEqual(hint["status"], "dsh_headless")
        self.assertFalse(hint["fake_pass"])
        self.assertFalse(hint["wired"])
        self.assertIn("dsh --profile headless", hint["reason"])
        self.assertIn("never invent once/pass", hint["reason"])

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
        self.assertIn("--profile headless", wrapper)
        self.assertIn("Do not use bash", wrapper)
        self.assertNotIn("api.deepseek.com", wrapper.lower())
        self.assertNotIn("--json", build_dsh_cmd_src(wrapper))


def build_dsh_cmd_src(wrapper_src: str) -> str:
    """Slice of wrapper that builds dsh argv — `--json` must not be passed."""
    start = wrapper_src.find("def build_dsh_cmd")
    end = wrapper_src.find("\ndef ", start + 1)
    return wrapper_src[start:end] if start >= 0 else wrapper_src


class TestExampleWrapperFailClosed(unittest.TestCase):
    def test_wrapper_exits_nonzero_no_once_pass_without_bin_or_key(self):
        req = _req("permission")
        envelope = _envelope(req, "permission")
        proc = subprocess.run(
            [sys.executable, str(WRAPPER)],
            input=json.dumps(envelope),
            capture_output=True,
            text=True,
            timeout=10,
            env=_fail_closed_env(),
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual((proc.stdout or "").strip(), "")
        self.assertNotIn('"decision": "once"', proc.stdout)
        self.assertNotIn('"verdict": "pass"', proc.stdout)
        self.assertIn("missing", proc.stderr)
        self.assertTrue(
            "COLLAB_DEEPSEEK_HARNESS_BIN" in proc.stderr
            or "DEEPSEEK_API_KEY" in proc.stderr
        )
        self.assertIn(req["application_id"], proc.stderr)

    def test_wrapper_missing_key_does_not_invent_once(self):
        req = _req("review")
        env = _fail_closed_env()
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
        self.assertIn("DEEPSEEK_API_KEY", proc.stderr)
        self.assertNotIn('"verdict": "pass"', proc.stdout)

    def test_adapter_default_wrapper_fail_closed(self):
        """Unmocked spawn of in-tree wrapper without bin/key must not invent once/pass."""
        req = _req("permission")
        ad = DeepSeekHarnessLeadAdapter(bin_path=str(WRAPPER))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COLLAB_DEEPSEEK_HARNESS_BIN", None)
            os.environ.pop("DEEPSEEK_API_KEY", None)
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


class TestWrapperDshInvoke(unittest.TestCase):
    """Mock subprocess: wrapper calls dsh --profile headless, not a fake once/pass."""

    def setUp(self):
        self.mod = _load_wrapper()

    def _run_main(self, envelope: dict, fake_run, env: dict):
        fd, path = tempfile.mkstemp(prefix="collab-ds-env-", suffix=".json")
        os.close(fd)
        Path(path).write_text(json.dumps(envelope), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch.object(self.mod.subprocess, "run", fake_run):
                    with mock.patch.object(self.mod.sys, "stdout", out):
                        with mock.patch.object(self.mod.sys, "stderr", err):
                            rc = self.mod.main(["--request-file", path])
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return rc, out.getvalue(), err.getvalue()

    def test_mock_success_permission_once(self):
        req = _req("permission")
        body = _ok_permission(req)
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")
            captured["cwd"] = kwargs.get("cwd")
            return _Proc(stdout=json.dumps(body), returncode=0)

        rc, stdout, stderr = self._run_main(
            _envelope(req, "permission"),
            fake_run,
            {
                "COLLAB_DEEPSEEK_HARNESS_BIN": "/usr/bin/true",
                "DEEPSEEK_API_KEY": "sk-test-not-real",
            },
        )
        self.assertEqual(rc, 0)
        parsed = json.loads(stdout)
        self.assertEqual(parsed["decision"], "once")
        self.assertEqual(parsed["application_id"], req["application_id"])
        self.assertEqual(parsed["context_summary"], req["context_summary"])
        self.assertIn("--profile", captured["cmd"])
        self.assertIn("headless", captured["cmd"])
        self.assertNotIn("--json", captured["cmd"])
        self.assertIn("-", captured["cmd"])
        task = captured["input"] or ""
        self.assertIn("Do not use bash", task)
        self.assertIn(req["application_id"], task)
        self.assertNotEqual(captured["cwd"], str(REPO))
        self.assertNotIn('"decision": "once"', stderr)

    def test_mock_success_review_pass(self):
        req = _req("review")
        body = _ok_review(req)

        def fake_run(cmd, **kwargs):
            preamble = "thinking...\n"
            return _Proc(stdout=preamble + json.dumps(body) + "\n", returncode=0)

        rc, stdout, _stderr = self._run_main(
            _envelope(req, "review"),
            fake_run,
            {
                "COLLAB_DEEPSEEK_HARNESS_BIN": "/usr/bin/true",
                "DEEPSEEK_API_KEY": "sk-test-not-real",
            },
        )
        self.assertEqual(rc, 0)
        parsed = json.loads(stdout)
        self.assertEqual(parsed["verdict"], "pass")
        self.assertEqual(parsed["application_id"], req["application_id"])

    def test_mock_nonzero_exit_no_once_pass(self):
        req = _req("permission")
        body = _ok_permission(req)

        def fake_run(cmd, **kwargs):
            return _Proc(stdout=json.dumps(body), stderr="dsh: boom", returncode=1)

        rc, stdout, stderr = self._run_main(
            _envelope(req, "permission"),
            fake_run,
            {
                "COLLAB_DEEPSEEK_HARNESS_BIN": "/usr/bin/true",
                "DEEPSEEK_API_KEY": "sk-test-not-real",
            },
        )
        self.assertEqual(rc, 2)
        self.assertEqual((stdout or "").strip(), "")
        self.assertNotIn('"decision":"once"', stdout.replace(" ", ""))
        self.assertIn("exit=1", stderr)

    def test_mock_illegal_json_no_once_pass(self):
        req = _req("permission")

        def fake_run(cmd, **kwargs):
            return _Proc(stdout="not a decision {{{", returncode=0)

        rc, stdout, stderr = self._run_main(
            _envelope(req, "permission"),
            fake_run,
            {
                "COLLAB_DEEPSEEK_HARNESS_BIN": "/usr/bin/true",
                "DEEPSEEK_API_KEY": "sk-test-not-real",
            },
        )
        self.assertEqual(rc, 2)
        self.assertEqual((stdout or "").strip(), "")
        self.assertNotIn('"decision": "once"', stdout)
        self.assertTrue(
            "illegal_json" in stderr
            or "illegal JSON" in stderr
            or "no parseable" in stderr
        )

    def test_mock_application_id_mismatch_no_once_pass(self):
        req = _req("permission")
        body = _ok_permission(req)
        body["application_id"] = "app_other_id"

        def fake_run(cmd, **kwargs):
            return _Proc(stdout=json.dumps(body), returncode=0)

        rc, stdout, stderr = self._run_main(
            _envelope(req, "permission"),
            fake_run,
            {
                "COLLAB_DEEPSEEK_HARNESS_BIN": "/usr/bin/true",
                "DEEPSEEK_API_KEY": "sk-test-not-real",
            },
        )
        self.assertEqual(rc, 2)
        self.assertEqual((stdout or "").strip(), "")
        self.assertIn("application_id", stderr)

    def test_mock_illegal_decision_no_once_pass(self):
        req = _req("permission")
        body = _ok_permission(req)
        body["decision"] = "always_yolo"

        def fake_run(cmd, **kwargs):
            return _Proc(stdout=json.dumps(body), returncode=0)

        rc, stdout, _stderr = self._run_main(
            _envelope(req, "permission"),
            fake_run,
            {
                "COLLAB_DEEPSEEK_HARNESS_BIN": "/usr/bin/true",
                "DEEPSEEK_API_KEY": "sk-test-not-real",
            },
        )
        self.assertEqual(rc, 2)
        self.assertEqual((stdout or "").strip(), "")

    def test_last_json_object_picks_last_decision(self):
        req = _req("permission")
        first = {"type": "session", "id": "x"}
        second = _ok_permission(req)
        blob = json.dumps(first) + "\nprose\n" + json.dumps(second)
        got = self.mod.last_json_object(blob)
        self.assertEqual(got["application_id"], req["application_id"])
        self.assertEqual(got["decision"], "once")
        self.assertIsNone(self.mod.last_json_object(""))
        self.assertIsNone(self.mod.last_json_object("no json here"))

    def test_headless_task_forbids_tools(self):
        req = _req("review")
        task = self.mod.build_headless_task(_envelope(req, "review"))
        self.assertIn("Do not use bash", task)
        self.assertIn("not a worker", task.lower())
        self.assertIn(req["application_id"], task)
        self.assertIn(req["context_summary"], task)
        cmd = self.mod.build_dsh_cmd(["dsh"])
        self.assertEqual(cmd, ["dsh", "--profile", "headless", "-"])
        self.assertNotIn("--json", cmd)

    def test_npx_wrapper_argv(self):
        with mock.patch.object(self.mod.shutil, "which", return_value="/usr/bin/npx"):
            with mock.patch.dict(
                os.environ,
                {
                    "COLLAB_DEEPSEEK_HARNESS_BIN": "npx @deepseek-ai/dsh",
                    "DEEPSEEK_API_KEY": "sk-test-not-real",
                },
                clear=False,
            ):
                argv, missing = self.mod.resolve_harness_bin()
        self.assertEqual(missing, [])
        self.assertEqual(argv[:3], ["npx", "@deepseek-ai/dsh"])
        self.assertEqual(self.mod.build_dsh_cmd(argv)[-3:], ["--profile", "headless", "-"])

    def test_fake_dsh_script_success_and_failure(self):
        """Real wrapper process against a fake dsh binary (no API)."""
        req = _req("permission")
        body = _ok_permission(req)
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "dsh"
            fake.write_text(_FAKE_DSH, encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            argv_log = Path(tmp) / "argv.json"
            stdin_log = Path(tmp) / "stdin.txt"
            env = os.environ.copy()
            env["COLLAB_DEEPSEEK_HARNESS_BIN"] = str(fake)
            env["DEEPSEEK_API_KEY"] = "sk-test-not-real"
            env["COLLAB_FAKE_DSH_STDOUT"] = json.dumps(body)
            env["COLLAB_FAKE_DSH_EXIT"] = "0"
            env["COLLAB_FAKE_DSH_ARGV"] = str(argv_log)
            env["COLLAB_FAKE_DSH_STDIN"] = str(stdin_log)
            proc = subprocess.run(
                [sys.executable, str(WRAPPER)],
                input=json.dumps(_envelope(req, "permission")),
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            parsed = json.loads(proc.stdout)
            self.assertEqual(parsed["decision"], "once")
            self.assertEqual(parsed["application_id"], req["application_id"])
            argv = json.loads(argv_log.read_text(encoding="utf-8"))
            self.assertIn("--profile", argv)
            self.assertIn("headless", argv)
            self.assertNotIn("--json", argv)
            task = stdin_log.read_text(encoding="utf-8")
            self.assertIn("Do not use bash", task)

            env["COLLAB_FAKE_DSH_EXIT"] = "1"
            env["COLLAB_FAKE_DSH_STDOUT"] = json.dumps(body)
            proc2 = subprocess.run(
                [sys.executable, str(WRAPPER)],
                input=json.dumps(_envelope(req, "permission")),
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
            self.assertEqual(proc2.returncode, 2)
            self.assertEqual((proc2.stdout or "").strip(), "")
            self.assertNotIn('"decision": "once"', proc2.stdout)


@unittest.skipUnless(
    _live_dsh_ready(),
    "optional live dsh smoke; needs COLLAB_DEEPSEEK_HARNESS_BIN or PATH dsh + DEEPSEEK_API_KEY",
)
class TestOptionalLiveDshSmoke(unittest.TestCase):
    def test_live_dsh_emits_bound_json_or_fail_closed(self):
        req = _req("permission")
        proc = subprocess.run(
            [sys.executable, str(WRAPPER)],
            input=json.dumps(_envelope(req, "permission")),
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode == 0:
            parsed = json.loads(proc.stdout)
            self.assertEqual(parsed.get("application_id"), req["application_id"])
            self.assertIn(parsed.get("decision"), ("once", "reject", "deny_job", "demand_safe_path"))
        else:
            self.assertEqual(proc.returncode, 2)
            self.assertEqual((proc.stdout or "").strip(), "")
            self.assertNotIn('"decision": "once"', proc.stdout)


if __name__ == "__main__":
    raise SystemExit(unittest.main(verbosity=2))
