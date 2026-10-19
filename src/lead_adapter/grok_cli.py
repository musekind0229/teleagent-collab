"""Grok CLI LeadAdapter — one optional backend; no disallowed-tools degradation retry."""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from lead_adapter.base import LeadAdapterABC, safe_failure
from lead_adapter.cancel import LeadCancelled, run_cancellable
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


MAX_TOOL_FREE_LEAD_TURNS = 32


def lead_max_turns(value):
    """Explicit bounded agentic-turn budget, not tokens or paid dispatch count.

    Keep defaults/legacy 1 and 2 unchanged. Larger values require the existing
    tool-free plan/review mode; permission dispatch remains one turn.
    """
    if type(value) is int:
        turns = value
    elif type(value) is str and re.fullmatch(r'[1-9][0-9]*', value):
        if len(value) > len(str(MAX_TOOL_FREE_LEAD_TURNS)):
            raise ValueError('lead max turns exceeds bounded maximum')
        turns = int(value)
    else:
        raise ValueError('lead max turns must be an exact positive integer')
    if not 1 <= turns <= MAX_TOOL_FREE_LEAD_TURNS:
        raise ValueError(f'lead max turns must be 1..{MAX_TOOL_FREE_LEAD_TURNS}')
    return turns


class GrokCliLeadAdapter(LeadAdapterABC):
    """Spawn Grok/compatible CLI once with disallowed-tools. Failures stay failed."""

    name = "grok_cli"

    def __init__(
        self,
        *,
        bin_path: str | None = None,
        disallowed_tools: str = "bash,shell,edit,write,web_search,web_fetch",
        max_turns: int = 1,
        tool_free: bool = False,
        caller_evidence_root: str | Path | None = None,
    ) -> None:
        self.max_turns = lead_max_turns(max_turns)
        if self.max_turns > 1 and not tool_free:
            raise ValueError('multiple turns require tool_free=True')
        explicit = (bin_path or "").strip()
        self.bin_path = explicit or resolve_lead_bin()
        self.disallowed_tools = disallowed_tools
        self.tool_free = tool_free
        self.caller_evidence_root = Path(caller_evidence_root) if caller_evidence_root else None

    def decide(
        self,
        request: dict,
        *,
        schema: dict,
        cwd: str,
        timeout_sec: float = 180,
    ) -> tuple[str, dict | None]:
        # Only tool-free plan/review can use configured internal CLI turns.
        # This is ONE admitted lead dispatch, not max_turns package claims.
        max_turns = self.max_turns if self.tool_free and request.get('kind') in ('plan', 'review') else 1
        hint = ""
        if isinstance(request.get("extra"), dict):
            hint = str(request["extra"].get("allow_hint") or "")
        prompt = format_lead_request_prompt(request, allow_hint=hint, tool_free=self.tool_free)
        if isinstance(request.get("extra"), dict) and request["extra"].get("legacy_prompt"):
            prompt = str(request["extra"]["legacy_prompt"]) + "\n\n" + prompt
        pinned_schema = pin_lead_response_schema(schema, request)
        cmd = [
            self.bin_path,
            "--prompt-file",
            "",  # Unique UTF-8 file assigned inside the protected lifecycle below.
            "--cwd",
            cwd,
            "--max-turns",
            str(max_turns),
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(pinned_schema),
            "--disallowed-tools",
            self.disallowed_tools,
        ]
        if self.tool_free:
            cmd += ['--tools', '', '--disable-web-search', '--no-subagents']
        # Intentionally NO retry that drops --disallowed-tools, widens tools or adds turns.
        prompt_path = None
        try:
            # Binary write preserves logical UTF-8 bytes, including LF, without
            # Windows text translation. Independent calls own independent files.
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix="collab-grok-", suffix=".txt",
                dir=os.environ.get("TMPDIR") or cwd, delete=False,
            ) as prompt_file:
                prompt_path = Path(prompt_file.name)
                prompt_file.write(prompt.encode("utf-8"))
            cmd[2] = str(prompt_path)
            if self.caller_evidence_root is not None:
                import uuid, hashlib, time
                folder = self.caller_evidence_root / ('call-' + uuid.uuid4().hex)
                folder.mkdir(parents=True)
                (folder/'PROMPT.txt').write_bytes(prompt.encode('utf-8'))
                (folder/'CALLER.json').write_text(json.dumps({'argv':cmd, 'os_cwd':os.getcwd(), 'cli_cwd':cwd, 'timeout_sec':float(timeout_sec), 'max_turns':max_turns, 'configured_max_turns':self.max_turns, 'tool_free':self.tool_free, 'prompt_sha256':hashlib.sha256(prompt_path.read_bytes()).hexdigest(), 'schema':pinned_schema, 'prepared_at':time.time(), 'stage':'prepared_before_spawn_not_provider_wire'}, indent=2), encoding='utf-8')
            # Own process group (planning and permission/review decisions):
            # timeout or a cancelled Goal stops the whole lead tree.
            proc = run_cancellable(cmd, timeout=float(timeout_sec))
        except (LeadCancelled, subprocess.TimeoutExpired) as exc:
            timed_out = isinstance(exc, subprocess.TimeoutExpired)
            raw, failed = safe_failure('timeout' if timed_out else 'call_failed',
                'grok_cli timeout' if timed_out else f'grok_cli stopped: {exc}')
            # Never promote partial JSON to a decision. Preserve trusted capture
            # on the failure path just as on normal subprocess completion.
            failed['raw_evidence'] = copy.deepcopy(getattr(exc, 'raw_evidence', {
                'capture_status': 'not_recorded', 'capture_complete': False}))
            failed['raw_evidence']['caller_observation'] = {
                'outcome': 'timeout' if timed_out else 'cancelled',
                'failure_stage': 'client_subprocess_deadline' if timed_out else 'client_cancel_scope',
                'timeout_sec': float(timeout_sec), 'max_turns': max_turns,
                'tool_free': self.tool_free, 'timeout_exception': timed_out,
                'cancel_scope_exception': not timed_out,
                'provider_cause': 'unknown'}
            return raw, failed
        except OSError as e:
            return safe_failure("call_failed", f"grok_cli spawn failed: {e}")
        finally:
            if prompt_path is not None:
                prompt_path.unlink(missing_ok=True)

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        raw = out if out else err

        # Trusted capture provenance is assigned by the binary subprocess layer.
        # Old decoded/mock returns cannot be reconstructed as original bytes.
        raw_evidence = copy.deepcopy(getattr(proc, "raw_evidence", {
            "capture_status": "not_recorded", "capture_complete": False,
            "reason": "only decoded text available; original bytes not recoverable",
        }))

        raw_evidence['caller_observation'] = {'outcome':'process_returned', 'timeout_sec':float(timeout_sec), 'max_turns':max_turns, 'tool_free':self.tool_free, 'timeout_exception':False, 'cancel_scope_exception':False}
        raw_evidence['decoded_text'] = {'stdout': proc.stdout or '', 'stderr': proc.stderr or ''}
        # Compatibility decoded views are explicitly NOT original capture bytes.
        raw_evidence.update(stdout=proc.stdout or '', stderr=proc.stderr or '',
                            full_raw=raw, raw_len=len(raw), returncode=proc.returncode,
                            truncated=len(raw) > 3000, text_fields_are_decoded=True)

        # Summary is separated from raw evidence; raw[:3000] only appears in summary with explicit tag
        if len(raw) > 3000:
            summary = f"[TRUNCATED: raw length {len(raw)} exceeds 3000]\n" + raw[:3000]
        else:
            summary = raw

        if proc.returncode not in (0, None) and not out:
            # Tool-name / CLI errors: keep failed — do NOT strip disallowed-tools and retry.
            err_dict = {
                "_lead_status": "call_failed",
                "error": err[:1500] or f"exit={proc.returncode}",
                "returncode": proc.returncode,
                "raw_evidence": raw_evidence,
                "summary": summary or "CALL_FAILED",
            }
            return summary or "CALL_FAILED", err_dict

        parsed = self._parse(out) or self._parse(err)
        if parsed is None:
            parsed = {"raw_text": raw}
        if isinstance(parsed, dict):
            # Model response fields must NEVER overwrite coordinator raw evidence.
            # If the model tried to return a forged "raw_evidence", isolate it and refuse trust.
            if "raw_evidence" in parsed and parsed["raw_evidence"] != raw_evidence:
                parsed["untrusted_raw_evidence"] = parsed.pop("raw_evidence")
            observed_turns = parsed.get('num_turns')
            if type(observed_turns) is not int or observed_turns < 0 or not any(k in parsed for k in ('stopReason', 'structuredOutput')):
                observed_turns = None
            raw_evidence['caller_observation'].update(
                configured_internal_turn_limit=self.max_turns,
                effective_internal_turn_limit=max_turns,
                reported_internal_turns=observed_turns,
                reported_internal_turns_source='CLI envelope report, not billing' if observed_turns is not None else 'unknown',
                resource_accounting='one admitted CLI dispatch; internal turns never create extra package claims')
            parsed["raw_evidence"] = copy.deepcopy(raw_evidence)
            parsed["summary"] = summary

            # Classify stopReason cancelled / structuredOutput None / placeholders
            # as incomplete / review_blocked, so they are not masked as application_id_mismatch
            # or treated as valid code FAIL.
            stop_reason = str(parsed.get("stopReason") or "").strip().lower()
            structured_out = parsed.get("structuredOutput")
            if stop_reason == "cancelled":
                parsed["_lead_status"] = "review_blocked"
                parsed["review_status"] = "review_blocked"
                parsed["incomplete"] = True
                parsed["reason"] = parsed.get("reason") or "lead execution cancelled; unreviewed"
            elif "structuredOutput" in parsed and structured_out is None:
                parsed["_lead_status"] = "review_blocked"
                parsed["review_status"] = "review_blocked"
                parsed["incomplete"] = True
                parsed["reason"] = parsed.get("reason") or "lead structured output is None; unreviewed"

            if parsed.get('incomplete') and 'max turns reached' in err.lower():
                parsed['reason'] = 'CLI max turns reached before structured output; incomplete '+str(request.get('kind') or 'lead')+' call, not a business review verdict'
                parsed['failure_stage'] = 'lead_cli_turn_limit'

        return summary, parsed

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
