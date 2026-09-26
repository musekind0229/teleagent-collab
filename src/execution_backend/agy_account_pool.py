"""Peripheral agy account-pool scheduler (HOME isolation, serial).

Does not touch teleagent_adapter or glue. One spawn = one HOME; no mid-run
swap; no multi-account concurrency. Pool JSON must not contain tokens.
Windows pool spawns clear the machine-wide ``gemini:antigravity`` keyring
slot, then pin HOME and USERPROFILE to that account.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from execution_backend.agy_error_classify import (
    CLASS_AUTH_INVALID,
    CLASS_ELIGIBILITY_BLOCKED,
    CLASS_OK,
    CLASS_QUOTA_EXHAUSTED,
    CLASS_RATE_LIMIT,
    classify,
    classify_result,
)
from execution_backend.base import BackendError, BackendStatus

ENV_POOL = "COLLAB_AGY_ACCOUNT_POOL"
ENV_PRECHECK = "COLLAB_AGY_POOL_PRECHECK"
ENV_PROFILE = "AGY_PROFILE"
ENV_HTTP_PROXY = "COLLAB_AGY_HTTP_PROXY"
ENV_HTTPS_PROXY = "COLLAB_AGY_HTTPS_PROXY"
FORCE_FILE_STORAGE = "GEMINI_FORCE_FILE_STORAGE"
KEYRING_TARGET = "gemini:antigravity"

ACCOUNT_STATES = frozenset({"available", "exhausted", "cooldown", "unavailable"})
DEFAULT_COOLDOWN_SEC = 300.0

_KNOWN_ACCOUNT_KEYS = frozenset(
    {"id", "name", "home", "state", "email_mask", "notes", "cooldown_until"}
)
_SECRET_KEY_RE = re.compile(
    r"token|secret|password|oauth|refresh|credential|cookie|id_token",
    re.I,
)
_TRUTHY = frozenset({"1", "true", "yes", "on", "live"})

PrecheckFn = Callable[[Any], Any]


class AccountPoolError(BackendError):
    """No usable account, or pool JSON is invalid."""

    def __init__(self, message: str, *, status: BackendStatus | str = BackendStatus.UNAVAILABLE) -> None:
        super().__init__(status, message, capability="agy_account_pool")


@dataclass
class Account:
    id: str
    home: str
    state: str = "available"
    email_mask: str = ""
    notes: str = ""
    cooldown_until: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.id

    def to_public_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "home": self.home,
            "state": self.state,
        }
        if self.email_mask:
            d["email_mask"] = self.email_mask
        if self.notes:
            d["notes"] = self.notes
        if self.cooldown_until:
            d["cooldown_until"] = self.cooldown_until
        for k, v in self.extra.items():
            if k in d or k in _KNOWN_ACCOUNT_KEYS:
                continue
            if _is_secret_key(k):
                continue
            d[k] = v
        return d


@dataclass
class AccountPool:
    accounts: list[Account]
    path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def by_id(self, ident: str) -> Account:
        for a in self.accounts:
            if a.id == ident:
                return a
        raise AccountPoolError(f"unknown account {ident!r}", status=BackendStatus.FAILED)

    def available(self) -> list[Account]:
        return [a for a in self.accounts if a.state == "available"]

    def to_json_obj(self) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for k, v in self.extra.items():
            if _is_secret_key(k):
                continue
            obj[k] = v
        obj["accounts"] = [a.to_public_dict() for a in self.accounts]
        return obj


def _is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(str(key or "")))


def _strip_secret_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_secret_keys(v) for k, v in obj.items() if not _is_secret_key(k)}
    if isinstance(obj, list):
        return [_strip_secret_keys(x) for x in obj]
    return obj


def _parse_cooldown_until(raw: str | None) -> float | None:
    if raw is None or str(raw).strip() == "":
        return None
    s = str(raw).strip()
    try:
        return float(s)
    except (TypeError, ValueError):
        pass
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def expire_cooldowns(pool: AccountPool, *, now: float | None = None) -> bool:
    """Promote cooldown accounts whose cooldown_until has passed back to available."""
    t = time.time() if now is None else float(now)
    changed = False
    for acc in pool.accounts:
        if acc.state != "cooldown":
            continue
        until = _parse_cooldown_until(acc.cooldown_until)
        if until is None or t >= until:
            acc.state = "available"
            acc.cooldown_until = None
            changed = True
    return changed


def _account_from_obj(raw: Mapping[str, Any]) -> Account:
    ident = str(raw.get("id") or raw.get("name") or "").strip()
    home = str(raw.get("home") or "").strip()
    state = str(raw.get("state") or "available").strip() or "available"
    if not ident:
        raise AccountPoolError("account missing id/name", status=BackendStatus.FAILED)
    if not home:
        raise AccountPoolError(f"account {ident!r} missing home", status=BackendStatus.FAILED)
    if not Path(home).is_absolute():
        raise AccountPoolError(
            f"account {ident!r} home must be an absolute path",
            status=BackendStatus.FAILED,
        )
    if state not in ACCOUNT_STATES:
        raise AccountPoolError(
            f"account {ident!r} state must be one of {sorted(ACCOUNT_STATES)}",
            status=BackendStatus.FAILED,
        )
    extra = {
        k: v
        for k, v in raw.items()
        if k not in _KNOWN_ACCOUNT_KEYS and not _is_secret_key(k)
    }
    until = raw.get("cooldown_until")
    return Account(
        id=ident,
        home=home,
        state=state,
        email_mask=str(raw.get("email_mask") or ""),
        notes=str(raw.get("notes") or ""),
        cooldown_until=None if until in (None, "") else str(until),
        extra=extra,
    )


def load_pool(path: str | Path) -> AccountPool:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise AccountPoolError(f"cannot read account pool {p}: {e}", status=BackendStatus.FAILED) from e
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise AccountPoolError(f"account pool {p} is not JSON: {e}", status=BackendStatus.FAILED) from e
    if not isinstance(raw, dict):
        raise AccountPoolError(f"account pool {p} must be a JSON object", status=BackendStatus.FAILED)
    raw = _strip_secret_keys(raw)
    rows = raw.get("accounts")
    if not isinstance(rows, list) or not rows:
        raise AccountPoolError(f"account pool {p} has no accounts[]", status=BackendStatus.FAILED)
    accounts = [_account_from_obj(a) for a in rows if isinstance(a, dict)]
    if not accounts:
        raise AccountPoolError(f"account pool {p} has no valid accounts", status=BackendStatus.FAILED)
    extra = {k: v for k, v in raw.items() if k != "accounts" and not _is_secret_key(k)}
    pool = AccountPool(accounts=accounts, path=p, extra=extra)
    expire_cooldowns(pool)
    return pool


def save_pool(path: str | Path, pool: AccountPool) -> None:
    """Atomic replace (tmp + os.replace). Never writes token/oauth fields."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = _strip_secret_keys(pool.to_json_obj())
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".agy-pool-", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    pool.path = p


