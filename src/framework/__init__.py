"""Path-B framework skeleton: Goal/Task/Run contracts without owning TeleAgent HTTP."""

from framework.charter_map import CharterMapError, map_charter_to_goal_task
from framework.lifecycle import ERROR_CLASSES, RUN_STATES, TASK_STATES, assert_transition
from framework.models import CONTRACT_VERSION, new_run_id, new_task_id

__all__ = [
    "CONTRACT_VERSION",
    "CharterMapError",
    "ERROR_CLASSES",
    "RUN_STATES",
    "TASK_STATES",
    "assert_transition",
    "map_charter_to_goal_task",
    "new_run_id",
    "new_task_id",
]
