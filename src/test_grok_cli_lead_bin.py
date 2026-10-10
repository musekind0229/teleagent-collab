#!/usr/bin/env python3
"""Simulated tests for Grok lead-bin resolution (Windows / posix). No live CLI."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from lead_adapter.cancel import LeadCancelled

from lead_adapter.grok_cli import (
    LEGACY_LINUX_LEAD_BIN,
    POSIX_DEFAULT_BASE_URL,
    WIN_DEFAULT_BASE_URL,
    GrokCliLeadAdapter,
    LeadBinNotFound,
    default_live_base_url,
    resolve_lead_bin,
)


def _touch(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


class TestResolveLeadBin(unittest.TestCase):
    def test_env_set_and_exists_wins(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            env_bin = _touch(root / "env" / "grok.exe")
            path_bin = _touch(root / "path" / "grok.exe")
            _touch(root / ".grok" / "bin" / "grok.exe")

            def which(name: str) -> str | None:
                return path_bin if name in ("grok", "grok.exe") else None

            got = resolve_lead_bin(
                env={"COLLAB_LEAD_BIN": env_bin},
                which=which,
                home=root,
                platform="win32",
            )
            self.assertEqual(got, env_bin)

    def test_env_set_missing_falls_through_to_path(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            missing = str(root / "no-such-grok.exe")
            path_bin = _touch(root / "path" / "grok.exe")

            def which(name: str) -> str | None:
                return path_bin if name == "grok.exe" else None

            got = resolve_lead_bin(
                env={"COLLAB_LEAD_BIN": missing},
                which=which,
                home=root / "empty-home",
                platform="win32",
            )
            self.assertEqual(got, path_bin)

    def test_path_grok_before_grok_exe(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            grok = _touch(root / "path" / "grok")
            grok_exe = _touch(root / "path" / "grok.exe")

            def which(name: str) -> str | None:
                if name == "grok":
                    return grok
                if name == "grok.exe":
                    return grok_exe
                return None

            got = resolve_lead_bin(
                env={},
                which=which,
                home=root / "empty-home",
                platform="linux",
            )
            self.assertEqual(got, grok)

    def test_path_grok_exe_when_grok_absent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            grok_exe = _touch(root / "path" / "grok.exe")

            def which(name: str) -> str | None:
                return grok_exe if name == "grok.exe" else None

            got = resolve_lead_bin(
                env={},
                which=which,
                home=root / "empty-home",
                platform="win32",
            )
            self.assertEqual(got, grok_exe)

    def test_win_home_grok_exe(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = _touch(root / ".grok" / "bin" / "grok.exe")
            _touch(root / ".grok" / "bin" / "grok")

            got = resolve_lead_bin(
                env={},
                which=lambda n: None,
                home=root,
                platform="win32",
            )
            self.assertEqual(got, target)

    def test_posix_home_grok(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = _touch(root / ".grok" / "bin" / "grok")
            _touch(root / ".grok" / "bin" / "grok.exe")

            got = resolve_lead_bin(
                env={},
                which=lambda n: None,
                home=root,
                platform="linux",
            )
            self.assertEqual(got, target)

    def test_win_home_ignores_posix_name_and_legacy_sh(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _touch(root / ".grok" / "bin" / "grok")

            with self.assertRaises(LeadBinNotFound) as cm:
                resolve_lead_bin(
                    env={},
                    which=lambda n: None,
                    home=root,
                    platform="win32",
                )
            msg = str(cm.exception)
            self.assertIn("grok.exe", msg)
            self.assertNotIn(LEGACY_LINUX_LEAD_BIN, msg)

    def test_legacy_linux_only_if_file_exists(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            def is_file(path: str) -> bool:
                return path == LEGACY_LINUX_LEAD_BIN

            got = resolve_lead_bin(
                env={},
                which=lambda n: None,
                home=root,
                platform="linux",
                is_file=is_file,
            )
            self.assertEqual(got, LEGACY_LINUX_LEAD_BIN)

    def test_not_found_clear_error(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with self.assertRaises(LeadBinNotFound) as cm:
                resolve_lead_bin(
                    env={"COLLAB_LEAD_BIN": str(root / "missing")},
                    which=lambda n: None,
                    home=root,
                    platform="linux",
                    is_file=lambda p: False,
                )
            msg = str(cm.exception)
            self.assertIn("COLLAB_LEAD_BIN", msg)
            self.assertIn("grok.exe", msg)
            self.assertIn(LEGACY_LINUX_LEAD_BIN, msg)

    def test_empty_env_treated_as_unset(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path_bin = _touch(root / "path" / "grok")

            got = resolve_lead_bin(
                env={"COLLAB_LEAD_BIN": "  "},
                which=lambda n: path_bin if n == "grok" else None,
                home=root / "empty-home",
                platform="linux",
            )
            self.assertEqual(got, path_bin)


class TestDefaultLiveBaseUrl(unittest.TestCase):
    def test_env_wins(self):
        url = default_live_base_url(
            env={"TELEAGENT_BASE_URL": "http://127.0.0.1:4400/"},
            platform="win32",
        )
        self.assertEqual(url, "http://127.0.0.1:4400")

    def test_win_defaults_4397(self):
        self.assertEqual(
            default_live_base_url(env={}, platform="win32"),
            WIN_DEFAULT_BASE_URL,
        )
        self.assertEqual(WIN_DEFAULT_BASE_URL, "http://127.0.0.1:4397")

    def test_posix_defaults_4399(self):
        self.assertEqual(
            default_live_base_url(env={}, platform="linux"),
            POSIX_DEFAULT_BASE_URL,
        )
        self.assertEqual(POSIX_DEFAULT_BASE_URL, "http://127.0.0.1:4399")


class TestGrokCliAdapterDefaults(unittest.TestCase):
    def test_explicit_bin_skips_resolve(self):
        ad = GrokCliLeadAdapter(bin_path="/bin/echo")
        self.assertEqual(ad.bin_path, "/bin/echo")

    def test_default_bin_uses_resolver(self):
        with mock.patch(
            "lead_adapter.grok_cli.resolve_lead_bin",
            return_value="/tmp/fake-grok",
        ) as resolved:
            ad = GrokCliLeadAdapter()
        self.assertEqual(ad.bin_path, "/tmp/fake-grok")
        resolved.assert_called_once_with()

    def test_decide_keeps_disallowed_tools_no_retry(self):
        req = {
            "application_id": "app-1",
            "context_summary": "sum",
            "kind": "permission",
            "task_goal": "g",
            "authorized_scope": ["ws"],
            "prohibitions": [],
            "acceptance_criteria": {},
            "current_application": {},
        }
        calls: list[list[str]] = []
        run_kwargs = {}

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            run_kwargs.update(kwargs)

            class P:
                returncode = 2
                stdout = ""
                stderr = "unknown tool name"

            return P()

        ad = GrokCliLeadAdapter(bin_path="/bin/echo")
        with mock.patch("lead_adapter.grok_cli.run_cancellable", fake_run):
            raw, parsed = ad.decide(req, schema={"type": "object"}, cwd="/tmp")
        self.assertEqual(len(calls), 1)
        self.assertIn("--disallowed-tools", calls[0])
        self.assertIn("timeout", run_kwargs)
        # run_cancellable decodes lead output as utf-8 with replacement.
        cancel_src = (Path(__file__).resolve().parent / "lead_adapter" / "cancel.py").read_text(encoding="utf-8")
        self.assertIn('encoding="utf-8"', cancel_src)
        self.assertIn('errors="replace"', cancel_src)
        self.assertEqual((parsed or {}).get("_lead_status"), "call_failed")
        src = Path(__file__).resolve().parent / "lead_adapter" / "grok_cli.py"
        text = src.read_text(encoding="utf-8")
        self.assertIn("NO retry that drops --disallowed-tools", text)
        self.assertNotIn("retry without disallowed-tools", text)


class TestGrokCliBoundedInlineReviewProfile(unittest.TestCase):
    def _make_req(self, kind: str = "review") -> dict:
        return {
            "application_id": "rev_test123",
            "context_summary": "sum_test123:review goal",
            "kind": kind,
            "task_goal": "review goal",
            "authorized_scope": ["scope"],
            "prohibitions": ["no tools"],
            "acceptance_criteria": {"verdict": "strict"},
            "current_application": {"target": "something"},
        }

    def test_review_opted_in_passes_bounded_flags_and_system_prompt(self):
        req = self._make_req("review")
        calls: list[list[str]] = []
        captured_kwargs: list[dict] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            captured_kwargs.append(dict(kwargs))

            class P:
                returncode = 0
                stdout = json.dumps({
                    "application_id": "rev_test123",
                    "context_summary": "sum_test123:review goal",
                    "verdict": "pass",
                    "reason": "clean",
                })
                stderr = ""

            return P()

        ad = GrokCliLeadAdapter(
            bin_path="/bin/fake-grok",
            bounded_inline_review_profile=True,
        )
        with mock.patch("lead_adapter.grok_cli.run_cancellable", fake_run):
            raw, parsed = ad.decide(req, schema={"type": "object"}, cwd="/tmp", timeout_sec=42)

        self.assertEqual(len(calls), 1)
        self.assertIn("timeout", captured_kwargs[0])
        self.assertEqual(captured_kwargs[0]["timeout"], 42.0)
        cmd = calls[0]
        # Bounded flags
        self.assertIn("--tools", cmd)
        tools_idx = cmd.index("--tools")
        self.assertEqual(cmd[tools_idx + 1], "")

        self.assertIn("--no-subagents", cmd)
        self.assertIn("--disable-web-search", cmd)

        self.assertIn("--reasoning-effort", cmd)
        effort_idx = cmd.index("--reasoning-effort")
        self.assertEqual(cmd[effort_idx + 1], "low")

        self.assertIn("--system-prompt-override", cmd)
        sp_idx = cmd.index("--system-prompt-override")
        self.assertTrue(len(cmd[sp_idx + 1]) > 0)

        # Preserve --disallowed-tools, schema, cwd, timeout bound, and one-call behavior
        self.assertIn("--disallowed-tools", cmd)
        self.assertIn("--json-schema", cmd)
        schema_idx = cmd.index("--json-schema")
        pinned = json.loads(cmd[schema_idx + 1])
        self.assertEqual(pinned.get("properties", {}).get("application_id", {}).get("const"), "rev_test123")
        self.assertEqual(pinned.get("properties", {}).get("context_summary", {}).get("const"), "sum_test123:review goal")

        self.assertEqual(parsed.get("verdict"), "pass")

    def test_review_opted_out_unchanged_behavior(self):
        req = self._make_req("review")
        calls: list[list[str]] = []
        captured_kwargs: list[dict] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            captured_kwargs.append(dict(kwargs))

            class P:
                returncode = 0
                stdout = json.dumps({
                    "application_id": "rev_test123",
                    "context_summary": "sum_test123:review goal",
                    "verdict": "pass",
                    "reason": "clean",
                })
                stderr = ""

            return P()

        # Default is False (opted out)
        ad = GrokCliLeadAdapter(bin_path="/bin/fake-grok")
        with mock.patch("lead_adapter.grok_cli.run_cancellable", fake_run):
            ad.decide(req, schema={"type": "object"}, cwd="/tmp")

        self.assertEqual(len(calls), 1)
        self.assertIn("timeout", captured_kwargs[0])
        cmd = calls[0]
        self.assertNotIn("--tools", cmd)
        self.assertNotIn("--no-subagents", cmd)
        self.assertNotIn("--disable-web-search", cmd)
        self.assertNotIn("--reasoning-effort", cmd)
        self.assertNotIn("--system-prompt-override", cmd)
        self.assertIn("--disallowed-tools", cmd)

    def test_plan_and_permission_unchanged_when_opted_in(self):
        for kind in ("plan", "permission"):
            with self.subTest(kind=kind):
                req = self._make_req(kind)
                calls: list[list[str]] = []
                captured_kwargs: list[dict] = []

                def fake_run(cmd, **kwargs):
                    calls.append(list(cmd))
                    captured_kwargs.append(dict(kwargs))

                    class P:
                        returncode = 0
                        stdout = json.dumps({
                            "application_id": "rev_test123",
                            "context_summary": "sum_test123:review goal",
                            "decision": "once",
                            "reason": "approved",
                        })
                        stderr = ""

                    return P()

                ad = GrokCliLeadAdapter(
                    bin_path="/bin/fake-grok",
                    bounded_inline_review_profile=True,
                )
                with mock.patch("lead_adapter.grok_cli.run_cancellable", fake_run):
                    ad.decide(req, schema={"type": "object"}, cwd="/tmp")

                self.assertEqual(len(calls), 1)
                self.assertIn("timeout", captured_kwargs[0])
                cmd = calls[0]
                self.assertNotIn("--tools", cmd)
                self.assertNotIn("--no-subagents", cmd)
                self.assertNotIn("--disable-web-search", cmd)
                self.assertNotIn("--reasoning-effort", cmd)
                self.assertNotIn("--system-prompt-override", cmd)
                self.assertIn("--disallowed-tools", cmd)

    def test_opted_in_review_timeout_fails_with_no_retry(self):
        req = self._make_req("review")
        calls: list[list[str]] = []
        captured_kwargs: list[dict] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            captured_kwargs.append(dict(kwargs))
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30.0)

        ad = GrokCliLeadAdapter(
            bin_path="/bin/fake-grok",
            bounded_inline_review_profile=True,
        )
        with mock.patch("lead_adapter.grok_cli.run_cancellable", fake_run):
            raw, parsed = ad.decide(req, schema={"type": "object"}, cwd="/tmp", timeout_sec=30.0)

        self.assertEqual(len(calls), 1)
        self.assertIn("timeout", captured_kwargs[0])
        self.assertEqual(raw, "TIMEOUT")
        self.assertEqual(parsed, {"_lead_status": "timeout", "error": "grok_cli timeout"})

    def test_opted_in_review_spawn_failure_fails_with_no_retry(self):
        req = self._make_req("review")
        calls: list[list[str]] = []
        captured_kwargs: list[dict] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            captured_kwargs.append(dict(kwargs))
            raise OSError("permission denied")

        ad = GrokCliLeadAdapter(
            bin_path="/bin/fake-grok",
            bounded_inline_review_profile=True,
        )
        with mock.patch("lead_adapter.grok_cli.run_cancellable", fake_run):
            raw, parsed = ad.decide(req, schema={"type": "object"}, cwd="/tmp")

        self.assertEqual(len(calls), 1)
        self.assertIn("timeout", captured_kwargs[0])
        self.assertEqual(raw, "CALL_FAILED")
        self.assertEqual(parsed.get("_lead_status"), "call_failed")
        self.assertIn("grok_cli spawn failed", parsed.get("error", ""))

    def test_opted_in_review_nonzero_exit_fails_with_no_retry(self):
        req = self._make_req("review")
        calls: list[list[str]] = []
        captured_kwargs: list[dict] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            captured_kwargs.append(dict(kwargs))

            class P:
                returncode = 1
                stdout = ""
                stderr = "flag error or model crash"

            return P()

        ad = GrokCliLeadAdapter(
            bin_path="/bin/fake-grok",
            bounded_inline_review_profile=True,
        )
        with mock.patch("lead_adapter.grok_cli.run_cancellable", fake_run):
            raw, parsed = ad.decide(req, schema={"type": "object"}, cwd="/tmp")

        self.assertEqual(len(calls), 1)
        self.assertIn("timeout", captured_kwargs[0])
        self.assertEqual(parsed.get("_lead_status"), "call_failed")
        self.assertEqual(parsed.get("returncode"), 1)
        self.assertIn("flag error or model crash", parsed.get("error", ""))

    def test_opted_in_review_cancelled_returns_call_failed(self):
        req = self._make_req("review")
        calls: list[list[str]] = []
        captured_kwargs: list[dict] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            captured_kwargs.append(dict(kwargs))
            raise LeadCancelled("goal stopped")

        ad = GrokCliLeadAdapter(
            bin_path="/bin/fake-grok",
            bounded_inline_review_profile=True,
        )
        with mock.patch("lead_adapter.grok_cli.run_cancellable", fake_run):
            raw, parsed = ad.decide(req, schema={"type": "object"}, cwd="/tmp")

        self.assertEqual(len(calls), 1)
        self.assertIn("timeout", captured_kwargs[0])
        self.assertEqual(raw, "CALL_FAILED")
        self.assertEqual(parsed.get("_lead_status"), "call_failed")
        self.assertIn("grok_cli stopped", parsed.get("error", ""))

    def test_grok_cli_source_uses_run_cancellable_not_subprocess_run(self):
        src = (Path(__file__).resolve().parent / "lead_adapter" / "grok_cli.py").read_text(encoding="utf-8")
        self.assertIn("run_cancellable(cmd", src)
        self.assertNotIn("subprocess.run(", src)



@unittest.skipIf(sys.platform == "win32", "posix shebang fake binary")
class TestGrokCliBoundedProfileRealCancelChain(unittest.TestCase):
    """No mocks: the opted-in review argv goes through the real run_cancellable."""

    def test_bounded_review_argv_through_real_run_cancellable(self):
        import os
        import stat

        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake-grok"
            fake.write_text(
                "#!" + sys.executable + "\n"
                "import json, sys\n"
                "print(json.dumps({'application_id': 'rev_real', 'context_summary': 's',"
                " 'verdict': 'pass', 'reason': 'ok', 'argv': sys.argv[1:]}))\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            req = {"application_id": "rev_real", "context_summary": "s", "kind": "review"}
            ad = GrokCliLeadAdapter(bin_path=str(fake), bounded_inline_review_profile=True)
            with mock.patch("subprocess.run", side_effect=AssertionError("subprocess.run must not be used")):
                raw, parsed = ad.decide(req, schema={"type": "object"}, cwd=tmp, timeout_sec=30)
            argv = (parsed or {}).get("argv") or []
            self.assertIn("--tools", argv, raw)
            self.assertEqual(argv[argv.index("--tools") + 1], "")
            for flag in ("--no-subagents", "--disable-web-search", "--disallowed-tools"):
                self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "low")
            self.assertEqual(parsed.get("verdict"), "pass")

if __name__ == "__main__":
    unittest.main()