def apply_class_to_state(
    account: Account,
    err_class: str,
    *,
    now: float | None = None,
    cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
) -> bool:
    """Map classifier output onto pool state. Returns True if state changed.

    eligibility_blocked / auth_invalid -> unavailable (do not dispatch)
    quota_exhausted -> exhausted (NOT eligibility)
    rate_limit -> cooldown
    ok / ordinary_task_failure -> no account-state change
    """
    mapping = {
        CLASS_ELIGIBILITY_BLOCKED: "unavailable",
        CLASS_AUTH_INVALID: "unavailable",
        CLASS_QUOTA_EXHAUSTED: "exhausted",
        CLASS_RATE_LIMIT: "cooldown",
    }
    new_state = mapping.get(str(err_class or "").strip())
    if not new_state:
        return False
    changed = account.state != new_state
    account.state = new_state
    if new_state == "cooldown":
        t = time.time() if now is None else float(now)
        until = datetime.fromtimestamp(t + float(cooldown_sec), tz=timezone.utc)
        account.cooldown_until = until.isoformat()
        changed = True
    elif new_state != "cooldown":
        if account.cooldown_until:
            account.cooldown_until = None
            changed = True
    return changed


def mark_account(
    pool: AccountPool,
    account_id: str,
    state: str,
    *,
    persist: bool = True,
    cooldown_until: str | None = None,
) -> Account:
    if state not in ACCOUNT_STATES:
        raise AccountPoolError(
            f"state must be one of {sorted(ACCOUNT_STATES)}",
            status=BackendStatus.FAILED,
        )
    acc = pool.by_id(account_id)
    acc.state = state
    if cooldown_until is not None:
        acc.cooldown_until = cooldown_until
    elif state != "cooldown":
        acc.cooldown_until = None
    if persist and pool.path is not None:
        save_pool(pool.path, pool)
    return acc


