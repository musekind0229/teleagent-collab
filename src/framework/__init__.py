"""Path-B framework skeleton: Goal/Task/Run contracts without owning TeleAgent HTTP."""

from framework.charter_map import CharterMapError, map_charter_to_goal_task
from framework.goal_ownership import (
    GoalOwnership,
    GoalOwnershipError,
    GoalOwnershipStore,
    PlanRevisionProposal,
    attach_goal_ownership,
    open_goal_ownership,
    plan_from_mapped,
    reset_goal_ownership_cache,
    validate_plan_revision,
)
from framework.id_projection import stable_goal_task_ids
from framework.lifecycle import (
    DECISION_CHANNEL_LEAD_CODES,
    ERROR_CLASSES,
    RUN_STATES,
    TASK_STATES,
    assert_transition,
    map_error_class,
    project_scheduler_state,
)
from framework.models import CONTRACT_VERSION, new_run_id, new_task_id
from framework.project_report import attach_framework_projection
from framework.task_deps import (
    deps_satisfied,
    parse_depends_on,
    unsatisfied_deps,
)

__all__ = [
    "CONTRACT_VERSION",
    "CharterMapError",
    "DECISION_CHANNEL_LEAD_CODES",
    "ERROR_CLASSES",
    "GoalOwnership",
    "GoalOwnershipError",
    "GoalOwnershipStore",
    "PlanRevisionProposal",
    "RUN_STATES",
    "TASK_STATES",
    "assert_transition",
    "attach_framework_projection",
    "attach_goal_ownership",
    "deps_satisfied",
    "map_charter_to_goal_task",
    "map_error_class",
    "new_run_id",
    "new_task_id",
    "open_goal_ownership",
    "parse_depends_on",
    "plan_from_mapped",
    "project_scheduler_state",
    "reset_goal_ownership_cache",
    "stable_goal_task_ids",
    "unsatisfied_deps",
    "validate_plan_revision",
]
