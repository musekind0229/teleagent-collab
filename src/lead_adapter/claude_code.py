"""Claude Code LeadAdapter: one read-only ``claude -p`` call, no retry, no fake PASS.

Flags confirmed against ``claude --help`` (Claude Code 2.1.302). The call is::

    claude -p --output-format json --json-schema <pinned schema>
           --tools "" --permission-mode plan --permission-prompts none
           --disallowedTools Bash,Edit,Write,... --no-session-persistence
           --strict-mcp-config [--model M]

The prompt goes on stdin. ``--tools ""`` disables every built-in tool and plan
mode plus ``--permission-prompts none`` denies anything that would prompt, so the
lead can only answer. It runs through ``run_cancellable`` (own process group,
timeout and a cancelled Goal stop the tree), the same chain grok_cli uses.

This module never reads Claude credentials (``~/.claude``, keychain, API keys)
and never logs in. Live login is not verified; "Not logged in" maps to
``call_failed`` with ``lead_error_code=not_logged_in``.

Binary: ``COLLAB_CLAUDE_LEAD_BIN`` or PATH ``claude`` (Windows also
``claude.cmd``/``claude.exe``). ``COLLAB_LEAD_BIN`` is deliberately ignored so a
Grok lead path never collides. Model: ``model=`` > ``COLLAB_CLAUDE_LEAD_MODEL`` >
``DEFAULT_MODEL``, always a full model ID on argv. Aliases such as ``haiku`` were
observed to resolve to a different model, so the default is pinned. The model the
CLI actually used (``modelUsage``) and the cost are kept on ``last_run``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from lead_adapter.base import LeadAdapterABC, safe_failure
from lead_adapter.cancel import LeadCancelled, run_cancellable
from lead_adapter.codex_cli import redact_secrets
from lead_adapter.schema import (
    LeadDecisionError,
    format_lead_request_prompt,
    pin_lead_response_schema,
    validate_lead_decision,
)

# Cheapest full ID that the CLI honoured in a live probe (modelUsage == requested).
DEFAULT_MODEL = "claude-haiku-5-5"

# Every tool that could write, execute or reach the network. Belt and braces on
# top of ``--tools ""``.
DISALLOWED_TOOLS = "Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Task"

FORBIDDEN_FLAGS: tuple[str, ...] = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--add-dir",
    "--worktree",
    "-w",
)
FORBIDDEN_PERMISSION_MODES = frozenset({"bypassPermissions", "acceptEdits", "auto", "dontAsk"})

_JSON_ONLY_LINE = (
    "Reply with ONLY one JSON object matching the schema, no prose, no code fences. "
    "Do not use tools, run commands or edit files."
)
_NOT_LOGGED_IN = re.compile(
    r"not logged in|please run /login|invalid api key|oauth token (?:has )?expired|authentication_error",
    re.I,
)


class ClaudeBinNotFound(FileNotFoundError):
    """No Claude lead binary in COLLAB_CLAUDE_LEAD_BIN or PATH."""


def resolve_claude_bin(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    platform: str | None = None,
    is_file: Callable[[str], bool] | None = None,
) -> str:
    """Resolve the Claude CLI used as lead. Does not spawn it."""
    environ = env if env is not None else os.environ
    plat = platform if platform is not None else sys.platform
    which_fn = which if which is not None else shutil.which
    file_ok = is_file if is_file is not None else (lambda p: Path(p).is_file())
    tried: list[str] = []
    raw = (environ.get("COLLAB_CLAUDE_LEAD_BIN") or "").strip()
    if raw:
        tried.append(f"COLLAB_CLAUDE_LEAD_BIN={raw}")
        if file_ok(raw):
            return raw
    names = ["claude"]
    if str(plat).startswith("win"):
        names.extend(["claude.cmd", "claude.exe"])
    for name in names:
        tried.append(f"PATH {name}")
        hit = which_fn(name)
        if hit and file_ok(hit):
            return hit
    raise ClaudeBinNotFound(
        "Claude lead binary not found. Install Claude Code "
        "(npm install -g @anthropic-ai/claude-code) or set COLLAB_CLAUDE_LEAD_BIN. "
        f"Tried: {', '.join(tried)}"
    )


def build_command(bin_path: str, *, schema: dict, model: str | None) -> list[str]:
    argv = [
        bin_path,
        "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(schema, ensure_ascii=False),
        "--tools", "",
        "--permission-mode", "plan",
        "--permission-prompts", "none",
        "--disallowedTools", DISALLOWED_TOOLS,
        "--no-session-persistence",
        "--strict-mcp-config",
    ]
    if model:
        argv.extend(["--model", model])
    return argv


def find_forbidden_flag(argv: list[str]) -> str | None:
    for i, tok in enumerate(argv[1:], start=1):
        if tok in FORBIDDEN_FLAGS:
            return tok
        if tok == "--permission-mode" and i + 1 < len(argv) and argv[i + 1] in FORBIDDEN_PERMISSION_MODES:
            return f"--permission-mode {argv[i + 1]}"
    return None


def _illegal(msg: str, code: str = "illegal_output") -> tuple[str, dict]:
    return "ILLEGAL_OUTPUT", {"_lead_status": "error", "error": redact_secrets(msg)[:1500], "lead_error_code": code}


def _call_failed(msg: str, **extra: Any) -> tuple[str, dict]:
    env = {"_lead_status": "call_failed", "error": redact_secrets(msg)[-1500:] or "call_failed"}
    env.update(extra)
    return "CALL_FAILED", env


def _strict_object(text: str) -> dict | None:
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


class ClaudeCodeLeadAdapter(LeadAdapterABC):
    """Spawn ``claude -p`` once, read-only. Failures stay failures."""

    name = "claude_code"
    STATUS = "wired_unverified"

    def __init__(self, *, bin_path: str | None = None, model: str | None = None, platform: str | None = None) -> None:
        self._bin_override = (bin_path or "").strip() or None
        self._model = (model or "").strip() or None
        self._platform = platform
        self.last_run: dict[str, Any] | None = None

    def _resolve_bin(self) -> str:
        if self._bin_override:
            if os.path.isfile(self._bin_override):
                return self._bin_override
            raise ClaudeBinNotFound(f"Claude lead binary not found: bin_path={self._bin_override!r} is not a file.")
        return resolve_claude_bin(platform=self._platform)

    def _model_name(self) -> str | None:
        return self._model or (os.environ.get("COLLAB_CLAUDE_LEAD_MODEL") or "").strip() or DEFAULT_MODEL

    def decide(self, request: dict, *, schema: dict, cwd: str, timeout_sec: float = 180) -> tuple[str, dict | None]:
        req = request if isinstance(request, dict) else {}
        try:
            bin_path = self._resolve_bin()
        except ClaudeBinNotFound as exc:
            return safe_failure("call_failed", redact_secrets(str(exc)))

        hint = str(req["extra"].get("allow_hint") or "") if isinstance(req.get("extra"), dict) else ""
        prompt = format_lead_request_prompt(req, allow_hint=hint) + "\n\n" + _JSON_ONLY_LINE
        pinned = pin_lead_response_schema(schema, req)
        model = self._model_name()
        self.last_run = {"model_requested": model, "models_used": [], "model_mismatch": False, "cost_usd": None}
        argv = build_command(bin_path, schema=pinned, model=model)
        bad = find_forbidden_flag(argv)
        if bad:  # never rewrite argv and retry
            return safe_failure("call_failed", f"claude_code refused forbidden flag: {bad}")
        work = cwd if isinstance(cwd, str) and cwd and os.path.isdir(cwd) else None
        try:
            proc = run_cancellable(argv, timeout=float(timeout_sec), input_text=prompt, platform=self._platform, cwd=work)
        except LeadCancelled as exc:
            return safe_failure("call_failed", redact_secrets(f"claude_code stopped: {exc}"))
        except subprocess.TimeoutExpired:
            return safe_failure("timeout", f"claude_code timeout after {float(timeout_sec):g}s")
        except OSError as exc:
            return safe_failure("call_failed", redact_secrets(f"claude_code spawn failed: {exc}"))
        return self._interpret(proc.returncode, proc.stdout or "", proc.stderr or "", req)

    def _interpret(self, code: int | None, stdout: str, stderr: str, req: dict) -> tuple[str, dict | None]:
        out = stdout.strip()
        envelope = _strict_object(out) if out else None
        result_text = str(envelope.get("result") or "") if envelope else ""
        if envelope is not None and self.last_run is not None:
            used = envelope.get("modelUsage")
            used_ids = sorted(used.keys()) if isinstance(used, dict) else []
            cost = envelope.get("total_cost_usd")
            self.last_run.update({
                "models_used": used_ids,
                "model_mismatch": bool(used_ids) and used_ids != [self.last_run["model_requested"]],
                "cost_usd": cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
                "num_turns": envelope.get("num_turns"),
            })
        login_blob = f"{result_text}\n{stderr}" if envelope else f"{out}\n{stderr}"
        # Non-zero is never a decision, even when stdout carries valid JSON.
        if code != 0:
            if _NOT_LOGGED_IN.search(login_blob):
                return _call_failed("claude_code not logged in (run `claude` once and complete /login)",
                                    lead_error_code="not_logged_in", returncode=code)
            return _call_failed((stderr.strip() or result_text or f"exit={code}"), returncode=code)
        if envelope is None:
            return _illegal("claude_code output is not a JSON object" if out else "claude_code produced no output")
        if envelope.get("is_error") is True or str(envelope.get("subtype") or "success") != "success":
            if _NOT_LOGGED_IN.search(login_blob):
                return _call_failed("claude_code not logged in", lead_error_code="not_logged_in")
            return _call_failed(f"claude_code reported error: {envelope.get('subtype')}: {result_text[:500]}")
        decision = envelope.get("structured_output")
        if not isinstance(decision, dict):
            decision = _strict_object(result_text.strip())  # strict: no fences, no regex extraction
        if decision is None:
            return _illegal("claude_code result is not a JSON decision object")
        if decision.get("safe_path_hint") == "":
            decision.pop("safe_path_hint", None)
        raw = json.dumps(decision, ensure_ascii=False)[:3000]
        expected = str(req.get("application_id") or "")
        if expected and str(decision.get("application_id") or "") != expected:
            return _illegal(f"application_id mismatch: expected {expected!r} got {decision.get('application_id')!r}",
                            "application_id_mismatch")
        kind = req.get("kind")
        if kind in ("permission", "review"):
            try:
                validate_lead_decision(raw, decision, request=req, kind=kind)
            except LeadDecisionError as exc:
                return _illegal(f"invalid {kind} decision: {exc}", exc.code)
        return raw, decision

    def doctor_hint(self) -> dict:
        bin_path = bin_error = None
        try:
            bin_path = self._resolve_bin()
        except ClaudeBinNotFound as exc:
            bin_error = str(exc)
        return {
            "status": self.STATUS,
            "name": self.name,
            "fake_pass": False,
            "bin_path": bin_path,
            "bin_error": bin_error,
            "model": self._model_name(),
            "last_run": self.last_run,
            "tools": "none",
            "permission_mode": "plan",
            "live_verified": False,
        }


__all__ = [
    "DEFAULT_MODEL",
    "DISALLOWED_TOOLS",
    "FORBIDDEN_FLAGS",
    "ClaudeBinNotFound",
    "ClaudeCodeLeadAdapter",
    "build_command",
    "find_forbidden_flag",
    "resolve_claude_bin",
]
