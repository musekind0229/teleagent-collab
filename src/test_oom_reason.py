"""OOM-killed workers and an OOM-killed service must say so (mde, 2026-10-04).

RED on f6195fc: a worker SIGKILLed by the cgroup OOM killer failed with a
bare exit/stderr text and source task_failed; a restart after the service
itself was OOM-killed did not mention OOM.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from platform_services import cgroup_oom

_POSIX = os.name == "posix" and hasattr(os, "killpg")

# Fake agy: bump the (fake) cgroup oom_kill counter, then die from SIGKILL,
# exactly what the kernel's cgroup OOM killer leaves behind.
_OOM_AGY = textwrap.dedent(
    """\
    import os, signal, sys
    events = os.environ["FAKE_MEMORY_EVENTS"]
    text = open(events).read().replace("oom_kill 0", "oom_kill 1")
    open(events, "w").write(text)
    print("allocating", file=sys.stderr, flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
    """
)



class OomNoteUnit(unittest.TestCase):
    def test_counter_parse_and_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "memory.events").write_text("low 0\nhigh 0\nmax 2\noom 1\noom_kill 2\n")
            (d / "memory.max").write_text("157286400\n")
            self.assertEqual(cgroup_oom.oom_kill_count(d), 2)
            note = cgroup_oom.oom_note(d, 1, -9)
            self.assertTrue(note.startswith("killed by OOM (cgroup memory limit)"), note)
            self.assertIn("memory.max=157286400", note)
            self.assertIsNone(cgroup_oom.oom_note(d, 2, -9), "no increase: no OOM claim")
            self.assertIsNone(cgroup_oom.oom_note(None, 0, -9))
            self.assertIn("likely", cgroup_oom.oom_note(d, 1, 1))

    def test_sigkill_codes(self):
        self.assertTrue(cgroup_oom.killed_by_sigkill(-9))
        self.assertTrue(cgroup_oom.killed_by_sigkill(137))
        self.assertFalse(cgroup_oom.killed_by_sigkill(1))


@unittest.skipUnless(_POSIX, "fake agy wrapper is a POSIX shell script")
class OomKilledWorkerIsReported(unittest.TestCase):
    def test_worker_sigkilled_with_oom_kill_increase(self):
        from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cg = root / "cg"
            cg.mkdir()
            (cg / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n")
            (cg / "memory.max").write_text("157286400\n")
            script = root / "oom_agy.py"
            script.write_text(_OOM_AGY, encoding="utf-8")
            wrapper = root / "agy"
            wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n", encoding="utf-8")
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
            home = root / "home"
            home.mkdir()
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(home),
                "FAKE_MEMORY_EVENTS": str(cg / "memory.events"),
            }
            be = AntigravityCliExecutionBackend(bin_path=str(wrapper), environ=env, timeout_sec=30)
            with mock.patch.object(cgroup_oom, "cgroup_dir", lambda pid="self", **kw: cg):
                run = be.start_run(title="t", directory=str(root / "ws"), instruction="eat memory")
                for _ in range(100):
                    if be.observe_run(run["run_id"]).get("state") not in ("running", "busy", None):
                        break
                    time.sleep(0.1)
                out = be.collect_result(run["run_id"])
            self.assertFalse(out["ok"])
            self.assertTrue(out["error"].startswith("killed by OOM (cgroup memory limit)"), out["error"])
            self.assertEqual(out.get("error_source"), "oom")

            from framework.failure_projection import project_failed_tasks

            proj = project_failed_tasks([{"task_id": "t1", "status": "failed", "result": out}])
            self.assertTrue(proj["failure_reason"].startswith("killed by OOM"), proj["failure_reason"])
            self.assertEqual(proj["primary_failure"]["source"], "oom")
            self.assertIn("memory limit", proj["primary_failure"]["next_step"])


class OomKilledServiceIsReportedAfterRestart(unittest.TestCase):
    def tearDown(self):
        from execution_backend.agy_run_registry import set_previous_exit_note

        set_previous_exit_note("")

    def test_execstoppost_record_then_reap_reason(self):
        from execution_backend.agy_run_registry import describe_reaped, set_previous_exit_note
        from framework.service_exit import consume_previous_exit, previous_exit_note, record_exit

        with tempfile.TemporaryDirectory() as tmp:
            record_exit(tmp, {"SERVICE_RESULT": "oom-kill", "EXIT_CODE": "killed", "EXIT_STATUS": "KILL"})
            row = consume_previous_exit(tmp)
            self.assertEqual(row["service_result"], "oom-kill")
            self.assertIsNone(consume_previous_exit(tmp), "record is consumed once")
            set_previous_exit_note(previous_exit_note(row))
            for outcome in ("already_exited", "stopped_at_shutdown", "killed"):
                text = describe_reaped("agy_x", {"pid": 7, "outcome": outcome})
                self.assertIn("killed by OOM (cgroup memory limit)", text, outcome)
            from framework.failure_projection import project_failed_tasks

            reaped = {"ok": False, "error": "backend resume failed: BackendError: " + text}
            proj = project_failed_tasks([{"task_id": "t1", "status": "failed", "result": reaped}])
            self.assertEqual(proj["primary_failure"]["source"], "oom")

    def test_normal_stop_adds_nothing(self):
        from framework.service_exit import previous_exit_note

        self.assertEqual(previous_exit_note({"service_result": "success"}), "")
        self.assertEqual(previous_exit_note(None), "")


if __name__ == "__main__":
    unittest.main()
