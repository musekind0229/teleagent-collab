"""Pluggable lead adapters (条3).

Grok CLI, Codex CLI (``codex_cli``, read-only sandbox, live login unverified),
and DeepSeek harness are backends. Claude Code is still a stub. The name
``codex`` is dialogue-as-lead (inprocess), not the Codex CLI.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from lead_adapter.base import LeadAdapter, LeadAdapterABC, safe_failure
from lead_adapter.claude_code import ClaudeCodeLeadAdapter
from lead_adapter.codex_cli import CodexCliLeadAdapter
from lead_adapter.deepseek_harness import DeepSeekHarnessLeadAdapter
from lead_adapter.grok_cli import (
    GrokCliLeadAdapter,
    LeadBinNotFound,
    default_live_base_url,
    resolve_lead_bin,
)
from lead_adapter.inprocess import InProcessLeadAdapter
from lead_adapter.schema import (
    LeadDecisionError,
    build_lead_request,
    format_lead_request_prompt,
    lead_permission_response_schema,
    lead_review_response_schema,
    pin_lead_response_schema,
    unwrap_structured,
    validate_lead_decision,
)

__all__ = [
    "LeadAdapter",
    "LeadAdapterABC",
    "GrokCliLeadAdapter",
    "LeadBinNotFound",
    "resolve_lead_bin",
    "default_live_base_url",
    "InProcessLeadAdapter",
    "ClaudeCodeLeadAdapter",
    "CodexCliLeadAdapter",
    "DeepSeekHarnessLeadAdapter",
    "LeadDecisionError",
    "safe_failure",
    "build_lead_request",
    "format_lead_request_prompt",
    "lead_permission_response_schema",
    "lead_review_response_schema",
    "pin_lead_response_schema",
    "unwrap_structured",
    "validate_lead_decision",
    "get_lead_adapter",
]

_log = logging.getLogger("lead_adapter")

# Exact warning text for the historical "codex" alias (still inprocess).
_CODEX_ALIAS_WARNING = (
    'lead adapter alias "codex" means inprocess (dialogue-as-lead), '
    "not the Codex CLI; 要用 Codex CLI 请设 codex_cli"
)


def get_lead_adapter(kind: str | None = None, **kwargs: Any) -> LeadAdapter:
    """Factory. COLLAB_LEAD_ADAPTER=grok_cli|inprocess|claude_code|codex_cli|deepseek_harness.

    Default grok_cli. ``claude_code`` is still a stub and fails closed
    (call_failed, never fake PASS).

    ``codex_cli`` / ``codex-cli`` spawn the Codex CLI once (``--sandbox read-only``,
    ``approval_policy=never``). Missing binary → call_failed on decide; the
    constructor does not raise. Live Codex login is not verified.

    ``codex`` (and ``COLLAB_LEAD_ADAPTER=codex``) stays ``InProcessLeadAdapter``
    — the current dialogue as lead — and logs a warning. It is not the Codex CLI.

    deepseek / deepseek_harness is JSON-in/JSON-out over dsh --profile headless;
    missing bin/key or illegal JSON → call_failed, never fake PASS.
    """
    name = (kind or os.environ.get("COLLAB_LEAD_ADAPTER") or "grok_cli").strip().lower()
    if name in ("inprocess", "in_process", "codex", "file", "stdin"):
        # "codex" stays dialogue-as-lead. Codex CLI is the separate kind codex_cli.
        if name == "codex":
            _log.warning(_CODEX_ALIAS_WARNING)
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
        return CodexCliLeadAdapter(**{
            k: v for k, v in kwargs.items()
            if k in ("bin_path",)
        })
    if name in ("deepseek", "deepseek_harness", "deepseek-harness"):
        return DeepSeekHarnessLeadAdapter(**{
            k: v for k, v in kwargs.items()
            if k in ("bin_path", "io_mode", "disallowed_tools", "max_turns")
        })
    raise ValueError(f"unknown lead adapter kind: {name!r}")