def _normalize_precheck(result: Any) -> str:
    if result is None:
        return CLASS_OK
    if isinstance(result, str):
        return result.strip() or CLASS_OK
    if isinstance(result, dict):
        for k in ("class", "err_class"):
            val = result.get(k)
            if val:
                return str(val).strip()
        return classify(
            str(result.get("stdout") or ""),
            str(result.get("stderr") or ""),
            result.get("rc"),
        )
    return str(result)


def select_account(
    pool: AccountPool,
    *,
    precheck: PrecheckFn | None = None,
    persist: bool = True,
) -> Account | None:
    """Pick the first *available* account. Optional precheck runs per candidate.

    Precheck classes:
      eligibility_blocked / auth_invalid -> unavailable, skip, try next
      quota_exhausted -> exhausted, try next
      rate_limit -> cooldown, try next
      ok -> selected
    Non-matching classes skip this candidate without changing state.

    Does not swap HOME mid-run; the caller binds one account per spawn.
    """
    expire_cooldowns(pool)
    mutated = False
    for acc in pool.accounts:
        if acc.state != "available":
            continue
        if precheck is None:
            return acc
        cls = _normalize_precheck(precheck(acc))
        if apply_class_to_state(acc, cls):
            mutated = True
        if cls == CLASS_OK:
            if persist and mutated and pool.path is not None:
                save_pool(pool.path, pool)
            return acc
    if persist and mutated and pool.path is not None:
        save_pool(pool.path, pool)
    return None


def _is_windows() -> bool:
    return os.name == "nt" or str(sys.platform).startswith("win")


def _proxy_text(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def _proxy_from(src: Mapping[str, Any] | None, key: str) -> str:
    if not src:
        return ""
    return _proxy_text(src.get(key))


def _configured_proxies(
    account: Account,
    env: Mapping[str, str],
    pool: AccountPool | None,
) -> tuple[str, str]:
    """Account field, then pool-top, then COLLAB_AGY_*. HTTP-only mirrors to HTTPS."""
    acct = account.extra or {}
    top = pool.extra if pool is not None else None
    http = (
        _proxy_from(acct, "http_proxy")
        or _proxy_from(top, "http_proxy")
        or _proxy_text(env.get(ENV_HTTP_PROXY))
    )
    https = (
        _proxy_from(acct, "https_proxy")
        or _proxy_from(top, "https_proxy")
        or _proxy_text(env.get(ENV_HTTPS_PROXY))
    )
    if http and not https:
        https = http
    return http, https


def account_environ(
    account: Account,
    base: Mapping[str, str] | None = None,
    *,
    pool: AccountPool | None = None,
) -> dict[str, str]:
    """Environ for one spawn: HOME + file-storage + AGY_PROFILE.

    On Windows, USERPROFILE is the same path as HOME. HTTP_PROXY / HTTPS_PROXY
    are set only when the account, the pool, or COLLAB_AGY_* configures them.
    Preserves AGY_BIN / AGY_MODEL / AGY_AUTO_APPROVE from *base*.
    Does not set AGY_AUTO_APPROVE (skip-permissions stays off unless already on).
    """
    env = dict(os.environ if base is None else base)
    home = str(account.home)
    env["HOME"] = home
    if _is_windows():
        env["USERPROFILE"] = home
    env[FORCE_FILE_STORAGE] = "true"
    env[ENV_PROFILE] = str(account.id)
    http, https = _configured_proxies(account, env, pool)
    if http:
        env["HTTP_PROXY"] = http
    if https:
        env["HTTPS_PROXY"] = https
    return env


def clear_windows_antigravity_keyring() -> None:
    """Delete cmdkey target ``gemini:antigravity``. Non-Windows returns immediately.

    A missing slot (non-zero exit, not found, or cmdkey itself missing) is non-fatal.
    """
    if not _is_windows():
        return
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "check": False,
        "timeout": 15,
    }
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if flags:
        kwargs["creationflags"] = flags
    try:
        subprocess.run(["cmdkey", f"/delete:{KEYRING_TARGET}"], **kwargs)
    except (OSError, subprocess.SubprocessError):
        return


