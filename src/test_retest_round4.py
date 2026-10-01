"""Round-4 box re-test fixes. Offline fakes only (fake lead / fake agy)."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from framework.app_service import CollabApplication, LeadAdapterPlanner
from test_app_service import _AsyncBackend, _request

_POSIX = os.name != "nt"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # zombie counts as gone
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0] != "Z"
    except OSError:
        return True


def _script(root: Path, name: str, body: str) -> str:
    path = root / name
    path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


# ---- 1: cancelling a Goal stops the planner's lead process tree ----------------
_SLOW_LEAD = r'''
import os, subprocess, sys, time
root = os.path.dirname(os.path.abspath(__file__))
open(os.path.join(root, "lead.pid"), "w").write(str(os.getpid()))
subprocess.Popen([sys.executable, "-c",
    "import time,sys;time.sleep(2.5);open(sys.argv[1],'w').write('late')", os.path.join(root, "child.txt")])
time.sleep(30)
'''


@unittest.skipUnless(_POSIX, "POSIX process groups")
class CancelStopsLeadTests(unittest.TestCase):
    def _app(self, root: Path):
        from lead_adapter.grok_cli import GrokCliLeadAdapter

        lead = _script(root, "slow_lead", _SLOW_LEAD)
        planner = LeadAdapterPlanner(GrokCliLeadAdapter(bin_path=lead), cwd=root, timeout_sec=60)
        app = CollabApplication(root / "persist", planner=planner, backend=_AsyncBackend())
        app.coordinator.plan_inline_wait_sec = 0.05
        return app

    def _wait_pid(self, root: Path) -> int:
        for _ in range(50):
            if (root / "lead.pid").exists() and (root / "lead.pid").read_text():
                return int((root / "lead.pid").read_text())
            time.sleep(0.1)
        self.fail("lead never started")

    def test_cancel_stops_lead_and_its_children_and_records_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = self._app(root)
            gid = app.submit(_request())["goal_id"]
            app.coordinator.process_goal(gid)
            pid = self._wait_pid(root)
            started = time.monotonic()
            out = app.cancel(gid, "user cancel")
            self.assertLess(time.monotonic() - started, 5.0)
            self.assertFalse(_alive(pid), "lead still running after cancel")
            time.sleep(3.0)
            self.assertFalse((root / "child.txt").exists(), "lead child kept running after cancel")
            self.assertEqual(out["state"], "cancelled")
            events = app.events(gid)["events"]
            stops = [e for e in events if e.get("op") == "planner_stopped"]
            self.assertEqual(len(stops), 1, [e.get("op") for e in events])
            self.assertEqual(stops[0]["lead_processes"][0]["pid"], pid)
            self.assertEqual(stops[0]["lead_processes"][0]["method"], "killpg")
            self.assertEqual(stops[0]["planner_thread"], "stopped")
            self.assertEqual(app.status(gid)["planner_stopped"]["reason"], "goal cancelled")

    def test_service_shutdown_stops_planning_lead(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = self._app(root)
            gid = app.submit(_request())["goal_id"]
            app.coordinator.process_goal(gid)
            pid = self._wait_pid(root)
            rows = app.coordinator.stop_all_planning("service shutdown")
            self.assertEqual([r["goal_id"] for r in rows], [gid])
            self.assertFalse(_alive(pid))


class StopTreeWindowsTests(unittest.TestCase):
    def test_windows_uses_taskkill_tree(self):
        from lead_adapter import cancel

        proc = mock.Mock(pid=4321)
        proc.poll.return_value = None
        with mock.patch.object(cancel.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            row = cancel.stop_process_tree(proc, "win32")
        self.assertEqual(run.call_args[0][0], ["taskkill", "/F", "/T", "/PID", "4321"])
        self.assertEqual(row["method"], "taskkill /F /T")
        self.assertEqual(row["result"], "stopped")

    def test_windows_lead_starts_in_new_process_group(self):
        from lead_adapter.cancel import CREATE_NEW_PROCESS_GROUP, popen_group_kwargs

        self.assertEqual(popen_group_kwargs("win32"), {"creationflags": CREATE_NEW_PROCESS_GROUP})
        self.assertEqual(popen_group_kwargs("linux"), {"start_new_session": True})



# ---- 2: shared-HOME session files must not heartbeat someone else's run ------
_FAKE_AGY = r"""
import json, os, re, sys, time, uuid
prompt = next((a[len("--print="):] for a in sys.argv[1:] if a.startswith("--print=")), "")
def m(name): return re.findall(name + r"\[([^\]]*)\]", prompt)
conv = os.path.join(os.environ["HOME"], ".gemini", "antigravity-cli", "conversations")
for spec in m("HOLD"):          # keep own conversation file open, append every 0.2s
    os.makedirs(conv, exist_ok=True)
    fh = open(os.path.join(conv, f"{uuid.uuid4()}.db"), "a")
    for _ in range(int(spec)):
        fh.write("x"); fh.flush(); time.sleep(0.2)
