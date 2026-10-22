"""Unit tests for LeadReviewRunner. Written before the runner module exists.

The harness (fake backend, CollabApplication, pending review) runs on current
code. Every case except H0 lazy-imports framework.lead_review_runner and is
red until that module implements spec §6.
"""
from __future__ import annotations

import copy
import re
import tempfile
import threading
import time
import unittest
import uuid

from execution_backend.base import BackendError, BackendStatus, default_capabilities
from framework.app_service import CollabApplication, DeterministicPlanner
from lead_adapter.cancel import LeadCancelled, current_scope
from lead_adapter.schema import LeadDecisionError
from tests.desktop_lock_isolation import install_desktop_lock_isolation

_REQUEST_RE = re.compile(r"^agyrev:(agy_[0-9a-f]{12}):r([1-9][0-9]*)$")
_PASS = {"pass", "approve", "allow", "once"}
_FAIL = {"fail", "reject", "deny", "deny_job"}


def _load_runner():
    """Import the runner. Each test calls this so a missing module fails that test only."""
    from framework.lead_review_runner import LeadReviewRunner

    return LeadReviewRunner


def _parse_request_id(request_id: str):
    match = _REQUEST_RE.match(str(request_id or ""))
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def _normalize_verdict(verdict: str) -> str:
    token = str(verdict or "").strip().lower()
    if token in _PASS:
        return "pass"
    if token in _FAIL:
        return "fail"
    if token == "unavailable":
        return "unavailable"
    raise ValueError(f"unsupported review verdict {verdict!r}")


def _request(key: str) -> dict:
    """Same minimal prose-acceptance body as test_app_service._request, with a unique key."""
    return {
        "idempotency_key": key,
        "client_id": "test-suite",
        "title": "Application entry pilot",
        "goal": "Create the requested delivery artifact inside the assigned workspace.",
        "boundaries": {
            "must": ["Stay inside the assigned workspace"],
            "must_not": ["Do not use network or system tools"],
        },
        "acceptance": {"artifacts": ["delivery.txt"], "text": "delivery.txt exists"},
        "budget": {"wall_sec": 30, "max_reworks": 0},
    }


def _coerce_step(item) -> dict:
    if isinstance(item, dict):
        return dict(item)
    text = str(item)
    if text == "block":
        return {"op": "block", "honor_cancel": True, "verdict": "pass", "reason": "blocked"}
    if text == "block_pass":
        return {"op": "block", "honor_cancel": False, "verdict": "pass", "reason": "late pass"}
    if text == "timeout":
        return {"op": "timeout"}
    if text == "illegal_json":
        return {"op": "illegal_json"}
    if text == "unauthorized":
        return {"op": "unauthorized"}
    if text == "pass" or text.startswith("pass:"):
        reason = text.split(":", 1)[1] if ":" in text else "pass"
        return {"op": "verdict", "verdict": "pass", "reason": reason}
    if text == "fail" or text.startswith("fail:"):
        reason = text.split(":", 1)[1] if ":" in text else "fail"
        return {"op": "verdict", "verdict": "fail", "reason": reason}
    raise AssertionError(f"bad FakePlanner script step {item!r}")


