#!/usr/bin/env python3
"""Example DeepSeek harness lead wrapper — 真 harness 未接线验收.

JSON-in / JSON-out contract for DeepSeekHarnessLeadAdapter (COLLAB_LEAD_BIN):

  stdin (default) or --request-file PATH:
  {
    "protocol": "collab-lead-v1",
    "adapter": "deepseek_harness",
    "request": { ... build_lead_request ... },
    "schema": { ... pin_lead_response_schema ... },
    "cwd": "...",
    "prompt": "..."
  }

  stdout (only once a real harness is wired): strict decision JSON that
  echoes application_id and context_summary byte-for-byte, plus
  decision ∈ once|reject|deny_job|demand_safe_path  or  verdict ∈ pass|fail.

This script does NOT call DeepSeek's online API and does NOT guess harness
CLI flags. Without a wired harness it exits 2 and writes no once/pass JSON.

Env:
  COLLAB_LEAD_ADAPTER=deepseek_harness
  COLLAB_LEAD_BIN=/path/to/this/script
  COLLAB_DEEPSEEK_LEAD_BIN   # optional adapter-specific override
  COLLAB_DEEPSEEK_IO=stdin|file
  COLLAB_DEEPSEEK_HARNESS_BIN  # reserved; setting it still does not invent argv
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _load_envelope(argv: list[str]) -> dict | None:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--request-file", dest="request_file", default="")
    ap.add_argument("--cwd", dest="cwd", default="")
    args, _unknown = ap.parse_known_args(argv)
    if args.request_file:
        try:
            text = Path(args.request_file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"deepseek_harness wrapper: cannot read --request-file: {e}", file=sys.stderr)
            return None
    else:
        text = sys.stdin.read()
    text = (text or "").strip()
    if not text:
        print("deepseek_harness wrapper: empty envelope (stdin or --request-file)", file=sys.stderr)
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"deepseek_harness wrapper: illegal envelope JSON: {e}", file=sys.stderr)
        return None
    if not isinstance(obj, dict):
        print("deepseek_harness wrapper: envelope is not a JSON object", file=sys.stderr)
        return None
    return obj


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    envelope = _load_envelope(argv)
    req = envelope.get("request") if isinstance(envelope, dict) else None
    app_id = None
    if isinstance(req, dict):
        app_id = req.get("application_id")
    elif isinstance(envelope, dict):
        app_id = envelope.get("application_id")

    harness = (os.environ.get("COLLAB_DEEPSEEK_HARNESS_BIN") or "").strip()
    if harness:
        print(
            "deepseek_harness wrapper: COLLAB_DEEPSEEK_HARNESS_BIN is set but the "
            "DeepSeek harness CLI contract is not wired. Refusing to guess argv "
            f"or POST to a DeepSeek HTTP API. application_id={app_id!r}",
            file=sys.stderr,
        )
        return 2

    print(
        "deepseek_harness wrapper: 真 harness 未接线验收. "
        "No live DeepSeek CLI/API. Fail-closed (no once/pass). "
        f"application_id={app_id!r}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
