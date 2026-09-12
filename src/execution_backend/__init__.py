"""Second execution backend lane (path-B). Ledger remains collab; Hermes is not the source of truth."""
from __future__ import annotations

from execution_backend.base import (
    BackendCapability,
    BackendError,
    BackendStatus,
    ExecutionBackend,
    ExecutionBackendABC,
    unsupported,
)
from execution_backend.closed_loop import (
    REVIEW_STUB_ENV,
    artifact_review,
    decision_fn_from_env,
    local_rules_artifact_review,
    make_decision_channel_fail,
    make_fail_once_then_pass,
    run_inprocess_closed_loop,
)
from execution_backend.inprocess_v1 import InProcessExecutionBackend, run_file_job_via_public_api
from execution_backend.run_job_wire import (
    ENV_NAME as COLLAB_EXECUTION_BACKEND_ENV,
    KIND_INPROCESS,
    KIND_TELEAGENT,
    resolve_run_job_backend,
    run_inprocess_charter,
)
from execution_backend.two_job_isolation import (
    ARTIFACT_A as ISO_ARTIFACT_A,
    ARTIFACT_B as ISO_ARTIFACT_B,
    ISO_A_CHARTER,
    ISO_B_CHARTER,
    IsolationError,
    allocate_isolated_workdirs,
    check_workdir_isolation,
    isolation_report,
    prove_shared_workdir_is_not_isolation,
    prove_shared_workspace_collides,
    require_distinct_workdirs,
    run_one_inprocess_job,
    run_two_jobs_isolated,
)

__all__ = [
    "BackendCapability",
    "BackendError",
    "BackendStatus",
    "ExecutionBackend",
    "ExecutionBackendABC",
    "InProcessExecutionBackend",
    "unsupported",
    "run_file_job_via_public_api",
    "get_execution_backend",
    "COLLAB_EXECUTION_BACKEND_ENV",
    "KIND_INPROCESS",
    "KIND_TELEAGENT",
    "resolve_run_job_backend",
    "run_inprocess_charter",
    "REVIEW_STUB_ENV",
    "artifact_review",
    "decision_fn_from_env",
    "local_rules_artifact_review",
    "make_decision_channel_fail",
    "make_fail_once_then_pass",
    "run_inprocess_closed_loop",
    "ISO_A_CHARTER",
    "ISO_B_CHARTER",
    "ISO_ARTIFACT_A",
    "ISO_ARTIFACT_B",
    "IsolationError",
    "allocate_isolated_workdirs",
    "check_workdir_isolation",
    "isolation_report",
    "prove_shared_workdir_is_not_isolation",
    "prove_shared_workspace_collides",
    "require_distinct_workdirs",
    "run_one_inprocess_job",
    "run_two_jobs_isolated",
]


def get_execution_backend(name: str | None = None, **kwargs):
    """Factory. Default inprocess.local_v1. Hermes not offered here."""
    key = (name or "inprocess").strip().lower()
    if key in ("inprocess", "inprocess.local_v1", "in_process", "local"):
        return InProcessExecutionBackend(**kwargs)
    raise BackendError(BackendStatus.UNSUPPORTED, f"unknown execution backend={name!r}", capability="get_execution_backend")
