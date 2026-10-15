#!/usr/bin/env python3
"""Minimal simple collab run entry (CLI).

Reads a goal specification (JSON or CLI flags), plans via pluggable Lead,
executes via ExecutionBackend (antigravity or inprocess), saves raw results
and workspace artifacts, and presents them to the Lead for final decision/rework/diagnose.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from framework.app_service import DeterministicPlanner, LeadAdapterPlanner
from framework.simple_collab import SimpleCollabRunner, build_default_goal_request
from execution_backend import get_execution_backend
from execution_backend.inprocess_v1 import InProcessExecutionBackend
from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend
from lead_adapter import get_lead_adapter
from lead_adapter.inprocess import InProcessLeadAdapter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Simple Collab Runner entry")
    parser.add_argument("--persist", default="./.collab-run-data", help="Persistence directory")
    parser.add_argument("--title", default="", help="Goal title")
    parser.add_argument("--goal", default="", help="Goal desired outcome")
    parser.add_argument("--goal-file", default="", help="Path to goal request JSON file")
    parser.add_argument(
        "--artifacts",
        nargs="*",
        default=[],
        help="Expected artifact filenames relative to workspace",
    )
    parser.add_argument(
        "--backend",
        choices=["inprocess", "antigravity", "agy"],
        default="inprocess",
        help="Execution backend to run tasks (default: inprocess)",
    )
    parser.add_argument(
        "--planner",
        choices=["deterministic", "lead"],
        default="lead",
        help="Planner choice (default: lead)",
    )
    parser.add_argument(
        "--lead-adapter",
        default="",
        help="Lead adapter kind (inprocess, grok_cli, codex_cli, deepseek_harness). Default from COLLAB_LEAD_ADAPTER or inprocess",
    )
    parser.add_argument(
        "--lead-bin",
        default="",
        help="Optional path to lead CLI binary (e.g. grok executable)",
    )
    parser.add_argument(
        "--lead-model",
        default="",
        help="Lead model choice (e.g. grok-3, etc.)",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Execution backend worker model (e.g. gemini-2.5-pro, etc.)",
    )
    parser.add_argument(
        "--agy-bin",
        default="",
        help="Explicit path to agy CLI binary",
    )
    parser.add_argument(
        "--exchange-dir",
        default="",
        help="Exchange directory for inprocess lead adapter (default: <persist>/lead_exchange)",
    )
    parser.add_argument(
        "--max-reworks",
        type=int,
        default=2,
        help="Max reworks allowed upon review failure (default: 2)",
    )
    parser.add_argument(
        "--timeout-sec",
        type=float,
        default=120.0,
        help="Goal execution timeout seconds (default: 120.0)",
    )
    parser.add_argument(
        "--agy-account-pool",
        default="",
        help="Optional agy account pool path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    persist = Path(args.persist).resolve()
    persist.mkdir(parents=True, exist_ok=True)

    # 1. Load or build goal request
    if args.goal_file:
        file_path = Path(args.goal_file)
        if not file_path.is_file():
            print(f"Error: goal file not found: {file_path}", file=sys.stderr)
            return 2
        try:
            body = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Error reading goal JSON: {e}", file=sys.stderr)
            return 2
    else:
        title = args.title or "Simple Collab Run"
        goal = args.goal or title
        # Only attach model to body if backend supports per-goal model selection (antigravity/agy)
        selected_model = args.model if args.backend in ("antigravity", "agy") else ""
        body = build_default_goal_request(
            title=title,
            goal=goal,
            artifacts=args.artifacts or None,
            max_reworks=args.max_reworks,
            wall_sec=args.timeout_sec,
            model=selected_model,
            lead_model=args.lead_model,
        )

    # 2. Setup backend
    if args.backend in ("antigravity", "agy"):
        kw: dict[str, Any] = {"run_registry_path": str(persist / "agy-runs.json")}
        if args.agy_bin:
            kw["bin_path"] = args.agy_bin
        if args.model:
            kw["default_model"] = args.model
        if args.agy_account_pool:
            kw["account_pool_path"] = args.agy_account_pool
        backend = get_execution_backend("antigravity", **kw)
    else:
        backend = InProcessExecutionBackend()

    # 3. Setup lead adapter / planner
    if args.planner == "deterministic":
        planner = DeterministicPlanner()
    else:
        adapter_kind = (args.lead_adapter or os.environ.get("COLLAB_LEAD_ADAPTER") or "inprocess").strip().lower()
        adapter_kw: dict[str, Any] = {}
        if args.lead_bin:
            adapter_kw["bin_path"] = args.lead_bin
        if adapter_kind in ("inprocess", "in_process", "codex", "file", "stdin"):
            exchange = args.exchange_dir or str(persist / "lead_exchange")
            adapter_kw["exchange_dir"] = exchange
        adapter = get_lead_adapter(adapter_kind, **adapter_kw)
        lead_work = persist / "lead-work"
        lead_work.mkdir(parents=True, exist_ok=True)
        planner = LeadAdapterPlanner(adapter, cwd=lead_work)

    # 4. Run simple collab loop
    runner = SimpleCollabRunner(persist, planner=planner, backend=backend)
    result = runner.run_goal(body, timeout_sec=args.timeout_sec)

    # 5. Output summary
    print(json.dumps({
        "ok": result.get("ok"),
        "state": result.get("state"),
        "goal_id": result.get("goal_id"),
        "is_duplicate": result.get("is_duplicate"),
        "timed_out": result.get("timed_out"),
        "continuation_required": result.get("continuation_required"),
        "lead_reviews": result.get("lead_reviews"),
        "event_id": result.get("event_id"),
        "report": result.get("report"),
    }, ensure_ascii=False, indent=2))

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
