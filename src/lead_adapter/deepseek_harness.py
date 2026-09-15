"""DeepSeek harness LeadAdapter — JSON-in/JSON-out wrapper; 真 harness 未接线验收.

Call surface matches grok_cli's fail-closed spawn (one shot, no tool-restriction
retry): COLLAB_LEAD_BIN (or COLLAB_DEEPSEEK_LEAD_BIN) is an executable wrapper
that reads a lead envelope on stdin or --request-file and writes a decision
JSON object on stdout.

The real DeepSeek harness CLI argv / HTTP API is not wired. The in-tree
example wrapper (bin/run-deepseek-lead.py) exits non-zero rather than invent
once/pass. Do not treat this adapter as a live DeepSeek API client.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from lead_adapter.base import LeadAdapterABC, safe_failure
from lead_adapter.schema import format_lead_request_prompt, pin_lead_response_schema

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_WRAPPER = str(_REPO_ROOT / "bin" / "run-deepseek-lead.py")

# Hint only — never stripped for a retry (条3). Real DeepSeek CLI may ignore it.
_DEFAULT_DISALLOWED = "bash,shell,edit,write,web_search,web_fetch"


class DeepSeekHarnessLeadAdapter(LeadAdapterABC):
    """Spawn a JSON wrapper once. Failures stay failed. Status: 真 harness 未接线验收."""

    name = "deepseek_harness"
    STATUS = "unwired"
    REASON = (
        "DeepSeek harness lead adapter is landed as JSON-in/JSON-out. "
        "真 harness 未接线验收 — no live DeepSeek CLI/API. "
        "Illegal/timeout/spawn failure → call_failed; never invent once/pass."
    )

    def __init__(
        self,
        *,
        bin_path: str | None = None,
        io_mode: str | None = None,
        disallowed_tools: str = _DEFAULT_DISALLOWED,
        max_turns: int = 1,
    ) -> None:
        explicit = bin_path
        if explicit is None:
            explicit = os.environ.get("COLLAB_DEEPSEEK_LEAD_BIN") or os.environ.get(
                "COLLAB_LEAD_BIN"
            )
        self.bin_path = (explicit or _DEFAULT_WRAPPER).strip() or _DEFAULT_WRAPPER
        mode = (io_mode or os.environ.get("COLLAB_DEEPSEEK_IO") or "stdin").strip().lower()
        self.io_mode = "file" if mode in ("file", "path", "request-file") else "stdin"
        self.disallowed_tools = disallowed_tools
        self.max_turns = max_turns

    def decide(
        self,
        request: dict,
        *,
        schema: dict,
        cwd: str,
        timeout_sec: float = 180,
    ) -> tuple[str, dict | None]:
        if not self.bin_path:
            return safe_failure(
                "call_failed",
                f"{self.REASON} missing COLLAB_LEAD_BIN "
                f"application_id={request.get('application_id')!r}",
            )

        hint = ""
        if isinstance(request.get("extra"), dict):
            hint = str(request["extra"].get("allow_hint") or "")
        prompt = format_lead_request_prompt(request, allow_hint=hint)
        if isinstance(request.get("extra"), dict) and request["extra"].get("legacy_prompt"):
            prompt = str(request["extra"]["legacy_prompt"]) + "\n\n" + prompt
        pinned_schema = pin_lead_response_schema(schema, request)
        envelope = {
            "protocol": "collab-lead-v1",
            "adapter": self.name,
            "request": request,
            "schema": pinned_schema,
            "cwd": cwd,
            "prompt": prompt,
            "timeout_sec": float(timeout_sec),
            "constraints": {
                "disallowed_tools": self.disallowed_tools,
                "max_turns": self.max_turns,
                "no_retry_without_constraints": True,
            },
        }
        payload = json.dumps(envelope, ensure_ascii=False, default=str)

        tmp_path: str | None = None
        try:
            cmd, stdin_text, tmp_path = self._build_cmd(payload)
            # Intentionally NO retry that drops constraints / tool limits (条3).
            try:
                proc = subprocess.run(
                    cmd,
                    input=stdin_text,
                    capture_output=True,
                    text=True,
                    timeout=float(timeout_sec),
                    cwd=cwd or None,
                )
            except subprocess.TimeoutExpired:
                return safe_failure("timeout", "deepseek_harness timeout")
            except OSError as e:
                return safe_failure("call_failed", f"deepseek_harness spawn failed: {e}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        raw = out if out else err
        if proc.returncode not in (0, None) and not out:
            # Wrapper / CLI errors: keep failed — do NOT invent once/pass or retry.
            return raw[:3000] or "CALL_FAILED", {
                "_lead_status": "call_failed",
                "error": err[:1500] or f"exit={proc.returncode}",
                "returncode": proc.returncode,
            }

        parsed = self._parse(out) or self._parse(err)
        return (raw[:3000] if raw else ""), parsed

    def _build_cmd(self, payload: str) -> tuple[list[str], str | None, str | None]:
        argv = self._interpreter_prefix() + [self.bin_path]
        if self.io_mode == "file":
            fd, tmp_path = tempfile.mkstemp(prefix="collab-deepseek-lead-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.write("\n")
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            return argv + ["--request-file", tmp_path], None, tmp_path
        return argv, payload, None

    def _interpreter_prefix(self) -> list[str]:
        """Prefix the current interpreter for .py wrappers so +x is not required."""
        if self.bin_path.endswith(".py"):
            return [sys.executable]
        return []

    def doctor_hint(self) -> dict:
        return {
            "status": self.STATUS,
            "name": self.name,
            "reason": self.REASON,
            "fake_pass": False,
            "wired": False,
            "io_mode": self.io_mode,
            "bin_path": self.bin_path,
        }

    @staticmethod
    def _parse(text: str) -> dict | None:
        if not text:
            return None
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except Exception:
            m = re.search(r"\{[\s\S]*\}", text)
            if not m:
                return None
            try:
                obj = json.loads(m.group(0))
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None


__all__ = ["DeepSeekHarnessLeadAdapter"]
