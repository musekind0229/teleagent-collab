#!/usr/bin/env python3
"""DeepSeek harness lead wrapper — dsh --profile headless → collab-lead-v1 JSON.

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

  stdout (success, exit 0): one decision JSON object that echoes
  application_id and context_summary byte-for-byte, plus
  decision ∈ once|reject|deny_job|demand_safe_path  or  verdict ∈ pass|fail.

dsh default stdout is the final answer text (exit 0/1). `--json` is an
event stream, not collab-lead-v1 — this wrapper does not pass `--json`.
The last JSON object on stdout is parsed, then schema/semantic-checked.

Lead is not a worker: the headless task forbids bash/write/tools and
demands JSON only. dsh is spawned in a throwaway cwd so tools cannot
edit this repo. Fail-closed (exit 2, empty stdout) when bin/key is
missing, dsh exits non-zero, JSON is missing/illegal, or binding fails.

Env:
  COLLAB_LEAD_ADAPTER=deepseek_harness
  COLLAB_LEAD_BIN=/path/to/this/script
  COLLAB_DEEPSEEK_LEAD_BIN   # optional adapter-specific override
  COLLAB_DEEPSEEK_IO=stdin|file
  COLLAB_DEEPSEEK_HARNESS_BIN=dsh   # or npx @deepseek-ai/dsh; else PATH dsh
  DEEPSEEK_API_KEY=...
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

PERMISSION_DECISIONS = frozenset({"once", "reject", "deny_job", "demand_safe_path"})
REVIEW_VERDICTS = frozenset({"pass", "fail"})

# Headless task prefix: lead-only, no tools, JSON only.
_HEADLESS_CONSTRAINTS = """You are the TeleAgent collaboration LEAD, not a worker.
Do not execute the worker task. Do not use bash, shell, write, edit, or any
filesystem or network tool. Do not create, modify, or delete files. Do not
install packages or run commands. Do not inspect or change the repository.

Your only job is to emit one collab-lead-v1 decision JSON object and nothing
else. No markdown fences, no commentary, no tool calls, no prose.

