"""ExecutionBackend public surface — peer to TeleAgent adapter, not Hermes ledger."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from framework.progress_budget import (
    budget_enforcement_capability,
    metering_capability,
    progress_capability,
)

# Same token as framework.app_service.API_VERSION. Kept here so backends do not
# import the application facade (that import is the other direction).
CAPABILITIES_API_VERSION = "collab-app.v0.1"

# Caller-required names accepted by POST /v1/requests. Unknown names are 400.
REQUIRED_CAPABILITY_NAMES = frozenset(
    {
        "permission_gate",
        "question_channel",
        "review_channel",
        "external_input_enforcement",
        "os_sandbox",
        "access_audit",
        "no_skip_permissions",
        "lead_review",
        "decomposition",
    }
)

SKIP_PERMISSIONS_WARNING = (
    "backend runs with skip-permissions: no permission gate; "
    "pinned external inputs are prompt-only"
)


class BackendCapability(str, Enum):
    START_RUN = "start_run"
    OBSERVE_RUN = "observe_run"
    COLLECT_RESULT = "collect_result"
    LIST_PENDING = "list_pending_actions"
    REPLY_PERMISSION = "reply_permission"
    CANCEL = "cancel"


class BackendStatus(str, Enum):
    OK = "ok"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class BackendError(RuntimeError):
    def __init__(self, status: BackendStatus | str, message: str = "", *, capability: str = "") -> None:
        self.status = BackendStatus(status) if not isinstance(status, BackendStatus) else status
        self.capability = capability
        super().__init__(message or self.status.value)


def unsupported(capability: str, detail: str = "") -> dict[str, Any]:
    """Public unsupported response — never invents approval."""
    return {
        "ok": False,
        "status": BackendStatus.UNSUPPORTED.value,
        "capability": capability,
        "error": detail or f"{capability} unsupported",
        "reply": None,  # explicit: do not treat as once/reject
    }


@runtime_checkable
class ExecutionBackend(Protocol):
    """Minimal execution surface used by framework (not TeleAgent HTTP)."""

    backend_id: str

    def start_run(
        self,
        *,
        title: str,
        directory: str,
        instruction: str = "",
        artifacts: list[str] | None = None,
        charter: dict | None = None,
    ) -> dict[str, Any]:
        ...

    def observe_run(
        self,
        run_id: str,
        *,
        dispatch_user_message_id: str | None = None,
        fetch_messages: bool = True,
    ) -> dict[str, Any]:
        ...

    def collect_result(self, run_id: str) -> dict[str, Any]:
        ...

    def list_pending_actions(self, *, session_id: str | None = None) -> tuple[int, list]:
        ...

    def reply_permission(self, request_id: str, reply: str) -> tuple[int, Any]:
        ...

    def cancel(self, run_id: str) -> tuple[int, Any]:
        ...


def default_capabilities(
    *,
    backend_id: str = "unknown",
    kind: str = "unknown",
) -> dict[str, Any]:
    """Conservative capability document. Real backends override the honest fields.

    Bools are False. Tri-state fields are ``"unknown"`` except
    ``external_inputs.enforcement``, whose closed set uses ``"none"`` for
    "no gate". ``"unknown"`` is never treated as satisfied.
    """
    return {
        "api_version": CAPABILITIES_API_VERSION,
        "backend": {"id": str(backend_id or "unknown"), "kind": str(kind or "unknown")},
        "planner": {"name": "unknown", "decomposes": False, "lead_review": False},
        "channels": {"permission": False, "question": False, "review": False},
        "external_inputs": {"max": 8, "enforcement": "none"},
        "isolation": {
            "os_sandbox": False,
            "access_audit": "unknown",
            "prompt_constraints": False,
        },
        "skip_permissions": "unknown",
        "resume": "unknown",
        "acceptance": {
            "artifact_presence": False,
            "exact_content": False,
            "lead_review": False,
            "executable_checks": False,
        },
        "progress": progress_capability(
            available=False,
            heartbeat=False,
            artifact_checkpoint=False,
            subagent_observability="unknown",
        ),
        "metering": metering_capability(
            live_usage=False,
            usage_at_end=False,
            tool_calls=False,
            fields=[],
            source="none",
        ),
        "budget_enforcement": budget_enforcement_capability(),
        "usage": {"source": "unknown"},
        "concurrency": {"max_runs": "unknown", "limited_by": []},
        "warnings": [],
    }


def _channels(caps: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = caps.get("channels")
    return raw if isinstance(raw, Mapping) else {}


def _isolation(caps: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = caps.get("isolation")
    return raw if isinstance(raw, Mapping) else {}


def _external(caps: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = caps.get("external_inputs")
    return raw if isinstance(raw, Mapping) else {}


def _planner(caps: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = caps.get("planner")
    return raw if isinstance(raw, Mapping) else {}


def capability_met(caps: Mapping[str, Any], name: str) -> bool:
    """True only for a positive, known capability. ``"unknown"`` is not enough."""
    if name == "permission_gate":
        # A channel that is bypassed by skip-permissions is not a gate.
        return _channels(caps).get("permission") is True and caps.get("skip_permissions") is False
    if name == "question_channel":
        return _channels(caps).get("question") is True
    if name == "review_channel":
        return _channels(caps).get("review") is True
    if name == "external_input_enforcement":
        return _external(caps).get("enforcement") == "permission_gate"
    if name == "os_sandbox":
        return _isolation(caps).get("os_sandbox") is True
    if name == "access_audit":
        return _isolation(caps).get("access_audit") == "decision_log"
    if name == "no_skip_permissions":
        return caps.get("skip_permissions") is False
    if name == "lead_review":
        # The planner being able to review is not enough: the backend must route
        # results to that lead (acceptance.lead_review is already planner AND
        # backend). agy has no review channel, so its output is never lead-reviewed.
        acceptance = caps.get("acceptance") if isinstance(caps.get("acceptance"), Mapping) else {}
        return _planner(caps).get("lead_review") is True and acceptance.get("lead_review") is True
    if name == "decomposition":
        return _planner(caps).get("decomposes") is True
    return False


def unmet_capabilities(caps: Mapping[str, Any], required: Sequence[str]) -> list[str]:
    """Required names this snapshot does not satisfy, in request order."""
    missing: list[str] = []
    for name in required:
        if name not in missing and not capability_met(caps, name):
            missing.append(name)
    return missing


class ExecutionBackendABC(ABC):
    backend_id: str = "abstract"

    def capabilities(self) -> dict[str, Any]:
        """What this backend actually enforces. Default is unknown/false, not a grant."""
        return default_capabilities(backend_id=str(getattr(self, "backend_id", "unknown")))

    @abstractmethod
    def start_run(
        self,
        *,
        title: str,
        directory: str,
        instruction: str = "",
        artifacts: list[str] | None = None,
        charter: dict | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def observe_run(
        self,
        run_id: str,
        *,
        dispatch_user_message_id: str | None = None,
        fetch_messages: bool = True,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def collect_result(self, run_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def list_pending_actions(self, *, session_id: str | None = None) -> tuple[int, list]:
        raise NotImplementedError

    @abstractmethod
    def reply_permission(self, request_id: str, reply: str) -> tuple[int, Any]:
        raise NotImplementedError

    @abstractmethod
    def cancel(self, run_id: str) -> tuple[int, Any]:
        raise NotImplementedError
