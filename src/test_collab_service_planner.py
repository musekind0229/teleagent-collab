"""collab-service --planner lead uses COLLAB_LEAD_ADAPTER (no live Codex/Grok)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
BIN = ROOT / "bin"
for p in (str(SRC), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from framework.app_service import DeterministicPlanner, LeadAdapterPlanner  # noqa: E402
from lead_adapter.claude_code import ClaudeCodeLeadAdapter  # noqa: E402
from lead_adapter.codex_cli import CodexCliLeadAdapter  # noqa: E402
from lead_adapter.deepseek_harness import DeepSeekHarnessLeadAdapter  # noqa: E402
from lead_adapter.grok_cli import GrokCliLeadAdapter  # noqa: E402


def _load_collab_service():
    path = BIN / "collab-service.py"
    spec = importlib.util.spec_from_file_location("collab_service_planner_mod", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load collab-service")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SERVICE = _load_collab_service()

_REFUSAL = "use COLLAB_LEAD_ADAPTER=codex_cli for the Codex CLI"


class CollabServiceLeadPlannerTests(unittest.TestCase):
    def test_lead_planner_uses_codex_cli_and_honors_timeout(self):
        mod = SERVICE
        with tempfile.TemporaryDirectory() as td:
            persist = Path(td)
            with mock.patch.dict(os.environ, {"COLLAB_LEAD_ADAPTER": "codex_cli"}, clear=False):
                planner = mod._planner("lead", persist)
                honored = mod._planner("lead", persist, 240)
            self.assertIsInstance(planner, LeadAdapterPlanner)
            self.assertIsInstance(planner.adapter, CodexCliLeadAdapter)
            self.assertEqual(planner.adapter.name, "codex_cli")
            self.assertEqual(planner.timeout_sec, 180)
            self.assertEqual(planner.cwd, str((persist / "lead-work").resolve()))
            self.assertTrue((persist / "lead-work").is_dir())
            self.assertEqual(honored.timeout_sec, 240)
            self.assertIsInstance(honored.adapter, CodexCliLeadAdapter)

    def test_codex_cli_hyphen_alias_and_other_adapters(self):
        mod = SERVICE
        with tempfile.TemporaryDirectory() as td:
            persist = Path(td)
            secret = persist / "Users" / "Admin" / "secret-token-dir" / "run-deepseek-lead.py"
            with mock.patch.dict(os.environ, {"COLLAB_LEAD_ADAPTER": "codex-cli"}, clear=False):
                hyphen = mod._planner("lead", persist, 15)
            with mock.patch.dict(os.environ, {"COLLAB_LEAD_ADAPTER": "claude_code"}, clear=False):
                claude = mod._planner("lead", persist)
            with mock.patch.dict(
                os.environ,
                {
                    "COLLAB_LEAD_ADAPTER": "deepseek_harness",
                    "COLLAB_LEAD_BIN": str(secret),
                    "COLLAB_DEEPSEEK_LEAD_BIN": str(secret),
                },
                clear=False,
            ):
                deepseek = mod._planner("lead", persist, 30)
        self.assertIsInstance(hyphen.adapter, CodexCliLeadAdapter)
        self.assertEqual(hyphen.timeout_sec, 15)
        self.assertIsInstance(claude.adapter, ClaudeCodeLeadAdapter)
        self.assertEqual(claude.timeout_sec, 180)
        self.assertIsInstance(deepseek.adapter, DeepSeekHarnessLeadAdapter)
        self.assertEqual(deepseek.timeout_sec, 30)
        line = mod._lead_planner_stderr_line(deepseek)
        self.assertEqual(line, "lead planner: adapter=deepseek_harness timeout=30s")
        self.assertNotIn("secret-token-dir", line or "")
        self.assertNotIn(str(secret), line or "")

    def test_default_lead_adapter_is_grok_cli(self):
        mod = SERVICE
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "grok"
            fake.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"COLLAB_LEAD_BIN": str(fake)}, clear=False):
                os.environ.pop("COLLAB_LEAD_ADAPTER", None)
                planner = mod._planner("lead", root / "persist")
        self.assertIsInstance(planner.adapter, GrokCliLeadAdapter)
        self.assertEqual(planner.adapter.name, "grok_cli")
        self.assertEqual(planner.timeout_sec, 180)
        line = mod._lead_planner_stderr_line(planner)
        self.assertEqual(line, "lead planner: adapter=grok_cli timeout=180s")
        self.assertNotIn(str(fake), line or "")

    def test_inprocess_and_codex_alias_are_refused(self):
        mod = SERVICE
        for kind in ("codex", "inprocess", "in_process", "file", "stdin"):
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    exchange = root / "exchange-must-not-be-created"
                    env = {
                        "COLLAB_LEAD_ADAPTER": kind,
                        "COLLAB_LEAD_EXCHANGE": str(exchange),
                    }
                    logs = (
                        self.assertLogs("lead_adapter", level="WARNING")
                        if kind == "codex"
                        else contextlib.nullcontext()
                    )
                    with mock.patch.dict(os.environ, env, clear=False):
                        with logs:
                            with self.assertRaises(mod.InProcessLeadRefused) as ctx:
                                mod._planner("lead", root / "persist")
                    self.assertIn(_REFUSAL, str(ctx.exception))
                    self.assertIn("file-protocol", str(ctx.exception))
                    self.assertFalse((root / "persist" / "lead-work").exists())
                    self.assertFalse(exchange.exists())

    def test_grok_planner_ignores_lead_adapter_env(self):
        mod = SERVICE
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "bin" / "grok"
            fake.parent.mkdir()
            fake.write_text("", encoding="utf-8")
            env = {
                "COLLAB_LEAD_ADAPTER": "codex_cli",
                "COLLAB_LEAD_BIN": str(fake),
                "COLLAB_CODEX_LEAD_BIN": str(root / "secret" / "codex.cmd"),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                planner = mod._planner("grok", root / "persist", 90)
                untouched = mod._planner("grok", root / "persist")
            self.assertIsInstance(planner, LeadAdapterPlanner)
            self.assertIsInstance(planner.adapter, GrokCliLeadAdapter)
            self.assertNotIsInstance(planner.adapter, CodexCliLeadAdapter)
            self.assertEqual(planner.adapter.name, "grok_cli")
            self.assertEqual(planner.timeout_sec, 90)
            self.assertEqual(untouched.timeout_sec, 180)
            self.assertTrue((root / "persist" / "lead-work").is_dir())
            line = mod._lead_planner_stderr_line(planner)
            self.assertEqual(line, "lead planner: adapter=grok_cli timeout=90s")
            self.assertNotIn(str(fake), line or "")
            self.assertNotIn("codex.cmd", line or "")

    def test_deterministic_planner_unchanged(self):
        mod = SERVICE
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.dict(os.environ, {"COLLAB_LEAD_ADAPTER": "codex_cli"}, clear=False):
                planner = mod._planner("deterministic", root / "persist", 10)
            self.assertIsInstance(planner, DeterministicPlanner)
            self.assertFalse(isinstance(planner, LeadAdapterPlanner))
            self.assertFalse((root / "persist").exists())

    def test_argparse_accepts_lead_and_timeout(self):
        mod = SERVICE
        ns = mod.build_parser().parse_args(["--planner", "lead", "--lead-timeout", "240"])
        self.assertEqual(ns.planner, "lead")
        self.assertEqual(ns.lead_timeout, 240)
        parser = mod.build_parser()
        planner_help = next(
            action.help or ""
            for action in parser._actions
            if "--planner" in action.option_strings
        )
        self.assertIn("permission/review", planner_help)
        self.assertIn("illegal JSON", planner_help)
        self.assertIn("non-auto kinds like question/system_action", planner_help)
        self.assertIn("pending human decision", planner_help)
        self.assertIn("--lead-timeout", parser.format_help())

    def test_lead_timeout_must_be_positive(self):
        mod = SERVICE
        for bad in ("0", "-1", "0.0", "abc", "nan", "inf"):
            with self.subTest(bad=bad):
                err = io.StringIO()
                with mock.patch.object(sys, "stderr", err):
                    with self.assertRaises(SystemExit) as ctx:
                        mod.build_parser().parse_args(["--planner", "lead", "--lead-timeout", bad])
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("lead timeout must be", err.getvalue())

    def test_main_refuses_inprocess_with_exit_2(self):
        mod = SERVICE
        for kind in ("codex", "inprocess"):
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as td:
                    err = io.StringIO()
                    env = {"COLLAB_LEAD_ADAPTER": kind, "COLLAB_LEAD_EXCHANGE": str(Path(td) / "ex")}
                    with mock.patch.dict(os.environ, env, clear=False):
                        with mock.patch.object(sys, "stderr", err):
                            with self.assertRaises(SystemExit) as ctx:
                                mod.main(["--planner", "lead", "--persist", td])
                    self.assertEqual(ctx.exception.code, 2)
                    text = err.getvalue()
                    self.assertIn(_REFUSAL, text)
                    self.assertNotIn(str(Path(td) / "ex"), text)

    def test_startup_banner_codex_cli_has_basename_only(self):
        mod = SERVICE
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            secret = root / "Users" / "Admin" / "secret-token-dir"
            secret.mkdir(parents=True)
            binary = secret / "codex.cmd"
            binary.write_text("", encoding="utf-8")
            persist = root / "app"
            err = io.StringIO()
            env = {
                "COLLAB_LEAD_ADAPTER": "codex_cli",
                "COLLAB_CODEX_LEAD_BIN": str(binary),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch.object(sys, "stderr", err), mock.patch.object(sys, "stdout", io.StringIO()):
                    code = mod.main(
                        [
                            "--planner",
                            "lead",
                            "--lead-timeout",
                            "240",
                            "--persist",
                            str(persist),
                            "--once",
                            "--backend",
                            "inprocess",
                        ]
                    )
        self.assertEqual(code, 0)
        text = err.getvalue()
        self.assertIn("lead planner: adapter=codex_cli timeout=240s bin=codex.cmd", text)
        self.assertNotIn("secret-token-dir", text)
        self.assertNotIn("Users", text)
        self.assertNotIn(str(secret), text)
        self.assertNotIn(str(persist), text)

    def test_startup_banner_absent_for_deterministic_present_for_grok(self):
        mod = SERVICE
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "opt" / "grok"
            fake.parent.mkdir()
            fake.write_text("", encoding="utf-8")
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"COLLAB_LEAD_BIN": str(fake)}, clear=False):
                with mock.patch.object(sys, "stderr", err), mock.patch.object(sys, "stdout", io.StringIO()):
                    code = mod.main(
                        ["--planner", "deterministic", "--persist", str(root / "det"), "--once"]
                    )
            self.assertEqual(code, 0)
            self.assertNotIn("lead planner:", err.getvalue())

            err = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {"COLLAB_LEAD_ADAPTER": "codex_cli", "COLLAB_LEAD_BIN": str(fake)},
                clear=False,
            ):
                with mock.patch.object(sys, "stderr", err), mock.patch.object(sys, "stdout", io.StringIO()):
                    code = mod.main(
                        [
                            "--planner",
                            "grok",
                            "--lead-timeout",
                            "90",
                            "--persist",
                            str(root / "grok-app"),
                            "--once",
                        ]
                    )
        self.assertEqual(code, 0)
        text = err.getvalue()
        self.assertIn("lead planner: adapter=grok_cli timeout=90s", text)
        self.assertNotIn(str(fake), text)
        self.assertNotIn("bin=", text)

    def test_windows_codex_path_banner_is_basename_only(self):
        mod = SERVICE
        with tempfile.TemporaryDirectory() as td:
            persist = Path(td)
            win = r"C:\Users\Admin\.local\share\TeleAgent\runtimes\node\codex.cmd"
            env = {"COLLAB_LEAD_ADAPTER": "codex_cli", "COLLAB_CODEX_LEAD_BIN": win}
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch.object(mod.shutil, "which", return_value=None):
                    planner = mod._planner("lead", persist)
                    line = mod._lead_planner_stderr_line(planner)
        self.assertEqual(line, "lead planner: adapter=codex_cli timeout=180s bin=codex.cmd")
        self.assertNotIn("Users", line or "")
        self.assertNotIn("TeleAgent", line or "")
        self.assertNotIn("\\", line or "")


if __name__ == "__main__":
    unittest.main()
