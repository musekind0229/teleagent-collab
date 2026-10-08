#!/usr/bin/env python3
"""End-to-end agy lead-review loop. Red until the app wires the review runner.

Drives CollabApplication through AppCoordinator.process_all (CollabApplication
has no process_all). Fake binaries only: src/testdata/fake_agy_review.py and
fake_lead_review.py. No review module is imported.
"""
from __future__ import annotations

import json
import os
import re
import signal
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_TESTS = str(SRC.parent / "tests")
if _TESTS not in sys.path:
    sys.path.append(_TESTS)

from desktop_lock_isolation import install_desktop_lock_isolation  # noqa: E402
from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend  # noqa: E402
from framework.app_service import CollabApplication, LeadAdapterPlanner  # noqa: E402

FAKE_AGY = SRC / "testdata" / "fake_agy_review.py"
FAKE_LEAD = SRC / "testdata" / "fake_lead_review.py"
PROSE = "Judge whether delivery.md explains the change in plain language."
_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_DROP = (
    "AGY_BIN",
    "COLLAB_AGY_ACCOUNT_POOL",
    "COLLAB_AGY_REVIEW_STORE",
    "COLLAB_AGY_RUN_REGISTRY",
    "COLLAB_AGY_POOL_PRECHECK",
    "AGY_AUTO_APPROVE",
    "COLLAB_AGY_AUTO_APPROVE",
    "COLLAB_LEAD_BIN",
    "COLLAB_CODEX_LEAD_BIN",
)
_FORBIDDEN = (
    "timed_out",
    "wall clock",
    "wall_sec",
    "budget_exhausted",
    "deadline exhausted",
)
_RUN_ID = re.compile(r"^agy_[0-9a-f]{12}$")
_ROUND = re.compile(r":r([1-9][0-9]*)$")


