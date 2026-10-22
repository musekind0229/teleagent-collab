"""Classify agy print/models failures without printing secrets.

Ported from the multi-account probe classifier. This module must not import
or read anything under the probe tree at runtime.

Classes (first match wins):
  eligibility_blocked   -- signed in but Antigravity not available (location/account)
  auth_invalid          -- not signed in / authentication required or failed
  quota_exhausted       -- MODEL_CAPACITY_EXHAUSTED / RESOURCE_EXHAUSTED / credits
                           (true quota; may use day_boundary cooldown)
  temporary_no_capacity -- model 503 / No capacity (transient; short duration
                           cooldown — NOT permanent exhausted, NOT eligibility)
  rate_limit            -- 429 / overloaded / try again later
  ok                    -- JSON status SUCCESS/OK/COMPLETED/STOP/DONE, or rc in
                           (None, 0) with non-empty output that matched none of
                           the classes above and is not a FAIL status.
                           ``agy models`` success is a plain model list (no
                           JSON status field) and must be ok.
  empty_failure         -- no stdout and no stderr
  ordinary_task_failure -- non-zero rc, or JSON status ERROR/FAILED/FAIL

eligibility_blocked is not auth_invalid and not quota_exhausted: oauth may
already be on disk; the account is simply not eligible in this location.
Explicit genuine location/product eligibility blocks win first (even if mixed
with 503 or auth/quota). The known transient envelope "Eligibility check
failed: UNAVAILABLE (code 503): The service is currently unavailable" yields to
auth_invalid if auth matches, quota_exhausted if quota matches, else
temporary_no_capacity (not eligibility_blocked). Other eligibility checks stay
eligibility_blocked before auth/quota.
Scheduler mapping (agy_account_pool.apply_class_to_state):
  eligibility_blocked / auth_invalid -> unavailable (do not dispatch)
  quota_exhausted -> cooldown (pool cooldown_sec / cooldown_mode; recoverable)
  temporary_no_capacity / rate_limit -> short duration cooldown
      (temp_cooldown_sec; always duration — never day_boundary / never
       permanent exhausted; expires like cooldown, not like exhausted)
  ok / ordinary_task_failure -> no account-state change

Model ``UNAVAILABLE (code 503): No capacity…`` is *temporary* capacity,
not daily quota and not eligibility — do not mark the HOME unavailable for
geo/product block, and do not wait until local midnight.
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
# True account/model quota — may justify day_boundary. Do NOT include bare 503.
QUOTA = re.compile(
    r"MODEL_CAPACITY_EXHAUSTED|"
    r"capacity.?exhausted|"
    r"RESOURCE_EXHAUSTED|"
    r"quota.?exceed|"
    r"out of credits|"
    r"no credits|"
    r"fetchQuotaStatus",
    re.I,
)
# Transient server capacity — short cooldown only (checked after QUOTA so a
# blob that also says MODEL_CAPACITY_EXHAUSTED still counts as true quota).
TEMP_NO_CAPACITY = re.compile(
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
CLASS_TEMPORARY_NO_CAPACITY = "temporary_no_capacity"
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
    # Explicit genuine location/product eligibility block must win first (even if mixed with 503 or auth/quota):
    if re.search(r"not currently available in your location|not eligible for Antigravity", blob, re.I):
        return CLASS_ELIGIBILITY_BLOCKED
    # Fixed known eligibility 503 envelope (transient service unavailable, not account/geo block).
    # Only this exact known 503 envelope yields to AUTH or QUOTA when mixed:
    if re.search(r"Eligibility check failed:\s*UNAVAILABLE\s*\(code\s*503\):\s*The service is currently unavailable", blob, re.I):
        if AUTH.search(blob):
            return CLASS_AUTH_INVALID
        if QUOTA.search(blob):
            return CLASS_QUOTA_EXHAUSTED
        return CLASS_TEMPORARY_NO_CAPACITY
    # Remaining bare or other eligibility blocks (including unknown mixed with auth or quota):
    if ELIG.search(blob):
        return CLASS_ELIGIBILITY_BLOCKED
    if AUTH.search(blob):
        return CLASS_AUTH_INVALID
    if QUOTA.search(blob):
        return CLASS_QUOTA_EXHAUSTED
    if TEMP_NO_CAPACITY.search(blob):
        return CLASS_TEMPORARY_NO_CAPACITY
    if RATE.search(blob):
        return CLASS_RATE_LIMIT
    if status and status.upper() in _OK_STATUS:
        return CLASS_OK
    if (rc not in (None, 0)) or (status and status.upper() in _FAIL_STATUS):
        return CLASS_ORDINARY
    if not (stdout or stderr):
        return CLASS_EMPTY_FAILURE
    # rc is None or 0, output is non-empty, and nothing above matched.
    # Successful ``agy models`` is a plain list, not a JSON status.
    return CLASS_OK


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
    "CLASS_TEMPORARY_NO_CAPACITY",
    "classify",
    "classify_agy_error",
    "classify_result",
]
