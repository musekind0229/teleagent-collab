#!/usr/bin/env python3
"""Thin CLI for the durable Goal layer (file-backed, no transport).

Knife 14 five ops plus v0.2 P1 delegation fields (submitter, autonomy,
ownership claim, events, return-to-upper escalate).

Usage:
  python3 bin/durable-cli.py --persist DIR submit --submit-key KEY --submitter ID \\
      --autonomy bounded_autonomy --coordinator CID --title T --outcome O
  python3 bin/durable-cli.py --persist DIR get GOAL_ID
  python3 bin/durable-cli.py --persist DIR events GOAL_ID
  python3 bin/durable-cli.py --persist DIR escalate GOAL_ID --kind over_budget --reason R
  python3 bin/durable-cli.py --persist DIR resolve GOAL_ID --decision-id D --verdict V --actor-id ID
  python3 bin/durable-cli.py --persist DIR cancel GOAL_ID
  python3 bin/durable-cli.py --persist DIR report GOAL_ID
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

from framework.durable_api import (  # noqa: E402
    DurableLayer,
    kernel_promotes_identity_memory,
)


def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _json_flag(raw: str):
    if not raw:
        return None
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Durable Goal layer (file-backed). Does not bind a transport.",
    )
    parser.add_argument(
        "--persist",
        default=".",
        help="workdir; store lives under <persist>/.collab-durable/",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sub = sub.add_parser("submit", help="submit_goal (idempotent via submit key)")
    p_sub.add_argument("--submit-key", required=True)
    p_sub.add_argument("--title", default="")
    p_sub.add_argument("--outcome", default="")
    p_sub.add_argument("--goal-json", default="", help="optional Goal object JSON")
    p_sub.add_argument("--submitter", default="", help="submitter id (audit)")
    p_sub.add_argument("--external-goal-ref", default="", help="upper-layer goal reference")
    p_sub.add_argument(
        "--autonomy",
        default="",
        help="explicit_plan | bounded_autonomy (optional JSON object)",
    )
    p_sub.add_argument("--coordinator", default="", help="claim this coordinator on submit")
    p_sub.add_argument("--budget-json", default="", help='e.g. {"wall_sec": 60}')
    p_sub.add_argument("--boundaries-json", default="", help='e.g. {"must":[],"must_not":[]}')
    p_sub.add_argument("--tasks-json", default="", help="optional tasks list JSON")

    p_get = sub.add_parser("get", help="get_goal snapshot")
    p_get.add_argument("goal_id")

    p_evt = sub.add_parser("events", help="list history + pending (status vs decision_required)")
    p_evt.add_argument("goal_id")

    p_pend = sub.add_parser("pending", help="pending decisions only")
    p_pend.add_argument("goal_id")

    p_esc = sub.add_parser("escalate", help="open a return-to-upper pending (no silent retry)")
    p_esc.add_argument("goal_id")
    p_esc.add_argument(
        "--kind",
        required=True,
        help="over_budget | out_of_scope | insufficient_auth",
    )
    p_esc.add_argument("--reason", required=True)
    p_esc.add_argument("--details-json", default="")
    p_esc.add_argument("--task-id", default="")

    p_res = sub.add_parser("resolve", help="resolve exactly one pending decision")
    p_res.add_argument("goal_id")
    p_res.add_argument("--decision-id", default="")
    p_res.add_argument("--request-id", default="")
    p_res.add_argument("--verdict", default="")
    p_res.add_argument("--reason", default="")
    p_res.add_argument("--actions-json", default="", help="optional JSON list of related actions")
    p_res.add_argument("--extra-json", default="", help="optional extra JSON (batch keys are refused)")
    p_res.add_argument("--actor-id", default="", help="caller identity (required by kernel)")
    p_res.add_argument("--ownership-version", default="", help="optional ownership version if Goal has a coordinator")

    p_open = sub.add_parser("open-decision", help="open one pending decision (kernel helper)")
    p_open.add_argument("goal_id")
    p_open.add_argument("--kind", default="action_approval")
    p_open.add_argument("--decision-id", default="")
    p_open.add_argument("--task-id", default="")

    p_can = sub.add_parser("cancel", help="cancel_goal → cancel_requested (not cancelled)")
    p_can.add_argument("goal_id")
    p_can.add_argument("--reason", default="")

    p_eff = sub.add_parser("effect-cancel", help="terminal cancelled after cancel_requested")
    p_eff.add_argument("goal_id")

    p_rep = sub.add_parser("report", help="get_report snapshot")
    p_rep.add_argument("goal_id")

    p_add = sub.add_parser("add-task", help="add a child task (refused after cancel)")
    p_add.add_argument("goal_id")
    p_add.add_argument("--title", default="child")
    p_add.add_argument("--status", default="queued")

    p_hand = sub.add_parser("handoff", help="handoff coordinator (bumps ownership version)")
    p_hand.add_argument("goal_id")
    p_hand.add_argument("--from-coordinator", required=True)
    p_hand.add_argument("--to-coordinator", required=True)
    p_hand.add_argument("--version", default="", help="expected ownership version")

    args = parser.parse_args(argv)
    layer = DurableLayer.open(args.persist)

    if args.cmd == "submit":
        goal = _json_flag(args.goal_json) if args.goal_json else {}
        if not isinstance(goal, dict):
            goal = {}
        if args.budget_json:
            goal["budget"] = json.loads(args.budget_json)
        if args.boundaries_json:
            goal["boundaries"] = json.loads(args.boundaries_json)
        autonomy = args.autonomy
        if autonomy and autonomy.strip().startswith("{"):
            autonomy = json.loads(autonomy)
        tasks = json.loads(args.tasks_json) if args.tasks_json else None
        out = layer.submit_goal(
            submit_key=args.submit_key,
            title=args.title,
            desired_outcome=args.outcome,
            goal=goal or None,
            tasks=tasks,
            submitter_id=args.submitter,
            external_goal_ref=args.external_goal_ref,
            autonomy=autonomy or None,
            coordinator_id=args.coordinator,
        )
        _print(out)
        return 0 if out.get("ok") else 1

    if args.cmd == "get":
        out = layer.get_goal(args.goal_id)
        _print(out)
        return 0 if out.get("ok") else 1

    if args.cmd == "events":
        out = layer.list_events(args.goal_id)
        _print(out)
        return 0 if out.get("ok") else 1

    if args.cmd == "pending":
        out = layer.list_events(args.goal_id)
        if out.get("ok"):
            out = {
                "ok": True,
                "goal_id": out.get("goal_id"),
                "pending": out.get("pending") or [],
                "pending_count": out.get("pending_count") or 0,
            }
        _print(out)
        return 0 if out.get("ok") else 1

    if args.cmd == "escalate":
        details = json.loads(args.details_json) if args.details_json else None
        out = layer.escalate_to_upper(
            args.goal_id,
            kind=args.kind,
            reason=args.reason,
            details=details,
            task_id=args.task_id,
        )
        _print(out)
        return 0 if out.get("ok") else 1

    if args.cmd == "resolve":
        actions = json.loads(args.actions_json) if args.actions_json else None
        extra = json.loads(args.extra_json) if args.extra_json else None
        resolve_extra = extra if isinstance(extra, dict) else {}
        if args.actor_id:
            resolve_extra["actor_id"] = args.actor_id
        if args.ownership_version != "":
            try:
                resolve_extra["ownership_version"] = int(args.ownership_version)
            except ValueError:
                resolve_extra["ownership_version"] = args.ownership_version
        out = layer.resolve_decision(
            args.goal_id,
            decision_id=args.decision_id,
            request_id=args.request_id,
            verdict=args.verdict,
            reason=args.reason,
            actions=actions,
            extra=resolve_extra or None,
            actor_id=args.actor_id,
        )
        _print(out)
        return 0 if out.get("ok") else 1

    if args.cmd == "open-decision":
        out = layer.open_decision(
            args.goal_id,
            kind=args.kind,
            decision_id=args.decision_id,
            task_id=args.task_id,
        )
        _print(out)
        return 0 if out.get("ok") else 1

    if args.cmd == "cancel":
        out = layer.cancel_goal(args.goal_id, reason=args.reason)
        _print(out)
        return 0 if out.get("ok") else 1

    if args.cmd == "effect-cancel":
        out = layer.effect_cancel(args.goal_id)
        _print(out)
        return 0 if out.get("ok") else 1

    if args.cmd == "report":
        out = layer.get_report(args.goal_id)
        _print(out)
        return 0 if out.get("ok") else 1

    if args.cmd == "add-task":
        out = layer.add_child_task(args.goal_id, title=args.title, status=args.status)
        _print(out)
        return 0 if out.get("ok") else 1

    if args.cmd == "handoff":
        ver = None
        if args.version != "":
            try:
                ver = int(args.version)
            except ValueError:
                ver = None
        out = layer.handoff_coordinator(
            args.goal_id,
            from_coordinator_id=args.from_coordinator,
            to_coordinator_id=args.to_coordinator,
            expected_version=ver,
        )
        _print(out)
        return 0 if out.get("ok") else 1

    _print(
        {
            "ok": False,
            "error": f"unknown cmd {args.cmd}",
            "kernel_promotes_identity_memory": kernel_promotes_identity_memory(),
        }
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
