"""Stage-2 inprocess closed loop: public execute + independent artifact_review + rework.

Uses only ExecutionBackend public methods (start_run / observe_run / collect_result)
plus an independent artifact_review gate (inprocess lead / local rules).

Rework is a new Run on the same Task. Wall-clock budget is never reset.
decision_channel_failed does not consume business rework.

Hermes is not the task source or ledger.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from charter import expected_artifacts as resolve_arts, job_name
from completion import (
    ReworkBudget,
    artifacts_all_present,
    build_acceptance_packet,
    confirm_artifacts_for_lead_approve,
    snapshot_artifacts,
)
from execution_backend.inprocess_v1 import InProcessExecutionBackend, run_file_job_via_public_api
from framework.charter_map import map_charter_to_goal_task
from framework.lifecycle import map_error_class
from framework.models import CONTRACT_VERSION, contract_fingerprint, make_run
from lead_adapter import (
    LeadDecisionError,
    build_lead_request,
    lead_review_response_schema,
    validate_lead_decision,
)
from lead_adapter.inprocess import InProcessLeadAdapter

REVIEW_STUB_ENV = "COLLAB_INPROCESS_REVIEW_STUB"


def local_rules_artifact_review(request: dict, schema: dict | None = None) -> dict:
    """Deterministic local-rules artifact_review (inprocess lead, no Hermes).

    Pass only when required artifacts exist and hello-style files have exactly
    one non-empty line. Always echoes application_id / context_summary.
    """
    app_id = str(request.get("application_id") or "")
    summary = str(request.get("context_summary") or "")
    packet = request.get("current_application") if isinstance(request.get("current_application"), dict) else {}
    missing = list(packet.get("missing") or [])
    complete = bool(packet.get("artifacts_complete"))
    bad: list[str] = []
    if not complete or missing:
        bad.append(f"incomplete missing={missing}")
    for rec in packet.get("artifacts") or []:
        if not isinstance(rec, dict):
            continue
        path = Path(str(rec.get("path") or ""))
        if not rec.get("exists") or not path.is_file():
            bad.append(f"missing:{path.name or path}")
            continue
        name = path.name.lower()
        if "hello" in name:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as e:
                bad.append(f"unreadable:{path.name}:{e}")
                continue
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if len(lines) != 1:
                bad.append(f"hello_not_one_line:{path.name}")
    if bad:
        verdict = "fail"
        reason = "local_rules: " + "; ".join(bad)
    else:
        verdict = "pass"
        reason = "local_rules: artifacts present and hello-style checks passed"
    return {
        "application_id": app_id,
        "context_summary": summary,
        "verdict": verdict,
        "reason": reason,
    }


def make_fail_once_then_pass() -> Callable[[dict, dict], dict]:
    """Test/demo stub: first artifact_review fails, subsequent pass. Binding intact."""
    state = {"n": 0}

    def _fn(request: dict, schema: dict | None = None) -> dict:
        state["n"] += 1
        fail = state["n"] == 1
        return {
            "application_id": str(request.get("application_id") or ""),
            "context_summary": str(request.get("context_summary") or ""),
            "verdict": "fail" if fail else "pass",
            "reason": "stub: first review fail" if fail else "stub: subsequent review pass",
        }

    _fn.calls = state  # type: ignore[attr-defined]
    return _fn


def make_decision_channel_fail(*, code: str = "application_id_mismatch") -> Callable[[dict, dict], dict]:
    """Test stub: return an unbound / illegal review payload (protocol fail, not verdict fail)."""

    def _fn(request: dict, schema: dict | None = None) -> dict:
        if code == "illegal_json":
            return {"not": "a review"}  # missing verdict/reason/id
        if code == "timeout":
            return {"_lead_status": "timeout", "error": "stub timeout"}
        if code == "call_failed":
            return {"_lead_status": "call_failed", "error": "stub call_failed"}
        return {
            "application_id": "wrong_application_id",
            "context_summary": str(request.get("context_summary") or ""),
            "verdict": "pass",
            "reason": "stub: unbound pass must not count as acceptance",
        }

    return _fn


def decision_fn_from_env(environ: dict | None = None) -> Callable[[dict, dict], dict] | None:
    """Optional deterministic review stub for tests/demo. Unset → local rules."""
    import os

    env = environ if environ is not None else os.environ
    stub = str(env.get(REVIEW_STUB_ENV) or "").strip().lower()
    if stub in ("fail_once", "fail-once"):
        return make_fail_once_then_pass()
    if stub in ("channel_fail", "decision_channel_fail", "mismatch"):
        return make_decision_channel_fail()
    if stub in ("pass", "local", "local_rules"):
        return local_rules_artifact_review
    return None


def _bind_lead(
    *,
    lead: Any | None,
    decision_fn: Callable[[dict, dict], dict] | None,
    exchange_dir: str | Path | None,
) -> Any:
    if lead is not None:
        return lead
    fn = decision_fn if decision_fn is not None else local_rules_artifact_review
    return InProcessLeadAdapter(exchange_dir=exchange_dir, decision_fn=fn)


def artifact_review(
    *,
    charter: dict,
    workdir: str | Path,
    collect: dict,
    lead: Any | None = None,
    decision_fn: Callable[[dict, dict], dict] | None = None,
    run_id: str = "",
    task_id: str = "",
    goal_id: str = "",
    job_name_s: str = "",
    exchange_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Public independent acceptance gate (kind=artifact_review).

    Does not call ExecutionBackend. Semantic fail → acceptance_failed.
    Binding/protocol fail → decision_channel_failed (caller must not rework worker).
    """
    root = Path(workdir)
    if exchange_dir is None:
        exchange_dir = root / "_lead_exchange"
    name = job_name_s or job_name(charter)
    try:
        expected = resolve_arts(charter, workspace=root)
    except Exception:
        expected = list(collect.get("artifacts") or [])
    if not expected:
        expected = list(collect.get("artifacts") or [])

    recs = snapshot_artifacts(expected)
    packet = build_acceptance_packet(
        job_name=name,
        goal=str(charter.get("goal") or ""),
        acceptance_criteria=charter.get("acceptance") or charter.get("done_when") or {"artifacts": expected},
        expected_artifacts=expected,
        execution_result={
            "ok": collect.get("ok"),
            "state": collect.get("state"),
            "finish": collect.get("finish"),
            "backend": collect.get("backend"),
            "run_id": collect.get("run_id") or run_id,
            "path": "inprocess.local_v1",
        },
        error=str(collect.get("error") or ""),
        notes=["artifact_review via inprocess lead / local rules"],
        state=str(collect.get("state") or ""),
        session_id=str(collect.get("run_id") or run_id or ""),
    )
    out: dict[str, Any] = {
        "kind": "artifact_review",
        "ok": False,
        "verdict": None,
        "error_class": None,
        "lead_error_code": None,
        "decision": None,
        "packet": packet,
        "artifact_gate": None,
        "run_id": run_id,
        "task_id": task_id,
        "goal_id": goal_id,
        "producer": "rule",
    }
    if not artifacts_all_present(recs) or not packet.get("artifacts_complete"):
        out["verdict"] = "fail"
        out["error_class"] = map_error_class(kind="acceptance_failed")
        out["reason"] = f"artifacts incomplete missing={packet.get('missing')}"
        return out

    unchanged, gate = confirm_artifacts_for_lead_approve(packet)
    out["artifact_gate"] = gate
    if not unchanged:
        out["verdict"] = "fail"
        out["error_class"] = map_error_class(kind="acceptance_failed")
        out["reason"] = f"artifact fingerprint changed before review: {gate.get('diffs')}"
        return out

    req = build_lead_request(
        kind="review",
        goal=str(charter.get("goal") or ""),
        authorized_scope=charter.get("must") or [],
        prohibitions=charter.get("must_not") or [],
        acceptance_criteria=packet.get("acceptance_criteria"),
        current_application=packet,
        charter=charter,
    )
    schema = lead_review_response_schema()
    adapter = _bind_lead(lead=lead, decision_fn=decision_fn, exchange_dir=exchange_dir)
    raw, parsed = adapter.decide(req, schema=schema, cwd=str(root), timeout_sec=30)
    try:
        decision = validate_lead_decision(raw, parsed, request=req, kind="review")
    except LeadDecisionError as e:
        out["lead_error_code"] = e.code
        out["error_class"] = map_error_class(kind="decision_channel", lead_code=e.code)
        out["reason"] = f"decision_channel_failed ({e.code}): {e}"
        out["verdict"] = None
        return out

    verdict = str(decision.get("verdict") or "fail").lower()
    out["decision"] = decision
    out["verdict"] = verdict
    out["reason"] = str(decision.get("reason") or "")
    out["request_id"] = req.get("application_id")
    out["binding"] = {
        "application_id": req.get("application_id"),
        "context_digest": req.get("context_summary"),
        "legacy_context_summary": req.get("context_summary"),
    }

    unchanged2, gate2 = confirm_artifacts_for_lead_approve(packet)
    out["artifact_gate_post"] = gate2
    if verdict == "pass" and not unchanged2:
        out["ok"] = False
        out["verdict"] = "fail"
        out["error_class"] = map_error_class(kind="acceptance_failed")
        out["reason"] = f"artifacts changed during review: {gate2.get('diffs')}"
        return out
    if verdict == "pass":
        out["ok"] = True
        out["error_class"] = None
        out["contract_decision"] = {
            "contract_version": CONTRACT_VERSION,
            "request_id": req.get("application_id"),
            "kind": "artifact_review",
            "goal_id": goal_id,
            "task_id": task_id,
            "run_id": run_id,
            "binding": out["binding"],
            "verdict": "pass",
            "reason": out["reason"] or "pass",
            "producer": "rule",
        }
        return out

    out["ok"] = False
    out["error_class"] = map_error_class(kind="acceptance_failed")
    return out


