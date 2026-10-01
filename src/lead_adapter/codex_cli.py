"""Codex CLI LeadAdapter — one read-only ``codex exec``, no retry, no fake PASS.

Live Codex login is not verified. This module never reads ``~/.codex/auth.json``
or any other Codex credential file, and it never loosens the sandbox.

Binary resolution is lazy (``CodexCliLeadAdapter()`` does not spawn or search).
``COLLAB_CODEX_LEAD_BIN`` wins over ``COLLAB_LEAD_BIN`` so a Grok lead path does
not collide. Example on DESKTOP-TBB531F (codex-cli 0.155.0)::

    C:\\Users\\Admin\\.local\\share\\TeleAgent\\runtimes\\node\\codex.cmd

The alias ``codex`` is not this adapter — it stays in-process dialogue-as-lead.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from lead_adapter.base import LeadAdapterABC, safe_failure
from lead_adapter.cancel import LeadCancelled, communicate as _cancellable_communicate, current_scope
from lead_adapter.schema import format_lead_request_prompt, pin_lead_response_schema

# codex-cli 0.155.0 on DESKTOP-TBB531F. Documentation / error hint only.
EXAMPLE_WINDOWS_CODEX_BIN = (
    r"C:\Users\Admin\.local\share\TeleAgent\runtimes\node\codex.cmd"
)

TEMP_PREFIX = "collab-codex-lead-"

# Never place these on the exec argv. Space-separated entries are adjacent tokens
# (``-s workspace-write``), not a single argv element. Long-form sandbox modes are
# the same hazard as the short ``-s`` forms.
FORBIDDEN_FLAGS: tuple[str, ...] = (
    "--dangerously-bypass-approvals-and-sandbox",
    "--full-auto",
    "--approve-for-me",
    "--dangerously-bypass-hook-trust",
    "--add-dir",
    "--worktree",
    "-s workspace-write",
    "-s danger-full-access",
    "--sandbox workspace-write",
    "--sandbox danger-full-access",
)

# cmd.exe metacharacters. Space is intentionally allowed (paths). Checked only
# when spawning a Windows ``.cmd`` / ``.bat`` via ``cmd /c``.
_CMD_META = set('"%!^&|<>\r\n')
# ``-c approval_policy=never``: bare ``never`` is not valid TOML, so codex uses
# the raw string literal (per ``codex exec --help``). No quotes on argv, so the
# cmd.exe metacharacter scan has no exceptions.

_JSON_ONLY_LINE = (
    "Reply with ONLY one JSON object matching the schema, no prose, no code fences. "
    "Do not run commands or edit files."
)

_SK_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")
_BEARER_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_HEX_RE = re.compile(r"(?<![A-Za-z0-9])[A-Fa-f0-9]{32,}(?![A-Za-z0-9])")
_B64_RE = re.compile(
    r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=])"
)

_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


class CodexBinNotFound(FileNotFoundError):
    """No Codex lead binary in COLLAB_CODEX_LEAD_BIN, COLLAB_LEAD_BIN, or PATH."""


class CodexArgvRejected(ValueError):
    """A Windows ``.cmd`` argv element contains a cmd.exe metacharacter."""

    def __init__(self, arg: str, index: int) -> None:
        self.arg = arg
        self.index = index
        super().__init__(
            "codex_cli refused argv"
            f"[{index}] for Windows cmd.exe spawn "
            f"(contains a cmd metacharacter): {arg!r}"
        )


def redact_secrets(text: str) -> str:
    """Mask token-shaped substrings before they land in a failure envelope.

    Covers ``sk-...``, ``Bearer ...``, and hex / base64 runs of 32+ characters.
    Ordinary short errors and paths are left alone.
    """
    if not text:
        return text
    text = _SK_RE.sub("sk-***", text)
    text = _BEARER_RE.sub("Bearer ***", text)
    text = _HEX_RE.sub("***", text)
    text = _B64_RE.sub("***", text)
    return text


def resolve_codex_bin(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    platform: str | None = None,
    is_file: Callable[[str], bool] | None = None,
) -> str:
    """Resolve the Codex CLI binary used as lead. Does not spawn it.

    Order (each explicit path only if it is a file):
    1. ``COLLAB_CODEX_LEAD_BIN`` — Codex-specific, so a Grok ``COLLAB_LEAD_BIN``
       does not win by accident
    2. ``COLLAB_LEAD_BIN``
    3. PATH ``codex``, then on Windows ``codex.cmd`` and ``codex.exe``

    Raises ``CodexBinNotFound`` listing what was tried.
    Example ``COLLAB_CODEX_LEAD_BIN`` (DESKTOP-TBB531F, codex-cli 0.155.0):
    ``C:\\Users\\Admin\\.local\\share\\TeleAgent\\runtimes\\node\\codex.cmd``.
    """
    environ = env if env is not None else os.environ
    plat = platform if platform is not None else sys.platform
    which_fn = which if which is not None else shutil.which
    file_ok = is_file if is_file is not None else (lambda p: Path(p).is_file())
    tried: list[str] = []

    def take_explicit(var: str) -> str | None:
        raw = (environ.get(var) or "").strip()
        if not raw:
            return None
        tried.append(f"{var}={raw}")
        if file_ok(raw):
            return raw
        return None

    found = take_explicit("COLLAB_CODEX_LEAD_BIN")
    if found:
        return found
    found = take_explicit("COLLAB_LEAD_BIN")
    if found:
        return found

    names = ["codex"]
    if str(plat).startswith("win"):
        names.extend(["codex.cmd", "codex.exe"])
    for name in names:
        tried.append(f"PATH {name}")
        hit = which_fn(name)
        if hit and file_ok(hit):
            return hit

    raise CodexBinNotFound(
        "Codex lead binary not found. Set COLLAB_CODEX_LEAD_BIN to an existing "
        "codex binary (example DESKTOP-TBB531F codex-cli 0.155.0: "
        f"{EXAMPLE_WINDOWS_CODEX_BIN}). "
        "COLLAB_CODEX_LEAD_BIN is checked before COLLAB_LEAD_BIN so a grok lead "
        f"bin does not collide. Tried: {', '.join(tried)}"
    )


def codex_output_schema(pinned: dict) -> dict:
    """Deep-copy a pinned lead schema into Codex strict structured output.

    ``--output-schema`` requires every property in ``required``,
    ``additionalProperties: false``, and ``enum`` rather than ``const``.
    Optional strings such as ``safe_path_hint`` become required (the model may
    send ``""``). The caller's schema is not mutated.
    """
    if not isinstance(pinned, dict):
        pinned = {"type": "object", "properties": {}}
    out = copy.deepcopy(pinned)
    _strictify_schema(out)
    return out


def _strictify_schema(node: Any) -> None:
    if not isinstance(node, dict):
        return
    if "const" in node:
        value = node.pop("const")
        node["enum"] = [value]
        if "type" not in node:
            node["type"] = _json_type_name(value)
    props = node.get("properties")
    if isinstance(props, dict):
        for sub in props.values():
            _strictify_schema(sub)
        node["required"] = list(props.keys())
        node["additionalProperties"] = False
    items = node.get("items")
    if isinstance(items, dict):
        _strictify_schema(items)
    elif isinstance(items, list):
        for sub in items:
            _strictify_schema(sub)
    for key in ("anyOf", "oneOf", "allOf", "$defs", "definitions"):
        seq = node.get(key)
        if isinstance(seq, list):
            for sub in seq:
                _strictify_schema(sub)
        elif isinstance(seq, dict):
            for sub in seq.values():
                _strictify_schema(sub)


def _json_type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def build_command(
    bin_path: str,
    *,
    cwd: str,
    schema_path: str,
    last_message_path: str,
) -> list[str]:
    """Argv for one read-only ``codex exec``. Prompt is stdin (trailing ``-``).

    No ``--json``, no sandbox other than ``read-only``, no approval bypass.
    """
    return [
        bin_path,
        "exec",
        "--sandbox",
        "read-only",
        "-c",
        "approval_policy=never",
        "--ephemeral",
        "--skip-git-repo-check",
        "--color",
        "never",
        "-C",
        cwd,
        "--output-schema",
        schema_path,
        "-o",
        last_message_path,
        "-",
    ]


def find_forbidden_flag(argv: list[str]) -> str | None:
    """Return the ``FORBIDDEN_FLAGS`` entry present in ``argv``, if any."""
    args = [str(a) for a in argv]
    for flag in FORBIDDEN_FLAGS:
        if " " in flag:
            left, right = flag.split(" ", 1)
            for i in range(len(args) - 1):
                if args[i] == left and args[i + 1] == right:
                    return flag
        elif flag in args:
            return flag
    return None


def _is_batch_script(path: str) -> bool:
    return str(path).lower().endswith((".cmd", ".bat"))


def spawn_spec(
    argv: list[str],
    platform: str | None = None,
    comspec: str | None = None,
) -> tuple[list[str] | str, bool]:
    """Return ``(args, use_string)`` for ``subprocess.Popen``.

    Non-batch binaries (and batch files when not on Windows) spawn the argv
    list directly. On Windows, a ``.cmd`` / ``.bat`` binary is not handed to
    ``CreateProcess`` (BatBadBut-style escaping). It becomes one command-line
    string::

        {COMSPEC} /d /s /c "{list2cmdline(argv)}"

    so cmd's ``/s`` quote rule strips exactly one pair of quotes. ``use_string``
    is True only in that case. Raises ``CodexArgvRejected`` (no spawn) when a
    Windows batch argv element contains a cmd metacharacter.
    """
    plat = platform if platform is not None else sys.platform
    exe = str(argv[0]) if argv else ""
    if _is_batch_script(exe) and str(plat).startswith("win"):
        for index, arg in enumerate(argv):
            text = str(arg)
            if any(ch in text for ch in _CMD_META):
                raise CodexArgvRejected(text, index)
        com = comspec if comspec is not None else (os.environ.get("COMSPEC") or "cmd.exe")
        inner = subprocess.list2cmdline([str(a) for a in argv])
        # Single string: do not let Popen/list2cmdline re-quote the /c payload.
        return f'{com} /d /s /c "{inner}"', True
    return [str(a) for a in argv], False


def _compose_prompt(request: dict) -> str:
    extra = request.get("extra") if isinstance(request.get("extra"), dict) else {}
    hint = str(extra.get("allow_hint") or "") if extra else ""
    prompt = format_lead_request_prompt(request, allow_hint=hint)
    if extra and extra.get("legacy_prompt"):
        prompt = str(extra["legacy_prompt"]) + "\n\n" + prompt
    if not prompt.endswith("\n"):
        prompt += "\n"
    return prompt + _JSON_ONLY_LINE + "\n"


def _format_seconds(timeout_sec: float) -> str:
    n = float(timeout_sec)
    if n.is_integer():
        return str(int(n))
    text = f"{n:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _illegal_output(reason: str, model_text: str = "") -> tuple[str, dict]:
    """Failure envelope. ``raw`` is a marker so upstream cannot regex the prose."""
    detail = reason
    if model_text:
        snippet = model_text.strip().replace("\r", " ").replace("\n", " ")
        if len(snippet) > 300:
            snippet = snippet[:300]
        detail = f"{reason}: {snippet}"
    detail = redact_secrets(detail)
    return (
        "ILLEGAL_OUTPUT",
        {
            "_lead_status": "error",
            "error": f"codex_cli illegal output: {detail}",
            "lead_error_code": "illegal_output",
        },
    )


def _id_mismatch(expected: str, got: Any) -> tuple[str, dict]:
    msg = redact_secrets(
        f"codex_cli application_id mismatch: expected {expected!r} got {got!r}"
    )
    return (
        "ILLEGAL_OUTPUT",
        {
            "_lead_status": "error",
            "error": msg,
            "lead_error_code": "application_id_mismatch",
        },
    )


def _nonzero(stderr: str, code: int) -> tuple[str, dict]:
    err = redact_secrets((stderr or "").strip())
    if len(err) > 1500:
        err = err[-1500:]
    return (
        "CALL_FAILED",
        {
            "_lead_status": "call_failed",
            "error": err or f"exit={code}",
            "returncode": code,
        },
    )


def _read_last_message(path: str) -> str | None:
    """Stripped ``-o`` file, or None when missing/empty so stdout can be tried."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    return stripped


