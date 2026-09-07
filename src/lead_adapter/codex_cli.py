"""Codex CLI LeadAdapter stub — 待办真机；禁止假 PASS。

Skeleton only. Returns call_failed. Prefer InProcessLeadAdapter when the
current Codex/conversation dialogue acts as lead (file exchange protocol).
"""
from __future__ import annotations

from lead_adapter.base import LeadAdapterABC, safe_failure


class CodexCliLeadAdapter(LeadAdapterABC):
    """Skeleton only. Status: 待办 (TODO). Never auto-approves."""

    name = "codex_cli"
    STATUS = "todo"
    REASON = (
        "Codex CLI lead adapter is a stub (P3). "
        "No separate codex binary spawn yet — refuse to invent PASS/once. "
        "Use COLLAB_LEAD_ADAPTER=inprocess (file/decision_fn) or grok_cli."
    )

    def decide(
        self,
        request: dict,
        *,
        schema: dict,
        cwd: str,
        timeout_sec: float = 180,
    ) -> tuple[str, dict | None]:
        return safe_failure(
            "call_failed",
            f"{self.REASON} application_id={request.get('application_id')!r}",
        )

    def doctor_hint(self) -> dict:
        return {
            "status": self.STATUS,
            "name": self.name,
            "reason": self.REASON,
            "fake_pass": False,
        }


__all__ = ["CodexCliLeadAdapter"]
