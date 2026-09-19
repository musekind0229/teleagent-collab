#!/usr/bin/env python3
"""Simulated tests for Grok lead-bin resolution (Windows / posix). No live CLI."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

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
        with mock.patch("lead_adapter.grok_cli.subprocess.run", fake_run):
            raw, parsed = ad.decide(req, schema={"type": "object"}, cwd="/tmp")
        self.assertEqual(len(calls), 1)
        self.assertIn("--disallowed-tools", calls[0])
        self.assertEqual(run_kwargs.get("encoding"), "utf-8")
        self.assertEqual(run_kwargs.get("errors"), "replace")
        self.assertEqual((parsed or {}).get("_lead_status"), "call_failed")
        src = Path(__file__).resolve().parent / "lead_adapter" / "grok_cli.py"
        text = src.read_text(encoding="utf-8")
        self.assertIn("NO retry that drops --disallowed-tools", text)
        self.assertNotIn("retry without disallowed-tools", text)


if __name__ == "__main__":
    unittest.main()