def live_agy_precheck(
    account: Account,
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float = 20.0,
    pool: AccountPool | None = None,
) -> str:
    """Optional live ``agy models`` under the account HOME. Never skip-permissions.

    Default tests must inject a mock instead of calling this.
    """
    env = account_environ(account, environ, pool=pool)
    bin_path = str(env.get("AGY_BIN") or "").strip() or shutil.which("agy") or "/home/box/.local/bin/agy"
    cmd = [bin_path, "models"]
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=float(timeout),
            check=False,
        )
        return classify(proc.stdout or "", proc.stderr or "", proc.returncode)
    except Exception as e:  # noqa: BLE001 — precheck must not raise into dispatch
        return classify("", str(e), 1)


def resolve_pool_path(
    *,
    explicit: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    env = environ if environ is not None else os.environ
    raw = str(env.get(ENV_POOL) or "").strip()
    return raw or None


def _precheck_from_env(
    explicit: PrecheckFn | None,
    environ: Mapping[str, str] | None,
    pool: AccountPool | None = None,
) -> PrecheckFn | None:
    if explicit is not None:
        return explicit
    env = environ if environ is not None else os.environ
    raw = str(env.get(ENV_PRECHECK) or "").strip().lower()
    if raw in _TRUTHY:
        return lambda acc: live_agy_precheck(acc, environ=env, pool=pool)
    return None


def prepare_antigravity_environ_from_pool(
    pool_path: str | Path,
    *,
    base_environ: Mapping[str, str] | None = None,
    precheck: PrecheckFn | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Load pool, select one available account, return environ bound to that HOME.

    Raises AccountPoolError if nothing is dispatchable.
    """
    pool = load_pool(pool_path)
    chk = _precheck_from_env(precheck, base_environ, pool)
    acc = select_account(pool, precheck=chk, persist=persist)
    if acc is None:
        states = {a.id: a.state for a in pool.accounts}
        raise AccountPoolError(f"no available agy account in pool; states={states}")
    clear_windows_antigravity_keyring()
    env = account_environ(acc, base_environ, pool=pool)
    env[ENV_POOL] = str(Path(pool_path))
    return {"environ": env, "account": acc, "pool": pool, "agy_profile": acc.id}


def inject_agy_pool_into_backend_kwargs(kwargs: Mapping[str, Any] | None) -> dict[str, Any]:
    """Pop pool kwargs and, if a pool is configured, inject HOME/AGY_PROFILE environ.

    Unknown keys must not be forwarded to AntigravityCliExecutionBackend.
    """
    kw = dict(kwargs or {})
    pool_path = kw.pop("account_pool_path", None) or kw.pop("agy_account_pool", None)
    precheck = kw.pop("precheck", None)
    persist = kw.pop("persist_pool", True)
    environ = kw.get("environ")
    env_map: Mapping[str, str] | None = dict(environ) if environ is not None else None
    path = resolve_pool_path(explicit=pool_path, environ=env_map)
    if not path:
        return kw
    prepared = prepare_antigravity_environ_from_pool(
        path,
        base_environ=env_map,
        precheck=precheck,
        persist=bool(persist),
    )
    kw["environ"] = prepared["environ"]
    return kw


def apply_job_result_to_pool(
    pool: AccountPool,
    account: Account,
    result: Mapping[str, Any] | None,
    *,
    persist: bool = True,
) -> str:
    """After a finished spawn, classify the result and update pool state.

    ordinary_task_failure / ok do not mark the account bad. Never swaps HOME.
    """
    cls = classify_result(dict(result) if result is not None else None)
    if apply_class_to_state(account, cls) and persist and pool.path is not None:
        save_pool(pool.path, pool)
    return cls


def selected_profile_from_environ(environ: Mapping[str, str] | None) -> str:
    if not environ:
        return ""
    return str(environ.get(ENV_PROFILE) or "").strip()


__all__ = [
    "ACCOUNT_STATES",
    "Account",
    "AccountPool",
    "AccountPoolError",
    "DEFAULT_COOLDOWN_SEC",
    "ENV_HTTP_PROXY",
    "ENV_HTTPS_PROXY",
    "ENV_POOL",
    "ENV_PRECHECK",
    "ENV_PROFILE",
    "FORCE_FILE_STORAGE",
    "KEYRING_TARGET",
    "account_environ",
    "apply_class_to_state",
    "clear_windows_antigravity_keyring",
    "apply_job_result_to_pool",
    "expire_cooldowns",
    "inject_agy_pool_into_backend_kwargs",
    "live_agy_precheck",
    "load_pool",
    "mark_account",
    "prepare_antigravity_environ_from_pool",
    "resolve_pool_path",
    "save_pool",
    "select_account",
    "selected_profile_from_environ",
]
