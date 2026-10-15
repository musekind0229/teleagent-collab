"""Minimal simple collab run entry (PM simple real loop).

Pipeline:
  Goal Input -> Pluggable LeadAdapter planning -> ExecutionBackend execution (AGY/inprocess)
  -> Raw results and existing artifacts saved & returned to Lead -> Lead review (pass/rework/diagnose)
  -> Durable state transitions, pending consumption files, and callbacks.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from framework.app_service import (
    AppError,
    CollabApplication,
    DeterministicPlanner,
    LeadAdapterPlanner,
    _lead_failure_of,
    lead_error_retryable,
)
from framework.durable_api import DurableLayer
from execution_backend import get_execution_backend
from execution_backend.base import ExecutionBackend
from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend
from execution_backend.inprocess_v1 import InProcessExecutionBackend
from lead_adapter import get_lead_adapter
from lead_adapter.base import LeadAdapter
from lead_adapter.inprocess import InProcessLeadAdapter
from lead_adapter.schema import (
    LeadDecisionError,
    build_lead_request,
    lead_review_response_schema,
    unwrap_structured,
    validate_lead_decision,
)


def build_default_goal_request(
    *,
    title: str,
    goal: str,
    artifacts: list[str] | None = None,
    must: list[str] | None = None,
    must_not: list[str] | None = None,
    max_reworks: int = 2,
    wall_sec: float = 120.0,
    command: dict[str, Any] | None = None,
    idempotency_key: str = "",
    model: str = "",
    lead_model: str = "",
) -> dict[str, Any]:
    """Construct a clean, valid Goal request mapping."""
    arts = list(artifacts) if artifacts is not None else ["delivery.txt"]
    body: dict[str, Any] = {
        "idempotency_key": idempotency_key or f"simple-collab-{time.time_ns()}",
        "client_id": "simple-collab-runner",
        "title": str(title or "simple-collab goal"),
        "goal": str(goal or title or "execute outcome"),
        "boundaries": {
            "must": list(must or ["Stay inside assigned workspace"]),
            "must_not": list(must_not or ["Do not delete outside files", "Do not deploy"]),
        },
        "acceptance": {
            "artifacts": arts,
        },
        "budget": {
            "wall_sec": float(wall_sec),
            "max_reworks": int(max_reworks),
        },
    }
    if command is not None:
        body["acceptance"]["command"] = command
    if model:
        body["model"] = model
    if lead_model:
        body["lead_model"] = lead_model
    return body


class SimpleCollabRunner:
    """Minimal loop orchestrator coordinating Pluggable Lead + ExecutionBackend."""

    def __init__(
        self,
        persist_dir: str | Path,
        *,
        planner: Any = None,
        backend: ExecutionBackend | None = None,
        max_parallel_per_goal: int = 1,
        max_parallel_global: int = 2,
    ) -> None:
        self.persist_root = Path(persist_dir).resolve()
        self.persist_root.mkdir(parents=True, exist_ok=True)
        self.backend = backend or InProcessExecutionBackend()
        self.planner = planner or DeterministicPlanner()
        self.app = CollabApplication(
            self.persist_root,
            planner=self.planner,
            backend=self.backend,
            max_parallel_per_goal=max_parallel_per_goal,
            max_parallel_global=max_parallel_global,
        )

        # Directory for persisted consumable decisions & durable event notifications
        self.consumed_decisions_dir = self.persist_root / "consumed_decisions"
        self.consumed_decisions_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir = self.persist_root / "events"
        self.events_dir.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        """Cleanly close backend resources/processes."""
        if hasattr(self.backend, "close") and callable(getattr(self.backend, "close")):
            try:
                self.backend.close()
            except Exception:
                pass

    def __enter__(self) -> SimpleCollabRunner:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _collect_partial_artifacts(self, workspace_path: str) -> list[str]:
        """Collect existing files in workspace relative to workspace root (bounded, no symlinks)."""
        if not workspace_path:
            return []
        ws = Path(workspace_path)
        if not ws.is_dir():
            return []
        found: list[str] = []
        try:
            for root, dirs, files in os.walk(ws, followlinks=False):
                dirs[:] = [d for d in dirs if not d.startswith(".collab")]
                for f in files:
                    full_p = Path(root) / f
                    if not full_p.is_symlink() and full_p.is_file():
                        try:
                            rel = full_p.relative_to(ws).as_posix()
                            found.append(rel)
                        except ValueError:
                            pass
        except OSError:
            pass
        return sorted(found)

    def _persist_consumed_decision(self, app_id: str, payload: dict[str, Any]) -> None:
        """Atomically persist a consumed decision by unique application_id to prevent double-consumption."""
        dest = self.consumed_decisions_dir / f"{app_id}.json"
        tmp = self.consumed_decisions_dir / f"{app_id}.json.tmp.{time.time_ns()}"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(dest)

    def _is_decision_consumed(self, app_id: str) -> bool:
        dest = self.consumed_decisions_dir / f"{app_id}.json"
        return dest.is_file()

    def _persist_event(self, event_id: str, event_data: dict[str, Any]) -> None:
        """Atomically record durable event on disk."""
        dest = self.events_dir / f"{event_id}.json"
        tmp = self.events_dir / f"{event_id}.json.tmp.{time.time_ns()}"
        tmp.write_text(json.dumps(event_data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(dest)

    def get_pending_events(self) -> list[dict[str, Any]]:
        """Return all persisted events that are pending or errored."""
        results: list[dict[str, Any]] = []
        if not self.events_dir.is_dir():
            return results
        for p in self.events_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if data.get("status") in {"pending", "error"}:
                    results.append(data)
            except Exception:
                pass
        results.sort(key=lambda x: float(x.get("timestamp") or 0))
        return results

    def dispatch_event(
        self,
        event_id: str,
        callback: Callable[[dict[str, Any]], None],
    ) -> bool:
        """Attempt to dispatch one durable event. If callback succeeds, status=acked.
        If already acked, returns True idempotently without re-invoking callback."""
        dest = self.events_dir / f"{event_id}.json"
        if not dest.is_file():
            return False
        try:
            event_data = json.loads(dest.read_text(encoding="utf-8"))
        except Exception:
            return False

        if event_data.get("status") == "acked":
            # Already acknowledged: idempotent no-op
            return True

        try:
            callback(event_data)
            event_data["status"] = "acked"
            event_data["acked_at"] = time.time()
            event_data.pop("last_error", None)
            self._persist_event(event_id, event_data)
            return True
        except Exception as exc:
            event_data["status"] = "pending"
            event_data["last_error"] = f"{type(exc).__name__}: {exc}"
            event_data["failed_at"] = time.time()
            self._persist_event(event_id, event_data)
            return False

    def redeliver_pending_events(
        self,
        callback: Callable[[dict[str, Any]], None],
    ) -> list[str]:
        """Redeliver all unacked/pending events and return list of acked event IDs."""
        acked_ids: list[str] = []
        for ev in self.get_pending_events():
            eid = str(ev.get("event_id") or "")
            if eid and self.dispatch_event(eid, callback):
                acked_ids.append(eid)
        return acked_ids

    def _conduct_lead_review(
        self,
        *,
        goal_snap: Mapping[str, Any],
        task: Mapping[str, Any],
        worker_result: Mapping[str, Any],
        partial_artifacts: list[str],
        reason_hint: str = "",
        continuation_required: bool = False,
    ) -> dict[str, Any]:
        """Request review / judgment / next_action from current lead adapter or decider."""
        goal_id = str(goal_snap.get("goal_id") or task.get("goal_id") or "")
        task_id = str(task.get("task_id") or "")
        run_id = str(task.get("run_id") or worker_result.get("run_id") or "")
        workspace = str(task.get("workspace") or worker_result.get("workspace") or "")

        # Distinguish progress / timeout from actual terminal failure / success:
        # 1. Scope / event_phase differentiates "progress" vs "terminal"
        # 2. Content fingerprint over (status, error, partial_artifacts) ensures
        #    progress/timeout doesn't swallow final error and new partial artifacts!
        event_phase = "progress" if continuation_required else "terminal"
        sig_data = {
            "status": str(worker_result.get("status") or task.get("status") or ""),
            "error": str(worker_result.get("error") or worker_result.get("response") or ""),
            "artifacts": sorted(partial_artifacts),
        }
        sig_hash = hashlib.sha256(json.dumps(sig_data, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        app_id = f"rev_{goal_id}_{task_id}_{run_id or 'norun'}_{event_phase}_{sig_hash}"

        if self._is_decision_consumed(app_id):
            # Already consumed for this exact phase and result signature, return saved record
            try:
                content = (self.consumed_decisions_dir / f"{app_id}.json").read_text(encoding="utf-8")
                return json.loads(content)
            except Exception:
                pass

        # Check if planner has decide_action or underlying adapter
        adapter = getattr(self.planner, "adapter", None)
        decider = getattr(self.planner, "decide_action", None)

        verdict = "fail"
        reason = reason_hint or str(worker_result.get("error") or "worker completed with errors")
        next_action = "diagnose"
        next_action_source = "fallback"
        lead_status = "ok"
        details: dict[str, Any] = {}

        action_payload = {
            "task_id": task_id,
            "run_id": run_id,
            "workspace": workspace,
            "worker_result": dict(worker_result),
            "partial_artifacts": partial_artifacts,
            "continuation_required": continuation_required,
            "error": worker_result.get("error"),
            "status": worker_result.get("status") or task.get("status"),
        }

        if adapter is not None and hasattr(adapter, "decide"):
            goal_boundaries = (goal_snap.get("goal") or {}).get("boundaries") or {}
            lead_req = build_lead_request(
                kind="review",
                goal=str(task.get("title") or (goal_snap.get("goal") or {}).get("desired_outcome") or ""),
                authorized_scope=list(goal_boundaries.get("must") or ["Stay inside assigned workspace"]),
                prohibitions=list(goal_boundaries.get("must_not") or []),
                acceptance_criteria=dict(task.get("done_when") or (goal_snap.get("goal") or {}).get("acceptance") or {}),
                current_application={
                    "application_id": app_id,
                    "goal_id": goal_id,
                    "task_id": task_id,
                    "run_id": run_id,
                    "workspace": workspace,
                    "partial_artifacts": partial_artifacts,
                },
                application_id=app_id,
                extra={
                    "worker_result": dict(worker_result),
                    "partial_artifacts": partial_artifacts,
                    "continuation_required": continuation_required,
                    "worker_request": action_payload,
                },
            )
            schema = lead_review_response_schema()
            cwd = str(getattr(self.planner, "cwd", self.persist_root))
            raw, parsed = adapter.decide(lead_req, schema=schema, cwd=cwd)

            # Check safe_failure or adapter error
            if isinstance(parsed, Mapping) and parsed.get("_lead_status"):
                status_val = str(parsed.get("_lead_status"))
                lead_status = status_val
                verdict = "fail"
                reason = f"lead adapter status: {status_val}: " + str(parsed.get("error") or "")
                details = dict(parsed)
                next_action = "diagnose"
                next_action_source = "suggested"
            else:
                unwrapped = unwrap_structured(parsed) if isinstance(parsed, Mapping) else None
                try:
                    decision = validate_lead_decision(raw, parsed, request=lead_req, kind="review")
                    details = dict(decision)
                    lead_status = "ok"
                    verdict = str(decision.get("verdict") or decision.get("decision") or "fail").lower()
                    reason = str(decision.get("reason") or reason)
                    # Check whether lead explicitly returned next_action in the inner unwrapped payload or decision
                    raw_action = (unwrapped.get("next_action") if isinstance(unwrapped, Mapping) else None) or decision.get("next_action")
                    if raw_action:
                        next_action = str(raw_action)
                        next_action_source = "lead"
                    else:
                        next_action = "continue" if verdict == "pass" else "diagnose"
                        next_action_source = "suggested"
                except LeadDecisionError as exc:
                    # Invalid lead reply / wrapper mismatch / missing reason
                    if isinstance(unwrapped, Mapping) and ("verdict" in unwrapped or "decision" in unwrapped):
                        details = dict(unwrapped)
                        lead_status = "unwrapped"
                        verdict = str(unwrapped.get("verdict") or unwrapped.get("decision") or "fail").lower()
                        reason = str(unwrapped.get("reason") or reason)
                        if "next_action" in unwrapped and unwrapped["next_action"]:
                            next_action = str(unwrapped["next_action"])
                            next_action_source = "lead"
                        else:
                            next_action = "continue" if verdict == "pass" else "diagnose"
                            next_action_source = "suggested"
                    else:
                        details = {"raw": raw, "error": str(exc), "code": exc.code}
                        lead_status = "error"
                        verdict = "fail"
                        reason = f"lead decision validation failed [{exc.code}]: {exc}"
                        next_action = "diagnose"
                        next_action_source = "suggested"
        elif callable(decider):
            action = {
                "kind": "review",
                "payload": action_payload,
                "review_hint": reason_hint,
            }
            try:
                dec = decider(goal_snap, task, action)
                if isinstance(dec, Mapping):
                    details = dict(dec)
                    verdict = str(dec.get("verdict") or "fail").lower()
                    reason = str(dec.get("reason") or reason)
                    if "next_action" in dec and dec["next_action"]:
                        next_action = str(dec["next_action"])
                        next_action_source = "lead"
                    else:
                        next_action = "continue" if verdict == "pass" else "diagnose"
                        next_action_source = "suggested"
            except Exception as e:
                verdict = "fail"
                reason = f"decider raised {e}"
                next_action = "diagnose"
                next_action_source = "fallback"
                lead_status = "error"
        else:
            # Deterministic / fallback logic
            if worker_result.get("ok"):
                verdict = "pass"
                reason = "worker execution completed successfully"
                next_action = "complete"
                next_action_source = "fallback"
            else:
                verdict = "fail"
                reason = str(worker_result.get("error") or "worker execution failed")
                next_action = "diagnose"
                next_action_source = "fallback"

        decision_rec = {
            "application_id": app_id,
            "goal_id": goal_id,
            "task_id": task_id,
            "run_id": run_id,
            "workspace": workspace,
            "partial_artifacts": partial_artifacts,
            "verdict": verdict,
            "reason": reason,
            "next_action": next_action,
            "next_action_source": next_action_source,
            "lead_status": lead_status,
            "raw_result": dict(worker_result),
            "lead_details": details,
            "consumed_at": time.time(),
        }

        # If lead call experienced an error/call_failed/timeout, we do NOT fake lead consumption success:
        # We only persist as consumed if lead returned a valid decision (ok or unwrapped)
        if lead_status in {"ok", "unwrapped"}:
            self._persist_consumed_decision(app_id, decision_rec)
        return decision_rec

    def run_goal(
        self,
        request: Mapping[str, Any],
        *,
        timeout_sec: float = 60.0,
        poll_interval: float = 0.05,
        decision_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Submit goal, drive coordinator until terminal or timeout, guarantee lead review & durable notification."""
        sub = self.app.submit(dict(request))
        if not sub.get("ok"):
            return {
                "ok": False,
                "action": "submit_failed",
                "error": sub.get("error") or "submit_failed",
                "response": sub,
            }
        goal_id = str(sub["goal_id"])
        deadline = time.time() + float(timeout_sec)
        timed_out = False

        while time.time() < deadline:
            self.app.coordinator.process_goal(goal_id)
            st = self.app.status(goal_id)
            state = st.get("state")
            if state in {"completed", "failed", "cancelled"}:
                break
            time.sleep(poll_interval)
        else:
            timed_out = True

        st = self.app.status(goal_id)
        snap = self.app.layer.get_goal(goal_id).get("goal") or {}
        tasks = list(st.get("tasks") or snap.get("tasks") or [])

        lead_reviews: list[dict[str, Any]] = []
        has_continuation = False

        for t in tasks:
            task_status = str(t.get("status") or "")
            task_result = t.get("result") if isinstance(t.get("result"), Mapping) else {}
            existing_review = task_result.get("review") if isinstance(task_result, Mapping) else None
            ws = str(t.get("workspace") or task_result.get("workspace") or "")
            partial_artifacts = self._collect_partial_artifacts(ws)

            # Check if this task is still running when timeout arrived
            task_running_on_timeout = timed_out and task_status in {"running", "executing", "dispatched"}
            if task_running_on_timeout:
                has_continuation = True

            # If the task completed (passed or failed) or timed out while running:
            # check if a lead review was recorded. If not, trigger lead review now!
            if existing_review is None or not existing_review.get("source") == "lead" or task_running_on_timeout:
                res_to_review = dict(task_result)
                if not res_to_review:
                    res_to_review = {
                        "ok": False,
                        "status": task_status,
                        "error": "observation timeout reached" if task_running_on_timeout else "task completed without result",
                        "workspace": ws,
                    }
                review_rec = self._conduct_lead_review(
                    goal_snap=snap,
                    task=t,
                    worker_result=res_to_review,
                    partial_artifacts=partial_artifacts,
                    reason_hint="observation timeout reached while worker running" if task_running_on_timeout else "",
                    continuation_required=task_running_on_timeout,
                )
                lead_reviews.append(review_rec)
            elif existing_review is not None:
                # Review was already executed by coordinator
                lead_reviews.append({
                    "task_id": t.get("task_id"),
                    "verdict": "pass" if existing_review.get("status") == "passed" else "fail",
                    "reason": existing_review.get("evidence") or "",
                    "next_action": "continue" if existing_review.get("status") == "passed" else "rework",
                    "partial_artifacts": partial_artifacts,
                    "workspace": ws,
                })

        final_status = self.app.status(goal_id)
        report = self.app.report(goal_id)
        final_state = final_status.get("state")

        # In case of observation timeout: DO NOT auto-cancel the goal!
        if timed_out and final_state not in {"completed", "failed", "cancelled"}:
            final_state = "running"
            final_status["state"] = "running"

        # Construct durable completion/progress event: initially pending
        event_id = f"evt_{goal_id}_{int(time.time() * 1000)}"
        event_payload = {
            "event_id": event_id,
            "goal_id": goal_id,
            "state": final_state,
            "ok": final_state == "completed",
            "timed_out": timed_out,
            "continuation_required": has_continuation,
            "lead_reviews": lead_reviews,
            "tasks": final_status.get("tasks"),
            "timestamp": time.time(),
            "status": "pending",
        }
        self._persist_event(event_id, event_payload)

        # Attempt immediate dispatch if decision_callback is provided
        if decision_callback is not None:
            self.dispatch_event(event_id, decision_callback)

        return {
            "ok": final_state == "completed",
            "goal_id": goal_id,
            "state": final_state,
            "status": final_status,
            "report": report,
            "is_duplicate": bool(sub.get("duplicate")),
            "timed_out": timed_out,
            "continuation_required": has_continuation,
            "lead_reviews": lead_reviews,
            "event_id": event_id,
        }
