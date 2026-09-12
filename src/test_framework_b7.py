#!/usr/bin/env python3
"""Path-B knife7: run-job backend flag/env + hello via inprocess public API."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent
REPO = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_backend import (  # noqa: E402
    BackendError,
    BackendStatus,
    KIND_INPROCESS,
    KIND_TELEAGENT,
    resolve_run_job_backend,
    run_inprocess_charter,
)
from charter import load_charter  # noqa: E402

HELLO = REPO / "jobs/examples/hello.charter.yaml"


def _load_run_job():
    path = REPO / "bin" / "run-job.py"
    spec = importlib.util.spec_from_file_location("run_job_cli_knife7", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestResolveBackend(unittest.TestCase):
    def test_default_teleagent(self):
        self.assertEqual(resolve_run_job_backend(None, {}), KIND_TELEAGENT)
        self.assertEqual(resolve_run_job_backend("", {"COLLAB_EXECUTION_BACKEND": ""}), KIND_TELEAGENT)

    def test_flag_inprocess(self):
        self.assertEqual(resolve_run_job_backend("inprocess", {}), KIND_INPROCESS)
        self.assertEqual(resolve_run_job_backend("inprocess.local_v1", {}), KIND_INPROCESS)

    def test_flag_teleagent(self):
        self.assertEqual(resolve_run_job_backend("teleagent", {}), KIND_TELEAGENT)
        self.assertEqual(resolve_run_job_backend("teleagent.linux.local_v1", {}), KIND_TELEAGENT)

    def test_env_inprocess(self):
        self.assertEqual(
            resolve_run_job_backend(None, {"COLLAB_EXECUTION_BACKEND": "inprocess.local_v1"}),
            KIND_INPROCESS,
        )

    def test_flag_overrides_env(self):
        env = {"COLLAB_EXECUTION_BACKEND": "inprocess.local_v1"}
        self.assertEqual(resolve_run_job_backend("teleagent", env), KIND_TELEAGENT)
        env_ta = {"COLLAB_EXECUTION_BACKEND": "teleagent"}
        self.assertEqual(resolve_run_job_backend("inprocess", env_ta), KIND_INPROCESS)

    def test_hermes_unsupported(self):
        with self.assertRaises(BackendError) as ctx:
            resolve_run_job_backend("hermes", {})
        self.assertEqual(ctx.exception.status, BackendStatus.UNSUPPORTED)
        self.assertIn("Hermes", str(ctx.exception))

    def test_unknown_unsupported(self):
        with self.assertRaises(BackendError) as ctx:
            resolve_run_job_backend("not-a-backend", {})
        self.assertEqual(ctx.exception.status, BackendStatus.UNSUPPORTED)


class TestInprocessWire(unittest.TestCase):
    def test_hello_public_api_no_glue_import(self):
        charter = load_charter(HELLO)
        before = set(sys.modules)
        with tempfile.TemporaryDirectory() as td:
            result = run_inprocess_charter(charter=charter, workdir=td, name="hello")
            newly = set(sys.modules) - before
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["used_public_api_only"])
            self.assertEqual(result["path"], "inprocess.local_v1")
            self.assertEqual(result["pending_count"], 0)
            self.assertEqual(result["pending_summaries"], [])
            self.assertFalse(result["pending_seen"])
            self.assertEqual(result["backend"], "inprocess.local_v1")
            out = Path(td) / "hello-from-worker.txt"
            self.assertTrue(out.is_file(), result)
            self.assertEqual(len(out.read_text(encoding="utf-8").strip().splitlines()), 1)
            banned = [
                n
                for n in newly
                if n == "glue" or n.startswith("glue.") or n.startswith("teleagent_adapter")
            ]
            self.assertEqual(banned, [], f"inprocess path imported TA/glue: {banned}")


class TestRunJobCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_run_job()

    def _run(self, argv, env_extra=None):
        env = os.environ.copy()
        env.pop("COLLAB_EXECUTION_BACKEND", None)
        if env_extra:
            env.update(env_extra)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(os.environ, env, clear=True):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                rc = self.mod.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_dry_run_default_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = self._run(
                ["--dry-run", "--runs-dir", td, str(HELLO)]
            )
            self.assertEqual(rc, 0, err)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["state"], "dry_run")
            self.assertNotIn("backend", summary)
            status = json.loads(Path(summary["status"]).read_text(encoding="utf-8"))
            self.assertTrue(status["dry_run"])

    def test_flag_inprocess_hello(self):
        with tempfile.TemporaryDirectory() as td:
            ws = str(Path(td) / "ws")
            runs = str(Path(td) / "runs")
            rc, out, err = self._run(
                [
                    "--backend",
                    "inprocess",
                    "--workspace",
                    ws,
                    "--runs-dir",
                    runs,
                    str(HELLO),
                ]
            )
            self.assertEqual(rc, 0, err + out)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertTrue(summary["ok"], summary)
            self.assertEqual(summary["backend"], "inprocess")
            self.assertTrue(summary.get("used_public_api_only"))
            self.assertEqual(summary.get("backend_id"), "inprocess.local_v1")
            hello = Path(ws) / "hello-from-worker.txt"
            self.assertTrue(hello.is_file(), summary)
            line = hello.read_text(encoding="utf-8").strip()
            self.assertTrue(line)
            self.assertEqual(len(line.splitlines()), 1)
            status = json.loads(Path(summary["status"]).read_text(encoding="utf-8"))
            self.assertEqual(status.get("path"), "inprocess.local_v1")
            self.assertTrue(status.get("ok"))

    def test_env_inprocess_hello(self):
        with tempfile.TemporaryDirectory() as td:
            ws = str(Path(td) / "ws")
            runs = str(Path(td) / "runs")
            rc, out, err = self._run(
                ["--workspace", ws, "--runs-dir", runs, str(HELLO)],
                env_extra={"COLLAB_EXECUTION_BACKEND": "inprocess.local_v1"},
            )
            self.assertEqual(rc, 0, err + out)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertEqual(summary["backend"], "inprocess")
            self.assertTrue((Path(ws) / "hello-from-worker.txt").is_file())

    def test_unknown_backend_exit_2(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = self._run(
                ["--backend", "hermes", "--runs-dir", td, str(HELLO)]
            )
        self.assertEqual(rc, 2)
        self.assertIn("unsupported", err.lower())
        self.assertIn("Hermes", err)

    def test_dry_run_ignores_inprocess_flag(self):
        """--dry-run must not write artifacts even if --backend inprocess."""
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            ws.mkdir()
            rc, out, err = self._run(
                [
                    "--dry-run",
                    "--backend",
                    "inprocess",
                    "--workspace",
                    str(ws),
                    "--runs-dir",
                    td,
                    str(HELLO),
                ]
            )
            self.assertEqual(rc, 0, err)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertEqual(summary["state"], "dry_run")
            self.assertFalse((ws / "hello-from-worker.txt").exists())


if __name__ == "__main__":
    unittest.main()
