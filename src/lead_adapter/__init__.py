"""Pluggable lead adapters (条3): Grok CLI is one backend; Claude/Codex stubs 待办."""
from __future__ import annotations

import os
from typing import Any

from lead_adapter.base import LeadAdapter, LeadAdapterABC, safe_failure
from lead_adapter.claude_code import ClaudeCodeLeadAdapter
from lead_adapter.codex_cli import CodexCliLeadAdapter
from lead_adapter.grok_cli import GrokCliLeadAdapter
from lead_adapter.inprocess import InProcessLeadAdapter
from lead_adapter.schema import (
    LeadDecisionError,
    build_lead_request,
    format_lead_request_prompt,
    lead_permission_response_schema,
    lead_review_response_schema,
    unwrap_structured,
    validate_lead_decision,
)

__all__ = [
    "LeadAdapter",
    "LeadAdapterABC",
    "GrokCliLeadAdapter",
    "InProcessLeadAdapter",
    "ClaudeCodeLeadAdapter",
    "CodexCliLeadAdapter",
    "LeadDecisionError",
    "safe_failure",
    "build_lead_request",
    "format_lead_request_prompt",
    "lead_permission_response_schema",
    "lead_review_response_schema",
    "unwrap_structured",
    "validate_lead_decision",
    "get_lead_adapter",
]


def get_lead_adapter(kind: str | None = None, **kwargs: Any) -> LeadAdapter:
    """Factory. COLLAB_LEAD_ADAPTER=grok_cli|inprocess|claude_code|codex_cli (default grok_cli).

    Claude/Codex kinds return stubs that fail closed (call_failed) — never fake PASS.
    """
    name = (kind or os.environ.get("COLLAB_LEAD_ADAPTER") or "grok_cli").strip().lower()
    if name in ("inprocess", "in_process", "codex", "file", "stdin"):
        # "codex" alias keeps dialogue-as-lead (inprocess). Use codex_cli for the stub.
        return InProcessLeadAdapter(**{
            k: v for k, v in kwargs.items()
            if k in ("exchange_dir", "decision_fn", "poll_interval")
        })
    if name in ("grok", "grok_cli", "cli"):
        return GrokCliLeadAdapter(**{
            k: v for k, v in kwargs.items()
            if k in ("bin_path", "disallowed_tools", "max_turns")
        })
    if name in ("claude", "claude_code", "claude-code"):
        return ClaudeCodeLeadAdapter()
    if name in ("codex_cli", "codex-cli"):
        return CodexCliLeadAdapter()
    raise ValueError(f"unknown lead adapter kind: {name!r}")