def run_inprocess_closed_loop(
    *,
    charter: dict,
    workdir: str | Path,
    instruction: str = "",
    name: str = "",
    backend: InProcessExecutionBackend | None = None,
    lead: Any | None = None,
    decision_fn: Callable[[dict, dict], dict] | None = None,
    timeout_sec: float | None = None,
    force_lead_review: bool | None = None,
    max_reworks: int | None = None,
    exchange_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Execute a file job on inprocess.local_v1 with optional accept+rework closed loop.

    Each attempt: start_run / observe_run / collect_result only (via run_file_job_via_public_api).
    Same Task, new Run on acceptance_failed / implementation_failed. Wall never reset.
    decision_channel_failed stops without consuming rework.
    """
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    be = backend or InProcessExecutionBackend()
    job = name or job_name(charter)
    mapped = map_charter_to_goal_task(charter)
    goal = mapped["goal"]
    task = mapped["task"]
    goal_id = str(goal["goal_id"])
    task_id = str(task["task_id"])
    fp = contract_fingerprint(
        {
            "goal_id": goal_id,
            "task_id": task_id,
            "desired_outcome": goal.get("desired_outcome"),
            "acceptance": goal.get("acceptance"),
            "boundaries": goal.get("boundaries"),
        }
    )
    need_review = bool(charter.get("force_lead_review", False) if force_lead_review is None else force_lead_review)
    wall = timeout_sec
    if wall is None:
        wall = charter.get("timeout_sec") or goal.get("budget", {}).get("wall_sec") or 240
    reworks = max_reworks
    if reworks is None:
        reworks = int(charter.get("max_reworks") or goal.get("budget", {}).get("max_reworks") or 1)

    budget = ReworkBudget.start(float(wall), max_reworks=int(reworks))
    wall_at_start = budget.wall_deadline
    xdir = Path(exchange_dir) if exchange_dir is not None else (root / "_lead_exchange")
    adapter = _bind_lead(lead=lead, decision_fn=decision_fn, exchange_dir=xdir) if need_review else None

    notes: list[str] = [
        "backend=inprocess.local_v1 public API only (start_run/observe_run/collect_result)",
        "list_pending_actions empty; reply_permission unsupported (not called as approve)",
        "closed_loop=stage-2 artifact_review+rework" if need_review else "closed_loop=execute-only (force_lead_review false)",
    ]
    attempts: list[dict[str, Any]] = []
    last_raw: dict[str, Any] = {}
    last_review: dict[str, Any] | None = None
    error_class: str | None = None
    ok = False
    state = "fail"
    error = ""
    grok_review = ""

    while True:
        if budget.exhausted_wall():
            error_class = map_error_class(kind="budget_exhausted")
            error = "wall clock exhausted before closed loop finished"
            notes.append(error)
            break

        attempt_n = len(attempts) + 1
        run_rec = make_run(
            task_id=task_id,
            attempt=attempt_n,
            backend=be.backend_id,
            contract_fingerprint_value=fp,
            workspace_id=str(root),
            state="running",
        )
        raw = run_file_job_via_public_api(workdir=root, charter=charter, backend=be)
        last_raw = raw
        backend_run_id = str(raw.get("run_id") or "")
        native = str(raw.get("native_handle") or backend_run_id)
        run_rec["native_handle"] = native
        run_rec["artifact_refs"] = list(raw.get("artifacts") or [])
        rec = {
            "run": run_rec,
            "backend_run_id": backend_run_id,
            "collect": {
                "ok": raw.get("ok"),
                "state": raw.get("state"),
                "finish": raw.get("finish"),
                "error": raw.get("error"),
            },
            "review": None,
        }
        attempts.append(rec)
        notes.append(
            f"attempt={attempt_n} public_run_id={run_rec['run_id']} "
            f"backend_run_id={backend_run_id} collect_ok={bool(raw.get('ok'))}"
        )

        if not raw.get("ok"):
            error_class = map_error_class(kind="implementation_failed")
            run_rec["error_class"] = error_class
            run_rec["state"] = "failed"
            error = str(raw.get("error") or "collect_result not ok")
            if not budget.consume_rework():
                error_class = map_error_class(kind="budget_exhausted")
                error = f"implementation_failed; rework budget exhausted ({error})"
                notes.append(error)
                break
            notes.append("implementation_failed → rework new Run; wall unchanged")
            continue

        if not need_review:
            run_rec["state"] = "succeeded"
            ok = True
            state = "ok"
            error = ""
            error_class = None
            break

        review = artifact_review(
            charter=charter,
            workdir=root,
            collect=raw,
            lead=adapter,
            run_id=run_rec["run_id"],
            task_id=task_id,
            goal_id=goal_id,
            job_name_s=job,
            exchange_dir=xdir,
        )
        last_review = review
        rec["review"] = {
            "ok": review.get("ok"),
            "verdict": review.get("verdict"),
            "error_class": review.get("error_class"),
            "lead_error_code": review.get("lead_error_code"),
            "reason": review.get("reason"),
        }
        grok_review = str(review.get("verdict") or review.get("lead_error_code") or "")
        notes.append(
            f"artifact_review verdict={review.get('verdict')!r} "
            f"error_class={review.get('error_class')!r} code={review.get('lead_error_code')!r}"
        )

        if review.get("error_class") == "decision_channel_failed":
            error_class = "decision_channel_failed"
            run_rec["error_class"] = error_class
            run_rec["state"] = "failed"
            error = str(review.get("reason") or "decision_channel_failed")
            notes.append("decision_channel_failed → stop; rework budget not consumed as business rework")
            break

        if review.get("ok") and review.get("verdict") == "pass":
            run_rec["state"] = "succeeded"
            ok = True
            state = "ok"
            error = ""
            error_class = None
            break

        error_class = str(review.get("error_class") or "acceptance_failed")
        run_rec["error_class"] = error_class
        run_rec["state"] = "failed"
        error = str(review.get("reason") or "acceptance_failed")
        if not budget.consume_rework():
            error_class = map_error_class(kind="budget_exhausted")
            error = f"acceptance_failed; rework budget exhausted ({error})"
            notes.append(error)
            break
        notes.append("acceptance_failed → rework new Run same Task; wall unchanged")

    wall_unchanged = budget.wall_deadline == wall_at_start
    if not wall_unchanged:
        notes.append("BUG: wall_deadline changed during closed loop")
    used_new_run = len(attempts) > 1
    final_run = attempts[-1]["run"] if attempts else None
    public_ids = [a["run"]["run_id"] for a in attempts]
    backend_ids = [a["backend_run_id"] for a in attempts]
    if used_new_run:
        notes.append(
            f"rework used new Run: public {public_ids[0]} → {public_ids[-1]}; "
            f"backend {backend_ids[0]} → {backend_ids[-1]}; task_id={task_id} unchanged"
        )

    if raw_err := last_raw.get("error"):
        notes.append(f"backend_error={raw_err}")
    notes.append(f"used_public_api_only={bool(last_raw.get('used_public_api_only', True))}")
    notes.append(f"pending_count={last_raw.get('pending_count', 0)}")
    notes.append(f"native_handle={last_raw.get('native_handle') or ''}")
    if instruction:
        notes.append(f"instruction_chars={len(instruction)}")

    report: dict[str, Any] = {
        "name": job,
        "session_id": last_raw.get("run_id") or (final_run or {}).get("run_id") or "",
        "pending_seen": False,
        "pending_summaries": [],
        "grok_permission_decision": "",
        "grok_review_decision": grok_review,
        "api_replies": [],
        "hard_rule_rejects": [],
        "artifacts": list(last_raw.get("artifacts") or []),
        "state": state if ok else (last_raw.get("state") or state),
        "ok": bool(ok),
        "error": error,
        "path": "inprocess.local_v1",
        "notes": notes,
        "dry_run": False,
        "backend": last_raw.get("backend") or be.backend_id,
        "used_public_api_only": True,
        "native_handle": last_raw.get("native_handle") or "",
        "run_observation": last_raw.get("run_observation") or {},
        "pending_count": int(last_raw.get("pending_count") or 0),
        "error_class": error_class,
        "task_id": task_id,
        "goal_id": goal_id,
        "attempt": len(attempts),
        "run_id": (final_run or {}).get("run_id") or "",
        "run_ids": public_ids,
        "backend_run_ids": backend_ids,
        "rework_budget": budget.to_dict(),
        "wall_deadline": budget.wall_deadline,
        "wall_deadline_at_start": wall_at_start,
        "wall_deadline_unchanged": wall_unchanged,
        "rework_used_new_run": used_new_run,
        "force_lead_review": need_review,
        "artifact_review": last_review,
        "closed_loop": {
            "ok": bool(ok),
            "task_id": task_id,
            "goal_id": goal_id,
            "attempts": attempts,
            "attempt": len(attempts),
            "rework_used_new_run": used_new_run,
            "wall_deadline_unchanged": wall_unchanged,
            "error_class": error_class,
            "used_public_api_only": True,
            "force_lead_review": need_review,
        },
        "contract_fingerprint": fp,
    }
    if not ok and error_class:
        report["state"] = "fail"
    if ok:
        report["state"] = "ok"
        report["error"] = ""
        report["error_class"] = None
    try:
        from framework.project_report import attach_framework_projection

        attach_framework_projection(report, charter)
    except Exception as e:  # noqa: BLE001
        report.setdefault("notes", []).append(f"framework_projection skipped: {e}")
    return report


__all__ = [
    "REVIEW_STUB_ENV",
    "artifact_review",
    "decision_fn_from_env",
    "local_rules_artifact_review",
    "make_decision_channel_fail",
    "make_fail_once_then_pass",
    "run_inprocess_closed_loop",
]