def _kill_process_tree(proc: subprocess.Popen, platform: str) -> None:
    """Kill the lead process and its children. Never raise."""
    if str(platform).startswith("win"):
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass
        return
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = proc.pid
    if not pgid:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _reap_after_kill(proc: subprocess.Popen, platform: str) -> None:
    try:
        proc.communicate(timeout=5)
    except Exception:
        _kill_process_tree(proc, platform)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def _parse_decision_text(text: str, request: dict) -> tuple[str, dict]:
    """Strict parse. No fence stripping, no regex extraction, no stderr."""
    if not text or not str(text).strip():
        return _illegal_output("empty output")
    body = str(text).strip()
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return _illegal_output("not valid JSON", body)
    if not isinstance(obj, dict):
        kind = type(obj).__name__
        return _illegal_output(f"JSON {kind} is not an object", body)
    if obj.get("safe_path_hint") == "":
        obj.pop("safe_path_hint", None)
    expected_raw = request.get("application_id") if isinstance(request, dict) else None
    expected = "" if expected_raw is None else str(expected_raw)
    if expected != "":
        got = obj.get("application_id")
        got_s = "" if got is None else str(got)
        if got_s != expected:
            return _id_mismatch(expected, got)
    return body[:3000], obj


class CodexCliLeadAdapter(LeadAdapterABC):
    """Spawn ``codex exec`` once in a read-only sandbox. Failures stay failures.

    Constructor and ``get_lead_adapter("codex_cli")`` do not resolve the binary.
    ``decide`` returns ``call_failed`` when it is missing. Never retries, never
    drops ``--output-schema``, never switches the sandbox off ``read-only``.
    """

    name = "codex_cli"

    def __init__(
        self,
        *,
        bin_path: str | None = None,
        platform: str | None = None,
    ) -> None:
        explicit = (bin_path or "").strip()
        # Stored only. Existence is checked in decide / doctor_hint.
        self._bin_override = explicit or None
        self._platform = platform

    def _platform_name(self) -> str:
        return self._platform if self._platform is not None else sys.platform

    def _resolve_bin(self) -> str:
        if self._bin_override:
            if os.path.isfile(self._bin_override):
                return self._bin_override
            raise CodexBinNotFound(
                "Codex lead binary not found: "
                f"bin_path={self._bin_override!r} is not a file. "
                "Set COLLAB_CODEX_LEAD_BIN to an existing codex binary "
                "(example DESKTOP-TBB531F codex-cli 0.155.0: "
                f"{EXAMPLE_WINDOWS_CODEX_BIN})."
            )
        return resolve_codex_bin(platform=self._platform)

    def decide(
        self,
        request: dict,
        *,
        schema: dict,
        cwd: str,
        timeout_sec: float = 180,
    ) -> tuple[str, dict | None]:
        try:
            bin_path = self._resolve_bin()
        except CodexBinNotFound as exc:
            return safe_failure("call_failed", redact_secrets(str(exc)))

        prompt = _compose_prompt(request if isinstance(request, dict) else {})
        pinned = pin_lead_response_schema(schema, request if isinstance(request, dict) else {})
        strict_schema = codex_output_schema(pinned)
        tmp = tempfile.mkdtemp(prefix=TEMP_PREFIX)
        try:
            return self._run_once(
                bin_path=bin_path,
                prompt=prompt,
                strict_schema=strict_schema,
                request=request if isinstance(request, dict) else {},
                cwd=cwd,
                tmp=tmp,
                timeout_sec=float(timeout_sec),
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _run_once(
        self,
        *,
        bin_path: str,
        prompt: str,
        strict_schema: dict,
        request: dict,
        cwd: str,
        tmp: str,
        timeout_sec: float,
    ) -> tuple[str, dict | None]:
        schema_path = os.path.join(tmp, "schema.json")
        last_path = os.path.join(tmp, "last.json")
        Path(schema_path).write_text(
            json.dumps(strict_schema, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        work = cwd if isinstance(cwd, str) and cwd and os.path.isdir(cwd) else tmp
        argv = build_command(
            bin_path,
            cwd=work,
            schema_path=schema_path,
            last_message_path=last_path,
        )
        # One shot. A forbidden flag is call_failed — never rewrite argv and retry.
        bad = find_forbidden_flag(argv)
        if bad:
            return safe_failure(
                "call_failed",
                redact_secrets(f"codex_cli refused forbidden flag: {bad}"),
            )
        plat = self._platform_name()
        try:
            args, _use_string = spawn_spec(argv, platform=plat)
        except CodexArgvRejected as exc:
            return safe_failure("call_failed", redact_secrets(str(exc)))

        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "encoding": "utf-8",
            "errors": "replace",
            "shell": False,
        }
        if str(plat).startswith("win"):
            popen_kwargs["creationflags"] = _CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(args, **popen_kwargs)
        except OSError as exc:
            return safe_failure(
                "call_failed",
                redact_secrets(f"codex_cli spawn failed: {exc}"),
            )

        scope = current_scope()
        if scope is not None:
            scope.attach(proc, plat)
        try:
            try:
                if scope is None:
                    stdout, stderr = proc.communicate(input=prompt, timeout=timeout_sec)
                else:
                    stdout, stderr = _cancellable_communicate(
                        proc, timeout=timeout_sec, input_text=prompt, platform=plat, scope=scope
                    )
            except LeadCancelled as exc:
                return safe_failure("call_failed", redact_secrets(f"codex_cli stopped: {exc}"))
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc, plat)
                _reap_after_kill(proc, plat)
                return safe_failure(
                    "timeout",
                    redact_secrets(
                        f"codex_cli timeout after {_format_seconds(timeout_sec)}s"
                    ),
                )
        except OSError as exc:
            _kill_process_tree(proc, plat)
            _reap_after_kill(proc, plat)
            return safe_failure(
                "call_failed",
                redact_secrets(f"codex_cli spawn failed: {exc}"),
            )
        finally:
            if scope is not None:
                scope.detach(proc)

        # Non-zero is never a decision, even if last.json / stdout is valid JSON.
        code = proc.returncode
        if code != 0:
            return _nonzero(stderr or "", int(code) if code is not None else 1)

        file_text = _read_last_message(last_path)
        if file_text is not None:
            text = file_text
        else:
            text = (stdout or "").strip()
        # stderr is progress logging only — intentionally ignored here.
        return _parse_decision_text(text, request)

    def doctor_hint(self) -> dict:
        bin_path: str | None = None
        bin_error: str | None = None
        try:
            bin_path = self._resolve_bin()
        except CodexBinNotFound as exc:
            bin_error = str(exc)
        return {
            "status": "wired_unverified",
            "name": self.name,
            "fake_pass": False,
            "bin_path": bin_path,
            "bin_error": bin_error,
            "sandbox": "read-only",
            "approval_policy": "never",
            "live_verified": False,
        }


__all__ = [
    "EXAMPLE_WINDOWS_CODEX_BIN",
    "FORBIDDEN_FLAGS",
    "TEMP_PREFIX",
    "CodexArgvRejected",
    "CodexBinNotFound",
    "CodexCliLeadAdapter",
    "build_command",
    "codex_output_schema",
    "find_forbidden_flag",
    "redact_secrets",
    "resolve_codex_bin",
    "spawn_spec",
]
