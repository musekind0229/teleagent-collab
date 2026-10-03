#!/usr/bin/env python3
"""Classify agy print failures without printing secrets.

Classes (first match wins):
  eligibility_blocked   -- signed in but Antigravity not available (location/account)
  auth_invalid          -- not signed in / authentication required or failed
  quota_exhausted       -- MODEL_CAPACITY_EXHAUSTED / RESOURCE_EXHAUSTED / credits
  rate_limit            -- 429 / overloaded / try again later
  ok                    -- JSON status SUCCESS/OK/COMPLETED/STOP/DONE
  empty_failure         -- no stdout and no stderr
  ordinary_task_failure -- other ERROR / non-zero / leftover failure text

eligibility_blocked is not auth_invalid and not quota_exhausted: oauth may
already be on disk; the account is simply not eligible in this location.
Scheduler mapping: eligibility_blocked -> unavailable (do not dispatch).
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

ELIG = re.compile(
    r"Eligibility check failed|"
    r"not currently available in your location|"
    r"not eligible for Antigravity",
    re.I,
)
AUTH = re.compile(r"authentication required|authentication failed|please sign in|not signed in|unauthenticated|unauthorized|login", re.I)
QUOTA = re.compile(r"MODEL_CAPACITY_EXHAUSTED|capacity.?exhausted|RESOURCE_EXHAUSTED|quota.?exceed|out of credits|no credits|fetchQuotaStatus", re.I)
RATE = re.compile(r"\b429\b|rate.?limit|overloaded|temporarily unavailable|try again later", re.I)

def classify(stdout: str = "", stderr: str = "", rc: int | None = None) -> str:
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
        return "eligibility_blocked"
    if AUTH.search(blob):
        return "auth_invalid"
    if QUOTA.search(blob):
        return "quota_exhausted"
    if RATE.search(blob):
        return "rate_limit"
    if status and status.upper() in {"SUCCESS", "OK", "COMPLETED", "STOP", "DONE"}:
        return "ok"
    if (rc not in (None, 0)) or (status and status.upper() in {"ERROR", "FAILED", "FAIL"}):
        return "ordinary_task_failure"
    if not (stdout or stderr):
        return "empty_failure"
    return "ordinary_task_failure"

def main() -> int:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "fake_errors.jsonl"
    rows = [json.loads(l) for l in fixture.read_text().splitlines() if l.strip()]
    ok = 0
    for r in rows:
        got = classify(r.get("stdout", ""), r.get("stderr", ""))
        match = got == r["expect"]
        ok += int(match)
        print(json.dumps({"id": r["id"], "expect": r["expect"], "got": got, "pass": match}))
    print(json.dumps({"passed": ok, "total": len(rows)}))
    return 0 if ok == len(rows) else 1

if __name__ == "__main__":
    raise SystemExit(main())
