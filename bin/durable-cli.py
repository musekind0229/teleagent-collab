#!/usr/bin/env python3
"""Thin CLI for knife-14 durable/perpetual Goal layer (file-backed, no transport).

Usage:
  python3 bin/durable-cli.py --persist DIR submit --submit-key KEY --title T --outcome O
  python3 bin/durable-cli.py --persist DIR get GOAL_ID
  python3 bin/durable-cli.py --persist DIR resolve GOAL_ID --decision-id D --verdict V --reason R
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

    p_get = sub.add_parser("get", help="get_goal snapshot")
    p_get.add_argument("goal_id")

    p_res = sub.add_parser("resolve", help="resolve exactly one pending decision")
    p_res.add_argument("goal_id")
    p_res.add_argument("--decision-id", default="")
    p_res.add_argument("--request-id", default="")
    p_res.add_argument("--verdict", default="")
    p_res.add_argument("--reason", default="")
    p_res.add_argument("--actions-json", default="", help="optional JSON list of related actions")
    p_res.add_argument("--extra-json", default="", help="optional extra JSON (batch keys are refused)")

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

    args = parser.parse_args(argv)
    layer = DurableLayer.open(args.persist)

    if args.cmd == "submit":
        goal = None
        if args.goal_json:
            goal = json.loads(args.goal_json)
        out = layer.submit_goal(
            submit_key=args.submit_key,
            title=args.title,
            desired_outcome=args.outcome,
            goal=goal,
        )
        _print(out)
        return 0 if out.get("ok") else 1

    if args.cmd == "get":
        out = layer.get_goal(args.goal_id)
        _print(out)
        return 0 if out.get("ok") else 1

    if args.cmd == "resolve":
        actions = json.loads(args.actions_json) if args.actions_json else None
        extra = json.loads(args.extra_json) if args.extra_json else None
        out = layer.resolve_decision(
            args.goal_id,
            decision_id=args.decision_id,
            request_id=args.request_id,
            verdict=args.verdict,
            reason=args.reason,
            actions=actions,
            extra=extra if isinstance(extra, dict) else None,
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
