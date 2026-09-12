"""Windows TeleAgent adapter stub — 2.4.1 has no supported auth entry → blocked."""
from __future__ import annotations

from typing import Any

from teleagent_adapter.base import AdapterError, AdapterStatus, TeleAgentAdapterABC


class WindowsBlockedAdapter(TeleAgentAdapterABC):
    """Win TeleAgent 2.4.1: no supported Basic/local-v1 auth entry for workers.

    All worker operations raise AdapterError(BLOCKED). Do not invent workarounds
    (disable auth, open public ports, scrape GUI tokens).
    """

    PLATFORM = "windows"
    TELEAGENT_VERSION = "2.4.1"
    REASON = (
        "Windows TeleAgent 2.4.1 has no supported worker authentication entry "
        "(no documented local-v1 HMAC / Basic equivalent for automation). "
        "Adapter status=blocked. Use Linux local-v1 (:4399) instead."
    )

    def __init__(self, teleagent_version: str = "2.4.1") -> None:
        self.teleagent_version = teleagent_version or self.TELEAGENT_VERSION

    def _blocked(self) -> None:
        raise AdapterError(
            AdapterStatus.BLOCKED,
            f"{self.REASON} (reported version={self.teleagent_version})",
        )

    def create_session(self, *, title: str, directory: str) -> tuple[int, Any]:
        self._blocked()

    def prompt(self, session_id: str, text: str, *, directory: str | None = None) -> tuple[int, Any]:
        self._blocked()

    def list_permissions(self, *, session_id: str | None = None) -> tuple[int, list]:
        self._blocked()

    def list_pending_actions(self, *, session_id: str | None = None) -> tuple[int, list]:
        self._blocked()

    def list_questions(self, *, session_id: str | None = None) -> tuple[int, list]:
        self._blocked()

    def reply_question(self, request_id: str, answers: list) -> tuple[int, Any]:
        self._blocked()

    def reject_question(self, request_id: str) -> tuple[int, Any]:
        self._blocked()

    def reply_permission(self, request_id: str, reply: str) -> tuple[int, Any]:
        self._blocked()

    def session_status(self, session_id: str | None = None) -> tuple[int, Any]:
        self._blocked()

    def observe_run(
        self,
        session_id: str,
        *,
        dispatch_user_message_id: str | None = None,
        fetch_messages: bool = True,
    ) -> dict:
        self._blocked()

    def cancel(self, session_id: str) -> tuple[int, Any]:
        self._blocked()

    def refresh_creds(self) -> None:
        self._blocked()

    def reconnect(self) -> None:
        self._blocked()

    def resume(self, session_id: str) -> tuple[int, Any]:
        self._blocked()

    def doctor_hint(self) -> dict[str, str]:
        return {
            "status": AdapterStatus.BLOCKED.value,
            "platform": self.PLATFORM,
            "teleagent_version": self.teleagent_version,
            "reason": self.REASON,
        }


__all__ = ["WindowsBlockedAdapter"]
