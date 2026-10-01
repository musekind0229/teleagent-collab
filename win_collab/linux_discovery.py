"""Linux TeleAgent discovery. Credentials stay in memory and never appear in errors.

The base URL is ``TELEAGENT_URL`` when set, otherwise ``http://127.0.0.1:4399``.
``Client`` still refuses anything that is not ``http://127.0.0.1:<port>``.

Credentials come from this process when all three ``win_collab.client.KEYS`` are
set. Otherwise ``/proc/<pid>/environ`` is read only for processes whose
``/proc/<pid>/exe`` resolves to a TeleAgent image:

* ``/opt/TeleAgent/teleagent``
* anything under ``~/.local/share/TeleAgent/runtimes/`` (and the same path for
  other local home directories, so a root service can see the GUI user's runtime)
* or ``TELEAGENT_LINUX_IMAGES`` (``os.pathsep``-separated), which replaces the
  defaults. A trailing separator or an existing directory is a prefix; anything
  else is an exact image path.

Every match must carry the same key set (``pick_unique_creds``). Error text
counts verified images, unreadable environ blocks, and missing keys. It never
includes values.
"""
from __future__ import annotations

import errno
import os
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from win_collab.client import ALIASES, KEYS, pick_unique_creds

_ENV_CAP = 1024 * 1024
_DEFAULT_BASE = 'http://127.0.0.1:4399'
_OPT_IMAGE = Path('/opt/TeleAgent/teleagent')
_RUNTIMES_TAIL = Path('.local/share/TeleAgent/runtimes')


def linux_base_url(environ: Mapping[str, str] | None = None) -> str:
    """Return a normalized loopback URL. The raw value is never echoed."""
    env = os.environ if environ is None else environ
    raw = str(env.get('TELEAGENT_URL') or '').strip() or _DEFAULT_BASE
    raw = raw.rstrip('/')
    parsed = urllib.parse.urlsplit(raw)
    host = (parsed.hostname or '').lower()
    if (
        parsed.scheme != 'http'
        or host != '127.0.0.1'
        or parsed.username
        or parsed.password
        or parsed.path not in ('', '/')
        or parsed.query
        or parsed.fragment
        or not parsed.port
    ):
        raise RuntimeError('TeleAgent URL must be http://127.0.0.1:<port>')
    return f'http://127.0.0.1:{parsed.port}'


def linux_image_specs(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
    include_system_homes: bool = True,
) -> list[tuple[str, Path]]:
    """``('exact', path)`` or ``('prefix', directory)`` image allow-list."""
    env = os.environ if environ is None else environ
    raw = str(env.get('TELEAGENT_LINUX_IMAGES') or '').strip()
    parts = [part.strip() for part in raw.split(os.pathsep) if part.strip()] if raw else []
    if parts:
        specs: list[tuple[str, Path]] = []
        for part in parts:
            path = Path(part)
            prefix = part.endswith(('/', '\\')) or (path.exists() and path.is_dir())
            specs.append(('prefix' if prefix else 'exact', path.resolve()))
        return specs
    specs = [('exact', _OPT_IMAGE.resolve())]
    homes: list[Path] = []
    primary = Path(home) if home is not None else Path.home()
    homes.append(primary)
    if include_system_homes:
        homes.append(Path('/root'))
        home_root = Path('/home')
        if home_root.is_dir():
            try:
                homes.extend(entry for entry in home_root.iterdir() if entry.is_dir())
            except OSError:
                pass
    seen: set[str] = set()
    for item in homes:
        prefix = (item / _RUNTIMES_TAIL).resolve()
        key = os.path.normcase(str(prefix))
        if key in seen:
            continue
        seen.add(key)
        specs.append(('prefix', prefix))
    return specs


def _image_allowed(image: Path, specs: list[tuple[str, Path]]) -> bool:
    try:
        resolved = image.resolve()
    except OSError:
        resolved = image
    text = os.path.normcase(os.path.abspath(str(resolved)))
    for kind, spec in specs:
        spec_text = os.path.normcase(os.path.abspath(str(spec)))
        if kind == 'exact' and text == spec_text:
            return True
        if kind == 'prefix':
            try:
                Path(text).relative_to(spec_text)
            except ValueError:
                continue
            return True
    return False


