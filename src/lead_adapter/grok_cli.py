"""Grok CLI LeadAdapter — one optional backend; no disallowed-tools degradation retry."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from lead_adapter.base import LeadAdapterABC, safe_failure
from lead_adapter.schema import format_lead_request_prompt, pin_lead_response_schema

LEGACY_LINUX_LEAD_BIN = "/workspace/run-grok.sh"
WIN_DEFAULT_BASE_URL = "http://127.0.0.1:4397"
POSIX_DEFAULT_BASE_URL = "http://127.0.0.1:4399"


class LeadBinNotFound(FileNotFoundError):
    """No Grok lead binary in env, PATH, ~/.grok/bin, or the legacy Linux path."""


def default_live_base_url(
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> str:
    """TeleAgent worker HTTP for live Grok lead.

    ``TELEAGENT_BASE_URL`` wins when set. Windows defaults to :4397
    (DESKTOP-TBB531F worker HTTP); posix defaults to :4399.
    """
    environ = env if env is not None else os.environ
    plat = platform if platform is not None else sys.platform
    explicit = (environ.get("TELEAGENT_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    if plat.startswith("win"):
        return WIN_DEFAULT_BASE_URL
    return POSIX_DEFAULT_BASE_URL


def resolve_lead_bin(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    home: Path | None = None,
    platform: str | None = None,
    is_file: Callable[[str], bool] | None = None,
) -> str:
    """Resolve the Grok CLI binary used as lead.

    Order:
    1. ``COLLAB_LEAD_BIN`` if set and the path is a file
    2. PATH ``grok``, then ``grok.exe``
    3. ``~/.grok/bin/grok.exe`` on Windows, ``~/.grok/bin/grok`` on posix
    4. ``/workspace/run-grok.sh`` on posix only, and only if that file exists
       (legacy Linux). Never the Windows default.

    Raises ``LeadBinNotFound`` when nothing matches.
    """
    environ = env if env is not None else os.environ
    plat = platform if platform is not None else sys.platform
    which_fn = which if which is not None else shutil.which
    file_ok = is_file if is_file is not None else (lambda p: Path(p).is_file())
    home_path = Path(home) if home is not None else Path.home()
    tried: list[str] = []

    explicit = (environ.get("COLLAB_LEAD_BIN") or "").strip()
    if explicit:
        tried.append(f"COLLAB_LEAD_BIN={explicit}")
        if file_ok(explicit):
            return explicit

    for name in ("grok", "grok.exe"):
        tried.append(f"PATH {name}")
        found = which_fn(name)
        if found and file_ok(found):
            return found

    local_name = "grok.exe" if plat.startswith("win") else "grok"
    local = str(home_path / ".grok" / "bin" / local_name)
    tried.append(local)
    if file_ok(local):
        return local

    if not plat.startswith("win"):
        tried.append(LEGACY_LINUX_LEAD_BIN)
        if file_ok(LEGACY_LINUX_LEAD_BIN):
            return LEGACY_LINUX_LEAD_BIN

    raise LeadBinNotFound(
        "Grok lead binary not found. Set COLLAB_LEAD_BIN to an existing grok "
        "or grok.exe, put grok on PATH, or install under ~/.grok/bin "
        f"(Windows: grok.exe). Tried: {', '.join(tried)}"
    )


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
        explicit = (bin_path or "").strip()
        self.bin_path = explicit or resolve_lead_bin()
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
        pinned_schema = pin_lead_response_schema(schema, request)
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
            json.dumps(pinned_schema),
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


__all__ = [
    "GrokCliLeadAdapter",
    "LeadBinNotFound",
    "resolve_lead_bin",
    "default_live_base_url",
    "LEGACY_LINUX_LEAD_BIN",
    "WIN_DEFAULT_BASE_URL",
    "POSIX_DEFAULT_BASE_URL",
]
