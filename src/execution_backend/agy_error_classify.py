"""Classify agy print/models failures without printing secrets.

Ported from the multi-account probe classifier. This module must not import
or read anything under the probe tree at runtime.

Classes (first match wins):
  eligibility_blocked   -- signed in but Antigravity not available (location/account)
  auth_invalid          -- not signed in / authentication required or failed
  quota_exhausted       -- MODEL_CAPACITY_EXHAUSTED / RESOURCE_EXHAUSTED / credits
                           / model 503 No capacity (NOT eligibility)
  rate_limit            -- 429 / overloaded / try again later
  ok                    -- JSON status SUCCESS/OK/COMPLETED/STOP/DONE
  empty_failure         -- no stdout and no stderr
  ordinary_task_failure -- other ERROR / non-zero / leftover failure text

eligibility_blocked is not auth_invalid and not quota_exhausted: oauth may
already be on disk; the account is simply not eligible in this location.
Scheduler mapping: eligibility_blocked -> unavailable (do not dispatch).

Model ``UNAVAILABLE (code 503): No capacity…`` is quota/capacity, not
eligibility — do not mark the HOME unavailable for geo/product block.
"""
from __future__ import annotations

import json
import re
from typing import Any

ELIG = re.compile(
    r"Eligibility check failed|"
    r"not currently available in your location|"
    r"not eligible for Antigravity",
    re.I,
)
AUTH = re.compile(
    r"authentication required|authentication failed|please sign in|"
    r"not signed in|unauthenticated|unauthorized|login",
    re.I,
)
QUOTA = re.compile(
    r"MODEL_CAPACITY_EXHAUSTED|"
    r"capacity.?exhausted|"
    r"RESOURCE_EXHAUSTED|"
    r"quota.?exceed|"
    r"out of credits|"
    r"no credits|"
    r"fetchQuotaStatus|"
    r"no capacity|"
    r"UNAVAILABLE\s*\(code\s*503\)|"
    r"\(code\s*503\)",
    re.I,
)
RATE = re.compile(
    r"\b429\b|rate.?limit|overloaded|temporarily unavailable|try again later",
    re.I,
)

_OK_STATUS = frozenset({"SUCCESS", "OK", "COMPLETED", "STOP", "DONE"})
_FAIL_STATUS = frozenset({"ERROR", "FAILED", "FAIL"})

CLASS_ELIGIBILITY_BLOCKED = "eligibility_blocked"
CLASS_AUTH_INVALID = "auth_invalid"
CLASS_QUOTA_EXHAUSTED = "quota_exhausted"
CLASS_RATE_LIMIT = "rate_limit"
CLASS_OK = "ok"
CLASS_EMPTY_FAILURE = "empty_failure"
CLASS_ORDINARY = "ordinary_task_failure"


def classify(stdout: str = "", stderr: str = "", rc: int | None = None) -> str:
    """Return one of the classes above. First match wins; eligibility before auth/quota."""
    blob = f"{stdout}\n{stderr}"
    status = None
    try:
        obj = json.loads((stdout or "").strip())
        if isinstance(obj, dict):
            status = str(obj.get("status") or "")
            blob += "\n" + str(obj.get("error") or "")
    except Exception:
        pass
    # Eligibility is more specific than auth/quota and must win first:
    # the account is logged in; the product is geo-blocked.
    if ELIG.search(blob):
        return CLASS_ELIGIBILITY_BLOCKED
    if AUTH.search(blob):
        return CLASS_AUTH_INVALID
    if QUOTA.search(blob):
        return CLASS_QUOTA_EXHAUSTED
    if RATE.search(blob):
        return CLASS_RATE_LIMIT
    if status and status.upper() in _OK_STATUS:
        return CLASS_OK
    if (rc not in (None, 0)) or (status and status.upper() in _FAIL_STATUS):
        return CLASS_ORDINARY
    if not (stdout or stderr):
        return CLASS_EMPTY_FAILURE
    return CLASS_ORDINARY


def classify_agy_error(
    stdout: str = "",
    stderr: str = "",
    rc: int | None = None,
    *,
    error: str = "",
) -> str:
    """Same as classify; ``error`` is appended when collect_result only has that field."""
    extra_err = error or ""
    return classify(stdout, f"{stderr}\n{extra_err}".strip("\n"), rc)


def classify_result(result: dict[str, Any] | None) -> str:
    """Classify an ExecutionBackend collect_result / start_run payload."""
    if not isinstance(result, dict):
        return CLASS_EMPTY_FAILURE
    if result.get("ok") is True and not result.get("error"):
        return CLASS_OK
    stdout = str(result.get("stdout") or result.get("response") or "")
    stderr = str(result.get("stderr") or "")
    err = str(result.get("error") or result.get("assistant_error") or "")
    rc = result.get("returncode")
    if rc is None and result.get("ok") is False:
        rc = 1
    return classify_agy_error(stdout, stderr, rc, error=err)


__all__ = [
    "CLASS_AUTH_INVALID",
    "CLASS_ELIGIBILITY_BLOCKED",
    "CLASS_EMPTY_FAILURE",
    "CLASS_OK",
    "CLASS_ORDINARY",
    "CLASS_QUOTA_EXHAUSTED",
    "CLASS_RATE_LIMIT",
    "classify",
    "classify_agy_error",
    "classify_result",
]
