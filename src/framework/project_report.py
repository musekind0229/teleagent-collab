"""Optional read-only Goal/Task projection into job reports (does not affect ok/fail)."""
from __future__ import annotations

from typing import Any

from framework.charter_map import CharterMapError, map_charter_to_goal_task


def attach_framework_projection(report: dict, charter: dict | None) -> None:
    """Mutate report in place. Failures become notes only — never flip report['ok']."""
    if not isinstance(report, dict):
        return
    if not isinstance(charter, dict):
        report.setdefault("notes", []).append("framework_projection skipped: no charter")
        return
    prev_ok = report.get("ok")
    prev_state = report.get("state")
    prev_error = report.get("error")
    try:
        out = map_charter_to_goal_task(charter)
        proj: dict[str, Any] = {
            "goal": out["goal"],
            "task": out["task"],
            "warnings": out.get("warnings") or [],
            "allow_fields_missing": out.get("_allow_fields_missing") or [],
            "readonly": True,
        }
        if out.get("coordinator_id"):
            proj["coordinator_id"] = out["coordinator_id"]
        report["framework_projection"] = proj
    except CharterMapError as e:
        report.setdefault("notes", []).append(f"framework_projection skipped: {e}")
    except Exception as e:  # noqa: BLE001 — projection must never break the job
        report.setdefault("notes", []).append(f"framework_projection error: {e}")
    # Hard guarantee: projection does not change completion semantics
    if "ok" in report:
        report["ok"] = prev_ok
    if "state" in report:
        report["state"] = prev_state
    if "error" in report:
        report["error"] = prev_error
