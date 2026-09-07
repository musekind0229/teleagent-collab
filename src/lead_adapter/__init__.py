"""Pluggable lead adapters (条3): Grok CLI is one backend; in-process Codex dialogue supported."""
from __future__ import annotations

import os
from typing import Any

from lead_adapter.base import LeadAdapter, LeadAdapterABC, safe_failure
from lead_adapter.grok_cli import GrokCliLeadAdapter
from lead_adapter.inprocess import InProcessLeadAdapter
from lead_adapter.schema import (
    LeadDecisionError,
    build_lead_request,
    format_lead_request_prompt,
    lead_permission_response_schema,
    lead_review_response_schema,
    validate_lead_decision,
)

__all__ = [
    "LeadAdapter",
    "LeadAdapterABC",
    "GrokCliLeadAdapter",
    "InProcessLeadAdapter",
    "LeadDecisionError",
    "safe_failure",
    "build_lead_request",
    "format_lead_request_prompt",
    "lead_permission_response_schema",
    "lead_review_response_schema",
    "validate_lead_decision",
    "get_lead_adapter",
]


def get_lead_adapter(kind: str | None = None, **kwargs: Any) -> LeadAdapter:
    """Factory. COLLAB_LEAD_ADAPTER=grok_cli|inprocess (default grok_cli)."""
    name = (kind or os.environ.get("COLLAB_LEAD_ADAPTER") or "grok_cli").strip().lower()
    if name in ("inprocess", "in_process", "codex", "file", "stdin"):
        return InProcessLeadAdapter(**{
            k: v for k, v in kwargs.items()
            if k in ("exchange_dir", "decision_fn", "poll_interval")
        })
    if name in ("grok", "grok_cli", "cli"):
        return GrokCliLeadAdapter(**{
            k: v for k, v in kwargs.items()
            if k in ("bin_path", "disallowed_tools", "max_turns")
        })
    raise ValueError(f"unknown lead adapter kind: {name!r}")