for spec in m("SLEEP"):
    time.sleep(float(spec))
print(json.dumps({"conversation_id": "c", "status": "success", "response": "ok"}))
"""

_OTHER_WRITER = r"""
import os, sys, time, uuid
conv = os.path.join(sys.argv[1], ".gemini", "antigravity-cli", "conversations")
os.makedirs(conv, exist_ok=True)
hold = sys.argv[2] == "hold"
path = os.path.join(conv, f"{uuid.uuid4()}.db")
fh = open(path, "a") if hold else None
for _ in range(int(float(sys.argv[3]) / 0.2)):
    if hold:
        fh.write("x"); fh.flush()
    else:
        with open(path, "a") as f: f.write("x")
    time.sleep(0.2)
"""


def _agy_backend(root: Path):
    from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend

    home = root / "home"
    home.mkdir(exist_ok=True)
    script = root / "fake_agy.py"
    script.write_text(_FAKE_AGY, encoding="utf-8")
    wrapper = root / "fake_agy"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n", encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "USERPROFILE": str(home)}
    return AntigravityCliExecutionBackend(bin_path=str(wrapper), environ=env, timeout_sec=60), home


def _other_writer(root: Path, home: Path, mode: str, secs: float) -> subprocess.Popen:
    script = root / "other_writer.py"
    script.write_text(_OTHER_WRITER, encoding="utf-8")
    return subprocess.Popen([sys.executable, str(script), str(home), mode, str(secs)])


def _age(prog) -> float:
    from framework.progress_budget import parse_iso

    return time.time() - parse_iso(prog["last_heartbeat_at"])


@unittest.skipUnless(_POSIX, "fake agy wrapper is a POSIX shell script")
class SessionAttributionTests(unittest.TestCase):
    def test_other_process_session_file_does_not_refresh_silent_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            be, home = _agy_backend(root)
            run = be.start_run(title="t", directory=str(root / "ws"), instruction="SLEEP[5]")
            time.sleep(0.3)
            other = _other_writer(root, home, "hold", 4)
            try:
                time.sleep(2.5)
                prog = be.observe_run(run["run_id"])["progress"]
            finally:
                other.kill()
                other.wait()
            self.assertGreater(_age(prog), 1.5, f"foreign session file refreshed the heartbeat: {prog}")
            note = prog["heartbeat_signals"]["session"]
            self.assertFalse(note["used"])
            self.assertIn("held open by another process", note["reason"])
            be.cancel(run["run_id"])

    def test_without_fd_scan_two_runs_sharing_home_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            be, home = _agy_backend(root)
            be._session.fd_scan = False  # Windows / no /proc
            a = be.start_run(title="a", directory=str(root / "wa"), instruction="SLEEP[5]")
            b = be.start_run(title="b", directory=str(root / "wb"), instruction="SLEEP[5]")
            time.sleep(0.3)
            other = _other_writer(root, home, "nohold", 4)
            try:
                time.sleep(2.5)
                prog = be.observe_run(a["run_id"])["progress"]
            finally:
                other.kill()
                other.wait()
            self.assertGreater(_age(prog), 1.5, prog)
            note = prog["heartbeat_signals"]["session"]
            self.assertFalse(note["used"])
            self.assertIn("ambiguous: 2 runs", note["reason"])
            be.cancel(a["run_id"])
            be.cancel(b["run_id"])

    def test_run_holding_its_own_session_file_keeps_heartbeat(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            be, _home = _agy_backend(root)
            run = be.start_run(title="t", directory=str(root / "ws"), instruction="HOLD[20]")
            time.sleep(2.5)
            prog = be.observe_run(run["run_id"])["progress"]
            self.assertLess(_age(prog), 1.0, prog)
            self.assertEqual(prog["heartbeat_signals"]["session"], {"used": True, "attribution": "open_handle"})
            be.cancel(run["run_id"])

    def test_unused_reason_reaches_goal_progress(self):
        from framework.progress_budget import bound_task_progress

        note = {"session": {"used": False, "attribution": "none", "reason": "ambiguous: 2 runs of this service share this HOME"}}
        out = bound_task_progress({"phase": "executing", "source": "runner", "heartbeat_signals": note})
        self.assertEqual(out["heartbeat_signals"], note)


# ---- 3: artifacts once each, relative to the task workspace -------------------
def _client():
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "bin" / "hermes-collab-request.py"
    spec = importlib.util.spec_from_file_location("hcr_round4", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ArtifactDisplayTests(unittest.TestCase):
    def test_absolute_and_relative_spellings_collapse(self):
        hcr = _client()
        ws = "/srv/state/workspaces/g/task_1"
        payload = {
            "ok": True,
            "request_id": "g",
            "state": "completed",
            "tasks": [
                {
                    "task_id": "task_1",
                    "title": "T",
                    "status": "succeeded",
                    "workspace": ws,
                    "expected_artifacts": ["out.txt", "./sub/b.txt"],
                    "result": {"artifacts": [ws + "/out.txt", ws + "/sub/b.txt", "sub\\b.txt"]},
                }
            ],
        }
        view = hcr.summarize_status(payload)
        self.assertEqual(view["tasks"][0]["artifacts"], ["out.txt", "sub/b.txt"])
        self.assertEqual([a["path"] for a in view["artifacts"]], ["out.txt", "sub/b.txt"])

    def test_windows_workspace_case_and_separators(self):
        hcr = _client()
        self.assertEqual(hcr._workspace_relative("C:\\Work\\T1\\out.txt", "c:/work/t1"), "out.txt")
        self.assertEqual(hcr._workspace_relative("D:\\elsewhere\\x.txt", "c:/work/t1"), "D:\\elsewhere\\x.txt")


# ---- 4: plan coverage compares normalized names ---------------------------------
class PlanCoverageNormalizationTests(unittest.TestCase):
    def _plan(self, *artifacts):
        return {"application_id": "a", "context_summary": "c", "summary": "",
                "tasks": [{"task_key": "T", "title": "T", "instruction": "x", "depends_on": [], "artifacts": list(artifacts)}]}

    def _req(self, *artifacts):
        return {"application_id": "a", "context_summary": "c", "acceptance_criteria": {"artifacts": list(artifacts)}}

    def test_matching_names_are_not_rejected(self):
        from framework.app_service import validate_plan

        self.assertEqual(len(validate_plan(self._plan("a.txt", "b.txt"), self._req("a.txt", "b.txt"))["tasks"]), 1)

    def test_path_spellings_compare_normalized(self):
        from framework.app_service import validate_plan

        out = validate_plan(self._plan("a.txt", "sub/b.txt", "sub\\c.txt"), self._req("./a.txt", "sub\\b.txt", "sub/./c.txt"))
        self.assertEqual(out["tasks"][0]["artifacts"], ["a.txt", "sub/b.txt", "sub/c.txt"])

    def test_rejection_names_missing_and_delivered(self):
        from framework.app_service import AppError, validate_plan

        with self.assertRaises(AppError) as ctx:
            validate_plan(self._plan("./a.txt"), self._req("a.txt", "b.txt"))
        self.assertEqual(ctx.exception.code, "invalid_plan")
        self.assertIn("missing b.txt", str(ctx.exception))
        self.assertIn("plan delivers a.txt", str(ctx.exception))
        self.assertEqual(ctx.exception.extra["missing_artifacts"], ["b.txt"])


if __name__ == "__main__":
    unittest.main()
