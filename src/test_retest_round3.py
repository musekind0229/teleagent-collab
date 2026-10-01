"""Round-3 box re-test fixes (余坑 1-7). Offline fakes only; RED verified on 68c7acc."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from execution_backend.base import BackendError, BackendStatus
from framework.app_service import CollabApplication
from test_app_service import _AsyncBackend, _request

_POSIX = os.name != "nt"

_FAKE = textwrap.dedent(
    r'''
    import json, os, re, subprocess, sys, time
    prompt = next((a[len("--print="):] for a in sys.argv[1:] if a.startswith("--print=")), "")
    def m(name): return re.findall(name + r"\[([^\]]*)\]", prompt)
    for spec in m("CHILD"):
        n, _, name = spec.partition(":")
        subprocess.Popen([sys.executable, "-c", f"import time;time.sleep({float(n)});open({name!r},'w').write('x')"])
    for spec in m("GCHILD"):  # tool command in its own session/group, like agy's shell tool
        n, _, name = spec.partition(":")
        subprocess.Popen([sys.executable, "-c", f"import time;time.sleep({float(n)});open({name!r},'w').write('x')"], start_new_session=True)
    for spec in m("WRITE"):
        open(spec, "w").write("ok\n")
    for spec in m("SESSION"):
        n, iv = [float(x) for x in spec.split(",")]
        d = os.path.join(os.environ.get("HOME") or os.environ.get("USERPROFILE") or ".", ".gemini", "antigravity-cli", "conversations")
        os.makedirs(d, exist_ok=True)
        for _ in range(int(n)):
            open(os.path.join(d, "c.db"), "a").write("x"); time.sleep(iv)
    for spec in m("SLEEP"):
        time.sleep(float(spec))
    if m("FINALFAIL"):
        sys.stderr.write("agy: finalize failed\n"); sys.exit(1)
    print(json.dumps({"conversation_id": "c1", "status": "success", "response": "done", "usage": {"input_tokens": 1}}))
    '''
)


def _fake_agy(root: Path) -> str:
    script = root / "fake_agy.py"
    script.write_text(_FAKE, encoding="utf-8")
    if os.name == "nt":
        cmd = root / "fake_agy.cmd"
        cmd.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return str(cmd)
    wrapper = root / "fake_agy"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n", encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def _backend(root: Path, **kw):
    from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend

    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(root / "home"), "USERPROFILE": str(root / "home")}
    (root / "home").mkdir(exist_ok=True)
    return AntigravityCliExecutionBackend(bin_path=_fake_agy(root), environ=env, timeout_sec=60, **kw)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # zombie counts as gone
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0] != "Z"
    except OSError:
        return True


# ---- 1: orphaned agy workers after a service restart -------------------------
@unittest.skipUnless(_POSIX, "process-group kill path; Windows uses taskkill /T (see test_kill_tree_windows_uses_taskkill)")
class OrphanReapTests(unittest.TestCase):
    def test_restart_stops_orphan_worker_tree(self):
        from execution_backend.agy_run_registry import AgyRunRegistry  # noqa: F401  (module must exist)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            reg = root / "agy-runs.json"
            old = _backend(root, run_registry_path=str(reg))
            started = old.start_run(title="t", directory=str(ws), instruction="CHILD[3:late.txt] SLEEP[3]")
            pid = old._runs[started["run_id"]]["pid"]
            time.sleep(0.5)
            # The old service is gone without close(); a new one starts on the same persist dir.
            new = _backend(root, run_registry_path=str(reg))
            self.assertEqual([r["outcome"] for r in new.reaped_at_start], ["killed"])
            deadline = time.time() + 3
            while _alive(pid) and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(_alive(pid))
            time.sleep(3.5)
            self.assertFalse((ws / "late.txt").exists(), "orphan grandchild kept writing after restart")
            with self.assertRaises(BackendError) as ctx:
                new.observe_run(started["run_id"])
            self.assertIn("previous service process", str(ctx.exception))
            self.assertIn("stopped when the service restarted", str(ctx.exception))
            old._runs[started["run_id"]]["proc"].wait(timeout=5)

    def test_cancel_stops_tool_child_in_other_process_group(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            be = _backend(root)
            run = be.start_run(title="t", directory=str(ws), instruction="GCHILD[2.5:tool.txt] SLEEP[20]")
            time.sleep(0.8)
            be.observe_run(run["run_id"])  # sees the tool child
            be.cancel(run["run_id"])
            time.sleep(3.0)
            self.assertFalse((ws / "tool.txt").exists(), "tool child in its own group survived cancel")

    def test_restart_reaps_tool_child_after_worker_died(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            reg = root / "agy-runs.json"
            old = _backend(root, run_registry_path=str(reg))
            run = old.start_run(title="t", directory=str(ws), instruction="GCHILD[3:tool.txt] SLEEP[1]")
            time.sleep(0.6)
            old.observe_run(run["run_id"])  # records the tool child in agy-runs.json
            old._runs[run["run_id"]]["proc"].wait(timeout=5)  # worker exits; tool child lives on
            new = _backend(root, run_registry_path=str(reg))
            self.assertEqual([r["outcome"] for r in new.reaped_at_start], ["killed"])
            time.sleep(3.0)
            self.assertFalse((ws / "tool.txt").exists())

    def test_harvest_stops_tool_child_left_behind(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            be = _backend(root)
            run = be.start_run(title="t", directory=str(ws), instruction="GCHILD[3:tool.txt] SLEEP[1] WRITE[a.txt]")
            time.sleep(0.6)
            be.observe_run(run["run_id"])
            be.collect_result(run["run_id"])
            time.sleep(3.0)
            self.assertFalse((ws / "tool.txt").exists())

    def test_close_stops_live_worker_and_explains_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reg = root / "agy-runs.json"
            old = _backend(root, run_registry_path=str(reg))
            started = old.start_run(title="t", directory=str(root / "ws"), instruction="SLEEP[20]")
            proc = old._runs[started["run_id"]]["proc"]
            old.close()
            self.assertIsNotNone(proc.poll())
            new = _backend(root, run_registry_path=str(reg))
            with self.assertRaises(BackendError) as ctx:
                new.observe_run(started["run_id"])
            self.assertIn("stopped when the service shut down", str(ctx.exception))

    def test_reused_pid_is_left_alone(self):
        from execution_backend.agy_run_registry import AgyRunRegistry

        with tempfile.TemporaryDirectory() as td:
            reg = AgyRunRegistry(Path(td) / "r.json")
            victim = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True)
            try:
                reg.record("agy_x", pid=victim.pid)
                data = json.loads(reg.path.read_text())
                data["runs"]["agy_x"]["start_token"] = "linux:1"  # a different process once had this pid
                data["runs"]["agy_x"]["owner_pid"] = 999999
                reg.path.write_text(json.dumps(data))
                rows = AgyRunRegistry(reg.path).reap_orphans()
                self.assertEqual(rows[0]["outcome"], "pid_reused_left_alone")
                self.assertIsNone(victim.poll())
            finally:
                victim.kill()
                victim.wait()

    def test_finished_run_is_not_reaped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reg = root / "agy-runs.json"
            be = _backend(root, run_registry_path=str(reg))
            started = be.start_run(title="t", directory=str(root / "ws"), instruction="WRITE[a.txt]")
            be.collect_result(started["run_id"])
            self.assertEqual(json.loads(reg.read_text())["runs"], {})


class KillTreeWindowsTests(unittest.TestCase):
    def test_kill_tree_windows_uses_taskkill(self):
        from unittest import mock

        from execution_backend import agy_run_registry as mod

        calls = []
        with mock.patch.object(mod, "_is_windows", return_value=True), \
                mock.patch.object(mod, "_windows_start_token", return_value=None), \
                mock.patch.object(mod.subprocess, "run", side_effect=lambda cmd, **kw: calls.append(cmd)):
            self.assertTrue(mod.kill_process_tree(4242))
        self.assertEqual(calls, [["taskkill", "/F", "/T", "/PID", "4242"]])


# ---- 6: resume failure carries the reason ------------------------------------
class _ResumeLostBackend(_AsyncBackend):
    backend_id = "fake.resume_lost_v1"

    def observe_run(self, run_id, **kwargs):
        raise BackendError(BackendStatus.FAILED, "unknown run_id=async-run-1: run handles do not survive a restart")


class ResumeFailureReasonTests(unittest.TestCase):
    def test_failure_reason_names_the_cause(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, backend=_ResumeLostBackend())
            goal = app.submit(_request())["goal_id"]
            for _ in range(3):
                app.coordinator.process_goal(goal)
            view = app.status(goal)
            self.assertEqual(view["state"], "failed")
            self.assertIn("do not survive a restart", view["failure_reason"])
            self.assertIn("BackendError", view["failure_reason"])

    def test_reason_is_redacted(self):
        from framework.app_service import backend_error_text

        text = backend_error_text("backend resume failed", BackendError(BackendStatus.FAILED, "token=sk-abcdef1234567890abcdef"))
        self.assertNotIn("sk-abcdef1234567890abcdef", text)


# ---- 2: a slow lead must not hold the coordinator lock -----------------------
class _GatedPlanner:
    """Deterministic plan, but goals whose text says SLOW block until released."""

    def __init__(self) -> None:
        from framework.app_service import DeterministicPlanner

        self.inner = DeterministicPlanner()
        self.name = "gated.test"
        self.release = threading.Event()

    def plan(self, snap):
        goal = snap.get("goal") or {}
        if "SLOW" in str(goal.get("desired_outcome") or ""):
            self.release.wait(10)
        return self.inner.plan(snap)


class PlanningOffLockTests(unittest.TestCase):
    def test_slow_plan_does_not_block_other_goals(self):
        with tempfile.TemporaryDirectory() as td:
            planner = _GatedPlanner()
            app = CollabApplication(td, planner=planner, backend=_AsyncBackend())
            slow = dict(_request(), idempotency_key="slow", goal="SLOW planning goal")
            fast = dict(_request(), idempotency_key="fast")
            slow_id = app.submit(slow)["goal_id"]
            fast_id = app.submit(fast)["goal_id"]
            try:
                runner = threading.Thread(target=app.coordinator.process_all, daemon=True)
                runner.start()
                runner.join(4)
                self.assertFalse(runner.is_alive(), "process_all blocked on a slow planner")
                self.assertEqual(app.status(slow_id)["progress"]["phase"], "planning")
                for _ in range(4):
                    app.coordinator.process_goal(fast_id)
                self.assertEqual(app.status(fast_id)["state"], "completed")
            finally:
                planner.release.set()
            for _ in range(20):
                app.coordinator.process_all()
                if app.status(slow_id)["state"] == "completed":
                    break
                time.sleep(0.1)
            self.assertEqual(app.status(slow_id)["state"], "completed")
            self.assertEqual(len(app.status(slow_id)["tasks"]), 1, "plan applied once")

    def test_plan_finished_after_cancel_is_discarded(self):
        with tempfile.TemporaryDirectory() as td:
            planner = _GatedPlanner()
            app = CollabApplication(td, planner=planner, backend=_AsyncBackend())
            gid = app.submit(dict(_request(), goal="SLOW then cancelled"))["goal_id"]
            app.coordinator.plan_inline_wait_sec = 0.05
            app.coordinator.process_goal(gid)
            app.cancel(gid)
            app.coordinator.process_goal(gid)
            planner.release.set()
            time.sleep(0.2)
            app.coordinator.process_goal(gid)
            view = app.status(gid)
            self.assertEqual(view["state"], "cancelled")
            self.assertEqual(view["tasks"], [])


# ---- 3: agy stale must be reachable -------------------------------------------
class AgyStaleTests(unittest.TestCase):
    def _hb_age(self, be, run_id):
        from framework.progress_budget import parse_iso

        prog = be.observe_run(run_id)["progress"]
        return time.time() - parse_iso(prog["last_heartbeat_at"]), prog

    def test_live_but_silent_agy_heartbeat_ages(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            be = _backend(root)
            run = be.start_run(title="t", directory=str(root / "ws"), instruction="SLEEP[4]")
            time.sleep(2.0)
            age, prog = self._hb_age(be, run["run_id"])
            self.assertGreater(age, 1.5, f"process-alive poll must not refresh the heartbeat: {prog}")
            be.cancel(run["run_id"])

    def test_session_activity_keeps_heartbeat_fresh(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            be = _backend(root)
            run = be.start_run(title="t", directory=str(root / "ws"), instruction="SESSION[40,0.25]")
            try:
                # Silent run of the same length ages past 1.5s (test above); a
                # run touching its agy session files keeps a fresh heartbeat.
                time.sleep(2.0)
                deadline = time.time() + 5
                while True:
                    age, prog = self._hb_age(be, run["run_id"])
                    if "last_activity session" in prog["events"] and age < 1.5:
                        break
                    self.assertLess(time.time(), deadline, prog)
                    time.sleep(0.2)
            finally:
                be.cancel(run["run_id"])

    def test_goal_progress_goes_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = CollabApplication(root / "app", backend=_backend(root), stale_after_sec=1.0)
            req = dict(_request(), goal="SLEEP[5]")
            gid = app.submit(req)["goal_id"]
            app.coordinator.process_goal(gid)
            time.sleep(2.0)
            app.coordinator.process_goal(gid)
            self.assertEqual(app.status(gid)["progress"]["state"], "stale")
            app.cancel(gid)
            app.coordinator.process_goal(gid)

    def test_capability_names_activity_heartbeat(self):
        with tempfile.TemporaryDirectory() as td:
            caps = _backend(Path(td)).capabilities()
            self.assertEqual(caps["progress"]["heartbeat"], "runner_activity")


# ---- 4: prompt-only external inputs warn even without skip-permissions -------
class PromptOnlyWarningTests(unittest.TestCase):
    def _app(self, root: Path, *, skip: bool):
        from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend

        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(root)}
        if skip:
            env["AGY_AUTO_APPROVE"] = "1"
        be = AntigravityCliExecutionBackend(bin_path=_fake_agy(root), environ=env)
        return CollabApplication(root / "app", backend=be)

    def _pinned(self, root: Path) -> dict:
        import hashlib

        ext = root / "ext.txt"
        ext.write_text("pinned\n", encoding="utf-8")
        return {"path": str(ext), "sha256": hashlib.sha256(ext.read_bytes()).hexdigest()}

    def test_skip_off_still_warns_and_matches_capabilities(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = self._app(root, skip=False)
            caps = app.capabilities()
            self.assertFalse(caps["skip_permissions"])
            prompt_only = [w for w in caps["warnings"] if "prompt-only" in w]
            self.assertEqual(len(prompt_only), 1, caps["warnings"])
            sub = app.submit(dict(_request(), external_inputs=[self._pinned(root)]))
            self.assertIn(prompt_only[0], sub.get("warnings") or [])
            self.assertIn(prompt_only[0], app.status(sub["goal_id"])["warnings"])
            plain = app.submit(dict(_request(), idempotency_key="plain"))
            self.assertNotIn(prompt_only[0], app.status(plain["goal_id"])["warnings"])

    def test_skip_on_keeps_both_warnings(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = self._app(root, skip=True)
            sub = app.submit(dict(_request(), external_inputs=[self._pinned(root)], acknowledge_prompt_only_inputs=True))
            warnings = app.status(sub["goal_id"])["warnings"]
            self.assertTrue(any("skip-permissions" in w for w in warnings))
            self.assertTrue(any("nothing enforces it" in w for w in warnings))


# ---- 5: failed goals report delivered / candidate / missing artifacts -------
class FailedGoalArtifactsTests(unittest.TestCase):
    def _task(self, ws: Path, tid: str, status: str, expected, files=(), result=True):
        ws.mkdir(parents=True, exist_ok=True)
        for name in files:
            (ws / name).write_text("x\n", encoding="utf-8")
        row = {"task_id": tid, "status": status, "workspace": str(ws), "expected_artifacts": list(expected)}
        if result:
            row["result"] = {"ok": status == "succeeded", "error": "" if status == "succeeded" else "timeout"}
        return row

    def test_timeout_with_file_is_candidates_only_not_unknown(self):
        from framework.app_service import acceptance_status_view

        with tempfile.TemporaryDirectory() as td:
            rows = [self._task(Path(td) / "a", "A", "failed", ["out.txt"], ["out.txt"])]
            view = acceptance_status_view("failed", rows, {})
            self.assertEqual(view["artifacts"], "candidates_only")
            self.assertEqual(view["candidate_artifacts"], ["out.txt"])
            self.assertEqual(view["missing_artifacts"], [])

    def test_partial_chain_lists_candidate_and_missing(self):
        from framework.app_service import acceptance_status_view

        with tempfile.TemporaryDirectory() as td:
            rows = [
                self._task(Path(td) / "a", "A", "failed", ["a.txt"], ["a.txt"]),
                self._task(Path(td) / "b", "B", "queued", ["b.txt"], result=False),
            ]
            view = acceptance_status_view("failed", rows, {})
            self.assertEqual(view["artifacts"], "incomplete")
            self.assertEqual(view["candidate_artifacts"], ["a.txt"])
            self.assertEqual(view["missing_artifacts"], ["b.txt"])
            self.assertTrue(view["artifacts_by_task"][1]["not_run"])

    def test_nothing_produced_and_not_started(self):
        from framework.app_service import acceptance_status_view

        with tempfile.TemporaryDirectory() as td:
            rows = [self._task(Path(td) / "a", "A", "failed", ["out.txt"])]
            self.assertEqual(acceptance_status_view("failed", rows, {})["artifacts"], "none")
        self.assertEqual(acceptance_status_view("failed", [], {})["artifacts"], "not_started")

    def test_delivered_listed_on_success(self):
        from framework.app_service import acceptance_status_view

        with tempfile.TemporaryDirectory() as td:
            rows = [self._task(Path(td) / "a", "A", "succeeded", ["out.txt"], ["out.txt"])]
            view = acceptance_status_view("completed", rows, {})
            self.assertEqual((view["artifacts"], view["delivered_artifacts"]), ("complete", ["out.txt"]))


# ---- 7: planning/preparing/executing/finalizing/testing/reviewing (#5) -------
class _FinalizeFailBackend(_AsyncBackend):
    """Worker exits, CLI finalization fails; optionally a file was written first."""

    backend_id = "fake.finalize_fail_v1"
    write_candidate = True

    def observe_run(self, run_id, **kwargs):
        return {"busy": False, "finish_successful": False}

    def collect_result(self, run_id):
        if self.write_candidate:
            for rel in self.artifacts:
                (self.directory / rel).write_text("candidate\n", encoding="utf-8")
        return {"ok": False, "run_id": run_id, "error": "agy exit=1: finalize failed: connection reset"}


class PhaseVisibilityTests(unittest.TestCase):
    def _run(self, backend):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        app = CollabApplication(td.name, backend=backend)
        gid = app.submit(_request())["goal_id"]
        for _ in range(4):
            app.coordinator.process_goal(gid)
        return app.status(gid)

    def test_timeline_has_every_phase_window(self):
        view = self._run(_AsyncBackend())
        self.assertEqual(view["state"], "completed")
        phases = [(r["phase"], r["outcome"]) for r in view["phase_timeline"]]
        self.assertEqual(
            phases,
            [("planning", "ok"), ("preparing", "ok"), ("executing", "ok"), ("finalizing", "ok"), ("testing", "ok")],
        )
        self.assertTrue(all(r["ended_at"] for r in view["phase_timeline"]))

    def test_candidate_but_finalization_failed_vs_no_output(self):
        with_file = self._run(_FinalizeFailBackend())
        p = with_file["primary_failure"]
        self.assertEqual((p["failed_phase"], p["outcome"]), ("finalizing", "candidate_produced"))
        empty_backend = _FinalizeFailBackend()
        empty_backend.write_candidate = False
        p2 = self._run(empty_backend)["primary_failure"]
        self.assertEqual((p2["failed_phase"], p2["outcome"]), ("finalizing", "no_output"))
        self.assertNotEqual(p["outcome_summary"], p2["outcome_summary"])

    def test_acceptance_failure_is_testing_phase(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = CollabApplication(root / "app", backend=_backend(root))
            req = dict(_request(), goal="WRITE[delivery.txt]",
                       acceptance={"artifacts": ["delivery.txt"], "text": "delivery.txt must contain exactly NOPE"})
            gid = app.submit(req)["goal_id"]
            for _ in range(30):
                app.coordinator.process_goal(gid)
                if app.status(gid)["state"] in {"failed", "completed"}:
                    break
                time.sleep(0.1)
            p = app.status(gid)["primary_failure"]
            self.assertEqual((p["failed_phase"], p["outcome"]), ("testing", "candidate_produced"))

    def test_progress_shows_coordinator_phase(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, backend=_AsyncBackend())
            gid = app.submit(_request())["goal_id"]
            snap = app.layer.get_goal(gid)["goal"]
            fake = dict(snap)
            fake["tasks"] = [{"task_id": "t1", "status": "running",
                              "phases": [{"phase": "preparing", "started_at": "2026-10-02T00:00:00Z", "ended_at": None}]}]
            caps = app.capabilities()
            view = app.coordinator.goal_progress_view(fake, {}, {**caps, "progress": {"available": True}})
            self.assertEqual((view["state"], view["phase"]), ("preparing", "preparing"))
            fake["tasks"][0]["phases"][0]["phase"] = "reviewing"
            view = app.coordinator.goal_progress_view(fake, {}, {**caps, "progress": {"available": True}})
            self.assertEqual(view["phase"], "reviewing")

    def test_planning_failure_names_phase(self):
        from framework.failure_projection import goal_level_failure_brief

        brief = goal_level_failure_brief({"phase": "planning", "error": "lead timeout"})
        self.assertEqual((brief["failed_phase"], brief["outcome"]), ("planning", "no_output"))


# ---- found while re-testing: a plan must deliver every goal artifact --------
class PlanCoversGoalArtifactsTests(unittest.TestCase):
    def test_plan_missing_goal_artifact_is_invalid(self):
        from framework.app_service import AppError, validate_plan

        request = {"application_id": "a", "context_summary": "c",
                   "acceptance_criteria": {"artifacts": ["out.txt", "b.txt"]}}
        plan = {"application_id": "a", "context_summary": "c", "summary": "",
                "tasks": [{"task_key": "T", "title": "T", "instruction": "x", "depends_on": [], "artifacts": ["out.txt"]}]}
        with self.assertRaises(AppError) as ctx:
            validate_plan(plan, request)
        self.assertEqual(ctx.exception.code, "invalid_plan")
        self.assertIn("b.txt", str(ctx.exception))
        plan["tasks"][0]["artifacts"] = ["out.txt", "b.txt"]
        self.assertEqual(len(validate_plan(plan, request)["tasks"]), 1)


if __name__ == "__main__":
    unittest.main()
