"""Claude Code LeadAdapter stub — 待办真机；禁止假 PASS。

Not implemented for live runs. Returns call_failed so orchestration keeps
pending / safe-stop. Wire a real CLI when available; do not invent approvals.
"""
from __future__ import annotations

from lead_adapter.base import LeadAdapterABC, safe_failure


class ClaudeCodeLeadAdapter(LeadAdapterABC):
    """Skeleton only. Status: 待办 (TODO). Never auto-approves."""

    name = "claude_code"
    STATUS = "todo"
    REASON = (
        "Claude Code lead adapter is a stub (P3). "
        "No live CLI wiring yet — refuse to invent PASS/once. "
        "Use grok_cli or inprocess until implemented."
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


__all__ = ["ClaudeCodeLeadAdapter"]
