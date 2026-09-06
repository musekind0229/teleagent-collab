#!/usr/bin/env python3
"""Eternal → worker entry: load charter, call glue.run_job, land status/report.

Usage:
  python3 bin/run-job.py jobs/examples/hello.charter.yaml
  python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml

Reports land under jobs/runs/<name>-<utc>/ (status.json, report.json, report.md).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from charter import (  # noqa: E402
    CharterError,
    build_instruction,
    charter_for_glue,
    expected_artifacts,
    job_name,
    load_charter,
)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _run_dir(name: str, runs_root: Path) -> Path:
    d = runs_root / f"{name}-{_utc_stamp()}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_reports(out_dir: Path, charter: dict, result: dict, instruction: str) -> None:
    """Land status.json + report.json + report.md (agreed receive path for eternal)."""
    status = {
        "ok": bool(result.get("ok")),
        "state": result.get("state"),
        "session_id": result.get("session_id", ""),
        "exit_code": None,
        "artifacts": result.get("artifacts", []),
        "log_path": str(out_dir / "report.md"),
        "pending_permissions": result.get("pending_summaries", []),
        "error": result.get("error", ""),
        "grok_permission_decision": result.get("grok_permission_decision"),
        "grok_review_decision": result.get("grok_review_decision"),
        "path": result.get("path"),
        "charter_source": charter.get("_source"),
        "job_name": job_name(charter),
        "dry_run": bool(result.get("dry_run")),
    }
    (out_dir / "status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    report_obj = {
        "charter": {
            "name": job_name(charter),
            "goal": charter.get("goal"),
            "must": charter.get("must"),
            "must_not": charter.get("must_not"),
            "allow_secret_globs": charter.get("allow_secret_globs"),
            "allow_paths": charter.get("allow_paths"),
            "allow_keys": charter.get("allow_keys"),
            "done_when": charter.get("done_when"),
            "acceptance": charter.get("acceptance"),
            "source": charter.get("_source"),
        },
        "instruction": instruction,
        "result": {
            k: result.get(k)
            for k in (
                "ok",
                "state",
                "session_id",
                "artifacts",
                "error",
                "notes",
                "path",
                "pending_seen",
                "grok_permission_decision",
                "grok_review_decision",
                "api_replies",
                "hard_rule_rejects",
                "dry_run",
            )
            if k in result or True
        },
    }
    (out_dir / "report.json").write_text(
        json.dumps(report_obj, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    lines = [
        f"# Job report: {job_name(charter)}",
        "",
        f"- charter: `{charter.get('_source', '')}`",
        f"- state: **{result.get('state')}** ok={result.get('ok')}",
        f"- session_id: `{result.get('session_id') or 'n/a'}`",
        f"- path: `{result.get('path') or 'n/a'}`",
        f"- dry_run: {bool(result.get('dry_run'))}",
        f"- artifacts: `{result.get('artifacts')}`",
        f"- error: {result.get('error') or '(none)'}",
        "",
        "## Goal",
        "",
        charter.get("goal", ""),
        "",
        "## Instruction (from charter)",
        "",
        "```",
        instruction[:4000],
        "```",
        "",
        "## Notes",
        "",
        f"`{result.get('notes')}`",
        "",
    ]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def dry_run_result(charter: dict, instruction: str, arts: list[str]) -> dict:
    return {
        "name": job_name(charter),
        "session_id": "",
        "pending_seen": False,
        "pending_summaries": [],
        "grok_permission_decision": "",
        "grok_review_decision": "",
        "api_replies": [],
        "hard_rule_rejects": [],
        "artifacts": [],
        "state": "dry_run",
        "ok": True,
        "error": "",
        "path": "dry_run",
        "notes": [
            "dry-run: charter validated; glue.run_job not called",
            f"expected_artifacts={arts}",
            f"instruction_chars={len(instruction)}",
        ],
        "dry_run": True,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run a TeleAgent job from a charter file")
    ap.add_argument("charter", help="Path to .charter.yaml / .yaml / .json")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate charter + write reports without calling TeleAgent/glue.run_job",
    )
    ap.add_argument(
        "--runs-dir",
        default=str(REPO / "jobs" / "runs"),
        help="Directory for status/report outputs (default: jobs/runs)",
    )
    ap.add_argument(
        "--timeout-sec",
        type=int,
        default=None,
        help="Override charter timeout_sec",
    )
    ap.add_argument(
        "--workspace",
        default=None,
        help="Per-job workdir (default: glue.COLLAB). Use distinct dirs for parallel jobs.",
    )
    args = ap.parse_args(argv)

    try:
        charter = load_charter(args.charter)
    except CharterError as e:
        print(f"charter error: {e}", file=sys.stderr)
        return 2

    name = job_name(charter)
    instruction = build_instruction(charter)
    glue_charter = charter_for_glue(charter)

    # Lazy import: dry-run works even if TeleAgent creds missing.
    if args.dry_run:
        arts = expected_artifacts(charter, workspace=None)
        # Prefer relative names in dry-run when no workspace
        result = dry_run_result(charter, instruction, arts)
    else:
        import glue as g

        ws = Path(args.workspace) if args.workspace else Path(g.COLLAB)
        ws.mkdir(parents=True, exist_ok=True)
        arts = expected_artifacts(charter, workspace=ws)
        # Clean expected artifacts so sample jobs are repeatable
        for apath in arts:
            p = Path(apath)
            if p.exists() and p.is_file():
                p.unlink()

        timeout = args.timeout_sec
        if timeout is None:
            timeout = int(charter.get("timeout_sec") or 300)
        force_review = bool(charter.get("force_lead_review", False))

        t0 = time.time()
        result = g.run_job(
            name,
            instruction,
            arts,
            force_lead_review=force_review,
            timeout_sec=timeout,
            charter=glue_charter,
            worker_intent=charter.get("worker_intent")
            or f"Execute charter job {name!r}: {charter['goal'][:200]}",
            blocker=charter.get("blocker"),
            workspace=ws,
        )
        result.setdefault("notes", []).append(f"wall_sec={time.time() - t0:.1f}")
        result["dry_run"] = False

    out_dir = _run_dir(name, Path(args.runs_dir))
    write_reports(out_dir, charter, result, instruction)

    summary = {
        "ok": result.get("ok"),
        "state": result.get("state"),
        "out_dir": str(out_dir),
        "status": str(out_dir / "status.json"),
        "report_md": str(out_dir / "report.md"),
        "report_json": str(out_dir / "report.json"),
    }
    print(json.dumps(summary, ensure_ascii=False))
    if args.dry_run:
        return 0
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
