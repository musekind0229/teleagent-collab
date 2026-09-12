"""ExecutionBackend public surface — peer to TeleAgent adapter, not Hermes ledger."""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Protocol, runtime_checkable


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


class ExecutionBackendABC(ABC):
    backend_id: str = "abstract"

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