def linux_discovery_failure_message(
    *,
    verified: int,
    permission_denied: int,
    keys_missing: int,
    exe_permission_denied: int = 0,
    suspected_unreadable: int = 0,
) -> str:
    """Why discovery found no usable credential source. Counts only, never values."""
    counts = (
        f'verified={verified}, permission_denied={permission_denied}, '
        f'keys_missing={keys_missing}'
    )
    if exe_permission_denied:
        counts += f', exe_permission_denied={exe_permission_denied}'
    if verified <= 0 and suspected_unreadable:
        return (
            'TeleAgent processes found but environ not readable: '
            'run collab-service as the TeleAgent user or root '
            f'({counts}).'
        )
    if verified <= 0:
        return f'no TeleAgent process ({counts}).'
    if permission_denied and keys_missing:
        return (
            'TeleAgent processes found but environ not readable: '
            'run collab-service as the TeleAgent user or root; '
            f'keys not in environ (GUI not logged in?) ({counts}).'
        )
    if permission_denied:
        return (
            'TeleAgent processes found but environ not readable: '
            'run collab-service as the TeleAgent user or root '
            f'({counts}).'
        )
    if keys_missing:
        return f'keys not in environ (GUI not logged in?) ({counts}).'
    return f'Expected one verified TeleAgent credential source, found 0 ({counts}).'


def _each_pid(proc_root: Path):
    if not proc_root.is_dir():
        return
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.isdigit():
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                yield entry


def _default_resolve_exe(pid_dir: Path) -> Path:
    return (pid_dir / 'exe').resolve()


def _default_read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _comm_looks_like_teleagent(pid_dir: Path) -> bool:
    """``comm`` is the short process name, not the command line. Never a credential source."""
    try:
        text = (pid_dir / 'comm').read_text(encoding='utf-8', errors='replace').strip().lower()
    except OSError:
        return False
    return text == 'teleagent' or text.startswith('teleagent')


def _creds_from_block(data: bytes) -> dict | None:
    if len(data) > _ENV_CAP:
        return None
    found: dict[str, str] = {}
    for item in data.split(b'\0'):
        if b'=' not in item:
            continue
        key_b, value_b = item.split(b'=', 1)
        try:
            key = key_b.decode('utf-8')
            value = value_b.decode('utf-8')
        except UnicodeError:
            continue
        found[key] = value
    selected = {
        canonical: next((found.get(name, '') for name in names if found.get(name)), '')
        for canonical, names in ALIASES.items()
    }
    if not all(selected.get(key) for key in KEYS):
        return None
    return {key: selected[key] for key in KEYS}


@dataclass
class LinuxCredScan:
    """Discovery result. ``creds`` is omitted from ``repr`` and from ``public_dict``."""

    ok: bool = False
    reason: str = 'missing'
    detail: str = ''
    public_error: str = ''
    base_url: str = ''
    verified: int = 0
    permission_denied: int = 0
    keys_missing: int = 0
    exe_permission_denied: int = 0
    suspected_unreadable: int = 0
    explicit: bool = False
    creds: dict | None = field(default=None, repr=False)

    def public_dict(self) -> dict:
        detail = self.detail or self.public_error
        return {
            'ok': self.ok,
            'reason': self.reason,
            'source': self.reason,
            'detail': detail,
            'verified': self.verified,
            'permission_denied': self.permission_denied,
            'keys_missing': self.keys_missing,
        }


def linux_process_report(
    *,
    environ: Mapping[str, str] | None = None,
    proc_root: str | Path | None = None,
    image_specs: list[tuple[str, Path]] | None = None,
    home: Path | None = None,
    include_system_homes: bool = True,
    resolve_exe: Callable[[Path], Path] | None = None,
) -> dict:
    """Whether a TeleAgent image is running. Does not read environ values."""
    env = os.environ if environ is None else environ
    specs = image_specs if image_specs is not None else linux_image_specs(
        env, home=home, include_system_homes=include_system_homes,
    )
    resolver = resolve_exe or _default_resolve_exe
    root = Path(proc_root) if proc_root is not None else Path('/proc')
    verified = 0
    exe_permission_denied = 0
    suspected_unreadable = 0
    for pid_dir in _each_pid(root):
        try:
            image = resolver(pid_dir)
        except PermissionError:
            exe_permission_denied += 1
            if _comm_looks_like_teleagent(pid_dir):
                suspected_unreadable += 1
            continue
        except OSError as exc:
            if getattr(exc, 'errno', None) in (errno.EACCES, errno.EPERM):
                exe_permission_denied += 1
                if _comm_looks_like_teleagent(pid_dir):
                    suspected_unreadable += 1
            continue
        if _image_allowed(image, specs):
            verified += 1
    if verified:
        detail = f'{verified} TeleAgent process image(s)'
    elif suspected_unreadable:
        detail = (
            'TeleAgent processes found but environ not readable: '
            'run collab-service as the TeleAgent user or root'
        )
    else:
        detail = 'no TeleAgent process'
    return {
        'present': verified > 0,
        'verified': verified,
        'exe_permission_denied': exe_permission_denied,
        'suspected_unreadable': suspected_unreadable,
        'detail': detail,
    }


