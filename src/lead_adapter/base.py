"""LeadAdapter protocol — pluggable external lead (条3)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LeadAdapter(Protocol):
    """External lead: read pending ask via structured protocol, submit JSON decision."""

    name: str

    def decide(
        self,
        request: dict,
        *,
        schema: dict,
        cwd: str,
        timeout_sec: float = 180,
    ) -> tuple[str, dict | None]:
        """Return (raw_text, parsed_json_or_status).

        On timeout / call failure return raw marker + dict with _lead_status
        in {timeout, call_failed, error}. Never strip safety constraints to retry.
        """
        ...


class LeadAdapterABC(ABC):
    name: str = "base"

    @abstractmethod
    def decide(
        self,
        request: dict,
        *,
        schema: dict,
        cwd: str,
        timeout_sec: float = 180,
    ) -> tuple[str, dict | None]:
        raise NotImplementedError


def safe_failure(status: str, message: str = "") -> tuple[str, dict]:
    """Canonical failure envelope — keep pending / safe-stop upstream."""
    return status.upper(), {"_lead_status": status, "error": message or status}


__all__ = ["LeadAdapter", "LeadAdapterABC", "safe_failure"]
