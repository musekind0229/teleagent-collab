"""Grok CLI LeadAdapter — one optional backend; no disallowed-tools degradation retry."""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from lead_adapter.base import LeadAdapterABC, safe_failure
from lead_adapter.schema import format_lead_request_prompt


class GrokCliLeadAdapter(LeadAdapterABC):
    """Spawn Grok/compatible CLI once with disallowed-tools. Failures stay failed."""

    name = "grok_cli"

    def __init__(
        self,
        *,
        bin_path: str | None = None,
        disallowed_tools: str = "bash,shell,edit,write,web_search,web_fetch",
        max_turns: int = 1,
    ) -> None:
        self.bin_path = bin_path or os.environ.get("COLLAB_LEAD_BIN", "/workspace/run-grok.sh")
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
        hint = ""
        if isinstance(request.get("extra"), dict):
            hint = str(request["extra"].get("allow_hint") or "")
        prompt = format_lead_request_prompt(request, allow_hint=hint)
        if isinstance(request.get("extra"), dict) and request["extra"].get("legacy_prompt"):
            prompt = str(request["extra"]["legacy_prompt"]) + "\n\n" + prompt
        cmd = [
            self.bin_path,
            "-p",
            prompt,
            "--cwd",
            cwd,
            "--max-turns",
            str(self.max_turns),
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--disallowed-tools",
            self.disallowed_tools,
        ]
        # Intentionally NO retry that drops --disallowed-tools (条3: delete that degradation).
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=float(timeout_sec)
            )
        except subprocess.TimeoutExpired:
            return safe_failure("timeout", "grok_cli timeout")
        except OSError as e:
            return safe_failure("call_failed", f"grok_cli spawn failed: {e}")

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        raw = out if out else err
        if proc.returncode not in (0, None) and not out:
            # Tool-name / CLI errors: keep failed — do NOT strip disallowed-tools and retry.
            return raw[:3000] or "CALL_FAILED", {
                "_lead_status": "call_failed",
                "error": err[:1500] or f"exit={proc.returncode}",
                "returncode": proc.returncode,
            }

        parsed = self._parse(out) or self._parse(err)
        return (raw[:3000] if raw else ""), parsed

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


__all__ = ["GrokCliLeadAdapter"]
