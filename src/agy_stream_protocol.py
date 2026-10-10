"""Pure NDJSON stream protocol privacy parser.

Parses newline-delimited JSON lines into separate PUBLIC closed diagnostic metadata
and PRIVATE final payload.

Import restrictions: only json, math, typing, and dataclasses are permitted.
"""
from __future__ import annotations

import dataclasses
import json
import math
from typing import Any, Mapping

# Allowed event discriminator tokens
VALID_EVENT_TYPES: frozenset[str] = frozenset({"init", "step_update", "result"})

# Allowed source enums
VALID_SOURCE_TYPES: frozenset[str] = frozenset({"runner", "agent", "system"})

# Allowed step states
VALID_STEP_STATES: frozenset[str] = frozenset({"ACTIVE", "DONE"})

# Allowed step categories (documented category enums)
VALID_STEP_CATEGORIES: frozenset[str] = frozenset({
    "user_input",
    "agent_response",
    "tool",
    "checkpoint",
})

# Allowed terminal status types
VALID_STATUS_TYPES: frozenset[str] = frozenset({"SUCCESS", "FAILURE", "CANCELLED", "TIMEOUT"})

# Closed allowlist for numeric usage metrics
USAGE_ALLOWLIST: frozenset[str] = frozenset({
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "cache_read_tokens",
    "total_tokens",
})


def _validate_nonnegative_int(val: Any) -> int | None:
    """Validate that val is a nonnegative finite integer.
    
    Rejects bool, nonfinite floats (inf/nan), strings, and negative values.
    """
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if val >= 0 else None
    if isinstance(val, float):
        if math.isfinite(val) and val.is_integer() and val >= 0:
            return int(val)
        return None
    return None


def _validate_step_index(val: Any) -> int | None:
    """Validate that val is strictly an exact nonnegative integer.

    Rejects bool, all floats (including 1.0), strings, negatives, and None.
    """
    if isinstance(val, bool):
        return None
    if isinstance(val, int) and val >= 0:
        return val
    return None


def sanitize_usage_allowlist(raw: Any) -> dict[str, int] | None:
    """Sanitize usage mapping strictly against the USAGE_ALLOWLIST.
    
    Only permitted numeric keys are retained, and values must be nonnegative finite ints.
    Unknown keys and invalid values are omitted. Returns None if raw is not a mapping
    or if no valid keys are present.
    """
    if not isinstance(raw, (dict, Mapping)):
        return None
    res: dict[str, int] = {}
    for k in USAGE_ALLOWLIST:
        if k in raw:
            v = _validate_nonnegative_int(raw[k])
            if v is not None:
                res[k] = v
    if not res:
        return None
    return res


@dataclasses.dataclass(frozen=True)
class ParsedStreamLine:
    """Result interface for parsing a single NDJSON line.
    
    Attributes:
        public_metadata: Closed diagnostic metadata safe for logging/telemetry.
        private_payload: Terminal private payload kept strictly separate.
    """
    public_metadata: dict[str, Any]
    private_payload: dict[str, Any] | None = None


def parse_ndjson_line(line: str) -> ParsedStreamLine:
    """Parse one NDJSON line into separate PUBLIC closed metadata and PRIVATE final payload.
    
    Fixed protocol error is returned for malformed, non-object, or invalid inputs.
    No arbitrary strings or sensitive canaries leak into public metadata.
    """
    protocol_error_result = ParsedStreamLine(
        public_metadata={
            "event": "unknown",
            "source": "runner",
            "error": "protocol_error",
        },
        private_payload=None,
    )

    if not isinstance(line, str):
        return protocol_error_result

    s = line.strip()
    if not s or not s.startswith("{") or not s.endswith("}"):
        return protocol_error_result

    try:
        obj = json.loads(s)
    except Exception:
        return protocol_error_result

    if not isinstance(obj, dict):
        return protocol_error_result

    raw_event = obj.get("event")
    if not isinstance(raw_event, str) or raw_event not in VALID_EVENT_TYPES:
        return protocol_error_result

    # Base public metadata
    public_meta: dict[str, Any] = {
        "event": raw_event,
        "source": "runner",
    }

    if raw_event == "init":
        # init event has closed event and source
        return ParsedStreamLine(public_metadata=public_meta, private_payload=None)

    elif raw_event == "step_update":
        payload = obj.get("step_update")
        if not isinstance(payload, dict):
            return protocol_error_result

        step_update_meta: dict[str, Any] = {}

        # Validate step_index (exact nonnegative int; omit on invalid/missing)
        raw_idx = payload.get("step_index")
        idx_val = _validate_step_index(raw_idx)
        if idx_val is not None:
            step_update_meta["step_index"] = idx_val

        # Validate state: ACTIVE or DONE, otherwise unknown
        raw_state = payload.get("state")
        if isinstance(raw_state, str) and raw_state.strip().upper() in VALID_STEP_STATES:
            step_update_meta["state"] = raw_state.strip().upper()
        else:
            step_update_meta["state"] = "unknown"

        # Validate category: closed documented category enums, otherwise unknown
        raw_category = payload.get("category")
        if isinstance(raw_category, str) and raw_category.strip().lower() in VALID_STEP_CATEGORIES:
            step_update_meta["category"] = raw_category.strip().lower()
        else:
            step_update_meta["category"] = "unknown"

        # Usage under step_update if present
        if "usage" in payload:
            sanitized_usage = sanitize_usage_allowlist(payload.get("usage"))
            if sanitized_usage is not None:
                step_update_meta["usage"] = sanitized_usage

        public_meta["step_update"] = step_update_meta
        return ParsedStreamLine(public_metadata=public_meta, private_payload=None)

    elif raw_event == "result":
        payload = obj.get("result")
        if not isinstance(payload, dict):
            return protocol_error_result

        # Validate terminal status
        raw_status = payload.get("status")
        if not isinstance(raw_status, str) or raw_status.strip().upper() not in VALID_STATUS_TYPES:
            return protocol_error_result

        valid_status = raw_status.strip().upper()

        # Check for error: map raw error to fixed failure enum in public metadata
        has_error = payload.get("error") is not None
        effective_status = "FAILURE" if (has_error and valid_status == "SUCCESS") else valid_status

        public_meta["status"] = effective_status

        # Validate usage for public metadata
        sanitized_usage = sanitize_usage_allowlist(payload.get("usage"))
        if sanitized_usage is not None:
            public_meta["usage"] = sanitized_usage

        if has_error:
            public_meta["error"] = "protocol_failure"

        # Construct separate PRIVATE final payload
        private_payload: dict[str, Any] = {
            "status": effective_status,
        }
        if sanitized_usage is not None:
            private_payload["usage"] = sanitized_usage

        raw_cid = payload.get("conversation_id")
        if isinstance(raw_cid, str) and raw_cid.strip():
            private_payload["conversation_id"] = raw_cid.strip()

        raw_resp = payload.get("response")
        if raw_resp is not None:
            private_payload["response"] = str(raw_resp)

        if has_error:
            private_payload["error"] = "protocol_failure"

        return ParsedStreamLine(public_metadata=public_meta, private_payload=private_payload)

    return protocol_error_result