def _write_shim(directory: Path, script: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / name
    shim.write_text(
        "#!/bin/sh\n" + f'exec "{sys.executable}" "{script}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return shim


def _process_all(app):
    """One scheduler tick. Prefer CollabApplication.process_all when it exists."""
    tick = getattr(app, "process_all", None)
    if callable(tick):
        return tick()
    return app.coordinator.process_all()


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        text = stat_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    end = text.rfind(")")
    if end < 0:
        return True
    rest = text[end + 2 :].split()
    if rest and rest[0] in {"Z", "X"}:
        return False
    return True


def _jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


@unittest.skipIf(os.name == "nt", "POSIX shell shim; fake agy and lead are spawned through /bin/sh")
class AgyLeadReviewLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.maxDiff = None
        # Full env restore is registered first so it runs after the desktop-lock
        # cleanup, which still needs TELEAGENT_DESKTOP_LOCK_DIR.
        self._saved = dict(os.environ)
        self.addCleanup(self._restore_env)
        install_desktop_lock_isolation(self)
        self._tmp = tempfile.TemporaryDirectory(prefix="agy-lr-loop-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.persist = self.root / "persist"
        self.persist.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.registry = self.persist / "agy-runs.json"
        self.agy_script = self.root / "agy-steps.json"
        self.lead_script = self.root / "lead-steps.json"
        self.agy_log = self.root / "agy.jsonl"
        self.lead_log = self.root / "lead.jsonl"
        self.assertTrue(FAKE_AGY.is_file(), FAKE_AGY)
        self.assertTrue(FAKE_LEAD.is_file(), FAKE_LEAD)
        self.agy_shim = _write_shim(self.root / "bin", FAKE_AGY, "agy")
        self.lead_shim = _write_shim(self.root / "bin", FAKE_LEAD, "lead")
        self.lead_timeout = 2.0
        self._codex = False
        self._apps: list[CollabApplication] = []
        self._backends: list[AntigravityCliExecutionBackend] = []
        self._gates: list[Path] = []
        for key in _DROP + ("FAKE_LEAD_SHAPE",):
            os.environ.pop(key, None)
        os.environ["FAKE_AGY_SCRIPT"] = str(self.agy_script)
        os.environ["FAKE_AGY_LOG"] = str(self.agy_log)
        os.environ["FAKE_LEAD_SCRIPT"] = str(self.lead_script)
        os.environ["FAKE_LEAD_LOG"] = str(self.lead_log)
        self.app: CollabApplication | None = None
        self.backend: AntigravityCliExecutionBackend | None = None

    def tearDown(self) -> None:
        for gate in self._gates:
            try:
                gate.parent.mkdir(parents=True, exist_ok=True)
                gate.write_text("open", encoding="utf-8")
            except OSError:
                pass
        for app in self._apps:
            try:
                app.coordinator.stop_all_planning("test cleanup")
            except Exception:
                pass
        for backend in self._backends:
            try:
                backend.close()
            except Exception:
                pass
        self._kill_logged()

    def _restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved)

    def _kill_logged(self) -> None:
        me = os.getpid()
        for path in (self.agy_log, self.lead_log):
            for row in _jsonl(path):
                pid = row.get("pid")
                if isinstance(pid, int) and pid > 1 and pid != me:
                    self._kill_pid(pid)

    def _kill_pid(self, pid: int) -> None:
        if not _pid_alive(pid):
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def _wait_dead(self, pid: int, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return
            time.sleep(0.05)
        self._kill_pid(pid)
        self.assertFalse(_pid_alive(pid), f"pid {pid} still alive")

    def _gate(self, name: str) -> Path:
        path = self.root / name
        self._gates.append(path)
        return path

    def _agy_steps(self, steps: list) -> None:
        self.agy_script.write_text(json.dumps(steps), encoding="utf-8")

    def _lead_steps(self, steps: list) -> None:
        self.lead_script.write_text(json.dumps(steps), encoding="utf-8")

    def _ok(self, rel: str = "delivery.md", text: str = "delivered\n") -> dict:
        return {"do": "ok", "write": {rel: text}}

    def _backend(self) -> AntigravityCliExecutionBackend:
        env = os.environ.copy()
        for key in _DROP:
            env.pop(key, None)
        return AntigravityCliExecutionBackend(
            bin_path=str(self.agy_shim),
            run_registry_path=str(self.registry),
            poll_sec=0.05,
            environ=env,
        )

    def _planner(self):
        if self._codex:
            os.environ["FAKE_LEAD_SHAPE"] = "codex"
            from lead_adapter.codex_cli import CodexCliLeadAdapter

            adapter = CodexCliLeadAdapter(bin_path=str(self.lead_shim))
        else:
            os.environ.pop("FAKE_LEAD_SHAPE", None)
            from lead_adapter.grok_cli import GrokCliLeadAdapter

            adapter = GrokCliLeadAdapter(bin_path=str(self.lead_shim))
        return LeadAdapterPlanner(adapter, cwd=self.work, timeout_sec=self.lead_timeout)

    def _build(self) -> CollabApplication:
        backend = self._backend()
        self._backends.append(backend)
        app = CollabApplication(self.persist, planner=self._planner(), backend=backend)
        self._apps.append(app)
        self.app = app
        self.backend = backend
        return app

    def _restart(self) -> tuple[CollabApplication, AntigravityCliExecutionBackend]:
        """stop_all_planning, close the backend, then a new stack on the same persist dir."""
        assert self.app is not None and self.backend is not None
        self.app.coordinator.stop_all_planning("test restart")
        self.backend.close()
        return self._build(), self.backend

    def _request(
        self,
        key: str,
        *,
        prose: bool = True,
        artifacts: list[str] | None = None,
        max_reworks: int = 1,
        wall: int = 60,
        required: list[str] | None = None,
    ) -> dict:
        arts = ["delivery.md"] if artifacts is None else list(artifacts)
        acceptance: dict = {"artifacts": arts}
        if prose:
            acceptance["text"] = PROSE
        body = {
            "idempotency_key": key,
            "client_id": "test-suite",
            "title": key,
            "goal": "Produce the requested delivery artifact in the assigned workspace.",
            "boundaries": {
                "must": ["Stay inside the assigned workspace"],
                "must_not": ["Do not use network or system tools"],
            },
            "acceptance": acceptance,
            "budget": {"wall_sec": wall, "max_reworks": max_reworks},
        }
        if required is not None:
            body["required_capabilities"] = list(required)
        return body

    def _submit(self, key: str, **kwargs) -> str:
        assert self.app is not None
        opened = self.app.submit(self._request(key, **kwargs))
        self.assertTrue(opened.get("ok"), opened)
        self.assertTrue(opened.get("goal_id"), opened)
        return str(opened["goal_id"])

    def _drive(self, app, goal_id: str, pred=None, timeout: float = 8.0, times: list | None = None) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            started = time.monotonic()
            _process_all(app)
            elapsed = time.monotonic() - started
            if times is not None:
                times.append(elapsed)
            last = app.status(goal_id)
            if pred is not None and pred(last):
                return last
            if str(last.get("state") or "") in _TERMINAL:
                return last
            time.sleep(0.05)
        if last is None:
            last = app.status(goal_id)
        return last

    def _agy(self) -> list[dict]:
        return _jsonl(self.agy_log)

    def _reviews(self) -> list[dict]:
        return [row for row in _jsonl(self.lead_log) if row.get("kind") == "review"]

    def _wait_agy(self, count: int, timeout: float = 2.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        rows = self._agy()
        while len(rows) < count and time.monotonic() < deadline:
            time.sleep(0.05)
            rows = self._agy()
        return rows

    def _task(self, status: dict) -> dict:
        tasks = [row for row in (status.get("tasks") or []) if isinstance(row, dict)]
        self.assertEqual(len(tasks), 1, status)
        return tasks[0]

    def _result(self, status: dict) -> dict:
        result = self._task(status).get("result")
        self.assertIsInstance(result, dict, status)
        return result

    def _run_id(self, status: dict) -> str:
        task = self._task(status)
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        run_id = str(task.get("run_id") or result.get("run_id") or "")
        self.assertRegex(run_id, _RUN_ID)
        return run_id

    def _snap(self, app, goal_id: str) -> dict:
        got = app.layer.get_goal(goal_id)
        self.assertTrue(got.get("ok"), got)
        goal = got.get("goal")
        self.assertIsInstance(goal, dict, got)
        return goal

    def _all_decisions(self, app, goal_id: str) -> list[dict]:
        snap = self._snap(app, goal_id)
        rows: list[dict] = []
        for key in ("pending_decisions", "resolved_decisions"):
            for row in snap.get(key) or []:
                if isinstance(row, dict):
                    rows.append(row)
        return rows

    def _artifact_reviews(self, app, goal_id: str) -> list[dict]:
        rows = [row for row in self._all_decisions(app, goal_id) if row.get("kind") == "artifact_review"]
        rows.sort(key=self._round_of)
        return rows

    def _round_of(self, decision: dict) -> int:
        details = decision.get("details") if isinstance(decision.get("details"), dict) else {}
        lead = details.get("lead_review") if isinstance(details.get("lead_review"), dict) else {}
        if isinstance(lead.get("round"), int):
            return int(lead["round"])
        req = str(decision.get("request_id") or details.get("backend_request_id") or "")
        match = _ROUND.search(req)
        return int(match.group(1)) if match else 0

    def _request_id_of(self, status: dict) -> str:
        pending = status.get("pending_decisions") or []
        self.assertTrue(pending, status)
        row = pending[0]
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        return str(details.get("backend_request_id") or row.get("request_id") or "")

    def _lead_review_of(self, status: dict) -> dict | None:
        pending = status.get("pending_decisions") or []
        if not pending or not isinstance(pending[0], dict):
            return None
        details = pending[0].get("details") if isinstance(pending[0].get("details"), dict) else {}
        lead = details.get("lead_review")
        return lead if isinstance(lead, dict) else None

    def _review_running(self, status: dict) -> bool:
        if str(status.get("state") or "") in _TERMINAL:
            return False
        lead = self._lead_review_of(status)
        return bool(lead) and lead.get("status") == "running"

    def _attempt_running(self, attempt: int):
        def pred(status: dict) -> bool:
            lead = self._lead_review_of(status)
            return bool(
                lead
                and str(status.get("state") or "") not in _TERMINAL
                and lead.get("status") == "running"
                and lead.get("attempt") == attempt
            )

        return pred

    def _round_running(self, round_n: int, *, min_reviews: int):
        def pred(status: dict) -> bool:
            if not self._review_running(status):
                return False
            if len(self._reviews()) < min_reviews:
                return False
            req = self._request_id_of(status)
            lead = self._lead_review_of(status) or {}
            rnd = lead.get("round")
            if not isinstance(rnd, int):
                match = _ROUND.search(req)
                rnd = int(match.group(1)) if match else None
            return rnd == round_n and req.endswith(f":r{round_n}")

        return pred

    def _spy_annotates(self, app) -> list[str]:
        seen: list[str] = []
        orig = app.layer.annotate_decision

        def wrapped(goal_id, *args, **kwargs):
            details = kwargs.get("details")
            if isinstance(details, dict):
                lead = details.get("lead_review")
                if isinstance(lead, dict) and "status" in lead:
                    seen.append(str(lead.get("status")))
            return orig(goal_id, *args, **kwargs)

        app.layer.annotate_decision = wrapped
        return seen

    def _assert_interrupted(self, app, goal_id: str, seen: list[str]) -> None:
        if "interrupted" in seen:
            return
        for row in self._all_decisions(app, goal_id):
            details = row.get("details") if isinstance(row.get("details"), dict) else {}
            lead = details.get("lead_review") if isinstance(details.get("lead_review"), dict) else {}
            if lead.get("status") == "interrupted":
                return
            for item in lead.get("history") or []:
                if isinstance(item, dict) and item.get("status") == "interrupted":
                    return
        self.fail(f"lead review was not marked interrupted; annotate statuses={seen}")

    def _assert_event(self, app, goal_id: str, name: str) -> None:
        found: list[str] = []
        for row in self._all_decisions(app, goal_id):
            details = row.get("details") if isinstance(row.get("details"), dict) else {}
            lead = details.get("lead_review") if isinstance(details.get("lead_review"), dict) else {}
            for item in lead.get("events") or []:
                event = item.get("event") if isinstance(item, dict) else item
                if isinstance(event, str):
                    found.append(event)
                    if event == name:
                        return
        self.fail(f"{name} missing from decision lead_review.events; events={found}")

    def _forbid(self, text: str) -> None:
        for bit in _FORBIDDEN:
            self.assertNotIn(bit, text, text)

    def _resolve_direct(self, backend, request_id: str, verdict: str, reason: str):
        resolver = getattr(backend, "resolve_decision", None)
        self.assertTrue(
            callable(resolver),
            "AntigravityCliExecutionBackend.resolve_decision is missing",
        )
        out = resolver(request_id, verdict=verdict, reason=reason, answers=[{"by": "lead"}])
        if isinstance(out, dict):
            self.assertIsNot(out.get("ok"), False, out)
        return out

    def _assert_shared_run(self, status: dict, decisions: list[dict]) -> str:
        run_id = self._run_id(status)
        found = []
        for decision in decisions:
            req = str(decision.get("request_id") or "")
            match = re.search(r"agy_[0-9a-f]{12}", req)
            self.assertIsNotNone(match, decision)
            found.append(match.group(0))
        self.assertEqual(set(found), {run_id}, decisions)
        return run_id

    def test_e1_pass_completes(self) -> None:
        """E1: asserts a prose goal completes with review source lead_review, technical_review via_lead_gate, one artifact_review, one agy spawn, and one lead review.
        red on 49fa3a36: agy has no review channel, so the goal completes with review status unsupported and no artifact_review.
        """
        self._agy_steps([self._ok()])
        self._lead_steps(["pass:accepted"])
        app = self._build()
        goal_id = self._submit("e1")
        status = self._drive(app, goal_id)
        self.assertEqual(status["state"], "completed", status)
        review = self._result(status).get("review") or {}
        self.assertEqual(review.get("status"), "passed", review)
        self.assertEqual(review.get("source"), "lead_review", review)
        self.assertEqual(review.get("evidence"), "accepted", review)
        self.assertEqual(status["acceptance_status"]["technical_review"], "via_lead_gate", status["acceptance_status"])
        reviews = self._artifact_reviews(app, goal_id)
        self.assertEqual(len(reviews), 1, reviews)
        self.assertEqual(reviews[0].get("verdict"), "pass", reviews[0])
        rows = self._agy()
        self.assertEqual(len(rows), 1, rows)
        self.assertIs(rows[0].get("rework"), False, rows[0])
        self.assertEqual(len(self._reviews()), 1, _jsonl(self.lead_log))

    def test_e2_fail_then_pass(self) -> None:
        """E2: asserts fail then pass completes with rework.used 1, verdicts fail then pass, two agy spawns, and the same run_id.
        red on 49fa3a36: no rework loop; the goal completes after one agy spawn without a lead verdict.
        """
        self._agy_steps([self._ok(), self._ok(text="reworked\n")])
        self._lead_steps(["fail:needs-work", "pass:cleared"])
        app = self._build()
        goal_id = self._submit("e2")
        status = self._drive(app, goal_id)
        self.assertEqual(status["state"], "completed", status)
        rework = self._result(status).get("rework") or {}
        self.assertEqual(rework.get("used"), 1, rework)
        self.assertEqual(rework.get("max"), 1, rework)
        reviews = self._artifact_reviews(app, goal_id)
        self.assertEqual([row.get("verdict") for row in reviews], ["fail", "pass"], reviews)
        rows = self._agy()
        self.assertEqual(len(rows), 2, rows)
        self.assertIs(rows[0].get("rework"), False, rows[0])
        self.assertIs(rows[1].get("rework"), True, rows[1])
        self._assert_shared_run(status, reviews)

    def test_e3_rework_budget_exhausted(self) -> None:
        """E3: asserts a second fail with max_reworks 1 fails from acceptance with rework budget exhausted (1/1) and exactly two agy spawns.
        red on 49fa3a36: the prose goal completes instead of failing closed on the rework budget.
        """
        self._agy_steps([self._ok(), self._ok(text="still\n")])
        self._lead_steps(["fail:first", "fail:second"])
        app = self._build()
        goal_id = self._submit("e3")
        status = self._drive(app, goal_id)
        self.assertEqual(status["state"], "failed", status)
        self.assertEqual(status["primary_failure"]["source"], "acceptance", status.get("primary_failure"))
        error = str(self._result(status).get("error") or "")
        self.assertIn("rework budget exhausted (1/1)", error, error)
        self._forbid(error)
        self.assertEqual(len(self._agy()), 2, self._agy())

    def test_t4e_lead_timeout_twice(self) -> None:
        """T4e: asserts two lead timeouts fail with lead_review_unavailable: timeout after 2 attempt(s), no human decision, human_action_required, and the same error in the report.
        red on 49fa3a36: the review lead is never called, so the goal completes and the timeout text is absent.
        """
        self._agy_steps([self._ok()])
        self._lead_steps(["sleep:5", "sleep:5"])
        app = self._build()
        goal_id = self._submit("t4e")
        status = self._drive(app, goal_id, timeout=12.0)
        self.assertEqual(status["state"], "failed", status)
        error = str(self._result(status).get("error") or "")
        self.assertTrue(error.startswith("lead_review_unavailable: timeout after 2 attempt(s)"), error)
        self.assertIn("next step:", error, error)
        self._forbid(error)
        self.assertEqual(status.get("awaiting_human_count"), 0, status)
        self.assertEqual(status.get("pending_decisions"), [], status)
        reason = str(status.get("failure_reason") or "")
        self.assertTrue(reason.startswith("lead_review_unavailable: timeout after 2 attempt(s)"), reason)
        self.assertIn("next step:", reason, reason)
        lead = self._result(status).get("lead_review") or {}
        self.assertIs(lead.get("human_action_required"), True, lead)
        report = json.dumps(app.report(goal_id))
        self.assertIn(error, report)
        self.assertEqual(len(self._reviews()), 2, _jsonl(self.lead_log))

    def test_t4f_exit1_401_one_attempt(self) -> None:
        """T4f: asserts exit1_401 performs exactly one review call and fails lead_review_unavailable.
        red on 49fa3a36: the review lead is never called and the goal completes.
        """
        self._agy_steps([self._ok()])
        self._lead_steps(["exit1_401", "pass:should-not-run"])
        app = self._build()
        goal_id = self._submit("t4f")
        status = self._drive(app, goal_id)
        self.assertEqual(status["state"], "failed", status)
        error = str(self._result(status).get("error") or "")
        self.assertTrue(error.startswith("lead_review_unavailable:"), error)
        self.assertIn("after 1 attempt(s)", error, error)
        self._forbid(error)
        self.assertEqual(len(self._reviews()), 1, _jsonl(self.lead_log))

    def test_t6a_restart_during_review(self) -> None:
        """T6a: asserts a restart during a blocked review records interrupted, then attempt 2 passes, with two review calls and the first lead pid dead.
        red on 49fa3a36: there is no durable review job to interrupt; the goal completes without a lead review.
        """
        self.lead_timeout = 8.0
        gate = self._gate("t6a-gate")
        self._agy_steps([self._ok()])
        self._lead_steps([f"block_until:{gate}", "pass:recovered"])
        app = self._build()
        goal_id = self._submit("t6a")
        status = self._drive(app, goal_id, self._review_running, timeout=8.0)
        self.assertTrue(
            self._review_running(status),
            f"review did not stay running; state={status.get('state')} pending={status.get('pending_decisions')}",
        )
        self.assertGreaterEqual(len(self._reviews()), 1, _jsonl(self.lead_log))
        first_pid = int(self._reviews()[0]["pid"])
        app, _backend = self._restart()
        seen = self._spy_annotates(app)
        status = self._drive(app, goal_id, timeout=8.0)
        self._assert_interrupted(app, goal_id, seen)
        self.assertEqual(status["state"], "completed", status)
        review = self._result(status).get("review") or {}
        self.assertEqual(review.get("source"), "lead_review", review)
        self.assertEqual(len(self._reviews()), 2, _jsonl(self.lead_log))
        self._wait_dead(first_pid)

    def test_t6b_restart_round2_then_fail(self) -> None:
        """T6b: asserts a restart while round 2 is awaiting review keeps redos and a later fail exhausts (1/1) without a third agy spawn.
        red on 49fa3a36: round 2 review is never opened.
        """
        self.lead_timeout = 8.0
        gate = self._gate("t6b-gate")
        self._agy_steps([self._ok(), self._ok(text="round2\n")])
        self._lead_steps(["fail:round1", f"block_until:{gate}", "fail:round2"])
        app = self._build()
        goal_id = self._submit("t6b")
        status = self._drive(app, goal_id, self._round_running(2, min_reviews=2), timeout=8.0)
        self.assertTrue(
            self._round_running(2, min_reviews=2)(status),
            f"round 2 review never opened; state={status.get('state')} pending={status.get('pending_decisions')} reviews={self._reviews()}",
        )
        run_id = self._run_id(status)
        self.assertEqual(len(self._agy()), 2, self._agy())
        self.assertIs(self._agy()[1].get("rework"), True, self._agy()[1])
        app, _backend = self._restart()
        status = self._drive(app, goal_id, timeout=8.0)
        self.assertEqual(status["state"], "failed", status)
        error = str(self._result(status).get("error") or "")
        self.assertIn("rework budget exhausted (1/1)", error, error)
        self._forbid(error)
        self.assertEqual(self._run_id(status), run_id)
        self.assertEqual(len(self._agy()), 2, self._agy())

    def test_t6d_restart_at_attempt_2(self) -> None:
        """T6d: asserts a restart once attempt is already 2 does not call the lead again and fails lead_review_unavailable.
        red on 49fa3a36: attempts are not counted and the goal completes.
        """
        self.lead_timeout = 8.0
        gate1 = self._gate("t6d-gate-1")
        gate2 = self._gate("t6d-gate-2")
        self._agy_steps([self._ok()])
        self._lead_steps([f"block_until:{gate1}", f"block_until:{gate2}", "pass:should-not"])
        app = self._build()
        goal_id = self._submit("t6d")
        status = self._drive(app, goal_id, self._attempt_running(1), timeout=8.0)
        self.assertTrue(
            self._attempt_running(1)(status),
            f"attempt 1 never ran; state={status.get('state')} pending={status.get('pending_decisions')}",
        )
        app, _backend = self._restart()
        status = self._drive(app, goal_id, self._attempt_running(2), timeout=8.0)
        self.assertTrue(
            self._attempt_running(2)(status),
            f"attempt 2 never ran; state={status.get('state')} lead={self._lead_review_of(status)}",
        )
        self.assertEqual(len(self._reviews()), 2, _jsonl(self.lead_log))
        app, _backend = self._restart()
        status = self._drive(app, goal_id, timeout=8.0)
        self.assertEqual(status["state"], "failed", status)
        error = str(self._result(status).get("error") or "")
        self.assertTrue(error.startswith("lead_review_unavailable:"), error)
        self.assertIn("after 2 attempt(s)", error, error)
        self._forbid(error)
        self.assertEqual(len(self._reviews()), 2, _jsonl(self.lead_log))

    def test_t6e_backend_pass_durable_open(self) -> None:
        """T6e: asserts a backend pass with the durable decision still open is recovered after restart without another lead call.
        red on 49fa3a36: resolve_decision does not exist and the review never starts.
        """
        self.lead_timeout = 8.0
        gate = self._gate("t6e-gate")
        self._agy_steps([self._ok()])
        self._lead_steps([f"block_until:{gate}"])
        app = self._build()
        goal_id = self._submit("t6e")
        status = self._drive(app, goal_id, self._review_running, timeout=8.0)
        self.assertTrue(
            self._review_running(status),
            f"review did not stay running; state={status.get('state')} pending={status.get('pending_decisions')}",
        )
        request_id = self._request_id_of(status)
        self._resolve_direct(self.backend, request_id, "pass", "direct-pass")
        still = app.status(goal_id)
        self.assertTrue(still.get("pending_decisions"), still)
        before = len(self._reviews())
        app, _backend = self._restart()
        status = self._drive(app, goal_id, timeout=8.0)
        self.assertEqual(status["state"], "completed", status)
        review = self._result(status).get("review") or {}
        self.assertEqual(review.get("status"), "passed", review)
        self.assertEqual(review.get("source"), "lead_review", review)
        self.assertEqual(len(self._reviews()), before, _jsonl(self.lead_log))

    def test_c4_backend_fail_durable_open(self) -> None:
        """C4: asserts a backend fail that already spawned rework, with the durable decision still open, restarts into lead_review_interrupted without a third agy spawn.
        red on 49fa3a36: the review never starts, so the rework hang is not spawned.
        """
        self.lead_timeout = 8.0
        gate = self._gate("c4-gate")
        self._agy_steps([self._ok(), {"do": "hang"}])
        self._lead_steps([f"block_until:{gate}"])
        app = self._build()
        goal_id = self._submit("c4")
        status = self._drive(app, goal_id, self._review_running, timeout=8.0)
        self.assertTrue(
            self._review_running(status),
            f"review did not stay running; state={status.get('state')} pending={status.get('pending_decisions')}",
        )
        request_id = self._request_id_of(status)
        self.assertTrue(request_id.endswith(":r1"), request_id)
        self._resolve_direct(self.backend, request_id, "fail", "direct-fail")
        rows = self._wait_agy(2)
        self.assertEqual(len(rows), 2, rows)
        self.assertIs(rows[1].get("rework"), True, rows[1])
        still = app.status(goal_id)
        self.assertTrue(still.get("pending_decisions"), still)
        app, _backend = self._restart()
        self.assertEqual(len(self._agy()), 2, self._agy())
        status = self._drive(app, goal_id, timeout=8.0)
        self.assertEqual(len(self._agy()), 2, self._agy())
        self.assertEqual(status["state"], "failed", status)
        error = str(self._result(status).get("error") or "")
        self.assertTrue(error.startswith("lead_review_interrupted:"), error)
        self.assertIn("used reworks 1/1 are kept", error, error)
        self._forbid(error)
        closed = [
            row
            for row in self._artifact_reviews(app, goal_id)
            if str(row.get("request_id") or "").endswith(":r1") and row.get("verdict") == "fail"
        ]
        self.assertEqual(len(closed), 1, self._all_decisions(app, goal_id))
        resolved = self._snap(app, goal_id).get("resolved_decisions") or []
        self.assertIn(closed[0].get("decision_id"), [row.get("decision_id") for row in resolved])

    def test_t7_blocked_review_does_not_stall_other_goal(self) -> None:
        """T7: asserts a blocked review lets another artifact-only goal complete, each process_all in that window stays under 0.5s, and opening the gate completes the review.
        red on 49fa3a36: the first goal is not left in lead_review status running.
        """
        self.lead_timeout = 8.0
        gate = self._gate("t7-gate")
        self._agy_steps([self._ok(), self._ok("side.md", "side\n")])
        self._lead_steps([f"block_until:{gate}", "pass:after-gate"])
        app = self._build()
        goal_a = self._submit("t7-a")
        status_a = self._drive(app, goal_a, self._review_running, timeout=8.0)
        self.assertTrue(
            self._review_running(status_a),
            f"goal A never entered lead review; state={status_a.get('state')} pending={status_a.get('pending_decisions')}",
        )
        self.assertEqual(status_a["pending_decisions"][0].get("awaiting"), "lead", status_a["pending_decisions"])
        self.assertEqual(len(self._agy()), 1, self._agy())
        goal_b = self._submit("t7-b", prose=False, artifacts=["side.md"], max_reworks=0)
        times: list[float] = []
        deadline = time.monotonic() + 8.0
        status_b = app.status(goal_b)
        while time.monotonic() < deadline:
            started = time.monotonic()
            _process_all(app)
            times.append(time.monotonic() - started)
            status_b = app.status(goal_b)
            status_a = app.status(goal_a)
            if str(status_b.get("state") or "") in _TERMINAL or str(status_a.get("state") or "") in _TERMINAL:
                break
            time.sleep(0.05)
        slow = [item for item in times if item >= 0.5]
        self.assertTrue(times, "no process_all while A was in review")
        self.assertEqual(slow, [], f"process_all exceeded 0.5s: {times}")
        self.assertEqual(status_b["state"], "completed", status_b)
        status_a = app.status(goal_a)
        self.assertNotIn(status_a.get("state"), _TERMINAL, status_a)
        lead = self._lead_review_of(status_a) or {}
        self.assertEqual(lead.get("status"), "running", lead)
        self.assertEqual(status_a["pending_decisions"][0].get("awaiting"), "lead", status_a["pending_decisions"])
        gate.write_text("open", encoding="utf-8")
        status_a = self._drive(app, goal_a, timeout=8.0)
        self.assertEqual(status_a["state"], "completed", status_a)
        review = self._result(status_a).get("review") or {}
        self.assertEqual(review.get("source"), "lead_review", review)

    def test_t14_cancel_during_review(self) -> None:
        """T14: asserts cancel during review cancels the goal, records lead_review_stopped on the decision, and the lead pid is gone.
        red on 49fa3a36: no lead review process is running to stop.
        """
        self.lead_timeout = 8.0
        gate = self._gate("t14-gate")
        self._agy_steps([self._ok()])
        self._lead_steps([f"block_until:{gate}"])
        app = self._build()
        goal_id = self._submit("t14")
        status = self._drive(app, goal_id, self._review_running, timeout=8.0)
        self.assertTrue(
            self._review_running(status),
            f"review did not stay running; state={status.get('state')} pending={status.get('pending_decisions')}",
        )
        self.assertGreaterEqual(len(self._reviews()), 1, _jsonl(self.lead_log))
        pid = int(self._reviews()[-1]["pid"])
        self.assertTrue(_pid_alive(pid), pid)
        app.cancel(goal_id, "stop review")
        status = self._drive(app, goal_id, timeout=6.0)
        self.assertEqual(status["state"], "cancelled", status)
        self._assert_event(app, goal_id, "lead_review_stopped")
        self._wait_dead(pid)

    def test_t16_codex_fail_then_pass(self) -> None:
        """T16: asserts a codex-shaped fake lead fail then pass completes the goal.
        red on 49fa3a36: codex output is not applied as an agy lead review.
        """
        self._codex = True
        self._agy_steps([self._ok(), self._ok(text="codex-rework\n")])
        self._lead_steps(["fail:codex-no", "pass:codex-yes"])
        app = self._build()
        goal_id = self._submit("t16")
        status = self._drive(app, goal_id)
        self.assertEqual(status["state"], "completed", status)
        rework = self._result(status).get("rework") or {}
        self.assertEqual(rework.get("used"), 1, rework)
        review = self._result(status).get("review") or {}
        self.assertEqual(review.get("status"), "passed", review)
        self.assertEqual(review.get("source"), "lead_review", review)
        reviews = self._artifact_reviews(app, goal_id)
        self.assertEqual([row.get("verdict") for row in reviews], ["fail", "pass"], reviews)
        rows = self._agy()
        self.assertEqual(len(rows), 2, rows)
        self.assertIs(rows[1].get("rework"), True, rows[1])
        self.assertEqual(len(self._reviews()), 2, _jsonl(self.lead_log))

    def test_t10_capability_gate(self) -> None:
        """T10: asserts agy plus the lead planner advertises acceptance.lead_review and accepts required_capabilities lead_review.
        red on 49fa3a36: acceptance.lead_review is false and submit is 409.
        """
        self._agy_steps([self._ok()])
        self._lead_steps(["pass:accepted"])
        app = self._build()
        caps = app.capabilities()
        self.assertIs(caps["acceptance"]["lead_review"], True, caps["acceptance"])
        self.assertIs(caps["planner"]["lead_review"], True, caps["planner"])
        opened = app.submit(self._request("t10", required=["lead_review"]))
        self.assertTrue(opened.get("ok"), opened)
        self.assertTrue(opened.get("goal_id"), opened)


if __name__ == "__main__":
    unittest.main()
