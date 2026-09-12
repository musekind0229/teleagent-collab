#!/usr/bin/env python3
"""Simple v0.2 P1 caller: submit / events / escalate / resolve / get / report / cancel.

File-backed. No Bot product. No transport. Coordinator only accepts.

  PYTHONPATH=src python3 bin/delegate-demo.py --persist /tmp/p1-demo
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from framework.durable_api import DurableLayer, reset_durable_cache  # noqa: E402
from framework.goal_ownership import reset_goal_ownership_cache  # noqa: E402


def _print(label: str, payload: dict) -> None:
    print(f"## {label}")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v0.2 P1 durable delegation demo (file-backed).")
    parser.add_argument("--persist", default=".", help="persist dir for .collab-durable/")
    parser.add_argument("--submit-key", default="p1-demo-key")
    args = parser.parse_args(argv)

    reset_durable_cache()
    reset_goal_ownership_cache()
    layer = DurableLayer.open(args.persist)

    submitted = layer.submit_goal(
        submit_key=args.submit_key,
        title="fix login double-submit",
        desired_outcome="login form is idempotent; no deploy",
        submitter_id="upper-bot-1",
        external_goal_ref="parent:week-internal-beta",
        autonomy="bounded_autonomy",
        coordinator_id="coord-local-1",
        goal={
            "budget": {"wall_sec": 120, "max_reworks": 2},
            "boundaries": {
                "must": ["stay in assigned workspace"],
                "must_not": ["deploy", "touch other repos"],
            },
        },
        tasks=[{"title": "reproduce-and-fix", "status": "queued"}],
    )
    _print("submit_goal", submitted)
    if not submitted.get("ok"):
        return 1
    gid = submitted["goal_id"]

    got = layer.get_goal(gid)
    _print("get_goal", got)

    events = layer.list_events(gid)
    _print("list_events (before escalate)", events)

    esc = layer.escalate_to_upper(
        gid,
        kind="over_budget",
        reason="wall_sec reserve would exceed Goal budget; return to upper",
        details={"needed_wall_sec": 400, "silent_retry": False},
    )
    _print("escalate_to_upper", esc)
    if not esc.get("ok"):
        return 1
    did = (esc.get("decision") or {}).get("decision_id")

    pending = layer.list_events(gid)
    _print("list_events (decision_required pending)", pending)

    resolved = layer.resolve_decision(
        gid,
        decision_id=did,
        verdict="ack_raise_budget",
        reason="upper will revise contract budget; do not retry locally",
        actor_id="upper-bot-1",
    )
    _print("resolve_decision (submitter actor)", resolved)

    report = layer.get_report(gid)
    _print("get_report", report)

    cancelled = layer.cancel_goal(gid, reason="demo complete")
    _print("cancel_goal", cancelled)

    summary = {
        "ok": bool(
            submitted.get("ok")
            and got.get("ok")
            and esc.get("ok")
            and resolved.get("ok")
            and cancelled.get("ok")
        ),
        "goal_id": gid,
        "submitter_id": got.get("submitter_id"),
        "autonomy": (got.get("autonomy") or {}).get("mode"),
        "coordinator_id": (got.get("ownership") or {}).get("coordinator_id"),
        "ownership_version": (got.get("ownership") or {}).get("version"),
        "cancel_state": cancelled.get("state"),
        "cancelled": cancelled.get("cancelled"),
        "escalate_kind": (esc.get("decision") or {}).get("kind"),
        "silent_retry": esc.get("silent_retry"),
    }
    _print("summary", summary)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