def scan_linux_credentials(
    *,
    environ: Mapping[str, str] | None = None,
    proc_root: str | Path | None = None,
    image_specs: list[tuple[str, Path]] | None = None,
    home: Path | None = None,
    include_system_homes: bool = True,
    read_bytes: Callable[[Path], bytes] | None = None,
    resolve_exe: Callable[[Path], Path] | None = None,
) -> LinuxCredScan:
    """Find one agreed local-API credential set. Public fields never hold values."""
    env = os.environ if environ is None else environ
    scan = LinuxCredScan()
    try:
        scan.base_url = linux_base_url(env)
    except RuntimeError as exc:
        scan.reason = 'bad_url'
        scan.public_error = str(exc)
        scan.detail = scan.public_error
        return scan
    explicit = {key: str(env.get(key) or '') for key in KEYS}
    if all(explicit.values()):
        scan.ok = True
        scan.explicit = True
        scan.reason = 'process_env'
        scan.creds = dict(explicit)
        scan.detail = 'local API keys are set in this process environment (values omitted)'
        return scan
    specs = image_specs if image_specs is not None else linux_image_specs(
        env, home=home, include_system_homes=include_system_homes,
    )
    reader = read_bytes or _default_read_bytes
    resolver = resolve_exe or _default_resolve_exe
    root = Path(proc_root) if proc_root is not None else Path('/proc')
    sources: list[tuple[int, dict]] = []
    for pid_dir in _each_pid(root):
        try:
            image = resolver(pid_dir)
        except PermissionError:
            scan.exe_permission_denied += 1
            if _comm_looks_like_teleagent(pid_dir):
                scan.suspected_unreadable += 1
            continue
        except OSError as exc:
            if getattr(exc, 'errno', None) in (errno.EACCES, errno.EPERM):
                scan.exe_permission_denied += 1
                if _comm_looks_like_teleagent(pid_dir):
                    scan.suspected_unreadable += 1
            continue
        if not _image_allowed(image, specs):
            continue
        scan.verified += 1
        env_path = pid_dir / 'environ'
        try:
            data = reader(env_path)
        except PermissionError:
            scan.permission_denied += 1
            continue
        except OSError as exc:
            if getattr(exc, 'errno', None) in (errno.EACCES, errno.EPERM):
                scan.permission_denied += 1
            continue
        parsed = _creds_from_block(data)
        if parsed is None:
            scan.keys_missing += 1
            continue
        sources.append((int(pid_dir.name), parsed))
    if not sources:
        scan.public_error = linux_discovery_failure_message(
            verified=scan.verified,
            permission_denied=scan.permission_denied,
            keys_missing=scan.keys_missing,
            exe_permission_denied=scan.exe_permission_denied,
            suspected_unreadable=scan.suspected_unreadable,
        )
        scan.detail = scan.public_error
        if scan.verified <= 0:
            scan.reason = 'not_readable' if scan.suspected_unreadable else 'no_process'
        elif scan.permission_denied and not scan.keys_missing:
            scan.reason = 'not_readable'
        elif scan.keys_missing and not scan.permission_denied:
            scan.reason = 'not_logged_in'
        elif scan.permission_denied:
            scan.reason = 'not_readable'
        else:
            scan.reason = 'missing'
        return scan
    try:
        creds = pick_unique_creds(sources)
    except RuntimeError:
        scan.reason = 'conflict'
        scan.public_error = (
            'conflicting TeleAgent credential sources '
            f'(verified={scan.verified}, sources={len(sources)}). '
            'Refuse to guess. Values are not shown.'
        )
        scan.detail = scan.public_error
        return scan
    scan.ok = True
    scan.reason = 'proc_environ'
    scan.creds = creds
    scan.detail = 'creds discoverable from TeleAgent process environ (values omitted)'
    return scan


def discover_linux(
    *,
    environ: Mapping[str, str] | None = None,
    proc_root: str | Path | None = None,
    image_specs: list[tuple[str, Path]] | None = None,
    home: Path | None = None,
    include_system_homes: bool = True,
    read_bytes: Callable[[Path], bytes] | None = None,
    resolve_exe: Callable[[Path], Path] | None = None,
) -> tuple[str, dict]:
    """``(base_url, creds)`` for ``Client``. Raises ``RuntimeError`` without values."""
    scan = scan_linux_credentials(
        environ=environ,
        proc_root=proc_root,
        image_specs=image_specs,
        home=home,
        include_system_homes=include_system_homes,
        read_bytes=read_bytes,
        resolve_exe=resolve_exe,
    )
    if not scan.ok or not scan.creds or not scan.base_url:
        raise RuntimeError(scan.public_error or scan.detail or 'TeleAgent credentials unavailable')
    return scan.base_url, scan.creds


__all__ = [
    'LinuxCredScan',
    'discover_linux',
    'linux_base_url',
    'linux_discovery_failure_message',
    'linux_image_specs',
    'linux_process_report',
    'scan_linux_credentials',
]
