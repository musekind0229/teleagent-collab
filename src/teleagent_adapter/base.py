"""TeleAgent adapter Protocol / ABC + creds refresh / reconnect / resume rules."""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class AdapterStatus(str, Enum):
    OK = "ok"
    NOT_RUNNING = "not_running"
    VERSION_INCOMPATIBLE = "version_incompatible"
    MISSING_CREDS = "missing_creds"
    AUTH_FAILED = "auth_failed"
    API_INCOMPATIBLE = "api_incompatible"
    BLOCKED = "blocked"


class AdapterError(RuntimeError):
    def __init__(self, status: AdapterStatus | str, message: str = "") -> None:
        self.status = AdapterStatus(status) if not isinstance(status, AdapterStatus) else status
        super().__init__(message or self.status.value)


def creds_refresh_policy() -> dict[str, str]:
    """Rules for credential refresh (documented + enforceable by adapters)."""
    return {
        "source": "Read OPENCODE_SERVER_USERNAME/PASSWORD + SUPER_AGENT_LOCAL_SESSION_KEY "
        "from GUI/SAC child process environ only (memory). Never write secrets to git/reports.",
        "when": "On AdapterStatus.AUTH_FAILED / HTTP 401/403 / local_auth_missing; "
        "or explicit refresh_creds(). Do not poll-refresh on every call.",
        "scope": "Linux local-v1 only. Windows has no supported auth entry → blocked.",
    }


def reconnect_policy() -> dict[str, str]:
    return {
        "when": "TCP/connection errors to base_url, or doctor reports not_running then recovers.",
        "action": "Re-resolve base_url (default http://127.0.0.1:4399), refresh_creds once, "
        "retry the failed call at most once. Do not open public diagnostic ports.",
        "forbid": "Do not disable auth, change install packages, or deploy over IPv6 as a workaround.",
    }


def resume_policy() -> dict[str, str]:
    return {
        "when": "Caller supplies an existing session_id after process restart or transient disconnect.",
        "action": "resume(session_id) verifies session exists via GET /session/:id or list; "
        "then prompt/list_permissions continue on that id. Do not create a duplicate session.",
        "session_filter": "All permission/question/status/message handling MUST filter by sessionID.",
    }


@runtime_checkable
class TeleAgentAdapter(Protocol):
    """Minimal worker-facing TeleAgent surface used by glue/scheduler."""

    def create_session(self, *, title: str, directory: str) -> tuple[int, Any]:
        ...

    def prompt(self, session_id: str, text: str, *, directory: str | None = None) -> tuple[int, Any]:
        ...

    def list_permissions(self, *, session_id: str | None = None) -> tuple[int, list]:
        ...

    def list_questions(self, *, session_id: str | None = None) -> tuple[int, list]:
        ...

    def reply_question(self, request_id: str, answers: list) -> tuple[int, Any]:
        ...

    def reject_question(self, request_id: str) -> tuple[int, Any]:
        ...

    def reply_permission(self, request_id: str, reply: str) -> tuple[int, Any]:
        ...

    def session_status(self, session_id: str | None = None) -> tuple[int, Any]:
        ...

    def observe_run(
        self,
        session_id: str,
        *,
        dispatch_user_message_id: str | None = None,
        fetch_messages: bool = True,
    ) -> dict:
        ...

    def cancel(self, session_id: str) -> tuple[int, Any]:
        ...

    def refresh_creds(self) -> None:
        ...

    def reconnect(self) -> None:
        ...

    def resume(self, session_id: str) -> tuple[int, Any]:
        ...


class TeleAgentAdapterABC(ABC):
    """ABC twin of TeleAgentAdapter for concrete subclasses."""

    @abstractmethod
    def create_session(self, *, title: str, directory: str) -> tuple[int, Any]:
        raise NotImplementedError

    @abstractmethod
    def prompt(self, session_id: str, text: str, *, directory: str | None = None) -> tuple[int, Any]:
        raise NotImplementedError

    @abstractmethod
    def list_permissions(self, *, session_id: str | None = None) -> tuple[int, list]:
        raise NotImplementedError

    @abstractmethod
    def list_questions(self, *, session_id: str | None = None) -> tuple[int, list]:
        raise NotImplementedError

    @abstractmethod
    def reply_question(self, request_id: str, answers: list) -> tuple[int, Any]:
        raise NotImplementedError

    @abstractmethod
    def reject_question(self, request_id: str) -> tuple[int, Any]:
        raise NotImplementedError

    @abstractmethod
    def reply_permission(self, request_id: str, reply: str) -> tuple[int, Any]:
        raise NotImplementedError

    @abstractmethod
    def session_status(self, session_id: str | None = None) -> tuple[int, Any]:
        raise NotImplementedError

    @abstractmethod
    def observe_run(
        self,
        session_id: str,
        *,
        dispatch_user_message_id: str | None = None,
        fetch_messages: bool = True,
    ) -> dict:
        raise NotImplementedError

    @abstractmethod
    def cancel(self, session_id: str) -> tuple[int, Any]:
        raise NotImplementedError

    @abstractmethod
    def refresh_creds(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def reconnect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def resume(self, session_id: str) -> tuple[int, Any]:
        raise NotImplementedError


def session_id_of(obj: dict) -> str:
    if not isinstance(obj, dict):
        return ""
    for k in ("sessionID", "session_id", "sessionId"):
        v = obj.get(k)
        if v:
            return str(v)
    meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    for k in ("sessionID", "session_id", "sessionId"):
        v = meta.get(k)
        if v:
            return str(v)
    return ""


def filter_by_session(items: list | None, session_id: str | None) -> list:
    """Keep only items whose sessionID matches; empty session_id → return as-is."""
    if not isinstance(items, list):
        return []
    if not session_id:
        return [x for x in items if isinstance(x, dict)]
    out = []
    for x in items:
        if not isinstance(x, dict):
            continue
        sid = session_id_of(x)
        if sid == session_id:
            out.append(x)
    return out


__all__ = [
    "AdapterStatus",
    "AdapterError",
    "TeleAgentAdapter",
    "TeleAgentAdapterABC",
    "creds_refresh_policy",
    "reconnect_policy",
    "resume_policy",
    "session_id_of",
    "filter_by_session",
]
