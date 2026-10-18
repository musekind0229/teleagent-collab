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
from framework.acceptance_command import execute_command, validate_command


def build_behavior_evidence(
    *,
    command: Mapping[str, Any] | None,
    execution: Mapping[str, Any] | None,
    input_purpose: str = "",
    output_protection: Mapping[str, Any] | None = None,
    expected_assertions: list[str] | None = None,
    actual_assertions: list[str] | None = None,
    worker_self_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct a structured, reproducible behavior evidence record."""
    cmd_spec = dict(command) if isinstance(command, Mapping) else {}
    exec_res = dict(execution) if isinstance(execution, Mapping) else {}
    actual_cmd = exec_res.get("argv") or cmd_spec.get("argv") or []
    real_rc = exec_res.get("returncode")
    timed_out = bool(exec_res.get("timed_out"))
    configured = bool(exec_res.get("configured", bool(cmd_spec)))
    executed = "returncode" in exec_res and exec_res.get("returncode") is not None

    evidence: dict[str, Any] = {
        "actual_command": actual_cmd,
        "argv": actual_cmd,
        "cwd": str(exec_res.get("cwd") or cmd_spec.get("cwd") or "."),
        "real_rc": real_rc,
        "returncode": real_rc,
        "configured": configured,
        "executed": executed,
        "timed_out": timed_out,
        "input_purpose": str(input_purpose or "acceptance verification"),
        "output_protection": dict(output_protection or {
            "preserve_overwritten_by_empty": False,
            "details": "no preserve violations detected",
        }),
        "expected_assertions": list(expected_assertions or []),
        "actual_assertions": list(actual_assertions or []),
        "stdout": str(exec_res.get("stdout") or ""),
        "stderr": str(exec_res.get("stderr") or ""),
        "stdout_sha256": exec_res.get("stdout_sha256") or hashlib.sha256(str(exec_res.get("stdout") or "").encode("utf-8")).hexdigest(),
        "stderr_sha256": exec_res.get("stderr_sha256") or hashlib.sha256(str(exec_res.get("stderr") or "").encode("utf-8")).hexdigest(),
        "stdout_bytes": exec_res.get("stdout_total_bytes", len(str(exec_res.get("stdout") or "").encode("utf-8"))),
        "stderr_bytes": exec_res.get("stderr_total_bytes", len(str(exec_res.get("stderr") or "").encode("utf-8"))),
        "self_reported_vs_native": {
            "worker_self_report": dict(worker_self_report or {}),
            "native_run": {
                "argv": actual_cmd,
                "cwd": str(exec_res.get("cwd") or cmd_spec.get("cwd") or "."),
                "returncode": real_rc,
                "ok": exec_res.get("ok"),
                "timed_out": timed_out,
                "error": exec_res.get("error"),
            },
        },
    }
    return evidence


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

        # Extract acceptance command and behavior evidence
        goal_accept = (goal_snap.get("goal") or {}).get("acceptance") or {}
        task_accept = task.get("done_when") or {}
        # One effective caller contract for execution AND the review request.
        # Task fields extend/override ordinary goal fields. Explicit goal-owned
        # command/protection fields remain authoritative, including empty values.
        effective_accept = dict(goal_accept)
        effective_accept.update(task_accept)
        for caller_field in ("command", "output_protection"):
            if caller_field in goal_accept:
                effective_accept[caller_field] = goal_accept[caller_field]
        cmd_spec = effective_accept.get("command")

        # Protection semantics come ONLY from the caller contract, never filenames,
        # worker claims, or PRESERVE keywords. Currently one explicit invariant:
        # protected existing regular files must remain byte-for-byte unchanged.
        protection_contract = effective_accept.get("output_protection")
        protection_rows = []
        protection_unknown = False
        protection_active = protection_contract is not None
        if protection_active:
            paths = protection_contract.get("unchanged") if isinstance(protection_contract, Mapping) else None
            if (not isinstance(paths, list) or not paths or
                    any(not isinstance(x, str) or not x for x in paths) or len(set(paths)) != len(paths)):
                protection_unknown = True
                paths = []
            for rel in paths:
                row = {"path": rel, "invariant": "unchanged_bytes", "before": None, "after": None}
                try:
                    root = Path(workspace).resolve(strict=True)
                    target = root / rel
                    if Path(rel).is_absolute() or Path(rel).drive or ":" in rel or ".." in Path(rel).parts:
                        raise ValueError("invalid protected path")
                    cursor = root
                    for part in Path(rel).parts:
                        cursor = cursor / part
                        st = cursor.lstat()
                        if cursor.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
                            raise ValueError("reparse protected path")
                    if not target.is_file():
                        raise ValueError("baseline not regular file")
                    raw_bytes = target.read_bytes()
                    row["before"] = {"bytes": len(raw_bytes), "sha256": hashlib.sha256(raw_bytes).hexdigest(), "hex": raw_bytes.hex()}
                except (OSError, ValueError):
                    row["error"] = "baseline_unavailable"
                    protection_unknown = True
                protection_rows.append(row)

        # Caller-driven acceptance execution:
        # Never prioritize worker self-supplied acceptance_execution over caller execution.
        # Worker-supplied execution is recorded only as worker self-report evidence.
        worker_supplied_exec = worker_result.get("acceptance_execution")
        exec_res = None
        if cmd_spec and workspace and Path(workspace).is_dir() and not protection_unknown:
            evidence_root = self.persist_root / "evidence"
            binding = {"goal_id": goal_id, "task_id": task_id, "run_id": run_id}
            try:
                exec_res = execute_command(
                    cmd_spec,
                    workspace=workspace,
                    evidence_root=evidence_root,
                    binding=binding,
                )
            except Exception as exc:
                exec_res = {
                    "source": "caller_contract",
                    "binding": binding,
                    "configured": True,
                    "ok": False,
                    "returncode": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        elif worker_supplied_exec:
            # Command was configured or claimed, but caller execution did not run
            pass

        # Bind retained stream bytes to their own length/hash, not a worker log.
        if exec_res:
            for stream_name in ('stdout', 'stderr'):
                stream_path = exec_res.get(stream_name + '_path')
                if stream_path:
                    captured = Path(stream_path).read_bytes()
                    exec_res[stream_name + '_captured_bytes'] = len(captured)
                    exec_res[stream_name + '_sha256'] = hashlib.sha256(captured).hexdigest()
        protection_violated = False
        for row in protection_rows:
            if row["before"] is None:
                continue
            try:
                target = Path(workspace) / row["path"]
                cursor = Path(workspace)
                for part in Path(row["path"]).parts:
                    cursor = cursor / part
                    st = cursor.lstat()
                    if cursor.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
                        raise ValueError("reparse after action")
                raw_bytes = target.read_bytes()
                row["after"] = {"bytes": len(raw_bytes), "sha256": hashlib.sha256(raw_bytes).hexdigest(), "hex": raw_bytes.hex()}
                row["satisfied"] = row["before"] == row["after"]
            except (OSError, ValueError):
                row["satisfied"] = False
                row["error"] = "after_unavailable"
            protection_violated |= not row["satisfied"]
        output_prot = {
            "source": "caller_contract",
            "status": "unknown" if protection_unknown else ("not_configured" if not protection_active else ("failed" if protection_violated else "passed")),
            "contract": protection_contract,
            "snapshots": protection_rows,
            # Compatibility field now derives only from explicit caller invariant.
            "preserve_overwritten_by_empty": protection_violated,
            "details": "explicit unchanged-bytes invariant" if protection_active else "no protection invariant configured; protection not verified",
        }

        worker_self_report = {
            "status": worker_result.get("status") or task.get("status"),
            "ok": worker_result.get("ok"),
            "response": worker_result.get("response") or worker_result.get("output"),
            "error": worker_result.get("error"),
            "acceptance_execution": worker_supplied_exec,
            "behavior_evidence": worker_result.get("behavior_evidence"),
            "output_protection": worker_result.get("output_protection"),
        }

        # Build formal behavior evidence structure
        behavior_ev = build_behavior_evidence(
            command=cmd_spec,
            execution=exec_res,
            input_purpose=str(task.get("title") or goal_snap.get("goal", {}).get("desired_outcome") or "task execution"),
            output_protection=output_prot,
            expected_assertions=["returncode == 0", "explicit caller protection invariants satisfied when configured"],
            actual_assertions=[
                f"returncode == {exec_res.get('returncode')}" if exec_res else "no command execution",
                f"preserve_overwritten_by_empty == {output_prot.get('preserve_overwritten_by_empty') if output_prot else False}",
            ],
            worker_self_report=worker_self_report,
        )


        # Check command execution and evidence presence
        command_configured = bool(cmd_spec)
        command_executed = bool(exec_res and exec_res.get("executed", exec_res.get("returncode") is not None))
        command_failed = bool(exec_res and not exec_res.get("ok"))
        missing_materials = bool((command_configured and not command_executed) or protection_unknown or (protection_active and not command_executed))

        review_status = "reviewed"
        if missing_materials:
            review_status = "blocked"
            verdict = "fail"
            reason = "review_blocked: missing required caller acceptance command execution evidence"
            next_action = "diagnose"
            next_action_source = "system_guard"
        elif command_failed:
            verdict = "fail"
            reason = f"acceptance command failed with rc={exec_res.get('returncode')}: {exec_res.get('error') or exec_res.get('stderr')}"
            next_action = "rework"
            next_action_source = "behavior_evidence"
        elif output_prot and output_prot.get("preserve_overwritten_by_empty"):
            verdict = "fail"
            reason = f"output protection failure: {output_prot.get('details')}"
            next_action = "rework"
            next_action_source = "behavior_evidence"

        artifact_bindings = []
        for rel in effective_accept.get('artifacts', []):
            if not isinstance(rel, str) or Path(rel).is_absolute() or Path(rel).drive or ':' in rel or '..' in Path(rel).parts:
                continue
            path = Path(workspace) / rel
            try:
                cursor = Path(workspace)
                for part in Path(rel).parts:
                    cursor = cursor / part
                    st = cursor.lstat()
                    if cursor.is_symlink() or getattr(st, 'st_file_attributes', 0) & 0x400:
                        raise ValueError('reparse artifact')
                data = path.read_bytes()
                artifact_bindings.append({'path': rel, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
            except (OSError, ValueError):
                artifact_bindings.append({'path': rel, 'status': 'unavailable'})
        behavior_ev['caller_artifacts'] = artifact_bindings
        evidence_bytes = json.dumps({"binding": {"goal_id": goal_id, "task_id": task_id, "run_id": run_id}, "behavior_evidence": behavior_ev, "acceptance_execution": exec_res}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        evidence_dir = self.persist_root / "caller-review-snapshots"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence_sha = hashlib.sha256(evidence_bytes).hexdigest()
        evidence_path = evidence_dir / (evidence_sha + ".json")
        try:
            with evidence_path.open("xb") as stream:
                stream.write(evidence_bytes)
        except FileExistsError:
            if evidence_path.read_bytes() != evidence_bytes:
                raise ValueError("immutable evidence snapshot mismatch")
        behavior_ev["immutable_snapshot"] = {"path": str(evidence_path), "sha256": evidence_sha, "bytes": len(evidence_bytes), "encoding": "utf-8"}

        if adapter is not None and hasattr(adapter, "decide") and review_status != "blocked":
            goal_boundaries = (goal_snap.get("goal") or {}).get("boundaries") or {}
            lead_req = build_lead_request(
                kind="review",
                goal=str(task.get("title") or (goal_snap.get("goal") or {}).get("desired_outcome") or ""),
                authorized_scope=list(goal_boundaries.get("must") or ["Stay inside assigned workspace"]),
                prohibitions=list(goal_boundaries.get("must_not") or []),
                acceptance_criteria=dict(effective_accept),
                current_application={
                    "application_id": app_id,
                    "goal_id": goal_id,
                    "task_id": task_id,
                    "run_id": run_id,
                    "workspace": workspace,
                    "partial_artifacts": partial_artifacts,
                    "behavior_evidence": behavior_ev,
                },
                application_id=app_id,
                extra={
                    "worker_result": dict(worker_result),
                    "partial_artifacts": partial_artifacts,
                    "continuation_required": continuation_required,
                    "worker_request": action_payload,
                    "behavior_evidence": behavior_ev,
                    "acceptance_execution": exec_res,
                },
            )
            schema = lead_review_response_schema()
            cwd = str(getattr(self.planner, "cwd", self.persist_root))
            send_bytes = json.dumps({"request": lead_req, "schema": schema}, ensure_ascii=False, sort_keys=True).encode("utf-8")
            send_path = evidence_dir / (hashlib.sha256(send_bytes).hexdigest() + ".review.json")
            try:
                with send_path.open("xb") as stream:
                    stream.write(send_bytes)
            except FileExistsError:
                if send_path.read_bytes() != send_bytes:
                    raise ValueError("immutable review request mismatch")
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
                review_status = "blocked"
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
                        next_action = "continue" if verdict == "pass" else "rework"
                        next_action_source = "suggested"
                    # Behavior evidence check: if native execution failed or output protection violated, lead verdict cannot silently pass
                    if command_failed or (output_prot and output_prot.get("preserve_overwritten_by_empty")):
                        verdict = "fail"
                        if "acceptance command failed" not in reason and "output protection" not in reason:
                            reason = f"acceptance failed (rc={exec_res.get('returncode')}): {reason}"
                        next_action = "rework"
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
                        review_status = "blocked"
        elif callable(decider) and review_status != "blocked":
            action = {
                "kind": "review",
                "payload": action_payload,
                "review_hint": reason_hint,
                "behavior_evidence": behavior_ev,
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
                    if command_failed or (output_prot and output_prot.get("preserve_overwritten_by_empty")):
                        verdict = "fail"
                        next_action = "rework"
            except Exception as e:
                verdict = "fail"
                reason = f"decider raised {e}"
                next_action = "diagnose"
                next_action_source = "fallback"
                lead_status = "error"
                review_status = "blocked"
        elif review_status != "blocked":
            # Deterministic / fallback logic:
            # Worker self-report ok=True cannot pass alone if acceptance command was configured or unverified.
            if command_configured:
                if command_executed and not command_failed and not (output_prot and output_prot.get("preserve_overwritten_by_empty")):
                    verdict = "pass"
                    reason = "caller acceptance command passed; protection=" + output_prot["status"]
                    next_action = "complete"
                    next_action_source = "fallback"
                else:
                    verdict = "fail"
                    reason = f"acceptance failed (rc={exec_res.get('returncode') if exec_res else 'none'})"
                    next_action = "rework"
                    next_action_source = "fallback"
            else:
                if worker_result.get("ok") and not (output_prot and output_prot.get("preserve_overwritten_by_empty")):
                    verdict = "pass"
                    reason = "worker execution completed successfully"
                    next_action = "complete"
                    next_action_source = "fallback"
                else:
                    verdict = "fail"
                    reason = str(worker_result.get("error") or "worker execution failed")
                    next_action = "diagnose"
                    next_action_source = "fallback"

        if missing_materials:
            verdict, review_status, next_action = "fail", "blocked", "diagnose"
        elif command_failed or protection_violated:
            verdict, next_action = "fail", "rework"

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
            "review_status": review_status,
            "lead_status": lead_status,
            "raw_result": dict(worker_result),
            "behavior_evidence": behavior_ev,
            "acceptance_execution": exec_res,
            "lead_details": details,
            "consumed_at": time.time(),
        }

        # If lead call experienced an error/call_failed/timeout, we do NOT fake lead consumption success:
        # We only persist as consumed if lead returned a valid decision (ok or unwrapped)
        if lead_status in {"ok", "unwrapped"} and review_status != "blocked":
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
                exec_cmd = task_result.get("acceptance_execution")
                output_prot = task_result.get("output_protection") or {
                    "preserve_overwritten_by_empty": False,
                    "details": "no preserve violations detected",
                }
                behavior_ev = task_result.get("behavior_evidence") or build_behavior_evidence(
                    command=(snap.get("goal") or {}).get("acceptance", {}).get("command"),
                    execution=exec_cmd,
                    input_purpose=str(t.get("title") or "task execution"),
                    output_protection=output_prot,
                    expected_assertions=["returncode == 0"],
                    actual_assertions=[f"returncode == {exec_cmd.get('returncode')}" if exec_cmd else "no command execution"],
                    worker_self_report={
                        "status": task_status,
                        "ok": task_result.get("ok"),
                        "response": task_result.get("response"),
                    },
                )
                lead_reviews.append({
                    "task_id": t.get("task_id"),
                    "verdict": "pass" if existing_review.get("status") == "passed" else "fail",
                    "reason": existing_review.get("evidence") or "",
                    "next_action": "continue" if existing_review.get("status") == "passed" else "rework",
                    "review_status": "reviewed",
                    "lead_status": "ok",
                    "partial_artifacts": partial_artifacts,
                    "workspace": ws,
                    "behavior_evidence": behavior_ev,
                    "acceptance_execution": exec_cmd,
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