class FakeAsyncBackend:
    """In-process ExecutionBackend with the async review channel from spec §3.

    ``resolve_calls`` records every ``resolve_decision`` invocation, including
    ones that raise, so tests can see a single lead attempt and the absence of
    an ``unavailable`` send.

    ``race_conflict_once`` is ``(verdict, reason)`` or None. The next
    ``resolve_decision`` records that conflicting resolution first (so
    ``review_outcome`` is still None before the call) and then raises
    ``ValueError("review request already resolved as <verdict>")``.
    """

    backend_id = "fake.async_review"
    lead_review_mode = "async_v1"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, dict] = {}
        self._listeners: list = []
        self.resolve_calls: list[dict] = []
        self.race_conflict_once: tuple[str, str] | None = None

    def capabilities(self) -> dict:
        caps = default_capabilities(backend_id=self.backend_id, kind="fake")
        caps["channels"] = {"permission": False, "question": False, "review": True}
        caps["acceptance"]["lead_review"] = True
        return caps

    def start_run(
        self,
        *,
        title: str,
        directory: str,
        instruction: str = "",
        artifacts: list[str] | None = None,
        charter: dict | None = None,
    ) -> dict:
        run_id = f"agy_{uuid.uuid4().hex[:12]}"
        now = time.time()
        review = {
            "round": 1,
            "redos": 0,
            "max_redos": 1,
            "state": "awaiting_review",
            "deadline": now + 3600.0,
            "resolutions": {},
            "history": [],
            "request_id": f"agyrev:{run_id}:r1",
            "unavailable_reason": None,
        }
        rec = {
            "run_id": run_id,
            "directory": directory,
            "title": title,
            "review": review,
            "cancelled": False,
        }
        with self._lock:
            self._runs[run_id] = rec
        return {
            "ok": True,
            "backend": self.backend_id,
            "run_id": run_id,
            "native_handle": run_id,
            "state": "awaiting_review",
        }

    def _rec(self, run_id: str) -> dict:
        rec = self._runs.get(str(run_id or ""))
        if rec is None:
            raise BackendError(
                BackendStatus.FAILED,
                f"unknown run_id={run_id}",
                capability="observe_run",
            )
        return rec

    def observe_run(self, run_id, *, dispatch_user_message_id=None, fetch_messages=True):
        with self._lock:
            rec = self._rec(run_id)
            state = rec["review"]["state"]
        busy = state in {"running", "awaiting_review"}
        return {
            "busy": busy,
            "activity": "awaiting_review" if state == "awaiting_review" else ("busy" if busy else "idle"),
            "finish": None if busy else "stop",
            "finish_successful": state == "accepted",
            "errored": False,
            "run_id": run_id,
        }

    def list_pending_actions(self, *, session_id=None):
        with self._lock:
            rows = []
            for run_id, rec in self._runs.items():
                if session_id is not None and str(session_id) != run_id:
                    continue
                review = rec["review"]
                if review["state"] != "awaiting_review":
                    continue
                request_id = f"agyrev:{run_id}:r{review['round']}"
                rows.append(
                    {
                        "request_id": request_id,
                        "kind": "review",
                        "payload": {
                            "round": review["round"],
                            "finish": "stop",
                            "artifacts": {},
                            "acceptance_text": "delivery.txt exists",
                        },
                        "context_hash": "h",
                    }
                )
        return 200, rows

    def add_review_listener(self, callback) -> None:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def clear_listeners(self) -> None:
        with self._lock:
            self._listeners.clear()

    def review_view(self, run_id: str) -> dict:
        with self._lock:
            review = self._rec(run_id)["review"]
            return {
                "round": review["round"],
                "state": review["state"],
                "redos": review["redos"],
                "max_redos": review["max_redos"],
                "resolutions": copy.deepcopy(review["resolutions"]),
                "unavailable_reason": review.get("unavailable_reason"),
            }

    def advance_round(self, run_id: str, *, round: int, state: str) -> dict:
        """Move the controller to another round without resolving or notifying."""
        with self._lock:
            review = self._rec(run_id)["review"]
            review["round"] = int(round)
            review["state"] = str(state)
            if state == "awaiting_review":
                review["request_id"] = f"agyrev:{run_id}:r{int(round)}"
            else:
                review["request_id"] = None
        return self.review_view(run_id)

    def _commit_review_locked(self, rec, request_id, rnd, v, reason, answers) -> None:
        """Record one resolution. Caller holds ``_lock`` and does not notify."""
        review = rec["review"]
        by = "api"
        if isinstance(answers, list) and answers and isinstance(answers[0], dict):
            marker = answers[0].get("by")
            if isinstance(marker, str) and marker:
                by = marker
        review["resolutions"][request_id] = {
            "verdict": v,
            "reason": str(reason or "")[:2000],
            "at": time.time(),
            "by": by,
        }
        review["history"].append(
            {
                "round": rnd,
                "verdict": v,
                "reason": str(reason or "")[:2000],
                "by": by,
                "at": time.time(),
            }
        )
        if v == "pass":
            review["state"] = "accepted"
        elif v == "fail":
            remaining = float(review["deadline"]) - time.time()
            if review["redos"] < review["max_redos"] and remaining >= 30:
                review["redos"] += 1
                review["round"] += 1
                review["state"] = "running"
                review["request_id"] = None
            else:
                review["state"] = "rejected_final"
        elif v == "unavailable":
            review["state"] = "review_unavailable"
            review["unavailable_reason"] = str(reason or "")

    def resolve_decision(self, request_id, *, verdict, reason, answers=None) -> dict:
        self.resolve_calls.append(
            {
                "request_id": request_id,
                "verdict": verdict,
                "reason": reason,
                "answers": copy.deepcopy(answers) if isinstance(answers, list) else answers,
            }
        )
        parsed = _parse_request_id(request_id)
        if parsed is None:
            raise BackendError(
                BackendStatus.FAILED,
                f"not a review request {request_id!r}",
                capability="resolve_decision",
            )
        run_id, rnd = parsed
        with self._lock:
            rec = self._runs.get(run_id)
            if rec is None or "review" not in rec:
                raise BackendError(
                    BackendStatus.FAILED,
                    f"unknown review run {run_id}",
                    capability="resolve_decision",
                )
            v = _normalize_verdict(verdict)
            review = rec["review"]
            resolutions = review["resolutions"]
            # Injected race: review_outcome was None, then this call commits
            # the other writer's verdict and rejects the caller's verdict.
            if self.race_conflict_once is not None:
                conflict_verdict, conflict_reason = self.race_conflict_once
                self.race_conflict_once = None
                self._commit_review_locked(
                    rec,
                    request_id,
                    rnd,
                    _normalize_verdict(conflict_verdict),
                    conflict_reason,
                    [{"by": "human"}],
                )
            if request_id in resolutions:
                previous = resolutions[request_id]["verdict"]
                if previous == v:
                    return {
                        "ok": True,
                        "idempotent": True,
                        "request_id": request_id,
                        "kind": "review",
                        "controller_state": review["state"],
                    }
                raise ValueError(f"review request already resolved as {previous}")
            if rnd != review["round"] or review["state"] != "awaiting_review":
                raise ValueError(
                    f"stale review request {request_id}: current round r{review['round']}, state {review['state']}"
                )
            self._commit_review_locked(rec, request_id, rnd, v, reason, answers)
            new_state = review["state"]
            round_after = review["round"]
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(request_id, v, new_state)
            except Exception:
                pass
        return {
            "ok": True,
            "request_id": request_id,
            "kind": "review",
            "controller_state": new_state,
            "round": round_after,
        }

    def review_outcome(self, request_id) -> dict | None:
        parsed = _parse_request_id(request_id)
        if parsed is None:
            return None
        with self._lock:
            rec = self._runs.get(parsed[0])
            if rec is None:
                return None
            found = rec["review"]["resolutions"].get(request_id)
            if not isinstance(found, dict):
                return None
            return {
                "verdict": found["verdict"],
                "reason": found["reason"],
                "by": found["by"],
                "controller_state": rec["review"]["state"],
            }

    def collect_result(self, run_id) -> dict:
        with self._lock:
            rec = self._rec(run_id)
            state = rec["review"]["state"]
            directory = rec.get("directory") or ""
        return {
            "ok": state == "accepted",
            "run_id": run_id,
            "backend": self.backend_id,
            "state": state,
            "artifacts": [],
            "workspace": directory,
        }

    def cancel(self, run_id):
        with self._lock:
            rec = self._runs.get(str(run_id or ""))
            if rec is None:
                return 404, {"ok": False, "run_id": run_id}
            rec["cancelled"] = True
            rec["review"]["state"] = "cancelled"
        return 200, {"ok": True, "run_id": run_id, "state": "cancelled"}

    def reply_permission(self, request_id, reply):
        return 501, {"ok": False, "status": "unsupported", "capability": "reply_permission"}


class _BackendWithoutMode:
    """resolve_decision exists; lead_review_mode does not."""

    backend_id = "fake.no_lead_mode"

    def resolve_decision(self, request_id, *, verdict, reason, answers=None):
        return {"ok": True, "request_id": request_id, "verdict": verdict}


