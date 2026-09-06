#!/usr/bin/env python3
"""Minimal glue-path sim for 刀3 A/B: hard_rule_decision + once/reject branch (no TeleAgent)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from charter import load_charter, charter_for_glue
from hard_rules import hard_rule_decision

REPO = Path(__file__).resolve().parents[1]


def simulate_glue_permission(perm: dict, charter: dict) -> dict:
    """Mirror glue.run_job hard-rule branch (reject vs allowlisted once)."""
    job_charter = charter_for_glue(charter) if "_source" in charter or "goal" in charter else charter
    hr = hard_rule_decision(perm, charter=job_charter)
    out = {
        "pending_path": perm.get("path"),
        "hard_rule": hr,
        "allowlisted_flag": bool(perm.get("_hard_rule_allowlisted")),
        "api_reply": None,
        "via": None,
        "called_lead": False,
        "notes": [],
    }
    if hr and hr.get("reply") == "reject":
        out["api_reply"] = "reject"
        out["via"] = "hard_rule"
        out["notes"].append(f"hard_rule reject: {hr.get('reason', '')}")
        out["grok_permission_decision"] = "reject"
        return out
    if perm.get("_hard_rule_allowlisted"):
        out["api_reply"] = "once"
        out["via"] = "hard_rule_allowlisted"
        out["notes"].append(
            "hard_rule allowlisted (charter allow_secret_globs/allow_paths): "
            "once — legitimate small-risk secret path; logged, no lead"
        )
        out["grok_permission_decision"] = "once"
        return out
    out["api_reply"] = "lead_or_ordinary"
    out["via"] = "not_hard_rule"
    out["notes"].append("would continue to lead/ordinary path")
    return out


def main() -> int:
    a_path = REPO / "jobs/examples/hard-rule-A-deny.charter.yaml"
    b_path = REPO / "jobs/examples/hard-rule-B-allow.charter.yaml"
    charter_a = load_charter(a_path)
    charter_b = load_charter(b_path)

    pending_a = {
        "id": "live-sim-A",
        "tool": "read_file",
        "path": "/workspace/teleagent-collab/.env",
        "permission": "read",
        "patterns": ["**/.env*"],
        "message": "read GITHUB_TOKEN / secrets from .env (unauthorized)",
    }
    pending_b = {
        "id": "live-sim-B",
        "tool": "read_file",
        "path": "/workspace/teleagent-collab/.env",
        "permission": "read",
        "patterns": ["**/.env*"],
        "message": "read DATABASE_URL from repo .env (charter-allowlisted)",
    }

    res_a = simulate_glue_permission(pending_a, charter_a)
    res_b = simulate_glue_permission(pending_b, charter_b)

    # Expectations
    ok_a = res_a["api_reply"] == "reject" and res_a["via"] == "hard_rule" and not res_a["called_lead"]
    ok_b = (
        res_b["api_reply"] == "once"
        and res_b["via"] == "hard_rule_allowlisted"
        and res_b["allowlisted_flag"]
        and not res_b["called_lead"]
    )

    # always+allowlisted still reject
    always = dict(pending_b)
    always.pop("_hard_rule_allowlisted", None)
    always["requested_reply"] = "always"
    always_res = simulate_glue_permission(always, charter_b)
    ok_always = always_res["api_reply"] == "reject"

    out = {
        "A_unauthorized_env": {**res_a, "expect_reject": True, "ok": ok_a},
        "B_allowlisted_env": {**res_b, "expect_once": True, "ok": ok_b},
        "B_always_still_reject": {**always_res, "ok": ok_always},
        "ok": ok_a and ok_b and ok_always,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"live_ab_glue_sim: {'OK' if out['ok'] else 'FAIL'}", file=sys.stderr)
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
