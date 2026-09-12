"""Path-B framework skeleton: Goal/Task/Run contracts without owning TeleAgent HTTP."""

from framework.charter_map import CharterMapError, map_charter_to_goal_task
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

__all__ = [
    "CONTRACT_VERSION",
    "CharterMapError",
    "DECISION_CHANNEL_LEAD_CODES",
    "ERROR_CLASSES",
    "RUN_STATES",
    "TASK_STATES",
    "assert_transition",
    "attach_framework_projection",
    "map_charter_to_goal_task",
    "map_error_class",
    "new_run_id",
    "new_task_id",
    "project_scheduler_state",
    "stable_goal_task_ids",
]
