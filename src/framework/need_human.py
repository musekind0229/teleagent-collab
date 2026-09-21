"""Parse and sanitize need_human markers from worker / backend errors.

Win collab fail-closed recovery uses ``need_human: <reason>`` on job.error.
Goal HTTP callers should see a stable flag + short reason (no secrets), not
only a buried string inside tasks[].result.error.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

NEED_HUMAN_PREFIX = "need_human:"
_REASON_MAX = 300

# Redact obvious credential-shaped substrings; keep human-readable reason text.
_SECRETISH = re.compile(
    r"(?i)(bearer\s+[a-z0-9._\-+=/]+|"
    r"sk-[a-z0-9]{8,}|"
    r"(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+|"
    r"[a-f0-9]{48,})",
)


def sanitize_reason(text: str, *, max_len: int = _REASON_MAX) -> str:
    cleaned = _SECRETISH.sub("[redacted]", str(text or ""))
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3].rstrip() + "..."
    return cleaned


def parse_need_human(error: Any) -> dict[str, Any] | None:
    """Return ``{need_human, reason}`` when error is a need_human marker."""
    raw = str(error or "").strip()
    if not raw:
        return None
    lower = raw.lower()
    if not lower.startswith(NEED_HUMAN_PREFIX):
        return None
    body = raw[len(NEED_HUMAN_PREFIX) :].lstrip()
    reason = sanitize_reason(body or raw)
    return {"need_human": True, "reason": reason}


def enrich_result_for_need_human(result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Copy result and attach need_human / failure_reason when applicable."""
    if not isinstance(result, Mapping):
        return None
    out = dict(result)
    parsed = parse_need_human(out.get("error"))
    if parsed is None and out.get("need_human") is True:
        reason = sanitize_reason(str(out.get("failure_reason") or out.get("error") or "need_human"))
        parsed = {"need_human": True, "reason": reason}
    if parsed is None:
        return out
    out["need_human"] = True
    out["failure_reason"] = parsed["reason"]
    # Keep original error; ensure prefix so greppers still work.
    err = str(out.get("error") or "").strip()
    if err and not err.lower().startswith(NEED_HUMAN_PREFIX):
        out["error"] = f"{NEED_HUMAN_PREFIX} {sanitize_reason(err)}"
    elif not err:
        out["error"] = f"{NEED_HUMAN_PREFIX} {parsed['reason']}"
    else:
        # Re-sanitize body of existing need_human error
        body = err[len(NEED_HUMAN_PREFIX) :].lstrip() if err.lower().startswith(NEED_HUMAN_PREFIX) else err
        out["error"] = f"{NEED_HUMAN_PREFIX} {sanitize_reason(body)}"
    return out


def goal_need_human_view(
    *,
    failure: Mapping[str, Any] | None,
    tasks: list | None,
) -> dict[str, Any]:
    """Top-level fields for GET /v1/requests/{id}."""
    if isinstance(failure, Mapping) and (
        failure.get("need_human") is True
        or parse_need_human(failure.get("error")) is not None
    ):
        reason = sanitize_reason(
            str(failure.get("failure_reason") or failure.get("error") or "need_human")
        )
        return {"need_human": True, "failure_reason": reason}
    for task in tasks or []:
        if not isinstance(task, Mapping):
            continue
        result = task.get("result") if isinstance(task.get("result"), Mapping) else {}
        if result.get("need_human") is True:
            reason = sanitize_reason(
                str(result.get("failure_reason") or result.get("error") or "need_human")
            )
            return {"need_human": True, "failure_reason": reason}
        parsed = parse_need_human(result.get("error"))
        if parsed is not None:
            return {"need_human": True, "failure_reason": parsed["reason"]}
        if str(task.get("status") or "") == "failed":
            parsed = parse_need_human(task.get("error"))
            if parsed is not None:
                return {"need_human": True, "failure_reason": parsed["reason"]}
    return {"need_human": False, "failure_reason": ""}
