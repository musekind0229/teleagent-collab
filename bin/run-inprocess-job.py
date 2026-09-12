#!/usr/bin/env python3
"""Run a charter via inprocess ExecutionBackend public API only (no TeleAgent)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from charter import load_charter  # noqa: E402
from execution_backend import run_file_job_via_public_api  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: run-inprocess-job.py <charter.yaml> [workdir]", file=sys.stderr)
        return 2
    charter_path = Path(argv[1])
    workdir = Path(argv[2]) if len(argv) > 2 else ROOT / "jobs" / "runs" / f"inproc-{charter_path.stem}"
    charter = load_charter(charter_path)
    result = run_file_job_via_public_api(workdir=workdir, charter=charter)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
