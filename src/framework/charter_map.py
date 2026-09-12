"""Map legacy charter → single-task Goal + Task. Never invent unlimited allow_*."""
from __future__ import annotations

from typing import Any

from framework.delegation import autonomy_is_known, normalize_autonomy
from framework.models import CONTRACT_VERSION, new_goal_id, new_task_id
from framework.task_deps import depends_on_strings


class CharterMapError(ValueError):
    pass


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return list(v)
    raise CharterMapError(f"expected list, got {type(v).__name__}")


def map_charter_to_goal_task(
    charter: dict, *, goal_id: str | None = None, task_id: str | None = None
) -> dict[str, Any]:
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
    gid = (goal_id or str(charter.get("goal_id") or "").strip() or new_goal_id(name))
    tid = task_id or new_task_id()

    cb = charter.get("budget") if isinstance(charter.get("budget"), dict) else {}
    try:
        if cb.get("wall_sec") is not None:
            wall = float(cb.get("wall_sec"))
    except (TypeError, ValueError) as e:
        raise CharterMapError(f"invalid budget.wall_sec: {cb.get('wall_sec')!r}") from e

    budget: dict[str, Any] = {
        "wall_sec": wall,
        "max_reworks": int(cb.get("max_reworks") if cb.get("max_reworks") is not None else (charter.get("max_reworks") or 1)),
    }
    for src_key, dst_key, conv in (
        ("max_lead_calls", "max_lead_calls", int),
        ("max_attempts", "max_attempts", int),
        ("max_usage", "max_usage", float),
        ("cost_hint", "cost_hint", str),
    ):
        raw_v = cb.get(src_key) if src_key in cb else charter.get(src_key)
        if raw_v is None:
            continue
        try:
            budget[dst_key] = conv(raw_v)
        except (TypeError, ValueError) as e:
            raise CharterMapError(f"invalid {dst_key}: {raw_v!r}") from e

    try:
        dep_list = depends_on_strings(charter.get("depends_on"))
    except Exception as e:
        raise CharterMapError(f"invalid depends_on: {e}") from e

    goal = {
        "contract_version": CONTRACT_VERSION,
        "goal_id": gid,
        "title": name,
        "desired_outcome": goal_text,
        "boundaries": boundaries,
        "acceptance": acceptance,
        "budget": budget,
        "capability_requirements": [],
        "platform_allowlist": ["linux"],
        "source_charter_path": str(charter.get("_source") or ""),
    }
    if charter.get("force_lead_review"):
        goal["capability_requirements"].append("force_lead_review")

    coordinator = str(charter.get("coordinator_id") or "").strip()
    submitter = str(charter.get("submitter_id") or charter.get("submitter") or "").strip()
    ext_ref = str(charter.get("external_goal_ref") or "").strip()
    raw_auto = charter.get("autonomy")
    if raw_auto is None and isinstance(charter.get("delegation"), dict):
        dlg = charter["delegation"]
        raw_auto = dlg.get("autonomy") if dlg.get("autonomy") is not None else dlg.get("mode") or dlg

    hints: dict[str, str] = {}
    if coordinator:
        # Kernel-owned identity lives on GoalOwnership; role_hints is the
        # projection-safe hint (Goal schema has no coordinator field).
        hints["coordinator"] = coordinator
    if submitter:
        hints["submitter"] = submitter
    spec = normalize_autonomy(raw_auto) if raw_auto is not None else None
    if spec is not None and autonomy_is_known(spec):
        hints["autonomy"] = str(spec["mode"])
    if hints:
        goal["role_hints"] = hints

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
        "depends_on": dep_list,
        "inputs": task_inputs,
        "expected_artifacts": expected,
        "done_when": charter.get("done_when") if isinstance(charter.get("done_when"), dict) else {"artifacts": expected},
        "status": "queued",
        "assignee_role": "executor",
        "backend_requirement": "teleagent.linux.local_v1",
    }

    out: dict[str, Any] = {
        "goal": goal,
        "task": task,
        "warnings": warnings,
        "_allow_fields_missing": missing_allow,
    }
    if coordinator:
        out["coordinator_id"] = coordinator
    if submitter:
        out["submitter_id"] = submitter
    if ext_ref:
        out["external_goal_ref"] = ext_ref
    if spec is not None and autonomy_is_known(spec):
        out["autonomy"] = spec
    return out
