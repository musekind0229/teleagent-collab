"""Round 5 regressions: lead decisions in a process group, lead-runs reaping,
session attribution windows, tool CPU signal, Job Object (mocked), artifacts.

Each RED test fails on 395eb8d; see the commit message for which ones.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from execution_backend.agy_run_registry import AgyRunRegistry, process_start_token
from execution_backend.agy_session_attribution import SessionAttributor
from lead_adapter import cancel
from lead_adapter.grok_cli import GrokCliLeadAdapter
from lead_adapter.schema import build_lead_request, lead_review_response_schema

ROOT = Path(__file__).resolve().parents[1]
POSIX = os.name == "posix" and hasattr(os, "killpg")

# Lead that starts a grandchild (writes a marker after 2 s), then hangs.
_SLOW_LEAD = textwrap.dedent(
    """\
    #!/bin/sh
    echo $$ > "{root}/lead.pid"
    ( sleep 2; echo late > "{root}/grandchild.marker" ) &
    sleep 30
    """
)


def _script(root: Path, body: str) -> str:
    path = root / "slow_lead"
    path.write_text(body.format(root=root), encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a zombie of ours is not "running"
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            return fh.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except OSError:
        return True


def _req() -> dict:
    return build_lead_request(
        kind="review",
        goal="stay in ws",
        authorized_scope=["ws"],
        prohibitions=["secrets"],
        acceptance_criteria={"artifacts": ["a.txt"]},
        current_application={"tool": "write"},
    )


@unittest.skipUnless(POSIX, "process groups are POSIX; Windows uses taskkill/Job Object")
class DecisionTimeoutStopsGroup(unittest.TestCase):
    """RED: a permission/review decision (no CancelScope) that times out must
    stop the lead's whole group. 395eb8d used subprocess.run, which kills only
    the direct child, so the grandchild wrote its marker."""

    def _check(self, run):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lead = _script(root, _SLOW_LEAD)
            status, _ = run(lead, root)
            self.assertEqual(str(status).lower(), "timeout")
            pid = int((root / "lead.pid").read_text())
            time.sleep(3.0)
            self.assertFalse((root / "grandchild.marker").exists(), "grandchild outlived the decision timeout")
            self.assertFalse(_alive(pid), "lead still running after decision timeout")

    def test_grok_decide_timeout(self):
        def run(lead, root):
            ad = GrokCliLeadAdapter(bin_path=lead)
            return ad.decide(_req(), schema=lead_review_response_schema(), cwd=str(root), timeout_sec=1.0)

        self._check(run)

    def test_deepseek_decide_timeout(self):
        from lead_adapter.deepseek_harness import DeepSeekHarnessLeadAdapter

        def run(lead, root):
            ad = DeepSeekHarnessLeadAdapter(bin_path=lead)
            return ad.decide(_req(), schema=lead_review_response_schema(), cwd=str(root), timeout_sec=1.0)

        self._check(run)


@unittest.skipUnless(POSIX, "POSIX process check")
class LeadRegistry(unittest.TestCase):
    def tearDown(self):
        cancel.set_process_registry(None)

    def test_lead_pid_recorded_during_call_and_forgotten_after(self):
        """RED: 395eb8d had no lead process registry."""
        seen: list[tuple] = []

        class Rec:
            def record(self, run_id, *, pid, directory=""):
                seen.append(("record", run_id, pid))

            def forget(self, run_id):
                seen.append(("forget", run_id))

        cancel.set_process_registry(Rec())
        with tempfile.TemporaryDirectory() as tmp:
            out = cancel.run_cancellable([sys.executable, "-c", "print('hi')"], timeout=10)
        self.assertEqual(out.stdout.strip(), "hi")
        self.assertEqual([row[0] for row in seen], ["record", "forget"])
        self.assertTrue(seen[0][1].startswith("lead_"))
        self.assertEqual(seen[0][1], seen[1][1])

    def _service(self):
        spec = importlib.util.spec_from_file_location("collab_service_r5", ROOT / "bin" / "collab-service.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_restart_reaps_lead_of_dead_service_and_spares_reused_pid(self):
        """RED: 395eb8d had no _install_lead_registry / lead-runs.json."""
        svc = self._service()
        orphan = subprocess.Popen(["sleep", "60"], start_new_session=True)
        bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)
        dead = subprocess.Popen(["true"])
        dead.wait()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                persist = Path(tmp)
                reg = AgyRunRegistry(persist / "lead-runs.json")
                reg.record("lead_orphan", pid=orphan.pid)
                reg.record("lead_reused", pid=bystander.pid)
                data = json.loads((persist / "lead-runs.json").read_text())
                for entry in data["runs"].values():
                    entry["owner_pid"] = dead.pid  # service that recorded it is gone
                    entry["owner_token"] = "dead-service"
                data["runs"]["lead_reused"]["start_token"] = "not-this-process"
                (persist / "lead-runs.json").write_text(json.dumps(data))
                rows = svc._install_lead_registry(persist)
                outcomes = {r["run_id"]: r["outcome"] for r in rows}
                self.assertEqual(outcomes.get("lead_orphan"), "killed")
                self.assertEqual(outcomes.get("lead_reused"), "pid_reused_left_alone")
                orphan.wait(timeout=5)
                self.assertIsNone(bystander.poll(), "a reused pid must not be killed")
                self.assertIsNotNone(cancel._REGISTRY)
        finally:
            for p in (orphan, bystander):
                if p.poll() is None:
                    p.kill()
                    p.wait()


class SessionAttributionWindows(unittest.TestCase):
    def _rec(self, home: Path, started_at: float) -> dict:
        return {"run_id": "r1", "spawn_environ": {"HOME": str(home)}, "started_at": started_at, "_session_baseline": []}

    def test_log_named_at_run_start_is_attributed(self):
        """RED: 395eb8d had no log_start_time attribution."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            now = time.time()
            log = home / ".gemini" / "antigravity-cli" / "log"
            log.mkdir(parents=True)
            (log / time.strftime("cli-%Y%m%d_%H%M%S.log", time.localtime(now))).write_text("x")
            at, note = SessionAttributor(fd_scan=False).resolve(self._rec(home, now), pids=[], runs_sharing_home=1)
            self.assertEqual(note["attribution"], "log_start_time")
            self.assertTrue(note["used"])
            self.assertIsNotNone(at)

    def test_two_logs_at_same_start_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            now = time.time()
            log = home / ".gemini" / "antigravity-cli" / "log"
            log.mkdir(parents=True)
            for delta in (0, 1):
                (log / time.strftime("cli-%Y%m%d_%H%M%S.log", time.localtime(now + delta))).write_text("x")
            at, note = SessionAttributor(fd_scan=False).resolve(self._rec(home, now), pids=[], runs_sharing_home=1)
            self.assertIsNone(at)
            self.assertIn("ambiguous", note["reason"])

    def test_session_file_appearing_long_after_start_is_not_attributed(self):
        """RED: 395eb8d attributed any new conversation file (new_file)."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            conv = home / ".gemini" / "antigravity-cli" / "conversations"
            conv.mkdir(parents=True)
            (conv / "abc.db").write_text("x")
            at, note = SessionAttributor(fd_scan=False).resolve(
                self._rec(home, time.time() - 120), pids=[], runs_sharing_home=1
            )
            self.assertIsNone(at)
            self.assertEqual(note["attribution"], "none")
            self.assertIn("30s", note["reason"])

    def test_session_file_at_start_is_new_file_and_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            conv = home / ".gemini" / "antigravity-cli" / "conversations"
            conv.mkdir(parents=True)
            (conv / "abc.db").write_text("x")
            att = SessionAttributor(fd_scan=False)
            rec = self._rec(home, time.time())
            _, note = att.resolve(rec, pids=[], runs_sharing_home=1)
            self.assertEqual(note["attribution"], "new_file")
            self.assertEqual(att.check_conversation(rec, "abc"), "match")
            _, note = att.resolve(rec, pids=[], runs_sharing_home=1)
            self.assertEqual(note.get("verified"), "match")


@unittest.skipUnless(os.path.isdir("/proc/self"), "Linux /proc")
class ToolCpuSignal(unittest.TestCase):
    def test_busy_tool_counts_sleeping_tool_does_not(self):
        """RED: 395eb8d had no tool_cpu signal (a busy silent tool went stale)."""
        from execution_backend.antigravity_cli_v1 import TOOL_CPU_MIN_TICKS, AntigravityCliExecutionBackend as B

        busy = subprocess.Popen([sys.executable, "-c", "import time\nt=time.time()\nwhile time.time()-t<3: pass"])
        idle = subprocess.Popen(["sleep", "5"])
        try:
            rec_busy: dict = {}
            rec_idle: dict = {}
            B._tool_cpu_delta(rec_busy, [busy.pid])
            B._tool_cpu_delta(rec_idle, [idle.pid])
            time.sleep(0.6)
            self.assertGreaterEqual(B._tool_cpu_delta(rec_busy, [busy.pid]), TOOL_CPU_MIN_TICKS)
            self.assertLess(B._tool_cpu_delta(rec_idle, [idle.pid]), TOOL_CPU_MIN_TICKS)
        finally:
            for p in (busy, idle):
                p.kill()
                p.wait()

    def test_signal_view_passes_tool_cpu(self):
        from framework.progress_budget import heartbeat_signals_view

        view = heartbeat_signals_view({"tool_cpu": {"used": True, "processes": 2, "secret": "x"}})
        self.assertEqual(view, {"tool_cpu": {"used": True, "processes": 2}})


class WindowsJobObjectMocked(unittest.TestCase):
    """未在 Win 实测: mock kernel32 only."""

    def _k32(self, assign_ok=1):
        k = mock.MagicMock()
        k.CreateJobObjectW.return_value = 77
        k.SetInformationJobObject.return_value = 1
        k.AssignProcessToJobObject.return_value = assign_ok
        k.TerminateJobObject.return_value = 1
        return k

    def test_attach_terminate_close(self):
        from platform_services import win_job

        proc = mock.Mock(_handle=999)
        k = self._k32()
        self.assertEqual(win_job.attach(proc, "win32", kernel32=k), 77)
        flags = k.SetInformationJobObject.call_args
        self.assertEqual(flags.args[1], 9)
        k.AssignProcessToJobObject.assert_called_once_with(77, 999)
        self.assertTrue(win_job.terminate(proc))
        k.TerminateJobObject.assert_called_once_with(77, 1)
        win_job.close(proc)
        k.CloseHandle.assert_called_once_with(77)

    def test_assign_failure_and_non_windows_are_noops(self):
        from platform_services import win_job

        proc = mock.Mock(_handle=1, _collab_job=None)
        k = self._k32(assign_ok=0)
        self.assertIsNone(win_job.attach(proc, "win32", kernel32=k))
        k.CloseHandle.assert_called_once_with(77)
        self.assertFalse(win_job.terminate(proc))
        self.assertIsNone(win_job.attach(mock.Mock(), "linux"))

    def test_stop_process_tree_windows_terminates_job_then_taskkill(self):
        k = self._k32()
        proc = mock.Mock(pid=4321)
        proc.poll.return_value = None
        proc._collab_job = (k, 77)
        with mock.patch("lead_adapter.cancel.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            row = cancel.stop_process_tree(proc, "win32")
        self.assertEqual(row.get("job_object"), "terminated")
        self.assertEqual(run.call_args.args[0][:3], ["taskkill", "/F", "/T"])


class StatusArtifactIndex(unittest.TestCase):
    def test_relative_deduped_per_task(self):
        """RED: 395eb8d had no top-level artifacts in status (--full readers)."""
        from framework.artifact_index import goal_artifact_index

        ws = "/srv/w/task_1"
        tasks = [
            {"task_id": "t1", "result": {"workspace": ws, "artifacts": [f"{ws}/a.txt", "./a.txt", "sub\\b.md"]}},
            {"task_id": "t2", "workspace": "C:\\W\\t2", "result": {"artifacts": {"C:/w/t2/c.txt": {"size": 3}}}},
            {"task_id": "t3"},
        ]
        self.assertEqual(
            goal_artifact_index(tasks),
            [
                {"task_id": "t1", "path": "a.txt"},
                {"task_id": "t1", "path": "sub/b.md"},
                {"task_id": "t2", "path": "c.txt"},
            ],
        )


class FailedGoalExplainsMissingArtifacts(unittest.TestCase):
    """#12 live finding (round 5): worker exited 0, wrote other.md, delivery.md
    missing -> top-level reason was a bare "task failed" and outcome said
    "no output produced"."""

    def test_reason_names_missing_artifacts(self):
        """RED on 395eb8d: failure_reason was 'task failed'."""
        from framework.failure_projection import project_failed_tasks

        out = project_failed_tasks([
            {"task_id": "t1", "status": "failed", "result": {"ok": False, "error": "", "missing": ["delivery.md"]}}
        ])
        self.assertEqual(out["failure_reason"], "required artifacts missing: delivery.md")

    def test_worker_error_still_wins(self):
        from framework.failure_projection import project_failed_tasks

        out = project_failed_tasks([
            {"task_id": "t1", "status": "failed", "result": {"ok": False, "error": "boom", "missing": ["d.md"]}}
        ])
        self.assertEqual(out["failure_reason"], "boom")

    def _brief(self, ws: Path, inputs=None) -> dict:
        from framework.app_service import _annotate_task_failure

        task = {"task_id": "t1", "workspace": str(ws), "expected_artifacts": ["delivery.md"]}
        if inputs is not None:
            task["inputs"] = {"input_files": inputs}
        brief = {"source": "task_failed", "error": ""}
        _annotate_task_failure(brief, task)
        return brief

    def test_other_files_are_not_no_output(self):
        """RED on 395eb8d: outcome was no_output."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "other.md").write_text("x")
            brief = self._brief(ws)
            self.assertEqual(brief["outcome"], "other_output")
            self.assertIn("required artifacts missing", brief["outcome_summary"])

    def test_staged_inputs_do_not_count_as_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "a.txt").write_text("handed off")
            self.assertEqual(self._brief(ws, inputs=["a.txt"])["outcome"], "no_output")
            (ws / "delivery.md").write_text("x")
            self.assertEqual(self._brief(ws, inputs=["a.txt"])["outcome"], "candidate_produced")


if __name__ == "__main__":
    unittest.main()
