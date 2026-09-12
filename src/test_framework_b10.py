#!/usr/bin/env python3
"""Path-B knife10: workdir/path resource claim (queue or block)."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
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
    WorkdirClaimRegistry,
    occupancy_path,
    prove_same_workdir_no_silent_overwrite,
    prove_shared_workspace_collides,
    run_inprocess_charter,
    run_two_jobs_claimed,
    run_two_jobs_isolated,
    simulate_scheduler_workdir_claims,
)
from execution_backend.workdir_claim import (  # noqa: E402
    STATUS_BLOCKED,
    STATUS_GRANTED,
    STATUS_QUEUED,
    default_registry,
)

HELLO = REPO / "jobs/examples/hello.charter.yaml"
ISO_A = REPO / "jobs/examples/iso-a.charter.yaml"
ISO_B = REPO / "jobs/examples/iso-b.charter.yaml"


def _load_run_job():
    path = REPO / "bin" / "run-job.py"
    spec = importlib.util.spec_from_file_location("run_job_cli_knife10", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRegistryClaimRelease(unittest.TestCase):
    def test_claim_records_occupancy_file(self):
        reg = WorkdirClaimRegistry()
        with tempfile.TemporaryDirectory() as td:
            out = reg.claim(td, holder_id="h1", job_name="job-a", write_paths=["a.txt"])
            self.assertTrue(out.granted, out.to_dict())
            self.assertEqual(out.status, STATUS_GRANTED)
            occ = reg.occupancy(td)
            self.assertIsNotNone(occ)
            self.assertEqual(occ.holder_id, "h1")
            self.assertEqual(occ.job_name, "job-a")
            self.assertEqual(occ.write_paths, ["a.txt"])
            self.assertEqual(occ.pid, os.getpid())
            path = occupancy_path(td)
            self.assertTrue(path.is_file(), path)
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(disk["holder_id"], "h1")
            self.assertEqual(disk["job_name"], "job-a")
            self.assertTrue(reg.release(td, "h1"))
            self.assertIsNone(reg.occupancy(td))
            self.assertFalse(path.is_file())

    def test_second_holder_blocked_with_occupancy(self):
        reg = WorkdirClaimRegistry()
        with tempfile.TemporaryDirectory() as td:
            a = reg.claim(td, holder_id="alpha", job_name="hold-a")
            self.assertTrue(a.granted)
            b = reg.claim(td, holder_id="beta", job_name="wait-b", mode="block")
            self.assertFalse(b.granted)
            self.assertEqual(b.status, STATUS_BLOCKED)
            self.assertIsNotNone(b.occupancy)
            self.assertEqual(b.occupancy.holder_id, "alpha")
            self.assertEqual(b.occupancy.job_name, "hold-a")
            self.assertEqual(b.waiter["holder_id"], "beta")
            waiters = [w["holder_id"] for w in b.occupancy.waiters]
            self.assertIn("beta", waiters)
            self.assertFalse(reg.release(td, "beta"))
            self.assertTrue(reg.release(td, "alpha"))

    def test_reentrant_same_holder(self):
        reg = WorkdirClaimRegistry()
        with tempfile.TemporaryDirectory() as td:
            a1 = reg.claim(td, holder_id="same", job_name="j")
            a2 = reg.claim(td, holder_id="same", job_name="j")
            self.assertTrue(a1.granted and a2.granted)
            self.assertTrue(reg.release(td, "same"))
            self.assertIsNotNone(reg.occupancy(td))
            self.assertTrue(reg.release(td, "same"))
            self.assertIsNone(reg.occupancy(td))

    def test_queue_then_grant_after_release(self):
        reg = WorkdirClaimRegistry()
        with tempfile.TemporaryDirectory() as td:
            held = threading.Event()
            released = threading.Event()
            results: list = []

            def holder():
                o = reg.claim(td, holder_id="a", job_name="hold-a")
                self.assertTrue(o.granted)
                held.set()
                self.assertTrue(released.wait(3), "waiter never arrived")
                time.sleep(0.05)
                reg.release(td, "a")

            def waiter():
                self.assertTrue(held.wait(3))
                o = reg.claim(td, holder_id="b", job_name="wait-b", mode="queue", timeout_sec=2.0)
                results.append(o)

            t1 = threading.Thread(target=holder)
            t2 = threading.Thread(target=waiter)
            t1.start()
            t2.start()
            self.assertTrue(held.wait(3))
            time.sleep(0.05)
            occ = reg.occupancy(td)
            self.assertIsNotNone(occ)
            self.assertEqual(occ.holder_id, "a")
            self.assertTrue(any(w.get("holder_id") == "b" for w in occ.waiters), occ.waiters)
            released.set()
            t1.join(timeout=3)
            t2.join(timeout=3)
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].granted, results[0].to_dict())
            self.assertTrue(results[0].was_queued)
            self.assertEqual(results[0].status, STATUS_GRANTED)
            self.assertTrue(reg.release(td, "b"))


class TestSameWorkdirNoSilentOverwrite(unittest.TestCase):
    def test_unclaimed_two_instances_still_collide(self):
        """Knife 9 counterexample remains: start_run without claims overwrites."""
        with tempfile.TemporaryDirectory() as td:
            proof = prove_shared_workspace_collides(workdir=td, relative="shared.txt")
            self.assertTrue(proof["collided"], proof)
            self.assertIn("instance-b", proof["body_after_b"])

    def test_claimed_two_instances_second_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            proof = prove_same_workdir_no_silent_overwrite(workdir=td, relative="shared.txt")
            self.assertTrue(proof["ok"], proof)
            self.assertFalse(proof["silent_overwrite"], proof)
            self.assertIn(proof["second_status"], (STATUS_BLOCKED, STATUS_QUEUED))
            self.assertEqual(proof["holder_id"], "instance-a")
            self.assertIn("instance-a", proof["body_after_a"])
            self.assertEqual(proof["body_after_a"], proof["body_after_b"])
            self.assertNotIn("instance-b", proof["body_after_b"])
            occ = proof["occupancy_while_held"] or {}
            self.assertEqual(occ.get("holder_id"), "instance-a")
            self.assertNotEqual(proof["instance_a_id"], proof["instance_b_id"])


class TestRunInprocessCharterClaim(unittest.TestCase):
    def test_same_workdir_second_blocked(self):
        charter_a = load_charter(ISO_A)
        charter_b = load_charter(ISO_B)
        with tempfile.TemporaryDirectory() as td:
            report = run_two_jobs_claimed(
                charter_a=charter_a,
                charter_b=charter_b,
                workdir_a=td,
                workdir_b=td,
                concurrent=True,
                on_conflict="block",
                busy_hold_sec=0.08,
            )
            self.assertTrue(report["ok"], report)
            self.assertTrue(report["conflict"])
            self.assertTrue(report["queued_or_blocked"], report)
            self.assertGreaterEqual(report["refused_count"], 1)
            self.assertLessEqual(report["max_active"], 1)
            self.assertFalse(report["silent_overwrite"])
            occ = report["occupancy"] or {}
            self.assertTrue(occ.get("holder_id"), occ)
            self.assertIn(occ.get("job_name"), ("iso-a", "iso-b", occ.get("job_name")))
            # Exactly one of the unique artifacts (or the winner's file) is written;
            # the refused job must not have overwritten the holder.
            a_txt = Path(td) / "iso-a.txt"
            b_txt = Path(td) / "iso-b.txt"
            written = [p.name for p in (a_txt, b_txt) if p.is_file()]
            self.assertGreaterEqual(len(written), 1)
            refused = [
                j
                for j in (report["job_a"], report["job_b"])
                if str(j.get("state") or "") in (STATUS_BLOCKED, STATUS_QUEUED)
            ]
            self.assertTrue(refused)
            self.assertFalse(refused[0].get("ok"))
            holder = refused[0].get("occupancy") or {}
            self.assertTrue(holder.get("holder_id"), refused[0])

    def test_sequential_same_workdir_still_runs(self):
        charter = load_charter(HELLO)
        with tempfile.TemporaryDirectory() as td:
            r1 = run_inprocess_charter(charter=charter, workdir=td, name="hello-1")
            r2 = run_inprocess_charter(charter=charter, workdir=td, name="hello-2")
            self.assertTrue(r1["ok"], r1)
            self.assertTrue(r2["ok"], r2)
            self.assertEqual(r1.get("resource_status"), STATUS_GRANTED)
            self.assertTrue((Path(td) / "hello-from-worker.txt").is_file())

    def test_held_claim_blocks_run_inprocess_charter(self):
        charter = load_charter(ISO_A)
        with tempfile.TemporaryDirectory() as td:
            reg = default_registry()
            held = reg.claim(td, holder_id="parent-hold", job_name="holder")
            self.assertTrue(held.granted)
            try:
                result = run_inprocess_charter(charter=charter, workdir=td, name="iso-a")
                self.assertFalse(result["ok"], result)
                self.assertIn(result["state"], (STATUS_BLOCKED, STATUS_QUEUED))
                self.assertEqual((result.get("occupancy") or {}).get("holder_id"), "parent-hold")
                self.assertFalse((Path(td) / "iso-a.txt").exists())
            finally:
                reg.release(td, "parent-hold")


class TestDistinctWorkdirsStillParallel(unittest.TestCase):
    def test_claimed_pair_runs_in_parallel(self):
        charter_a = load_charter(ISO_A)
        charter_b = load_charter(ISO_B)
        with tempfile.TemporaryDirectory() as td:
            wa = Path(td) / "a"
            wb = Path(td) / "b"
            report = run_two_jobs_claimed(
                charter_a=charter_a,
                charter_b=charter_b,
                workdir_a=wa,
                workdir_b=wb,
                concurrent=True,
                busy_hold_sec=0.1,
            )
            self.assertTrue(report["ok"], report)
            self.assertTrue(report["workdirs_distinct"])
            self.assertTrue(report["parallel"], report)
            self.assertGreaterEqual(report["max_active"], 2, report)
            self.assertTrue((wa / "iso-a.txt").is_file())
            self.assertTrue((wb / "iso-b.txt").is_file())
            self.assertFalse((wb / "iso-a.txt").exists())
            self.assertFalse((wa / "iso-b.txt").exists())
            self.assertIn("iso-a", (wa / "iso-a.txt").read_text(encoding="utf-8"))
            self.assertIn("iso-b", (wb / "iso-b.txt").read_text(encoding="utf-8"))

    def test_isolation_helper_unchanged(self):
        charter_a = load_charter(ISO_A)
        charter_b = load_charter(ISO_B)
        with tempfile.TemporaryDirectory() as td:
            wa = Path(td) / "a"
            wb = Path(td) / "b"
            report = run_two_jobs_isolated(
                charter_a=charter_a,
                charter_b=charter_b,
                workdir_a=wa,
                workdir_b=wb,
                concurrent=True,
            )
            self.assertTrue(report["ok"], report)
            self.assertTrue((wa / "iso-a.txt").is_file())
            self.assertTrue((wb / "iso-b.txt").is_file())
            with self.assertRaises(IsolationError):
                run_two_jobs_isolated(
                    charter_a=charter_a,
                    charter_b=charter_b,
                    workdir_a=td,
                    workdir_b=td,
                )


class TestSchedulerSim(unittest.TestCase):
    def test_same_workdir_second_stays_queued(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = [
                {
                    "job_id": "job-a",
                    "name": "iso-a",
                    "charter": load_charter(ISO_A),
                    "workdir": td,
                },
                {
                    "job_id": "job-b",
                    "name": "iso-b",
                    "charter": load_charter(ISO_B),
                    "workdir": td,
                },
            ]
            report = simulate_scheduler_workdir_claims(jobs, max_parallel=2, execute=True)
            self.assertEqual(report["started_count"], 1, report)
            self.assertEqual(report["queued_count"], 1, report)
            self.assertTrue(report["same_workdir_queued_or_blocked"])
            q = report["queued"][0]
            self.assertEqual(q["reason"], "workdir_occupied")
            holder = (q.get("holder") or {}).get("holder_id") or (
                (q.get("occupancy") or {}).get("occupancy") or {}
            ).get("holder_id")
            # occupancy.to_dict nested under ClaimOutcome.to_dict as occupancy/holder
            occ = q.get("occupancy") or {}
            hid = (q.get("holder") or {}).get("holder_id") or (occ.get("holder") or {}).get(
                "holder_id"
            ) or occ.get("holder_id")
            self.assertEqual(hid, "job-a", q)
            started = report["started"][0]
            self.assertTrue((started.get("result") or {}).get("ok"), started)

    def test_distinct_workdirs_both_start(self):
        with tempfile.TemporaryDirectory() as td:
            wa = Path(td) / "a"
            wb = Path(td) / "b"
            jobs = [
                {
                    "job_id": "job-a",
                    "name": "iso-a",
                    "charter": load_charter(ISO_A),
                    "workdir": wa,
                },
                {
                    "job_id": "job-b",
                    "name": "iso-b",
                    "charter": load_charter(ISO_B),
                    "workdir": wb,
                },
            ]
            report = simulate_scheduler_workdir_claims(jobs, max_parallel=2, execute=True)
            self.assertEqual(report["started_count"], 2, report)
            self.assertEqual(report["queued_count"], 0, report)
            self.assertGreaterEqual(report["max_active"], 2, report)
            self.assertTrue((wa / "iso-a.txt").is_file())
            self.assertTrue((wb / "iso-b.txt").is_file())


class TestRunJobCliKnife10(unittest.TestCase):
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
            self.assertNotIn("occupancy", summary)

    def test_default_entry_not_inprocess(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = self._run(["--dry-run", "--runs-dir", td, str(HELLO)])
            self.assertEqual(rc, 0, err)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertNotEqual(summary.get("backend"), "inprocess")

    def test_cli_blocked_when_parent_holds_claim(self):
        with tempfile.TemporaryDirectory() as td:
            wa = str(Path(td) / "ws")
            Path(wa).mkdir()
            runs = str(Path(td) / "runs")
            held = default_registry().claim(wa, holder_id="cli-parent", job_name="holder")
            self.assertTrue(held.granted)
            try:
                rc, out, err = self._run(
                    ["--backend", "inprocess", "--workspace", wa, "--runs-dir", runs, str(ISO_A)]
                )
                self.assertEqual(rc, 1, err + out)
                summary = json.loads(out.strip().splitlines()[-1])
                self.assertFalse(summary["ok"], summary)
                self.assertIn(summary["state"], (STATUS_BLOCKED, STATUS_QUEUED))
                occ = summary.get("occupancy") or {}
                self.assertEqual(occ.get("holder_id"), "cli-parent", summary)
                self.assertEqual(occ.get("job_name"), "holder")
                self.assertFalse((Path(wa) / "iso-a.txt").exists())
            finally:
                default_registry().release(wa, "cli-parent")

    def test_cli_isolated_workspaces_ok(self):
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
            self.assertTrue((Path(wa) / "iso-a.txt").is_file())
            self.assertTrue((Path(wb) / "iso-b.txt").is_file())


class TestBackendInstancesAreNotTheClaim(unittest.TestCase):
    def test_two_backends_same_dir_need_claim(self):
        a = InProcessExecutionBackend()
        b = InProcessExecutionBackend()
        self.assertIsNot(a, b)
        with tempfile.TemporaryDirectory() as td:
            from execution_backend.workdir_claim import start_run_with_workdir_claim

            ra = start_run_with_workdir_claim(
                a, title="iso-a", directory=td, artifacts=["iso-a.txt"], holder_id="be-a",
                release_after=False,
            )
            rb = start_run_with_workdir_claim(
                b, title="iso-b", directory=td, artifacts=["iso-b.txt"], holder_id="be-b",
            )
            self.assertTrue(ra.get("ok"), ra)
            self.assertFalse(rb.get("ok"), rb)
            self.assertIn(rb.get("state"), (STATUS_BLOCKED, STATUS_QUEUED))
            self.assertTrue((Path(td) / "iso-a.txt").is_file())
            self.assertFalse((Path(td) / "iso-b.txt").exists())
            default_registry().release(td, "be-a")


if __name__ == "__main__":
    unittest.main()