Copy application_id and context_summary EXACTLY byte-for-byte from the request.
Permission decision must be one of: once, reject, deny_job, demand_safe_path.
Review verdict must be one of: pass, fail.
"""


def _fail(msg: str, *, app_id=None) -> int:
    extra = f" application_id={app_id!r}" if app_id is not None else ""
    print(f"deepseek_harness wrapper: {msg}{extra}", file=sys.stderr)
    return 2


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


def _harness_argv(spec: str) -> list[str]:
    spec = (spec or "").strip()
    if not spec:
        return []
    if os.path.isfile(spec):
        return [spec]
    return shlex.split(spec)


def resolve_harness_bin() -> tuple[list[str] | None, list[str]]:
    """Return (argv prefix or None, missing-field labels)."""
    missing: list[str] = []
    spec = (os.environ.get("COLLAB_DEEPSEEK_HARNESS_BIN") or "").strip()
    argv: list[str] | None
    if spec:
        argv = _harness_argv(spec)
        if not argv:
            missing.append("COLLAB_DEEPSEEK_HARNESS_BIN")
            argv = None
        else:
            head = argv[0]
            resolved = shutil.which(head) if not os.path.isfile(head) else head
            if not resolved and not os.path.isfile(head):
                missing.append(f"COLLAB_DEEPSEEK_HARNESS_BIN ({head} not found)")
                argv = None
    else:
        found = shutil.which("dsh")
        if found:
            argv = [found]
        else:
            argv = None
            missing.append("COLLAB_DEEPSEEK_HARNESS_BIN (or dsh on PATH)")
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        missing.append("DEEPSEEK_API_KEY")
    return argv, missing


def last_json_object(text: str) -> dict | None:
    """Last JSON object in text. `--json` event streams are not our schema."""
    if not text:
        return None
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    last: dict | None = None
    last_decision: dict | None = None
    i = 0
    while i < len(text):
        start = text.find("{", i)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            i = start + 1
            continue
        if isinstance(obj, dict):
            last = obj
            if "decision" in obj or "verdict" in obj:
                last_decision = obj
        i = max(end, start + 1)
    return last_decision if last_decision is not None else last


def build_headless_task(envelope: dict) -> str:
    """Task for `dsh --profile headless`: JSON-only lead, no tools."""
    req = envelope.get("request") if isinstance(envelope.get("request"), dict) else {}
    schema = envelope.get("schema") if isinstance(envelope.get("schema"), dict) else {}
    prompt = envelope.get("prompt") if isinstance(envelope.get("prompt"), str) else ""
    kind = req.get("kind") or "permission"
    parts = [
        _HEADLESS_CONSTRAINTS.strip(),
        "",
        f"Decision kind: {kind}",
        "Pinned response schema (echo application_id / context_summary as const):",
        json.dumps(schema, ensure_ascii=False, indent=2, default=str),
        "",
        "Full request JSON:",
        json.dumps(req, ensure_ascii=False, indent=2, default=str),
    ]
    if prompt.strip():
        parts.extend(["", "Lead prompt:", prompt.strip()])
    return "\n".join(parts) + "\n"


def build_dsh_cmd(harness_argv: list[str]) -> list[str]:
    """`dsh --profile headless -` with task on stdin (documented stdin equivalent)."""
    return list(harness_argv) + ["--profile", "headless", "-"]


def _kind_of(envelope: dict) -> str:
    req = envelope.get("request") if isinstance(envelope.get("request"), dict) else {}
    kind = req.get("kind") or envelope.get("kind") or "permission"
    return str(kind)


def validate_decision(parsed: dict | None, envelope: dict) -> tuple[dict | None, str]:
    """Schema/semantic check: application_id bind + legal decision/verdict.

    Prefers in-tree validate_lead_decision; falls back to the same rules.
    """
    req = envelope.get("request") if isinstance(envelope.get("request"), dict) else {}
    if not req and isinstance(envelope.get("application_id"), str):
        req = envelope
    kind = _kind_of(envelope)
    try:
        from lead_adapter.schema import LeadDecisionError, validate_lead_decision

        try:
            out = validate_lead_decision(
                json.dumps(parsed, ensure_ascii=False) if parsed else "",
                parsed,
                request=req,
                kind=kind,
            )
        except LeadDecisionError as e:
            return None, f"validate failed ({e.code}): {e}"
        return out, ""
    except ImportError:
        pass
    if not isinstance(parsed, dict):
        return None, "no parseable JSON decision object"
    expected_id = str(req.get("application_id") or "")
    got_id = str(parsed.get("application_id") or "")
    if not expected_id or got_id != expected_id:
        return None, f"application_id_mismatch expected {expected_id!r} got {got_id!r}"
    expected_summary = str(req.get("context_summary") or "")
    got_summary = str(parsed.get("context_summary") or "")
    if expected_summary and got_summary and got_summary != expected_summary:
        return None, (
            f"context_summary_mismatch expected {expected_summary!r} got {got_summary!r}"
        )
    if kind == "permission":
        decision = str(parsed.get("decision") or "").strip().lower()
        if decision not in PERMISSION_DECISIONS:
            return None, f"illegal_decision decision={decision!r}"
        if not str(parsed.get("reason") or "").strip():
            return None, "missing_reason"
    elif kind == "review":
        verdict = str(parsed.get("verdict") or "").strip().lower()
        if verdict not in REVIEW_VERDICTS:
            return None, f"illegal_verdict verdict={verdict!r}"
        if not str(parsed.get("reason") or "").strip():
            return None, "missing_reason"
    else:
        return None, f"unknown_kind kind={kind!r}"
    return parsed, ""


def _timeout_sec(envelope: dict) -> float:
    raw = envelope.get("timeout_sec")
    try:
        val = float(raw) if raw is not None else 180.0
    except (TypeError, ValueError):
        val = 180.0
    return val if val > 0 else 180.0


def invoke_dsh(harness_argv: list[str], task: str, *, timeout_sec: float) -> subprocess.CompletedProcess:
    cmd = build_dsh_cmd(harness_argv)
    # Throwaway cwd: even if the model ignores the no-tools rule, do not write
    # the collab repo or the job workspace. Do not pass --json.
    with tempfile.TemporaryDirectory(prefix="collab-dsh-lead-") as tmp:
        return subprocess.run(
            cmd,
            input=task,
            capture_output=True,
            text=True,
            timeout=float(timeout_sec),
            cwd=tmp,
            env=os.environ.copy(),
        )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    envelope = _load_envelope(argv)
    if envelope is None:
        return 2
    req = envelope.get("request") if isinstance(envelope.get("request"), dict) else None
    app_id = None
    if isinstance(req, dict):
        app_id = req.get("application_id")
    elif isinstance(envelope, dict):
        app_id = envelope.get("application_id")

    harness_argv, missing = resolve_harness_bin()
    if missing or not harness_argv:
        what = ", ".join(missing) if missing else "COLLAB_DEEPSEEK_HARNESS_BIN (or dsh on PATH)"
        return _fail(
            f"fail-closed, missing: {what}. "
            "Set COLLAB_DEEPSEEK_HARNESS_BIN=dsh (or npx @deepseek-ai/dsh) "
            "and DEEPSEEK_API_KEY. No once/pass invented.",
            app_id=app_id,
        )

    task = build_headless_task(envelope)
    try:
        proc = invoke_dsh(harness_argv, task, timeout_sec=_timeout_sec(envelope))
    except subprocess.TimeoutExpired:
        return _fail("dsh --profile headless timed out", app_id=app_id)
    except OSError as e:
        return _fail(f"dsh spawn failed: {e}", app_id=app_id)

    err = proc.stderr or ""
    if err:
        sys.stderr.write(err)
        if not err.endswith("\n"):
            sys.stderr.write("\n")

    if proc.returncode not in (0, None):
        return _fail(
            f"dsh --profile headless exit={proc.returncode} (stdout withheld; no once/pass)",
            app_id=app_id,
        )

    parsed = last_json_object(proc.stdout or "")
    validated, why = validate_decision(parsed, envelope)
    if validated is None:
        return _fail(
            f"{why or 'illegal JSON'}; dsh stdout is final-answer text, not --json events. "
            "No once/pass invented.",
            app_id=app_id,
        )

    print(json.dumps(validated, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
