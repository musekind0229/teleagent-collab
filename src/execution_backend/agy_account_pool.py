"""Peripheral agy account-pool scheduler (HOME isolation, serial).

Does not touch teleagent_adapter or glue. One spawn = one HOME; no mid-run
swap; no multi-account concurrency. Pool JSON must not contain tokens.
Windows pool spawns clear the machine-wide ``gemini:antigravity`` keyring
slot, then pin HOME and USERPROFILE to that account. Child environ also
sets SSH_* TEST-NET pseudos so agy 1.2.11 uses file token storage.
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
from datetime import datetime, timedelta, timezone
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
ENV_COOLDOWN_SEC = "COLLAB_AGY_COOLDOWN_SEC"
ENV_COOLDOWN_MODE = "COLLAB_AGY_COOLDOWN_MODE"
FORCE_FILE_STORAGE = "GEMINI_FORCE_FILE_STORAGE"
# Community SSH→file-creds trigger (agy detects SSH session). TEST-NET only.
SSH_CONNECTION_PSEUDO = "203.0.113.1 50000 203.0.113.2 22"
SSH_CLIENT_PSEUDO = "203.0.113.1 50000 22"
SSH_TTY_PSEUDO = "windows-agy-pool"  # Windows pool marker; broker uses /dev/pts/0 on Linux
KEYRING_TARGET = "gemini:antigravity"

ACCOUNT_STATES = frozenset({"available", "busy", "exhausted", "cooldown", "unavailable"})
DEFAULT_COOLDOWN_SEC = 300.0
COOLDOWN_MODE_DURATION = "duration"
COOLDOWN_MODE_DAY_BOUNDARY = "day_boundary"
_COOLDOWN_MODES = frozenset({COOLDOWN_MODE_DURATION, COOLDOWN_MODE_DAY_BOUNDARY})

_KNOWN_ACCOUNT_KEYS = frozenset(
    {"id", "name", "home", "state", "email_mask", "notes", "cooldown_until", "lease_pid", "lease_until"}
)
_SECRET_KEY_RE = re.compile(
    r"token|secret|password|oauth|refresh|credential|cookie|id_token",
    re.I,
)
_TRUTHY = frozenset({"1", "true", "yes", "on", "live"})

PrecheckFn = Callable[[Any], Any]

# Cross-process / cross-entrance home leases held until release_account_lease.
# Keyed by absolute HOME path. Kept here so inject_agy_pool_into_backend_kwargs
# can drop the return dict without releasing the lock early.
_HELD_HOME_LEASES: dict[str, Any] = {}


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
    lease_pid: int | None = None
    lease_until: str | None = None
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
        if self.lease_pid is not None:
            d["lease_pid"] = self.lease_pid
        if self.lease_until:
            d["lease_until"] = self.lease_until
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
    lease_until = raw.get("lease_until")
    lease_pid_raw = raw.get("lease_pid")
    lease_pid: int | None
    try:
        lease_pid = None if lease_pid_raw in (None, "") else int(lease_pid_raw)
    except (TypeError, ValueError):
        lease_pid = None
    return Account(
        id=ident,
        home=home,
        state=state,
        email_mask=str(raw.get("email_mask") or ""),
        notes=str(raw.get("notes") or ""),
        cooldown_until=None if until in (None, "") else str(until),
        lease_pid=lease_pid,
        lease_until=None if lease_until in (None, "") else str(lease_until),
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


def _coerce_cooldown_sec(raw: Any, default: float) -> float:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    if val < 0:
        return default
    return val


def _coerce_cooldown_mode(raw: Any, default: str = COOLDOWN_MODE_DURATION) -> str:
    text = str(raw or "").strip().lower()
    if text in _COOLDOWN_MODES:
        return text
    return default


def resolve_cooldown_settings(
    pool: AccountPool | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cooldown_sec: float | None = None,
    cooldown_mode: str | None = None,
) -> tuple[float, str]:
    """Resolve ``(cooldown_sec, cooldown_mode)``.

    Precedence: explicit args, then ``COLLAB_AGY_COOLDOWN_SEC`` /
    ``COLLAB_AGY_COOLDOWN_MODE``, then ``pool.extra``, then
    ``DEFAULT_COOLDOWN_SEC`` / ``duration``.
    """
    sec = DEFAULT_COOLDOWN_SEC
    mode = COOLDOWN_MODE_DURATION
    extra = pool.extra if pool is not None else None
    if isinstance(extra, Mapping):
        if "cooldown_sec" in extra:
            sec = _coerce_cooldown_sec(extra.get("cooldown_sec"), sec)
        if extra.get("cooldown_mode") not in (None, ""):
            mode = _coerce_cooldown_mode(extra.get("cooldown_mode"), mode)
    env = os.environ if environ is None else environ
    if str(env.get(ENV_COOLDOWN_SEC) or "").strip():
        sec = _coerce_cooldown_sec(env.get(ENV_COOLDOWN_SEC), sec)
    if str(env.get(ENV_COOLDOWN_MODE) or "").strip():
        mode = _coerce_cooldown_mode(env.get(ENV_COOLDOWN_MODE), mode)
    if cooldown_sec is not None:
        sec = _coerce_cooldown_sec(cooldown_sec, sec)
    if cooldown_mode is not None and str(cooldown_mode).strip():
        mode = _coerce_cooldown_mode(cooldown_mode, mode)
    return sec, mode


def cooldown_until_iso(
    now: float,
    *,
    cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
    cooldown_mode: str = COOLDOWN_MODE_DURATION,
) -> str:
    """UTC ISO instant when a cooldown ends.

    ``duration``: ``now + cooldown_sec``.
    ``day_boundary``: the next calendar midnight in the machine local
    timezone (on this Windows deploy, China Standard Time / Asia/Shanghai).
    The value is stored in UTC so ``expire_cooldowns`` can compare epochs.
    """
    mode = _coerce_cooldown_mode(cooldown_mode)
    if mode == COOLDOWN_MODE_DAY_BOUNDARY:
        local = datetime.fromtimestamp(float(now)).astimezone()
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        nxt = midnight + timedelta(days=1)
        return nxt.astimezone(timezone.utc).isoformat()
    until = datetime.fromtimestamp(float(now) + float(cooldown_sec), tz=timezone.utc)
    return until.isoformat()


def apply_class_to_state(
    account: Account,
    err_class: str,
    *,
    now: float | None = None,
    cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
    cooldown_mode: str = COOLDOWN_MODE_DURATION,
) -> bool:
    """Map classifier output onto pool state. Returns True if state changed.

    eligibility_blocked / auth_invalid -> unavailable (do not dispatch)
    quota_exhausted -> cooldown (set cooldown_until; NOT eligibility, NOT exhausted)
    rate_limit -> cooldown
    ok / ordinary_task_failure -> no account-state change

    ``exhausted`` stays in ACCOUNT_STATES for manual marks. The classifier
    path for quota does not use it, so expiry can restore the account.
    """
    mapping = {
        CLASS_ELIGIBILITY_BLOCKED: "unavailable",
        CLASS_AUTH_INVALID: "unavailable",
        CLASS_QUOTA_EXHAUSTED: "cooldown",
        CLASS_RATE_LIMIT: "cooldown",
    }
    new_state = mapping.get(str(err_class or "").strip())
    if not new_state:
        return False
    changed = account.state != new_state
    account.state = new_state
    if new_state == "cooldown":
        t = time.time() if now is None else float(now)
        account.cooldown_until = cooldown_until_iso(
            t,
            cooldown_sec=float(cooldown_sec),
            cooldown_mode=cooldown_mode,
        )
        changed = True
    elif account.cooldown_until:
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



DEFAULT_BUSY_LEASE_SEC = 3600.0
ENV_BUSY_LEASE_SEC = "COLLAB_AGY_BUSY_LEASE_SEC"
ENV_LOCK_DIR = "COLLAB_AGY_LOCK_DIR"


def _iso_now(ts: float | None = None) -> str:
    t = time.time() if ts is None else float(ts)
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        s = str(raw).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def _pid_alive(pid: int | None) -> bool:
    """Best-effort liveness. On Windows, os.kill(pid, 0) is unreliable — callers
    must also honor lease_until. Unknown/unsupported → assume alive.
    """
    if pid is None or int(pid) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (OSError, PermissionError, SystemError, ValueError):
        # PermissionError often means the process exists but we cannot signal it.
        return True


def busy_lease_sec(environ: Mapping[str, str] | None = None) -> float:
    env = environ if environ is not None else os.environ
    raw = str(env.get(ENV_BUSY_LEASE_SEC) or "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_BUSY_LEASE_SEC


def pool_global_lock_path(pool_path: str | Path, *, environ: Mapping[str, str] | None = None) -> Path:
    """One machine-wide lock file for agy account selection (serial / mutex)."""
    env = environ if environ is not None else os.environ
    override = str(env.get(ENV_LOCK_DIR) or "").strip()
    if override:
        root = Path(override)
    else:
        root = Path(tempfile.gettempdir()) / "collab-agy-locks"
    root.mkdir(parents=True, exist_ok=True)
    key = str(Path(pool_path).resolve()).replace(":", "_").replace("\\", "_").replace("/", "_")
    if len(key) > 120:
        import hashlib

        key = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return root / f"pool-{key}.lock"


def home_lease_lock_path(home: str | Path, *, environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get(ENV_LOCK_DIR) or "").strip()
    root = Path(override) if override else (Path(tempfile.gettempdir()) / "collab-agy-locks")
    root.mkdir(parents=True, exist_ok=True)
    key = str(Path(home).resolve()).replace(":", "_").replace("\\", "_").replace("/", "_")
    if len(key) > 120:
        import hashlib

        key = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return root / f"home-{key}.lock"


def expire_busy_leases(pool: AccountPool, *, now: float | None = None) -> bool:
    """Promote stale busy accounts (expired lease_until, or dead pid with no until)."""
    t = time.time() if now is None else float(now)
    changed = False
    for acc in pool.accounts:
        if acc.state != "busy":
            continue
        until = _parse_iso(acc.lease_until)
        if until is not None:
            stale = t >= until
        else:
            stale = not _pid_alive(acc.lease_pid)
        if stale:
            acc.state = "available"
            acc.lease_pid = None
            acc.lease_until = None
            _drop_held_home_lease(acc.home)
            changed = True
    return changed


def _home_key(home: str | Path) -> str:
    try:
        return str(Path(home).resolve())
    except OSError:
        return str(home)


def _retain_home_lease(home: str | Path, held: Any) -> None:
    if held is None:
        return
    key = _home_key(home)
    prev = _HELD_HOME_LEASES.pop(key, None)
    if prev is not None and prev is not held:
        try:
            prev.unlock_and_close()
        except Exception:
            pass
    _HELD_HOME_LEASES[key] = held


def _drop_held_home_lease(home: str | Path) -> None:
    held = _HELD_HOME_LEASES.pop(_home_key(home), None)
    if held is None:
        return
    try:
        held.unlock_and_close()
    except Exception:
        pass


def reserve_account(
    account: Account,
    *,
    now: float | None = None,
    lease_sec: float | None = None,
    pid: int | None = None,
) -> None:
    t = time.time() if now is None else float(now)
    sec = DEFAULT_BUSY_LEASE_SEC if lease_sec is None else float(lease_sec)
    account.state = "busy"
    account.lease_pid = int(os.getpid() if pid is None else pid)
    account.lease_until = _iso_now(t + max(1.0, sec))


def release_account_lease(
    pool: AccountPool,
    account: Account | str,
    *,
    persist: bool = True,
    force: bool = False,
) -> Account:
    """Clear busy lease → available (unless already cooldown/exhausted/unavailable)."""
    acc = pool.by_id(account) if isinstance(account, str) else account
    _drop_held_home_lease(acc.home)
    if acc.state == "busy" or force:
        if acc.state in ("cooldown", "exhausted", "unavailable") and not force:
            acc.lease_pid = None
            acc.lease_until = None
        else:
            acc.state = "available"
            acc.lease_pid = None
            acc.lease_until = None
    else:
        acc.lease_pid = None
        acc.lease_until = None
    if persist and pool.path is not None:
        save_pool(pool.path, pool)
    return acc


def _acquire_pool_lock(pool_path: str | Path, *, environ: Mapping[str, str] | None = None, blocking: bool = True):
    from platform_services import get_file_lock

    lock_path = pool_global_lock_path(pool_path, environ=environ)
    return get_file_lock().acquire(lock_path, blocking=blocking)


def select_account(
    pool: AccountPool,
    *,
    precheck: PrecheckFn | None = None,
    persist: bool = True,
    now: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> Account | None:
    """Pick the first *available* account. Optional precheck runs per candidate.

    Precheck classes:
      eligibility_blocked / auth_invalid -> unavailable, skip, try next
      quota_exhausted -> cooldown, try next
      rate_limit -> cooldown, try next
      ok -> selected
    Non-matching classes skip this candidate without changing state.
    Cooldown length comes from pool.extra / COLLAB_AGY_COOLDOWN_* (see
    resolve_cooldown_settings).

    Does not swap HOME mid-run; the caller binds one account per spawn.
    """
    expired = expire_cooldowns(pool, now=now)
    expired_busy = expire_busy_leases(pool, now=now)
    expired = expired or expired_busy
    sec, mode = resolve_cooldown_settings(pool, environ=environ)
    mutated = False
    chosen: Account | None = None
    for acc in pool.accounts:
        if acc.state != "available":
            continue
        if precheck is None:
            chosen = acc
            break
        cls = _normalize_precheck(precheck(acc))
        if apply_class_to_state(
            acc,
            cls,
            now=now,
            cooldown_sec=sec,
            cooldown_mode=mode,
        ):
            mutated = True
        if cls == CLASS_OK:
            chosen = acc
            break
    if persist and (mutated or expired) and pool.path is not None:
        save_pool(pool.path, pool)
    return chosen


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

    On Windows, USERPROFILE is the same path as HOME. SSH_CONNECTION /
    SSH_CLIENT / SSH_TTY are fixed TEST-NET pseudo values (child environ
    only) so agy 1.2.11 uses file token storage; it ignores
    GEMINI_FORCE_FILE_STORAGE, which is still set for forward compatibility.
    HTTP_PROXY / HTTPS_PROXY are set only when the account, the pool, or
    COLLAB_AGY_* configures them.
    Preserves AGY_BIN / AGY_MODEL / AGY_AUTO_APPROVE from *base*.
    Does not set AGY_AUTO_APPROVE (skip-permissions stays off unless already on).
    """
    env = dict(os.environ if base is None else base)
    home = str(account.home)
    env["HOME"] = home
    if _is_windows():
        env["USERPROFILE"] = home
    env[FORCE_FILE_STORAGE] = "true"
    env["SSH_CONNECTION"] = SSH_CONNECTION_PSEUDO
    env["SSH_CLIENT"] = SSH_CLIENT_PSEUDO
    env["SSH_TTY"] = SSH_TTY_PSEUDO
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
    """Load pool, select one available account under a cross-process mutex.

    Acquires a pool-global exclusive lock, picks an available account, marks it
    ``busy`` with a lease (pid + lease_until), optionally holds a per-HOME lock,
    then releases the global lock. Two entrances cannot reserve the same HOME.
    Caller should ``release_account_lease`` when the spawn finishes (also done
    from apply_job_result_to_pool / run_antigravity_charter).

    Raises AccountPoolError if nothing is dispatchable.
    """
    held_global = None
    held_home = None
    try:
        held_global = _acquire_pool_lock(pool_path, environ=base_environ, blocking=True)
    except Exception as e:  # noqa: BLE001
        raise AccountPoolError(
            f"cannot acquire agy pool lock: {type(e).__name__}: {e}",
            status=BackendStatus.UNAVAILABLE,
        ) from e
    try:
        pool = load_pool(pool_path)
        chk = _precheck_from_env(precheck, base_environ, pool)
        acc = select_account(
            pool,
            precheck=chk,
            persist=False,
            environ=base_environ,
        )
        if acc is None:
            states = {a.id: a.state for a in pool.accounts}
            raise AccountPoolError(f"no available agy account in pool; states={states}")
        # Minimal per-HOME lease: non-blocking exclusive lock on a lockfile.
        try:
            from platform_services import get_file_lock

            home_lock = home_lease_lock_path(acc.home, environ=base_environ)
            try:
                held_home = get_file_lock().acquire(home_lock, blocking=False)
            except BlockingIOError as e:
                raise AccountPoolError(
                    f"agy home already leased: account={acc.id!r} home={acc.home!r}",
                    status=BackendStatus.UNAVAILABLE,
                ) from e
        except AccountPoolError:
            raise
        except Exception:
            held_home = None  # JSON busy mark still serializes; home lock is best-effort
        reserve_account(
            acc,
            lease_sec=busy_lease_sec(base_environ),
        )
        if persist and pool.path is not None:
            save_pool(pool.path, pool)
        # Retain the per-HOME lock only when the busy mark is persisted; otherwise
        # tests that share fixed absolute homes would leak locks across cases.
        if persist:
            _retain_home_lease(acc.home, held_home)
        elif held_home is not None:
            try:
                held_home.unlock_and_close()
            except Exception:
                pass
            held_home = None
        clear_windows_antigravity_keyring()
        env = account_environ(acc, base_environ, pool=pool)
        env[ENV_POOL] = str(Path(pool_path))
        return {
            "environ": env,
            "account": acc,
            "pool": pool,
            "agy_profile": acc.id,
            "home_lease": held_home,
            "pool_lock_path": str(pool_global_lock_path(pool_path, environ=base_environ)),
        }
    finally:
        if held_global is not None:
            try:
                held_global.unlock_and_close()
            except Exception:
                pass


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
    now: float | None = None,
    environ: Mapping[str, str] | None = None,
    cooldown_sec: float | None = None,
    cooldown_mode: str | None = None,
) -> str:
    """After a finished spawn, classify the result and update pool state.

    ordinary_task_failure / ok do not mark the account bad. Quota and
    rate-limit land on cooldown (pool.extra / COLLAB_AGY_COOLDOWN_*).
    Never swaps HOME. ``account`` must be the object inside ``pool``.
    """
    sec, mode = resolve_cooldown_settings(
        pool,
        environ=environ,
        cooldown_sec=cooldown_sec,
        cooldown_mode=cooldown_mode,
    )
    cls = classify_result(dict(result) if result is not None else None)
    changed = apply_class_to_state(
        account,
        cls,
        now=now,
        cooldown_sec=sec,
        cooldown_mode=mode,
    )
    # Drop busy lease after the spawn finishes (unless classify moved it).
    _drop_held_home_lease(account.home)
    if account.state == "busy":
        account.state = "available"
        account.lease_pid = None
        account.lease_until = None
        changed = True
    elif account.lease_pid is not None or account.lease_until:
        account.lease_pid = None
        account.lease_until = None
        changed = True
    if changed and persist and pool.path is not None:
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
    "COOLDOWN_MODE_DAY_BOUNDARY",
    "COOLDOWN_MODE_DURATION",
    "DEFAULT_COOLDOWN_SEC",
    "ENV_COOLDOWN_MODE",
    "ENV_COOLDOWN_SEC",
    "ENV_HTTP_PROXY",
    "ENV_HTTPS_PROXY",
    "ENV_POOL",
    "ENV_PRECHECK",
    "ENV_PROFILE",
    "FORCE_FILE_STORAGE",
    "KEYRING_TARGET",
    "SSH_CLIENT_PSEUDO",
    "SSH_CONNECTION_PSEUDO",
    "SSH_TTY_PSEUDO",
    "account_environ",
    "apply_class_to_state",
    "clear_windows_antigravity_keyring",
    "apply_job_result_to_pool",
    "cooldown_until_iso",
    "expire_cooldowns",
    "inject_agy_pool_into_backend_kwargs",
    "live_agy_precheck",
    "load_pool",
    "mark_account",
    "prepare_antigravity_environ_from_pool",
    "busy_lease_sec",
    "home_lease_lock_path",
    "pool_global_lock_path",
    "expire_busy_leases",
    "release_account_lease",
    "reserve_account",
    "resolve_cooldown_settings",
    "resolve_pool_path",
    "save_pool",
    "select_account",
    "selected_profile_from_environ",
]
