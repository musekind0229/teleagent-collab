#!/usr/bin/env python3
"""Path-B knife9: two independent inprocess jobs isolated by workdir."""
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

from charter import load_charter  # noqa: E402
from execution_backend import (  # noqa: E402
    IsolationError,
    InProcessExecutionBackend,
    prove_shared_workspace_collides,
    require_distinct_workdirs,
    run_inprocess_charter,
    run_two_jobs_isolated,
)

HELLO = REPO / "jobs/examples/hello.charter.yaml"
ISO_A = REPO / "jobs/examples/iso-a.charter.yaml"
ISO_B = REPO / "jobs/examples/iso-b.charter.yaml"


def _load_run_job():
    path = REPO / "bin" / "run-job.py"
    spec = importlib.util.spec_from_file_location("run_job_cli_knife9", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestChartersDistinct(unittest.TestCase):
    def test_names_and_artifacts_differ(self):
        a = load_charter(ISO_A)
        b = load_charter(ISO_B)
        self.assertEqual(a["name"], "iso-a")
        self.assertEqual(b["name"], "iso-b")
        self.assertEqual(a["done_when"]["artifacts"], ["iso-a.txt"])
        self.assertEqual(b["done_when"]["artifacts"], ["iso-b.txt"])
        self.assertNotEqual(a["name"], b["name"])
        self.assertNotEqual(a["done_when"]["artifacts"], b["done_when"]["artifacts"])
        self.assertFalse(a.get("force_lead_review"))
        self.assertFalse(b.get("force_lead_review"))


class TestRequireDistinctWorkdirs(unittest.TestCase):
    def test_same_path_is_not_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(IsolationError) as ctx:
                require_distinct_workdirs(td, td)
            self.assertIn("shared workdir is not isolation", str(ctx.exception))

    def test_same_path_via_dot_resolve(self):
        with tempfile.TemporaryDirectory() as td:
            nested = Path(td) / "w"
            nested.mkdir()
            with self.assertRaises(IsolationError):
                require_distinct_workdirs(nested, nested / ".")

    def test_distinct_ok(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = require_distinct_workdirs(Path(td) / "a", Path(td) / "b")
            self.assertNotEqual(a, b)


class TestSharedWorkspaceNotIsolation(unittest.TestCase):
    def test_two_instances_one_workdir_collide(self):
        with tempfile.TemporaryDirectory() as td:
            proof = prove_shared_workspace_collides(workdir=td, relative="shared.txt")
            self.assertTrue(proof["instances_distinct"])
            self.assertNotEqual(proof["instance_a_id"], proof["instance_b_id"])
            self.assertTrue(proof["collided"], proof)
            self.assertIn("instance-a", proof["body_after_a"])
            self.assertIn("instance-b", proof["body_after_b"])
            self.assertEqual(Path(td, "shared.txt").read_text(encoding="utf-8"), proof["body_after_b"])


class TestSerialIsolation(unittest.TestCase):
    def test_helper_serial_separate_workdirs(self):
        charter_a = load_charter(ISO_A)
        charter_b = load_charter(ISO_B)
        with tempfile.TemporaryDirectory() as td:
            wa = Path(td) / "workdir_a"
            wb = Path(td) / "workdir_b"
            before = set(sys.modules)
            report = run_two_jobs_isolated(
                charter_a=charter_a,
                charter_b=charter_b,
                workdir_a=wa,
                workdir_b=wb,
                concurrent=False,
            )
            newly = set(sys.modules) - before
            self.assertTrue(report["ok"], report)
            self.assertTrue(report["isolation_ok"])
            self.assertTrue(report["serial"])
            self.assertFalse(report["concurrent"])
            self.assertEqual(report["isolation_by"], "workdir")
            self.assertNotEqual(report["workdir_a"], report["workdir_b"])
            self.assertEqual(report["artifact_of_a_in_b"], [])
            self.assertEqual(report["artifact_of_b_in_a"], [])
            self.assertTrue((wa / "iso-a.txt").is_file())
            self.assertTrue((wb / "iso-b.txt").is_file())
            self.assertFalse((wb / "iso-a.txt").exists())
            self.assertFalse((wa / "iso-b.txt").exists())
            self.assertTrue(report["job_a"]["used_public_api_only"])
            self.assertTrue(report["job_b"]["used_public_api_only"])
            banned = [
                n
                for n in newly
                if n == "glue" or n.startswith("glue.") or n.startswith("teleagent_adapter")
            ]
            self.assertEqual(banned, [], f"inprocess isolation imported TA/glue: {banned}")

    def test_helper_rejects_shared_root(self):
        charter_a = load_charter(ISO_A)
        charter_b = load_charter(ISO_B)
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(IsolationError):
                run_two_jobs_isolated(
                    charter_a=charter_a,
                    charter_b=charter_b,
                    workdir_a=td,
                    workdir_b=td,
                )


class TestConcurrentIsolation(unittest.TestCase):
    def test_helper_threads_still_isolated(self):
        charter_a = load_charter(ISO_A)
        charter_b = load_charter(ISO_B)
        with tempfile.TemporaryDirectory() as td:
            wa = Path(td) / "workdir_a"
            wb = Path(td) / "workdir_b"
            report = run_two_jobs_isolated(
                charter_a=charter_a,
                charter_b=charter_b,
                workdir_a=wa,
                workdir_b=wb,
                concurrent=True,
            )
            self.assertTrue(report["ok"], report)
            self.assertTrue(report["concurrent"])
            self.assertNotEqual(report["workdir_a"], report["workdir_b"])
            self.assertEqual(report["artifact_of_a_in_b"], [])
            self.assertEqual(report["artifact_of_b_in_a"], [])
            self.assertTrue((wa / "iso-a.txt").is_file())
            self.assertTrue((wb / "iso-b.txt").is_file())
            self.assertFalse((wb / "iso-a.txt").exists())
            self.assertFalse((wa / "iso-b.txt").exists())
            self.assertIn("iso-a", (wa / "iso-a.txt").read_text(encoding="utf-8"))
            self.assertIn("iso-b", (wb / "iso-b.txt").read_text(encoding="utf-8"))

    def test_same_relative_name_isolated_by_workdir(self):
        """Even identical relative artifact names stay isolated across workdirs."""
        charter = {
            "name": "same-rel",
            "goal": "write out.txt",
            "must": ["Stay in workdir"],
            "must_not": ["Touch other workdirs"],
            "done_when": {"artifacts": ["out.txt"]},
            "force_lead_review": False,
        }
        with tempfile.TemporaryDirectory() as td:
            wa = Path(td) / "a"
            wb = Path(td) / "b"
            report = run_two_jobs_isolated(
                charter_a={**charter, "name": "same-rel-a"},
                charter_b={**charter, "name": "same-rel-b"},
                workdir_a=wa,
                workdir_b=wb,
                concurrent=True,
            )
            self.assertTrue(report["ok"], report)
            self.assertTrue((wa / "out.txt").is_file())
            self.assertTrue((wb / "out.txt").is_file())
            self.assertNotEqual(
                (wa / "out.txt").read_text(encoding="utf-8"),
                (wb / "out.txt").read_text(encoding="utf-8"),
            )


class TestRunJobCliKnife9(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_run_job()

    def _run(self, argv, env_extra=None):
        env = os.environ.copy()
        env.pop("COLLAB_EXECUTION_BACKEND", None)
        env.pop("COLLAB_INPROCESS_REVIEW_STUB", None)
        if env_extra:
            env.update(env_extra)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(os.environ, env, clear=True):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                rc = self.mod.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_dry_run_hello_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = self._run(["--dry-run", "--runs-dir", td, str(HELLO)])
            self.assertEqual(rc, 0, err)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["state"], "dry_run")
            self.assertNotIn("backend", summary)

    def test_cli_serial_separate_workspaces(self):
        with tempfile.TemporaryDirectory() as td:
            wa = str(Path(td) / "ws_a")
            wb = str(Path(td) / "ws_b")
            runs = str(Path(td) / "runs")
            rc_a, out_a, err_a = self._run(
                ["--backend", "inprocess", "--workspace", wa, "--runs-dir", runs, str(ISO_A)]
            )
            rc_b, out_b, err_b = self._run(
                ["--backend", "inprocess", "--workspace", wb, "--runs-dir", runs, str(ISO_B)]
            )
            self.assertEqual(rc_a, 0, err_a + out_a)
            self.assertEqual(rc_b, 0, err_b + out_b)
            sa = json.loads(out_a.strip().splitlines()[-1])
            sb = json.loads(out_b.strip().splitlines()[-1])
            self.assertTrue(sa["ok"], sa)
            self.assertTrue(sb["ok"], sb)
            self.assertEqual(sa.get("backend"), "inprocess")
            self.assertEqual(sb.get("backend"), "inprocess")
            self.assertNotEqual(Path(sa["workspace"]).resolve(), Path(sb["workspace"]).resolve())
            self.assertTrue((Path(wa) / "iso-a.txt").is_file())
            self.assertTrue((Path(wb) / "iso-b.txt").is_file())
            self.assertFalse((Path(wb) / "iso-a.txt").exists())
            self.assertFalse((Path(wa) / "iso-b.txt").exists())

    def test_cli_concurrent_subprocesses_isolated(self):
        """Two OS processes still need distinct --workspace paths."""
        import subprocess

        with tempfile.TemporaryDirectory() as td:
            wa = str(Path(td) / "ws_a")
            wb = str(Path(td) / "ws_b")
            runs = str(Path(td) / "runs")
            Path(runs).mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.pop("COLLAB_EXECUTION_BACKEND", None)
            env.pop("COLLAB_INPROCESS_REVIEW_STUB", None)
            script = str(REPO / "bin" / "run-job.py")
            cmd_a = [
                sys.executable,
                script,
                "--backend",
                "inprocess",
                "--workspace",
                wa,
                "--runs-dir",
                runs,
                str(ISO_A),
            ]
            cmd_b = [
                sys.executable,
                script,
                "--backend",
                "inprocess",
                "--workspace",
                wb,
                "--runs-dir",
                runs,
                str(ISO_B),
            ]
            p_a = subprocess.Popen(
                cmd_a, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
            )
            p_b = subprocess.Popen(
                cmd_b, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
            )
            out_a, err_a = p_a.communicate()
            out_b, err_b = p_b.communicate()
            self.assertEqual(p_a.returncode, 0, err_a + out_a)
            self.assertEqual(p_b.returncode, 0, err_b + out_b)
            self.assertTrue((Path(wa) / "iso-a.txt").is_file(), out_a)
            self.assertTrue((Path(wb) / "iso-b.txt").is_file(), out_b)
            self.assertFalse((Path(wb) / "iso-a.txt").exists())
            self.assertFalse((Path(wa) / "iso-b.txt").exists())
            self.assertNotEqual(Path(wa).resolve(), Path(wb).resolve())

    def test_default_entry_not_inprocess(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = self._run(["--dry-run", "--runs-dir", td, str(HELLO)])
            self.assertEqual(rc, 0, err)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertNotEqual(summary.get("backend"), "inprocess")
            self.assertEqual(summary["state"], "dry_run")


class TestPublicApiNoGlue(unittest.TestCase):
    def test_iso_a_via_run_inprocess_charter(self):
        charter = load_charter(ISO_A)
        with tempfile.TemporaryDirectory() as td:
            result = run_inprocess_charter(charter=charter, workdir=td, name="iso-a")
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["used_public_api_only"])
            self.assertTrue((Path(td) / "iso-a.txt").is_file())
            self.assertFalse((Path(td) / "iso-b.txt").exists())

    def test_backend_instances_are_not_the_isolation_claim(self):
        """Two instances, two workdirs: files isolated. Instance id is not the proof."""
        a = InProcessExecutionBackend()
        b = InProcessExecutionBackend()
        self.assertIsNot(a, b)
        with tempfile.TemporaryDirectory() as td:
            wa = Path(td) / "a"
            wb = Path(td) / "b"
            a.start_run(title="iso-a", directory=str(wa), artifacts=["iso-a.txt"])
            b.start_run(title="iso-b", directory=str(wb), artifacts=["iso-b.txt"])
            self.assertTrue((wa / "iso-a.txt").is_file())
            self.assertTrue((wb / "iso-b.txt").is_file())
            self.assertFalse((wb / "iso-a.txt").exists())
            self.assertFalse((wa / "iso-b.txt").exists())


if __name__ == "__main__":
    unittest.main()
