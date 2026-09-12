"""Map legacy charter → single-task Goal + Task. Never invent unlimited allow_*."""
from __future__ import annotations

from typing import Any

from framework.models import CONTRACT_VERSION, new_goal_id, new_task_id


class CharterMapError(ValueError):
    pass


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return list(v)
    raise CharterMapError(f"expected list, got {type(v).__name__}")


def map_charter_to_goal_task(charter: dict, *, goal_id: str | None = None) -> dict[str, Any]:
    """Return {goal, task, warnings}. Does not mutate charter.

    Missing allow_* keys are NOT filled with wildcards or permissive defaults.
    If present as [], they stay []. If absent, Goal.boundaries omits them and
    warnings notes the gap — hard_rules must treat absent as unauthorized.
    """
    if not isinstance(charter, dict):
        raise CharterMapError("charter must be a dict")
    goal_text = str(charter.get("goal") or "").strip()
    if not goal_text:
        raise CharterMapError("charter.goal required")
    must = _as_list(charter.get("must"))
    must_not = _as_list(charter.get("must_not"))
    if "must" not in charter or "must_not" not in charter:
        raise CharterMapError("charter.must and charter.must_not required")

    missing_allow = [k for k in ("allow_secret_globs", "allow_paths", "allow_keys") if k not in charter]
    warnings: list[str] = []
    if missing_allow:
        warnings.append(
            "allow_* fields missing: " + ",".join(missing_allow) + " — omitted (not filled as allow-all)"
        )

    boundaries: dict[str, Any] = {"must": must, "must_not": must_not}
    for k in ("allow_secret_globs", "allow_paths", "allow_keys"):
        if k in charter:
            boundaries[k] = _as_list(charter.get(k))
    for k in ("allowed_surfaces", "user_gate_permissions"):
        if k in charter:
            boundaries[k] = _as_list(charter.get(k))

    acceptance: dict[str, Any] = {}
    if "acceptance" in charter:
        acceptance["text"] = charter.get("acceptance")
    if isinstance(charter.get("done_when"), dict):
        acceptance["done_when"] = charter["done_when"]
        arts = charter["done_when"].get("artifacts")
        if isinstance(arts, list):
            acceptance["artifacts"] = list(arts)
    elif "done_when" in charter:
        acceptance["done_when"] = charter.get("done_when")

    timeout = charter.get("timeout_sec")
    try:
        wall = float(timeout) if timeout is not None else 360.0
    except (TypeError, ValueError) as e:
        raise CharterMapError(f"invalid timeout_sec: {timeout!r}") from e

    name = str(charter.get("name") or "job")
    gid = goal_id or new_goal_id(name)
    tid = new_task_id()

    goal = {
        "contract_version": CONTRACT_VERSION,
        "goal_id": gid,
        "title": name,
        "desired_outcome": goal_text,
        "boundaries": boundaries,
        "acceptance": acceptance,
        "budget": {
            "wall_sec": wall,
            "max_reworks": int(charter.get("max_reworks") or 1),
        },
        "capability_requirements": [],
        "platform_allowlist": ["linux"],
        "source_charter_path": str(charter.get("_source") or ""),
    }
    if charter.get("force_lead_review"):
        goal["capability_requirements"].append("force_lead_review")

    expected = []
    if isinstance(acceptance.get("artifacts"), list):
        expected = list(acceptance["artifacts"])

    task_inputs: dict[str, Any] = {}
    for k in (
        "task_kind",
        "force_lead_review",
        "lead_review_steps",
        "install_roots",
        "network_allow",
        "rollback",
    ):
        if k in charter:
            task_inputs[k] = charter[k]

    task = {
        "contract_version": CONTRACT_VERSION,
        "task_id": tid,
        "goal_id": gid,
        "title": f"{name} implementation",
        "depends_on": [],
        "inputs": task_inputs,
        "expected_artifacts": expected,
        "done_when": charter.get("done_when") if isinstance(charter.get("done_when"), dict) else {"artifacts": expected},
        "status": "queued",
        "assignee_role": "executor",
        "backend_requirement": "teleagent.linux.local_v1",
    }

    return {"goal": goal, "task": task, "warnings": warnings, "_allow_fields_missing": missing_allow}
