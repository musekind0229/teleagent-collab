"""Wire old bin/run-job.py to a selectable ExecutionBackend.

Default remains TeleAgent (glue). inprocess.local_v1 uses only the public
ExecutionBackend surface: start_run / observe_run / collect_result plus
empty list_pending_actions. reply_permission stays unsupported — never
invent once/approve. Hermes is not offered.

Knife 8: inprocess path runs a stage-2 closed loop (independent artifact_review
+ same-Task new-Run rework) when force_lead_review is set. Wall budget is
never reset; decision_channel_failed does not consume business rework.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from execution_backend.base import BackendError, BackendStatus
from execution_backend.closed_loop import decision_fn_from_env, run_inprocess_closed_loop

ENV_NAME = "COLLAB_EXECUTION_BACKEND"
KIND_TELEAGENT = "teleagent"
KIND_INPROCESS = "inprocess"

_TELEAGENT_ALIASES = frozenset(
    {
        "teleagent",
        "teleagent.linux.local_v1",
        "ta",
        "glue",
    }
)
_INPROCESS_ALIASES = frozenset(
    {
        "inprocess",
        "inprocess.local_v1",
        "in_process",
    }
)


def resolve_run_job_backend(
    cli_value: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return KIND_TELEAGENT or KIND_INPROCESS.

    Priority: CLI ``--backend`` > env COLLAB_EXECUTION_BACKEND > teleagent.
    Unknown names (including hermes) raise BackendError UNSUPPORTED.
    """
    raw = (cli_value or "").strip()
    if not raw:
        env = environ if environ is not None else os.environ
        raw = str(env.get(ENV_NAME) or "").strip()
    if not raw:
        return KIND_TELEAGENT
    key = raw.lower()
    if key in _TELEAGENT_ALIASES:
        return KIND_TELEAGENT
    if key in _INPROCESS_ALIASES:
        return KIND_INPROCESS
    raise BackendError(
        BackendStatus.UNSUPPORTED,
        f"unsupported execution backend {raw!r}; "
        "supported: teleagent, inprocess (inprocess.local_v1). "
        "Hermes is not a collab execution backend.",
        capability="select_backend",
    )


def run_inprocess_charter(
    *,
    charter: dict,
    workdir: str | Path,
    instruction: str = "",
    name: str = "",
    timeout_sec: float | None = None,
    lead: Any | None = None,
    decision_fn: Callable[[dict, dict], dict] | None = None,
    force_lead_review: bool | None = None,
    max_reworks: int | None = None,
    exchange_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run a charter through inprocess.local_v1 public API only (no glue, no TA HTTP).

    Stage-2 closed loop: independent artifact_review when force_lead_review,
    rework as a new Run on the same Task. Optional COLLAB_INPROCESS_REVIEW_STUB
    (fail_once / channel_fail) is a test/demo hook only.
    """
    fn = decision_fn
    if fn is None and lead is None:
        fn = decision_fn_from_env(dict(environ) if environ is not None else None)
    return run_inprocess_closed_loop(
        charter=charter,
        workdir=workdir,
        instruction=instruction,
        name=name,
        lead=lead,
        decision_fn=fn,
        timeout_sec=timeout_sec,
        force_lead_review=force_lead_review,
        max_reworks=max_reworks,
        exchange_dir=exchange_dir,
    )