class FakePlanner:
    """Scripted decide_action. Blocks on an Event and can notice CancelScope."""

    name = "fake_lead"

    def __init__(self, script, *, timeout_sec: float = 1.0) -> None:
        self.timeout_sec = float(timeout_sec)
        self._script = [_coerce_step(item) for item in script]
        self._lock = threading.Lock()
        self._inflight = 0
        self._blockers: dict[str, threading.Event] = {}
        self.calls = 0
        self.completed = 0
        self.thread_ids: list[int] = []
        self.calls_log: list[dict] = []
        self.scopes: list = []
        self.latest_scope = None

    def decide_action(self, snap, task, action):
        request_id = str((action or {}).get("request_id") or "")
        with self._lock:
            if not self._script:
                raise AssertionError("FakePlanner script exhausted")
            step = self._script.pop(0)
            self.calls += 1
            self.thread_ids.append(threading.get_ident())
            self._inflight += 1
            self.calls_log.append({"request_id": request_id, "op": step.get("op")})
        scope = current_scope()
        if scope is not None:
            with self._lock:
                self.scopes.append(scope)
                self.latest_scope = scope
        try:
            return self._perform(step, request_id)
        finally:
            with self._lock:
                self._inflight -= 1
                self.completed += 1

    def _perform(self, step: dict, request_id: str):
        op = step.get("op")
        if op == "timeout":
            raise LeadDecisionError("timeout", "lead call timed out")
        if op == "illegal_json":
            raise LeadDecisionError("illegal_json", "no parseable JSON decision object")
        if op == "unauthorized":
            raise RuntimeError("401 unauthorized")
        if op == "block":
            gate = threading.Event()
            with self._lock:
                self._blockers[request_id] = gate
            while not gate.is_set():
                scope = current_scope()
                if scope is not None:
                    self.latest_scope = scope
                if step.get("honor_cancel", True) and scope is not None and scope.cancelled:
                    raise LeadCancelled(scope.reason or "cancelled")
                gate.wait(0.05)
            scope = current_scope()
            if scope is not None:
                self.latest_scope = scope
            if step.get("honor_cancel", True) and scope is not None and scope.cancelled:
                raise LeadCancelled(scope.reason or "cancelled")
            return {"verdict": step.get("verdict") or "pass", "reason": step.get("reason") or "pass"}
        if op == "verdict":
            return {"verdict": step["verdict"], "reason": step.get("reason") or step["verdict"]}
        raise AssertionError(f"unhandled planner step {step!r}")

    def wait_blocked(self, request_id: str, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                gate = self._blockers.get(request_id)
                if gate is not None and not gate.is_set():
                    return True
            time.sleep(0.01)
        return False

    def release_request(self, request_id: str) -> None:
        with self._lock:
            gate = self._blockers.get(request_id)
        if gate is None:
            raise AssertionError(f"no blocker for {request_id}")
        gate.set()

    def release_all(self) -> None:
        with self._lock:
            gates = list(self._blockers.values())
        for gate in gates:
            gate.set()

    def wait_completed(self, count: int, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self.completed >= count and self._inflight == 0:
                    return True
            time.sleep(0.01)
        return False


def _lead_review_events(app, goal_id: str, decision_id: str) -> list[dict]:
    """``details.lead_review.events`` for one decision.

    Pending decisions are searched before resolved decisions. ``record_goal_event``
    rejects these names, so they are not goal-history ops.
    """
    snap = app.layer.get_goal(goal_id)["goal"]
    found = None
    for bucket in ("pending_decisions", "resolved_decisions"):
        for row in snap.get(bucket) or []:
            if isinstance(row, dict) and row.get("decision_id") == decision_id:
                found = row
                break
        if found is not None:
            break
    raw = _lead_review(found or {}).get("events")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _events(app, goal_id: str, decision_id: str) -> list[str]:
    """Event names on ``details.lead_review.events`` of the concerned decision."""
    return [str(item.get("event") or "") for item in _lead_review_events(app, goal_id, decision_id)]


def _task_of(snap: dict, decision: dict) -> dict:
    want = str((decision or {}).get("task_id") or "")
    tasks = [row for row in (snap.get("tasks") or []) if isinstance(row, dict)]
    for task in tasks:
        if str(task.get("task_id") or "") == want:
            return task
    return tasks[0] if tasks else {}


def _load_decision(app, goal_id: str, decision_id: str):
    snap = app.layer.get_goal(goal_id)["goal"]
    found = None
    for bucket in ("pending_decisions", "resolved_decisions"):
        for row in snap.get(bucket) or []:
            if isinstance(row, dict) and row.get("decision_id") == decision_id:
                found = row
                break
        if found is not None:
            break
    decision = found or {}
    return snap, _task_of(snap, decision), decision


def _details(decision: dict) -> dict:
    raw = decision.get("details") if isinstance(decision, dict) else None
    return raw if isinstance(raw, dict) else {}


def _lead_review(decision: dict) -> dict:
    raw = _details(decision).get("lead_review")
    return raw if isinstance(raw, dict) else {}


def _action_of(decision: dict) -> dict:
    details = _details(decision)
    payload = details.get("payload") if isinstance(details.get("payload"), dict) else {}
    return {
        "kind": details.get("backend_kind"),
        "request_id": details.get("backend_request_id") or decision.get("request_id"),
        "payload": payload,
        "context_hash": details.get("context_hash"),
    }


def _pending_ids(snap: dict) -> list[str]:
    return [
        str(row.get("decision_id") or "")
        for row in (snap.get("pending_decisions") or [])
        if isinstance(row, dict)
    ]


def _verdicts(backend: FakeAsyncBackend) -> list[str]:
    return [str(row.get("verdict") or "") for row in backend.resolve_calls]


class LeadReviewRunnerTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        install_desktop_lock_isolation(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._planners: list[FakePlanner] = []
        self._runners: list = []
        self.addCleanup(self._shutdown)

    def _shutdown(self) -> None:
        for planner in self._planners:
            planner.release_all()
        for runner in list(self._runners):
            stop = getattr(runner, "stop_all", None)
            if not callable(stop):
                continue
            box: dict = {}

            def _run(stop=stop) -> None:
                try:
                    box["out"] = stop("test cleanup")
                except BaseException as exc:  # noqa: BLE001 — cleanup must not mask the test
                    box["exc"] = exc

            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            thread.join(4.0)
        for planner in self._planners:
            planner.release_all()
            planner.wait_completed(planner.calls, timeout=1.0)

    def _planner(self, script, *, timeout_sec: float = 1.0) -> FakePlanner:
        planner = FakePlanner(script, timeout_sec=timeout_sec)
        self._planners.append(planner)
        return planner

    def _make_app(self, backend) -> CollabApplication:
        # The service default global run cap is 4. C3c needs five dispatched
        # reviews so the runner cap, not the dispatcher, is what queues one.
        return CollabApplication(
            self._tmp.name,
            planner=DeterministicPlanner(),
            backend=backend,
            max_parallel_per_goal=4,
            max_parallel_global=8,
        )

    def _make_runner(self, app, planner, backend, *, token: str):
        lead_review_runner = _load_runner()
        runner = lead_review_runner(
            layer=app.layer,
            planner=planner,
            backend=backend,
            token=token,
        )
        self._runners.append(runner)
        hook = getattr(runner, "_on_backend_resolved", None)
        register = getattr(backend, "add_review_listener", None)
        # for_coordinator registers the listener. Direct construction must too,
        # or a human resolve cannot free the slot (T5e). Identity dedupe keeps
        # a second registration harmless.
        if callable(hook) and callable(register):
            register(hook)
        return runner

    def _park(self, app, key: str):
        submitted = app.submit(_request(key))
        goal_id = submitted["goal_id"]
        last = None
        snap = {}
        for _ in range(8):
            last = app.coordinator.process_all()
            snap = app.layer.get_goal(goal_id)["goal"]
            pending = [row for row in (snap.get("pending_decisions") or []) if isinstance(row, dict)]
            reviews = [row for row in pending if _details(row).get("backend_kind") == "review"]
            if len(pending) == 1 and len(reviews) == 1:
                return goal_id, snap, reviews[0]
            if snap.get("state") in {"failed", "completed", "cancelled"}:
                break
        self.fail(
            f"pending review not parked: state={snap.get('state')} "
            f"failure={snap.get('failure')} last={last} pending={snap.get('pending_decisions')}"
        )

    def _step(self, runner, snap, task, decision, action, *, timeout: float = 1.0):
        box: dict = {}

        def _run() -> None:
            try:
                box["out"] = runner.step(snap, task, decision=decision, action=action)
            except BaseException as exc:  # noqa: BLE001 — re-raised on the test thread
                box["exc"] = exc

        thread = threading.Thread(target=_run, daemon=True)
        started = time.monotonic()
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            self.fail(f"step blocked longer than {timeout}s")
        if "exc" in box:
            raise box["exc"]
        box["elapsed"] = time.monotonic() - started
        return box["out"], box["elapsed"]

    def _drive(self, runner, app, goal_id, decision_id, action, pred, *, timeout: float = 3.0):
        deadline = time.monotonic() + timeout
        last = None
        snap, task, decision = _load_decision(app, goal_id, decision_id)
        while time.monotonic() < deadline:
            snap, task, decision = _load_decision(app, goal_id, decision_id)
            last, _elapsed = self._step(runner, snap, task, decision, action)
            snap, task, decision = _load_decision(app, goal_id, decision_id)
            if pred(last, snap, decision):
                return last, snap, decision
            time.sleep(0.02)
        self.fail(f"drive timeout last={last!r} lead_review={_lead_review(decision)!r} status={decision.get('status')}")

    def _wait_event(self, app, goal_id: str, decision_id: str, name: str, *, timeout: float = 1.0):
        deadline = time.monotonic() + timeout
        while True:
            rows = [row for row in _lead_review_events(app, goal_id, decision_id) if row.get("event") == name]
            if rows:
                return rows
            if time.monotonic() >= deadline:
                self.fail(
                    f"missing lead_review event {name} on {decision_id}; "
                    f"events={_events(app, goal_id, decision_id)}"
                )
            time.sleep(0.02)

    def _wait_scope(self, planner: FakePlanner, *, timeout: float = 1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            scope = planner.latest_scope
            if scope is not None and scope.cancelled:
                return scope
            time.sleep(0.02)
        self.fail(f"lead scope was not cancelled; scope={planner.latest_scope!r}")

    def test_H0_guardrail_pending_review_stays_pending(self):
        """H0: asserts one pending review decision is created and stays pending.
        red on 49fa3a36: no — guardrail, passes on current code.
        """
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        goal_id, snap, decision = self._park(app, "h0-guardrail")
        details = _details(decision)
        self.assertEqual(details.get("backend_kind"), "review")
        self.assertRegex(str(details.get("backend_request_id") or ""), _REQUEST_RE.pattern)
        self.assertEqual(details.get("context_hash"), "h")
        self.assertNotIn("lead_review", details)
        self.assertNotIn("lead_attempts", details)
        self.assertNotIn("lead_error", details)
        self.assertEqual(decision.get("status"), "pending")
        self.assertEqual(snap.get("state"), "running")
        decision_id = decision["decision_id"]
        for _ in range(3):
            app.coordinator.process_all()
        snap = app.layer.get_goal(goal_id)["goal"]
        self.assertEqual(snap.get("state"), "running")
        self.assertEqual(_pending_ids(snap), [decision_id])
        self.assertEqual(snap.get("resolved_decisions") or [], [])
        reloaded = next(row for row in snap["pending_decisions"] if row["decision_id"] == decision_id)
        self.assertEqual(_details(reloaded).get("backend_kind"), "review")
        self.assertNotIn("lead_review", _details(reloaded))

    def test_R1_async_submit_returns_while_planner_blocks(self):
        """R1: asserts the first step returns lead_reviewing in under 0.2s with a running lead_review.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        parked = app.coordinator.planner
        planner = self._planner(["block"], timeout_sec=1.0)
        goal_id, _snap, decision = self._park(app, "r1-async")
        action = _action_of(decision)
        runner = self._make_runner(app, planner, backend, token="tokA")
        snap, task, decision = _load_decision(app, goal_id, decision["decision_id"])
        out, elapsed = self._step(runner, snap, task, decision, action, timeout=0.2)
        self.assertLess(elapsed, 0.2)
        self.assertEqual(out.get("action"), "lead_reviewing")
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("state"), "running")
        self.assertEqual(out.get("decision_id"), decision["decision_id"])
        self.assertEqual(out.get("attempt"), 1)
        self.assertTrue(planner.wait_blocked(action["request_id"], timeout=1.0))
        snap, _task, decision = _load_decision(app, goal_id, decision["decision_id"])
        details = _details(decision)
        lead = _lead_review(decision)
        self.assertEqual(lead.get("status"), "running")
        self.assertEqual(lead.get("attempt"), 1)
        self.assertEqual(lead.get("owner"), "tokA")
        self.assertRegex(str(lead.get("job_id") or ""), r"^lrj_[0-9a-f]{8}$")
        self.assertIsInstance(lead.get("started_at"), (int, float))
        self.assertIsInstance(lead.get("deadline"), (int, float))
        self.assertEqual(lead.get("timeout_sec"), planner.timeout_sec)
        self.assertEqual(lead.get("round"), 1)
        self.assertEqual(lead.get("request_id"), action["request_id"])
        self.assertEqual(lead.get("max_attempts"), 2)
        grace = _load_runner().WATCHDOG_GRACE_SEC
        self.assertAlmostEqual(
            float(lead["deadline"]) - float(lead["started_at"]),
            float(planner.timeout_sec) + float(grace),
            delta=1.0,
        )
        self.assertNotIn("lead_attempts", details)
        self.assertNotIn("lead_error", details)
        # awaiting=="lead" needs a planner with decide_action. The goal was parked
        # with DeterministicPlanner so process_all leaves the decision pending.
        app.coordinator.planner = planner
        try:
            row = app.status(goal_id)["pending_decisions"][0]
            self.assertEqual(row["awaiting"], "lead")
            self.assertNotIn("lead_attempts", row["details"])
            self.assertNotIn("lead_error", row["details"])
            self.assertNotIn("lead_error", row)
        finally:
            app.coordinator.planner = parked
        self.assertEqual(planner.calls, 1)
        self.assertNotEqual(planner.thread_ids[0], threading.get_ident())
        self.assertEqual(backend.resolve_calls, [])

    def test_R2_pass_resolves_backend_once(self):
        """R2: asserts one pass resolve with by=lead and a durable pass marked done.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        planner = self._planner(["pass:artifacts match"])
        goal_id, _snap, decision = self._park(app, "r2-pass")
        action = _action_of(decision)
        runner = self._make_runner(app, planner, backend, token="tokA")

        def _done(last, snap, decision):
            return decision.get("status") == "resolved" and _lead_review(decision).get("status") == "done"

        last, _snap, decision = self._drive(runner, app, goal_id, decision["decision_id"], action, _done)
        self.assertEqual(last.get("action"), "decision_resolved")
        self.assertEqual(last.get("lead"), "fake_lead")
        self.assertEqual(decision.get("verdict"), "pass")
        self.assertEqual(_pending_ids(app.layer.get_goal(goal_id)["goal"]), [])
        passes = [row for row in backend.resolve_calls if row.get("verdict") == "pass"]
        self.assertEqual(len(passes), 1)
        self.assertEqual(passes[0]["answers"], [{"by": "lead"}])
        self.assertEqual(passes[0]["reason"], "artifacts match")
        self.assertNotIn("unavailable", _verdicts(backend))
        self.assertEqual(planner.calls, 1)

    def test_T4a_timeout_twice_then_unavailable(self):
        """T4a: asserts two timeout attempts then unavailable, durable reject, never pass.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        planner = self._planner(["timeout", "timeout"])
        goal_id, _snap, decision = self._park(app, "t4a-timeout")
        action = _action_of(decision)
        runner = self._make_runner(app, planner, backend, token="tokA")

        def _done(last, snap, decision):
            return decision.get("status") == "resolved"

        _last, _snap, decision = self._drive(runner, app, goal_id, decision["decision_id"], action, _done)
        lead = _lead_review(decision)
        history = lead.get("history")
        self.assertIsInstance(history, list)
        attempts = sorted(
            {
                int(item["attempt"])
                for item in history
                if isinstance(item, dict) and (item.get("code") == "timeout" or item.get("status") == "timeout")
            }
        )
        self.assertEqual(attempts, [1, 2])
        self.assertEqual(lead.get("status"), "unavailable")
        self.assertEqual(lead.get("attempt"), 2)
        reason = "lead_review_unavailable: timeout after 2 attempt(s)"
        unavailable = [row for row in backend.resolve_calls if row.get("verdict") == "unavailable"]
        self.assertEqual(len(unavailable), 1)
        self.assertEqual(unavailable[0]["reason"], reason)
        self.assertEqual(unavailable[0]["answers"], [{"by": "system"}])
        self.assertNotIn("pass", _verdicts(backend))
        self.assertEqual(decision.get("verdict"), "reject")
        self.assertEqual(decision.get("reason"), reason)
        self.assertEqual(planner.calls, 2)

    def test_T4b_illegal_json_then_pass(self):
        """T4b: asserts illegal_json is retried and attempt 2 completes with pass.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        planner = self._planner(["illegal_json", "pass:second look"])
        goal_id, _snap, decision = self._park(app, "t4b-json")
        action = _action_of(decision)
        runner = self._make_runner(app, planner, backend, token="tokA")

        def _done(last, snap, decision):
            return decision.get("status") == "resolved" and decision.get("verdict") == "pass"

        _last, _snap, decision = self._drive(runner, app, goal_id, decision["decision_id"], action, _done)
        lead = _lead_review(decision)
        self.assertEqual(lead.get("status"), "done")
        self.assertEqual(lead.get("attempt"), 2)
        self.assertEqual(planner.calls, 2)
        passes = [row for row in backend.resolve_calls if row.get("verdict") == "pass"]
        self.assertEqual(len(passes), 1)
        self.assertEqual(passes[0]["answers"], [{"by": "lead"}])
        self.assertNotIn("unavailable", _verdicts(backend))

    def test_T4c_unauthorized_is_not_retried(self):
        """T4c: asserts a 401 unauthorized error finalizes after exactly one lead call.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        planner = self._planner(["unauthorized", "pass:should not run"])
        goal_id, _snap, decision = self._park(app, "t4c-401")
        action = _action_of(decision)
        runner = self._make_runner(app, planner, backend, token="tokA")

        def _done(last, snap, decision):
            return decision.get("status") == "resolved"

        _last, _snap, decision = self._drive(runner, app, goal_id, decision["decision_id"], action, _done)
        lead = _lead_review(decision)
        err = lead.get("last_error") if isinstance(lead.get("last_error"), dict) else {}
        code = str(err.get("code") or "")
        self.assertTrue(code)
        self.assertFalse(err.get("retryable"))
        blob = f"{err.get('message') or ''} {err.get('code') or ''}".lower()
        self.assertIn("401", blob)
        self.assertIn("unauthorized", blob)
        self.assertEqual(planner.calls, 1)
        self.assertEqual(lead.get("attempt"), 1)
        self.assertEqual(lead.get("status"), "unavailable")
        unavailable = [row for row in backend.resolve_calls if row.get("verdict") == "unavailable"]
        self.assertEqual(len(unavailable), 1)
        self.assertEqual(unavailable[0]["reason"], f"lead_review_unavailable: {code} after 1 attempt(s)")
        self.assertEqual(unavailable[0]["answers"], [{"by": "system"}])
        self.assertEqual(decision.get("verdict"), "reject")
        self.assertNotEqual(decision.get("verdict"), "pass")
        self.assertNotIn("pass", _verdicts(backend))

    def test_W1_watchdog_times_out_and_releases_slot(self):
        """W1: asserts a blocked lead past the watchdog becomes timeout and frees the slot.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        lead_review_runner = _load_runner()
        previous = lead_review_runner.WATCHDOG_GRACE_SEC
        lead_review_runner.WATCHDOG_GRACE_SEC = 0.2
        self.addCleanup(setattr, lead_review_runner, "WATCHDOG_GRACE_SEC", previous)
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        planner = self._planner(["block"], timeout_sec=0.2)
        goal_id, _snap, decision = self._park(app, "w1-watchdog")
        action = _action_of(decision)
        runner = self._make_runner(app, planner, backend, token="tokA")

        def _timed_out(last, snap, decision):
            return _lead_review(decision).get("status") == "timeout"

        _last, _snap, decision = self._drive(
            runner, app, goal_id, decision["decision_id"], action, _timed_out, timeout=2.0
        )
        lead = _lead_review(decision)
        err = lead.get("last_error") if isinstance(lead.get("last_error"), dict) else {}
        self.assertEqual(lead.get("status"), "timeout")
        self.assertEqual(err.get("code"), "timeout")
        self.assertTrue(err.get("retryable"))
        self.assertIn("watchdog", str(err.get("message") or "").lower())
        self.assertEqual(runner.live_jobs(), 0)
        self._wait_scope(planner)
        self.assertEqual(backend.resolve_calls, [])

    def test_T5c_late_result_after_watchdog_is_discarded(self):
        """T5c: asserts a late pass after the watchdog records lead_review_late_result and does not resolve pass.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        lead_review_runner = _load_runner()
        previous = lead_review_runner.WATCHDOG_GRACE_SEC
        lead_review_runner.WATCHDOG_GRACE_SEC = 0.2
        self.addCleanup(setattr, lead_review_runner, "WATCHDOG_GRACE_SEC", previous)
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        planner = self._planner(["block_pass", "fail:next attempt"], timeout_sec=0.2)
        goal_id, _snap, decision = self._park(app, "t5c-late")
        action = _action_of(decision)
        runner = self._make_runner(app, planner, backend, token="tokA")

        def _timed_out(last, snap, decision):
            return _lead_review(decision).get("status") == "timeout"

        self._drive(runner, app, goal_id, decision["decision_id"], action, _timed_out, timeout=2.0)
        self.assertEqual(runner.live_jobs(), 0)
        self.assertTrue(planner.wait_blocked(action["request_id"], timeout=1.0))
        planner.release_request(action["request_id"])
        self.assertTrue(planner.wait_completed(1, timeout=1.0))

        def _next_decided(last, snap, decision):
            return decision.get("status") == "resolved"

        _last, snap, decision = self._drive(runner, app, goal_id, decision["decision_id"], action, _next_decided)
        decision_id = decision["decision_id"]
        self.assertIn("lead_review_late_result", _events(app, goal_id, decision_id))
        rows = self._wait_event(app, goal_id, decision_id, "lead_review_late_result")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("event"), "lead_review_late_result")
        self.assertEqual(rows[0].get("decision_id"), decision_id)
        self.assertNotIn("pass", _verdicts(backend))
        self.assertEqual(decision.get("verdict"), "fail")
        self.assertEqual(planner.calls, 2)
        self.assertEqual(_events(app, goal_id, decision_id).count("lead_review_late_result"), 1)

    def test_T5d_duplicate_ticks_call_the_planner_once(self):
        """T5d: asserts five steps while the lead is blocked call the planner once.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        planner = self._planner(["block"])
        goal_id, _snap, decision = self._park(app, "t5d-dup")
        action = _action_of(decision)
        decision_id = decision["decision_id"]
        runner = self._make_runner(app, planner, backend, token="tokA")
        for _ in range(5):
            snap, task, decision = _load_decision(app, goal_id, decision_id)
            out, elapsed = self._step(runner, snap, task, decision, action)
            self.assertEqual(out.get("action"), "lead_reviewing")
            self.assertLess(elapsed, 0.2)
        self.assertTrue(planner.wait_blocked(action["request_id"], timeout=1.0))
        self.assertEqual(planner.calls, 1)
        self.assertEqual(len(planner.thread_ids), 1)
        self.assertNotEqual(planner.thread_ids[0], threading.get_ident())
        _snap, _task, decision = _load_decision(app, goal_id, decision_id)
        self.assertEqual(_lead_review(decision).get("attempt"), 1)
        self.assertEqual(_lead_review(decision).get("status"), "running")
        self.assertEqual(backend.resolve_calls, [])

    def test_T5e_human_resolve_cancels_the_lead(self):
        """T5e: asserts a human fail frees the slot, cancels the scope, and blocks a second resolve.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        planner = self._planner(["block"])
        goal_id, _snap, decision = self._park(app, "t5e-human")
        action = _action_of(decision)
        decision_id = decision["decision_id"]
        runner = self._make_runner(app, planner, backend, token="tokA")
        snap, task, decision = _load_decision(app, goal_id, decision_id)
        self._step(runner, snap, task, decision, action)
        self.assertTrue(planner.wait_blocked(action["request_id"], timeout=1.0))
        self.assertEqual(runner.live_jobs(), 1)
        app.resolve(goal_id, decision_id, {"verdict": "fail", "reason": "human"})
        self.assertEqual(runner.live_jobs(), 0)
        self._wait_scope(planner)
        self.assertIn("lead_review_superseded", _events(app, goal_id, decision_id))
        rows = self._wait_event(app, goal_id, decision_id, "lead_review_superseded")
        self.assertEqual(rows[0].get("event"), "lead_review_superseded")
        self.assertEqual(rows[0].get("decision_id"), decision_id)
        self.assertEqual(rows[0].get("request_id"), action["request_id"])
        before = len(backend.resolve_calls)
        self.assertEqual(_verdicts(backend), ["fail"])
        planner.release_all()
        planner.wait_completed(1, timeout=1.0)
        snap, task, decision = _load_decision(app, goal_id, decision_id)
        self._step(runner, snap, task, decision, action)
        self.assertEqual(len(backend.resolve_calls), before)
        self.assertNotIn("pass", _verdicts(backend))
        self.assertEqual(decision.get("verdict"), "fail")
        self.assertEqual(decision.get("reason"), "human")
        self.assertEqual(_pending_ids(snap), [])

    def test_C2a_conflicting_verdict_is_discarded(self):
        """C2a: asserts a raced lead pass is discarded and durable keeps the backend fail.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        planner = self._planner(["block_pass"])
        goal_id, _snap, decision = self._park(app, "c2a-conflict")
        action = _action_of(decision)
        decision_id = decision["decision_id"]
        runner = self._make_runner(app, planner, backend, token="tokA")
        snap, task, decision = _load_decision(app, goal_id, decision_id)
        self._step(runner, snap, task, decision, action)
        self.assertTrue(planner.wait_blocked(action["request_id"], timeout=1.0))
        # §6.1 reads review_outcome before resolve_decision. The conflict is
        # injected on the next resolve so that read is still None.
        self.assertIsNone(backend.review_outcome(action["request_id"]))
        backend.race_conflict_once = ("fail", "human")
        planner.release_request(action["request_id"])
        self.assertTrue(planner.wait_completed(1, timeout=1.0))

        def _done(last, snap, decision):
            return decision.get("status") == "resolved"

        _last, _snap, decision = self._drive(runner, app, goal_id, decision_id, action, _done)
        passes = [row for row in backend.resolve_calls if row.get("verdict") == "pass"]
        self.assertEqual(len(passes), 1)
        self.assertEqual(passes[0]["answers"], [{"by": "lead"}])
        self.assertEqual(_verdicts(backend), ["pass"])
        self.assertNotIn("unavailable", _verdicts(backend))
        outcome = backend.review_outcome(action["request_id"])
        self.assertEqual(outcome["verdict"], "fail")
        self.assertEqual(outcome["reason"], "human")
        self.assertEqual(decision.get("verdict"), outcome["verdict"])
        self.assertEqual(decision.get("reason"), outcome["reason"])
        self.assertEqual(decision.get("verdict"), "fail")
        self.assertNotEqual(decision.get("verdict"), "pass")
        self.assertEqual(_lead_review(decision).get("status"), "stale_discarded")
        self.assertIn("lead_review_stale_result", _events(app, goal_id, decision_id))
        rows = self._wait_event(app, goal_id, decision_id, "lead_review_stale_result")
        self.assertEqual(rows[0].get("event"), "lead_review_stale_result")
        self.assertEqual(rows[0].get("decision_id"), decision_id)
        self.assertEqual(rows[0].get("request_id"), action["request_id"])
        self.assertTrue(str(rows[0].get("error") or ""))

    def test_C2b_stale_round_closes_durable_without_unavailable(self):
        """C2b: asserts an old-round lead result is discarded and round 2 stays untouched.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        planner = self._planner(["block_pass"])
        goal_id, _snap, decision = self._park(app, "c2b-stale-round")
        action = _action_of(decision)
        decision_id = decision["decision_id"]
        request_id = action["request_id"]
        run_id, round_no = _parse_request_id(request_id)
        self.assertEqual(round_no, 1)
        runner = self._make_runner(app, planner, backend, token="tokA")
        snap, task, decision = _load_decision(app, goal_id, decision_id)
        self._step(runner, snap, task, decision, action)
        self.assertTrue(planner.wait_blocked(request_id, timeout=1.0))
        backend.advance_round(run_id, round=2, state="running")
        before = backend.review_view(run_id)
        self.assertEqual(before["round"], 2)
        self.assertEqual(before["state"], "running")
        planner.release_request(request_id)
        self.assertTrue(planner.wait_completed(1, timeout=1.0))

        def _done(last, snap, decision):
            return decision.get("status") == "resolved"

        _last, _snap, decision = self._drive(runner, app, goal_id, decision_id, action, _done)
        self.assertEqual(backend.review_view(run_id), before)
        self.assertNotIn("unavailable", _verdicts(backend))
        self.assertIn("pass", _verdicts(backend))
        self.assertEqual(decision.get("verdict"), "reject")
        self.assertTrue(str(decision.get("reason") or "").startswith("stale review request discarded"))
        self.assertEqual(_lead_review(decision).get("status"), "stale_discarded")
        self.assertIn("lead_review_stale_result", _events(app, goal_id, decision_id))
        rows = self._wait_event(app, goal_id, decision_id, "lead_review_stale_result")
        self.assertEqual(rows[0].get("event"), "lead_review_stale_result")
        self.assertEqual(rows[0].get("decision_id"), decision_id)
        self.assertIsNone(backend.review_outcome(request_id))

    def test_C3a_crash_after_annotate_restarts_at_attempt_2(self):
        """C3a: asserts a thread-start crash keeps attempt 1 and the next runner submits attempt 2.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        planner = self._planner(["pass:after restart"])
        goal_id, _snap, decision = self._park(app, "c3a-crash")
        action = _action_of(decision)
        decision_id = decision["decision_id"]
        runner_a = self._make_runner(app, planner, backend, token="tokA")
        seen: dict = {}

        def _boom(job):
            _snap, _task, current = _load_decision(app, goal_id, decision_id)
            lead = _lead_review(current)
            seen["status"] = lead.get("status")
            seen["owner"] = lead.get("owner")
            seen["attempt"] = lead.get("attempt")
            raise RuntimeError("thread_start_failed")

        runner_a._start_thread = _boom
        snap, task, decision = _load_decision(app, goal_id, decision_id)
        self._step(runner_a, snap, task, decision, action)
        self.assertEqual(seen.get("status"), "running")
        self.assertEqual(seen.get("owner"), "tokA")
        self.assertEqual(seen.get("attempt"), 1)
        _snap, _task, decision = _load_decision(app, goal_id, decision_id)
        lead = _lead_review(decision)
        running_left = lead.get("status") == "running" and lead.get("owner") == "tokA"
        failed = (
            lead.get("status") == "failed"
            and isinstance(lead.get("last_error"), dict)
            and lead["last_error"].get("code") == "thread_start_failed"
        )
        self.assertTrue(running_left or failed, lead)
        self.assertEqual(lead.get("attempt"), 1)
        self.assertEqual(planner.calls, 0)
        runner_b = self._make_runner(app, planner, backend, token="tokB")
        snap, task, decision = _load_decision(app, goal_id, decision_id)
        out, _elapsed = self._step(runner_b, snap, task, decision, action)
        self.assertEqual(out.get("action"), "lead_reviewing")
        self.assertTrue(planner.wait_completed(1, timeout=1.0))
        _snap, _task, decision = _load_decision(app, goal_id, decision_id)
        lead = _lead_review(decision)
        self.assertEqual(lead.get("attempt"), 2)
        self.assertGreaterEqual(int(lead.get("attempt") or 0), 1)
        self.assertEqual(planner.calls, 1)
        if running_left:
            statuses = [item.get("status") for item in (lead.get("history") or []) if isinstance(item, dict)]
            self.assertIn("interrupted", statuses)

    def test_C3b_queued_restart_submits_attempt_1(self):
        """C3b: asserts a queued decision consumed no attempt and the next runner submits exactly one call.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        planner = self._planner(["pass:from restart"])
        goal_id, _snap, decision = self._park(app, "c3b-queued")
        action = _action_of(decision)
        decision_id = decision["decision_id"]
        runner_a = self._make_runner(app, planner, backend, token="tokA")
        runner_a.MAX_CONCURRENT_REVIEWS = 0
        snap, task, decision = _load_decision(app, goal_id, decision_id)
        out, _elapsed = self._step(runner_a, snap, task, decision, action)
        self.assertEqual(out.get("action"), "lead_review_queued")
        _snap, _task, decision = _load_decision(app, goal_id, decision_id)
        lead = _lead_review(decision)
        self.assertEqual(lead.get("status"), "queued")
        self.assertEqual(int(lead.get("attempt") or 0), 0)
        self.assertEqual(planner.calls, 0)
        self.assertEqual(runner_a.live_jobs(), 0)
        runner_b = self._make_runner(app, planner, backend, token="tokB")
        snap, task, decision = _load_decision(app, goal_id, decision_id)
        out, _elapsed = self._step(runner_b, snap, task, decision, action)
        self.assertEqual(out.get("action"), "lead_reviewing")
        self.assertTrue(planner.wait_completed(1, timeout=1.0))
        _snap, _task, decision = _load_decision(app, goal_id, decision_id)
        lead = _lead_review(decision)
        self.assertEqual(lead.get("status"), "running")
        self.assertEqual(lead.get("attempt"), 1)
        self.assertEqual(lead.get("owner"), "tokB")
        self.assertEqual(planner.calls, 1)

    def test_C3c_concurrency_queue_and_stop_goal(self):
        """C3c: asserts four running reviews, one queued, and that finishing or stop_goal frees a slot.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        planner = self._planner(["block_pass"] * 5)
        runner = self._make_runner(app, planner, backend, token="tokA")
        runner.MAX_CONCURRENT_REVIEWS = 4
        parked = []
        for index in range(5):
            goal_id, _snap, decision = self._park(app, f"c3c-{index}")
            parked.append((goal_id, decision["decision_id"], _action_of(decision)))
        running = []
        queued = None
        for goal_id, decision_id, action in parked:
            snap, task, decision = _load_decision(app, goal_id, decision_id)
            out, _elapsed = self._step(runner, snap, task, decision, action)
            if out.get("action") == "lead_review_queued":
                queued = (goal_id, decision_id, action)
            else:
                self.assertEqual(out.get("action"), "lead_reviewing")
                running.append((goal_id, decision_id, action))
        self.assertIsNotNone(queued)
        self.assertEqual(len(running), 4)
        self.assertEqual(runner.live_jobs(), 4)
        for goal_id, decision_id, action in running:
            self.assertTrue(planner.wait_blocked(action["request_id"], timeout=1.0))
            _snap, _task, decision = _load_decision(app, goal_id, decision_id)
            self.assertEqual(_lead_review(decision).get("status"), "running")
        q_goal, q_id, q_action = queued
        _snap, _task, q_decision = _load_decision(app, q_goal, q_id)
        self.assertEqual(_lead_review(q_decision).get("status"), "queued")
        self.assertEqual(int(_lead_review(q_decision).get("attempt") or 0), 0)

        finish_goal, finish_id, finish_action = running[0]
        planner.release_request(finish_action["request_id"])

        def _resolved(last, snap, decision):
            return decision.get("status") == "resolved" and decision.get("verdict") == "pass"

        self._drive(runner, app, finish_goal, finish_id, finish_action, _resolved)
        self.assertEqual(runner.live_jobs(), 3)
        snap, task, decision = _load_decision(app, q_goal, q_id)
        out, _elapsed = self._step(runner, snap, task, decision, q_action)
        self.assertEqual(out.get("action"), "lead_reviewing")
        self.assertTrue(planner.wait_blocked(q_action["request_id"], timeout=1.0))
        self.assertEqual(runner.live_jobs(), 4)
        _snap, _task, decision = _load_decision(app, q_goal, q_id)
        self.assertEqual(_lead_review(decision).get("status"), "running")
        self.assertEqual(_lead_review(decision).get("attempt"), 1)

        stop_goal, stop_id, _stop_action = running[1]
        before = runner.live_jobs()
        runner.stop_goal(stop_goal, "stop one")
        self.assertEqual(runner.live_jobs(), before - 1)
        self.assertIn("lead_review_stopped", _events(app, stop_goal, stop_id))
        stopped = self._wait_event(app, stop_goal, stop_id, "lead_review_stopped")
        self.assertEqual(stopped[0].get("event"), "lead_review_stopped")
        self.assertEqual(stopped[0].get("decision_id"), stop_id)
        self.assertIn("stop one", str(stopped[0].get("reason") or ""))

    def test_C4r_recovers_backend_outcome_without_calling_lead(self):
        """C4r: asserts an existing backend fail resolves durable state with no lead call and no second resolve.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        planner = self._planner(["pass:should not run"])
        goal_id, _snap, decision = self._park(app, "c4r-recover")
        action = _action_of(decision)
        decision_id = decision["decision_id"]
        backend.resolve_decision(action["request_id"], verdict="fail", reason="backend already applied")
        self.assertEqual(backend.review_outcome(action["request_id"])["verdict"], "fail")
        seeded = len(backend.resolve_calls)
        runner = self._make_runner(app, planner, backend, token="tokA")
        snap, task, decision = _load_decision(app, goal_id, decision_id)
        out, _elapsed = self._step(runner, snap, task, decision, action)
        self.assertEqual(out.get("action"), "decision_resolved")
        self.assertTrue(out.get("recovered"))
        self.assertEqual(planner.calls, 0)
        self.assertEqual(len(backend.resolve_calls), seeded)
        _snap, _task, decision = _load_decision(app, goal_id, decision_id)
        self.assertEqual(decision.get("status"), "resolved")
        self.assertEqual(decision.get("verdict"), "fail")
        self.assertEqual(decision.get("reason"), "backend already applied")
        self.assertEqual(_lead_review(decision).get("status"), "done")
        self.assertEqual(_pending_ids(_snap), [])

    def test_T9b_permission_and_incapable_backends_are_not_owned(self):
        """T9b: asserts permission actions are not owned, and review is not owned without mode or decide_action.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        planner = self._planner([])
        runner = self._make_runner(app, planner, backend, token="tokA")
        review = {
            "kind": "review",
            "request_id": "agyrev:agy_0123456789ab:r1",
            "payload": {},
            "context_hash": "h",
        }
        permission = {"kind": "permission", "request_id": "perm-1", "payload": {}, "context_hash": "h"}
        self.assertTrue(runner.owns(review))
        self.assertFalse(runner.owns(permission))
        bare = _BackendWithoutMode()
        runner_bare = self._make_runner(app, planner, bare, token="tokB")
        self.assertFalse(runner_bare.owns(review))
        runner_det = self._make_runner(app, DeterministicPlanner(), backend, token="tokC")
        self.assertFalse(runner_det.owns(review))

    def test_ST_stop_all_reports_the_blocked_job(self):
        """ST: asserts stop_all returns the blocked decision and records lead_review_stopped.
        red on 49fa3a36: framework.lead_review_runner is missing.
        """
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        planner = self._planner(["block"])
        goal_id, _snap, decision = self._park(app, "st-stop")
        action = _action_of(decision)
        decision_id = decision["decision_id"]
        runner = self._make_runner(app, planner, backend, token="tokA")
        snap, task, decision = _load_decision(app, goal_id, decision_id)
        self._step(runner, snap, task, decision, action)
        self.assertTrue(planner.wait_blocked(action["request_id"], timeout=1.0))
        self.assertEqual(runner.live_jobs(), 1)
        box: dict = {}

        def _run() -> None:
            try:
                box["out"] = runner.stop_all("service shutdown")
            except BaseException as exc:  # noqa: BLE001
                box["exc"] = exc

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(4.0)
        self.assertFalse(thread.is_alive(), "stop_all blocked")
        if "exc" in box:
            raise box["exc"]
        rows = box["out"]
        self.assertIsInstance(rows, list)
        matched = [row for row in rows if isinstance(row, dict) and row.get("decision_id") == decision_id]
        self.assertEqual(len(matched), 1)
        self.assertIn("service shutdown", str(matched[0].get("reason") or ""))
        self.assertEqual(runner.live_jobs(), 0)
        self.assertIn("lead_review_stopped", _events(app, goal_id, decision_id))
        stopped = self._wait_event(app, goal_id, decision_id, "lead_review_stopped")
        self.assertEqual(stopped[0].get("event"), "lead_review_stopped")
        self.assertEqual(stopped[0].get("decision_id"), decision_id)
        self.assertIn("service shutdown", str(stopped[0].get("reason") or ""))
        self._wait_scope(planner)


if __name__ == "__main__":
    unittest.main()
