#!/usr/bin/env python3
"""Eternal entry: parallel scheduler + adaptive approval scan.

Usage:
  # Dry smoke (no TeleAgent): isolation + serial short-poll
  python3 bin/run-scheduler.py --smoke

  # Dry-run several charters in parallel (isolated workdirs)
  python3 bin/run-scheduler.py --dry-run --max-parallel 3 \\
      jobs/examples/hello.charter.yaml \\
      jobs/examples/hello.charter.yaml

  # Live (needs :4399 + COLLAB_LEAD_BIN)
  export COLLAB_LEAD_BIN=/workspace/run-grok.sh
  python3 bin/run-scheduler.py --max-parallel 3 jobs/examples/hello.charter.yaml

Reports: jobs/runs/<job_id>/{status.json,report.json}
Workdirs: jobs/workspaces/<job_id>/

Scan ≠ call_lead — see docs/parallel-scheduler.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from charter import CharterError, load_charter  # noqa: E402
from scheduler import (  # noqa: E402
    DEFAULT_MAX_PARALLEL,
    ParallelScheduler,
    smoke_parallel_isolation,
    smoke_serial_short_poll,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Parallel TeleAgent job scheduler")
    ap.add_argument(
        "charters",
        nargs="*",
        help="Charter paths (.yaml/.json). Omit with --smoke.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="No TeleAgent/lead HTTP; isolated workdirs + simulated completion",
    )
    ap.add_argument(
        "--max-parallel",
        type=int,
        default=DEFAULT_MAX_PARALLEL,
        help=f"Max concurrent jobs (default {DEFAULT_MAX_PARALLEL})",
    )
    ap.add_argument(
        "--smoke",
        action="store_true",
        help="Run built-in parallel-isolation + serial short-poll dry smokes",
    )
    ap.add_argument(
        "--workspaces-dir",
        default=str(REPO / "jobs" / "workspaces"),
        help="Root for per-job workdirs",
    )
    ap.add_argument(
        "--runs-dir",
        default=str(REPO / "jobs" / "runs"),
        help="Root for status/report outputs",
    )
    ap.add_argument(
        "--idle-scan-min",
        type=float,
        default=None,
        help="Idle multi-path scan min seconds (default 10)",
    )
    ap.add_argument(
        "--idle-scan-max",
        type=float,
        default=None,
        help="Idle multi-path scan max seconds (default 30)",
    )
    ap.add_argument(
        "--busy-poll-min",
        type=float,
        default=None,
        help="Busy/pending short-poll min seconds (default 1)",
    )
    ap.add_argument(
        "--busy-poll-max",
        type=float,
        default=None,
        help="Busy/pending short-poll max seconds (default 3)",
    )
    args = ap.parse_args(argv)

    if args.smoke:
        iso = smoke_parallel_isolation(n=3, max_parallel=min(3, args.max_parallel))
        ser = smoke_serial_short_poll()
        out = {
            "smoke": True,
            "parallel_isolation_ok": iso.get("isolation_ok"),
            "serial_short_poll_ok": ser.get("serial_ok"),
            "parallel_stats": iso.get("stats"),
            "serial_stats": {
                "lead_calls": ser.get("lead_calls"),
                "busy_interval_sample": ser.get("busy_interval_sample"),
                "stats": ser.get("stats"),
            },
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        ok = bool(iso.get("isolation_ok") and ser.get("serial_ok"))
        return 0 if ok else 1

    if not args.charters:
        ap.error("provide charter paths, or use --smoke")

    kw = {
        "max_parallel": args.max_parallel,
        "workspaces_root": Path(args.workspaces_dir),
        "runs_root": Path(args.runs_dir),
        "dry_run": args.dry_run,
    }
    if args.idle_scan_min is not None:
        kw["idle_min"] = args.idle_scan_min
    if args.idle_scan_max is not None:
        kw["idle_max"] = args.idle_scan_max
    if args.busy_poll_min is not None:
        kw["busy_min"] = args.busy_poll_min
    if args.busy_poll_max is not None:
        kw["busy_max"] = args.busy_poll_max

    sched = ParallelScheduler(**kw)
    try:
        for cpath in args.charters:
            try:
                charter = load_charter(cpath)
            except CharterError as e:
                print(f"charter error ({cpath}): {e}", file=sys.stderr)
                return 2
            sched.enqueue_charter(charter)
        summary = sched.run(sleep=not args.dry_run)
    finally:
        sched.shutdown()

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    oks = [j.get("ok") for j in summary.get("jobs", {}).values()]
    if not oks:
        return 1
    return 0 if all(oks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
