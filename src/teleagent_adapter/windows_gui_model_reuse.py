"""Reuse logged-in GUI TeleAgent model auth for Windows stdin_wrap.

Reads the GUI Chromium Local Storage token and app-auth device-meta, then
builds stdin payload keys the Go kernel expects:

- ``SUPER_AGENT_AUTH_STATE`` — JSON string of encryptAuthState
  ``{token, deviceId, installId}`` (AES-256-GCM, GUI-compatible)
- ``OPENCODE_CONFIG_CONTENT`` — JSON string with NewApi provider
- ``OPENCODE_CONFIG_DIR`` / ``TELEAGENT_CONFIG_DIR`` — GUI config path only
  (non-secret) when ``%USERPROFILE%\\.config\\TeleAgent`` exists

Never logs token or ciphertext. Never Credential Manager, never SeDebug,
never PEB scrape, never OAuth token.json. Fail-closed: caller must not spawn
a wrap that pretends model auth works.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from teleagent_adapter.windows_process_environ import (
    CREDS_BLOCKER_GUI_MODEL_AUTH_MISSING,
)

AUTH_STATE_ENV = "SUPER_AGENT_AUTH_STATE"
OPENCODE_CONFIG_CONTENT_ENV = "OPENCODE_CONFIG_CONTENT"
OPENCODE_CONFIG_DIR_ENV = "OPENCODE_CONFIG_DIR"
TELEAGENT_CONFIG_DIR_ENV = "TELEAGENT_CONFIG_DIR"
REUSE_GUI_MODEL_ENV = "TELEAGENT_WIN_REUSE_GUI_MODEL"
GUI_LEVELDB_DIR_ENV = "TELEAGENT_GUI_LEVELDB_DIR"
GUI_DEVICE_META_ENV = "TELEAGENT_GUI_DEVICE_META"

LOCAL_STORAGE_KEY = b"opencowork-auth"
NEWAPI_BASE_URL = "https://agent.teleai.com.cn/superCowork/sapi/api/v1"
NEWAPI_SMALL_MODEL = "NewApi/chat-lite"
_MAX_JSON_BYTES = 1 << 20
_BRACE_SCAN_WINDOW = 64

_RandBytes = Callable[[int], bytes]


class GuiModelAuthError(RuntimeError):
    """Fail-closed GUI model-auth error. ``reason`` is a short code, never a secret."""

    def __init__(self, reason: str, message: str, *, found_broken: bool = True) -> None:
        self.reason = reason
        self.found_broken = found_broken
        self.blocker = CREDS_BLOCKER_GUI_MODEL_AUTH_MISSING
        super().__init__(message)


@dataclass
class GuiAuthMaterial:
    """In-memory GUI auth fields. Secret fields are redacted in repr."""

    token: str
    device_id: str
    install_id: str

    def __repr__(self) -> str:
        return "GuiAuthMaterial(token=***, device_id=***, install_id=***)"


def reuse_gui_model_mode(environ: Mapping[str, str] | None = None) -> str:
    """Return ``off`` / ``auto`` / ``on``. Default ``auto``."""
    env = environ if environ is not None else os.environ
    raw = (env.get(REUSE_GUI_MODEL_ENV) or "auto").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return "off"
    if raw in ("1", "true", "yes", "on"):
        return "on"
    return "auto"


def default_leveldb_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = (env.get(GUI_LEVELDB_DIR_ENV) or "").strip()
    if override:
        return Path(override)
    user = (env.get("USERPROFILE") or env.get("HOME") or "").strip()
    if not user:
        user = str(Path.home())
    return Path(user) / ".local" / "share" / "TeleAgent" / "Local Storage" / "leveldb"


def default_device_meta_path(environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = (env.get(GUI_DEVICE_META_ENV) or "").strip()
    if override:
        return Path(override)
    appdata = (env.get("APPDATA") or "").strip()
    if appdata:
        return Path(appdata) / "TeleAgent" / "app-auth" / "device-meta.json"
    user = (env.get("USERPROFILE") or env.get("HOME") or "").strip()
    if not user:
        user = str(Path.home())
    return Path(user) / "AppData" / "Roaming" / "TeleAgent" / "app-auth" / "device-meta.json"


def default_teleagent_config_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    user = (env.get("USERPROFILE") or env.get("HOME") or "").strip()
    if not user:
        user = str(Path.home())
    return Path(user) / ".config" / "TeleAgent"


def gui_auth_sources_present(environ: Mapping[str, str] | None = None) -> bool:
    """True when both GUI auth sources exist (dir with ldb/log + device-meta file)."""
    leveldb = default_leveldb_dir(environ)
    meta = default_device_meta_path(environ)
    try:
        dir_ok = leveldb.is_dir() and any(
            p.is_file() and p.suffix.lower() in {".ldb", ".log"} for p in leveldb.iterdir()
        )
    except OSError:
        dir_ok = False
    try:
        meta_ok = meta.is_file()
    except OSError:
        meta_ok = False
    return bool(dir_ok and meta_ok)


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def encrypt_auth_state(
    state: Mapping[str, str],
    session_key: str,
    *,
    randbytes: _RandBytes | None = None,
) -> dict[str, str]:
    """Match GUI ``encryptAuthState``: AES-256-GCM, key=SHA256(utf8 sessionKey).

    Output ``{version: v1, iv, tag, ciphertext}`` each base64url without ``=``.
    Never logs plaintext or ciphertext.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:
        raise GuiModelAuthError(
            "encrypt_unavailable",
            "GUI model auth encrypt unavailable",
            found_broken=True,
        ) from e

    token = state.get("token") if isinstance(state, Mapping) else None
    device_id = state.get("deviceId") if isinstance(state, Mapping) else None
    install_id = state.get("installId") if isinstance(state, Mapping) else None
    if not (isinstance(token, str) and token and isinstance(device_id, str) and device_id
            and isinstance(install_id, str) and install_id):
        raise GuiModelAuthError(
            "auth_state_incomplete",
            "GUI model auth state incomplete",
            found_broken=True,
        )

    key = hashlib.sha256(session_key.encode("utf-8")).digest()
    rng = randbytes or os.urandom
    iv = rng(12)
    if not isinstance(iv, (bytes, bytearray)) or len(iv) != 12:
        raise GuiModelAuthError(
            "encrypt_failed",
            "GUI model auth encrypt failed",
            found_broken=True,
        )
    plaintext = json.dumps(
        {"token": token, "deviceId": device_id, "installId": install_id},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    packed = AESGCM(key).encrypt(bytes(iv), plaintext, None)
    if len(packed) < 16:
        raise GuiModelAuthError(
            "encrypt_failed",
            "GUI model auth encrypt failed",
            found_broken=True,
        )
    ciphertext, tag = packed[:-16], packed[-16:]
    return {
        "version": "v1",
        "iv": _b64url_nopad(bytes(iv)),
        "tag": _b64url_nopad(tag),
        "ciphertext": _b64url_nopad(ciphertext),
    }


def build_opencode_config_content() -> str:
    """Minimal OpenCode config JSON: NewApi provider + small_model. No secrets."""
    cfg = {
        "agent": {},
        "provider": {
            "NewApi": {
                "options": {
                    "baseURL": NEWAPI_BASE_URL,
                }
            }
        },
        "enabled_providers": ["NewApi"],
        "mcp": {},
        "small_model": NEWAPI_SMALL_MODEL,
    }
    return json.dumps(cfg, separators=(",", ":"), ensure_ascii=False)


def _brace_match_utf8(data: bytes, start: int) -> bytes | None:
    if start < 0 or start >= len(data) or data[start] != 0x7B:
        return None
    depth = 0
    in_str = False
    esc = False
    end_limit = min(len(data), start + _MAX_JSON_BYTES)
    for i in range(start, end_limit):
        c = data[i]
        if in_str:
            if esc:
                esc = False
            elif c == 0x5C:
                esc = True
            elif c == 0x22:
                in_str = False
            continue
        if c == 0x22:
            in_str = True
            continue
        if c == 0x7B:
            depth += 1
        elif c == 0x7D:
            depth -= 1
            if depth == 0:
                return data[start : i + 1]
    return None


def _brace_match_utf16le(data: bytes, start: int) -> bytes | None:
    if start < 0 or start + 1 >= len(data) or data[start] != 0x7B or data[start + 1] != 0:
        return None
    depth = 0
    in_str = False
    esc = False
    end_limit = min(len(data), start + _MAX_JSON_BYTES)
    i = start
    while i + 1 < end_limit:
        c = data[i]
        if data[i + 1] != 0:
            i += 2
            continue
        if in_str:
            if esc:
                esc = False
            elif c == 0x5C:
                esc = True
            elif c == 0x22:
                in_str = False
            i += 2
            continue
        if c == 0x22:
            in_str = True
        elif c == 0x7B:
            depth += 1
        elif c == 0x7D:
            depth -= 1
            if depth == 0:
                return data[start : i + 2]
        i += 2
    return None


def _token_from_parsed(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    state = obj.get("state")
    if isinstance(state, dict):
        tok = state.get("token")
        if isinstance(tok, str) and tok.strip():
            return tok.strip()
    tok = obj.get("token")
    if isinstance(tok, str) and tok.strip():
        return tok.strip()
    return None


def _token_from_json_bytes(raw: bytes, encoding: str) -> str | None:
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return _token_from_parsed(obj)


def _extract_token_from_blob(data: bytes) -> str | None:
    """Find ``opencowork-auth`` and brace-match JSON. Prefer last valid hit."""
    found: str | None = None
    needles = (LOCAL_STORAGE_KEY, "opencowork-auth".encode("utf-16-le"))
    for needle in needles:
        start = 0
        while True:
            idx = data.find(needle, start)
            if idx < 0:
                break
            after = idx + len(needle)
            window_end = min(len(data), after + _BRACE_SCAN_WINDOW)
            utf8_off = data.find(b"{", after, window_end)
            if utf8_off >= 0:
                blob = _brace_match_utf8(data, utf8_off)
                if blob:
                    tok = _token_from_json_bytes(blob, "utf-8")
                    if tok:
                        found = tok
            i = after
            utf16_off = -1
            while i + 1 < window_end:
                if data[i] == 0x7B and data[i + 1] == 0:
                    utf16_off = i
                    break
                i += 1
            if utf16_off >= 0:
                blob16 = _brace_match_utf16le(data, utf16_off)
                if blob16:
                    tok = _token_from_json_bytes(blob16, "utf-16-le")
                    if tok:
                        found = tok
            start = idx + 1
    return found


def _iter_leveldb_files(leveldb_dir: Path) -> list[Path]:
    try:
        files = [
            p
            for p in leveldb_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".ldb", ".log"}
        ]
    except OSError as e:
        raise GuiModelAuthError(
            "leveldb_unreadable",
            "GUI Local Storage leveldb unreadable",
            found_broken=True,
        ) from e
    files.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    return files


def load_gui_token_from_leveldb(
    leveldb_dir: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Read ``state.token`` from Chromium Local Storage ``*.ldb`` (newest mtime)."""
    path = leveldb_dir if leveldb_dir is not None else default_leveldb_dir(environ)
    if not path.is_dir():
        raise GuiModelAuthError(
            "leveldb_missing",
            "GUI Local Storage leveldb directory missing",
            found_broken=False,
        )
    files = _iter_leveldb_files(path)
    if not files:
        raise GuiModelAuthError(
            "leveldb_missing",
            "GUI Local Storage leveldb has no .ldb/.log files",
            found_broken=False,
        )
    last_err_broken = False
    for file_path in files:
        try:
            data = file_path.read_bytes()
        except OSError:
            last_err_broken = True
            continue
        tok = _extract_token_from_blob(data)
        if tok:
            return tok
    raise GuiModelAuthError(
        "token_missing",
        "GUI Local Storage token not found",
        found_broken=last_err_broken or bool(files),
    )


def load_device_meta(
    meta_path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Return ``(deviceId, installId)`` from GUI device-meta.json."""
    path = meta_path if meta_path is not None else default_device_meta_path(environ)
    if not path.is_file():
        raise GuiModelAuthError(
            "device_meta_missing",
            "GUI device-meta.json missing",
            found_broken=False,
        )
    try:
        raw = path.read_text(encoding="utf-8")
        obj = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise GuiModelAuthError(
            "device_meta_unreadable",
            "GUI device-meta.json unreadable",
            found_broken=True,
        ) from e
    if not isinstance(obj, dict):
        raise GuiModelAuthError(
            "device_meta_unreadable",
            "GUI device-meta.json unreadable",
            found_broken=True,
        )
    device_id = obj.get("deviceId")
    install_id = obj.get("installId")
    if not (isinstance(device_id, str) and device_id.strip()
            and isinstance(install_id, str) and install_id.strip()):
        raise GuiModelAuthError(
            "device_meta_incomplete",
            "GUI device-meta.json missing deviceId/installId",
            found_broken=True,
        )
    return device_id.strip(), install_id.strip()


def load_gui_auth_material(*, environ: Mapping[str, str] | None = None) -> GuiAuthMaterial:
    token = load_gui_token_from_leveldb(environ=environ)
    device_id, install_id = load_device_meta(environ=environ)
    return GuiAuthMaterial(token=token, device_id=device_id, install_id=install_id)


def build_gui_model_reuse_env(
    session_key: str,
    *,
    environ: Mapping[str, str] | None = None,
    randbytes: _RandBytes | None = None,
    material: GuiAuthMaterial | None = None,
) -> dict[str, str]:
    """Build stdin payload extras for GUI model reuse. Never logs secrets."""
    if not (isinstance(session_key, str) and session_key):
        raise GuiModelAuthError(
            "session_key_missing",
            "GUI model auth session key missing",
            found_broken=True,
        )
    auth = material if material is not None else load_gui_auth_material(environ=environ)
    enc = encrypt_auth_state(
        {"token": auth.token, "deviceId": auth.device_id, "installId": auth.install_id},
        session_key,
        randbytes=randbytes,
    )
    out: dict[str, str] = {
        AUTH_STATE_ENV: json.dumps(enc, separators=(",", ":"), ensure_ascii=False),
        OPENCODE_CONFIG_CONTENT_ENV: build_opencode_config_content(),
    }
    cfg = default_teleagent_config_dir(environ)
    try:
        if cfg.is_dir():
            out[OPENCODE_CONFIG_DIR_ENV] = str(cfg)
            out[TELEAGENT_CONFIG_DIR_ENV] = str(cfg)
    except OSError:
        pass
    return out


def prepare_gui_model_reuse(
    session_key: str,
    *,
    environ: Mapping[str, str] | None = None,
    randbytes: _RandBytes | None = None,
) -> dict[str, str] | None:
    """Return payload extras, ``None`` to skip, or raise on fail-closed.

    ``TELEAGENT_WIN_REUSE_GUI_MODEL``: ``0`` skip, ``1`` required, ``auto``
    (default) reuse when auth files are present and fail-closed if they are
    present but broken.
    """
    mode = reuse_gui_model_mode(environ)
    if mode == "off":
        return None
    present = gui_auth_sources_present(environ)
    if mode == "auto" and not present:
        return None
    try:
        return build_gui_model_reuse_env(
            session_key, environ=environ, randbytes=randbytes
        )
    except GuiModelAuthError:
        raise
    except Exception as e:
        raise GuiModelAuthError(
            "reuse_failed",
            "GUI model auth reuse failed",
            found_broken=True,
        ) from e


__all__ = [
    "AUTH_STATE_ENV",
    "GUI_DEVICE_META_ENV",
    "GUI_LEVELDB_DIR_ENV",
    "GuiAuthMaterial",
    "GuiModelAuthError",
    "NEWAPI_BASE_URL",
    "NEWAPI_SMALL_MODEL",
    "OPENCODE_CONFIG_CONTENT_ENV",
    "OPENCODE_CONFIG_DIR_ENV",
    "REUSE_GUI_MODEL_ENV",
    "TELEAGENT_CONFIG_DIR_ENV",
    "build_gui_model_reuse_env",
    "build_opencode_config_content",
    "default_device_meta_path",
    "default_leveldb_dir",
    "default_teleagent_config_dir",
    "encrypt_auth_state",
    "gui_auth_sources_present",
    "load_device_meta",
    "load_gui_auth_material",
    "load_gui_token_from_leveldb",
    "prepare_gui_model_reuse",
    "reuse_gui_model_mode",
]
