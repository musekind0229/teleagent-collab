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
from execution_backend.inprocess_v1 import InProcessExecutionBackend, run_file_job_via_public_api
from execution_backend.run_job_wire import (
    ENV_NAME as COLLAB_EXECUTION_BACKEND_ENV,
    KIND_INPROCESS,
    KIND_TELEAGENT,
    resolve_run_job_backend,
    run_inprocess_charter,
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
]


def get_execution_backend(name: str | None = None, **kwargs):
    """Factory. Default inprocess.local_v1. Hermes not offered here."""
    key = (name or "inprocess").strip().lower()
    if key in ("inprocess", "inprocess.local_v1", "in_process", "local"):
        return InProcessExecutionBackend(**kwargs)
    raise BackendError(BackendStatus.UNSUPPORTED, f"unknown execution backend={name!r}", capability="get_execution_backend")
