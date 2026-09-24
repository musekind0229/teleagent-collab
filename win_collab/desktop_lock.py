"""Machine-local exclusive occupancy for one discovered desktop GUI.

Two controllers (Goal API state vs win_collab state) must not both dispatch
onto the same TeleAgent base URL. The OS file lock is the authority; holder
JSON is diagnostic. Same-controller reclaim is allowed. A live foreign hold
is never unlocked, aborted, or stolen.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

DESKTOP_SESSION_BUSY = 'desktop_session_busy'

_MU = threading.Lock()
_HELD: dict[str, '_Hold'] = {}


class _Hold:
    def __init__(self, controller_id: str, pid: int, state_dir: str, lock) -> None:
        self.controller_id = controller_id
        self.pid = pid
        self.state_dir = state_dir
        self.lock = lock


def controller_id_for(state_dir: str | Path) -> str:
    key = os.path.normcase(str(Path(state_dir).resolve()))
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]


def lock_root() -> Path:
    override = os.environ.get('TELEAGENT_DESKTOP_LOCK_DIR', '').strip()
    if override:
        return Path(override)
    local = os.environ.get('LOCALAPPDATA', '').strip()
    if local:
        return Path(local) / 'teleagent-collab' / 'desktop-locks'
    return Path.home() / '.local' / 'share' / 'teleagent-collab' / 'desktop-locks'


def _base_key(base: str) -> str:
    text = str(base or '').strip().rstrip('/').lower()
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:32]


def _paths(key: str) -> tuple[Path, Path]:
    root = lock_root()
    return root / (key + '.lock'), root / (key + '.holder.json')


def _lock_backend():
    try:
        from platform_services import get_file_lock
        from platform_services.file_lock import FileLockUnsupported
    except ImportError:
        import sys
        src = str(Path(__file__).resolve().parents[1] / 'src')
        if src not in sys.path:
            sys.path.insert(0, src)
        from platform_services import get_file_lock
        from platform_services.file_lock import FileLockUnsupported
    return get_file_lock(), FileLockUnsupported


def _write_holder(key: str, hold: _Hold, base: str) -> None:
    _lock_path, holder_path = _paths(key)
    holder_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'controller_id': hold.controller_id,
        'pid': hold.pid,
        'state_dir': hold.state_dir,
        'base_url': str(base).strip().rstrip('/'),
    }
    temp = holder_path.with_suffix('.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, holder_path)


def read_holder(base: str) -> dict | None:
    _lock_path, holder_path = _paths(_base_key(base))
    if not holder_path.is_file():
        return None
    try:
        data = json.loads(holder_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def claim_desktop(base: str, state_dir: str | Path) -> bool:
    """Return True if this controller may dispatch to ``base``.

    False means another live controller holds the GUI. Does not unlock a
    foreign hold and does not talk to TeleAgent.
    """
    if not str(base or '').strip():
        return False
    key = _base_key(base)
    cid = controller_id_for(state_dir)
    state = str(Path(state_dir).resolve())
    with _MU:
        hold = _HELD.get(key)
        if hold is not None and not Path(hold.state_dir).is_dir():
            # In-process holder whose state dir is gone (abandoned). Keep the
            # OS lock; do not open a second one (Windows LockFileEx is per process).
            hold.controller_id = cid
            hold.pid = os.getpid()
            hold.state_dir = state
            try:
                _write_holder(key, hold, base)
            except OSError:
                pass
            return True
        if hold is not None and hold.controller_id == cid:
            return True
        if hold is not None:
            return False
        lock_path, _holder_path = _paths(key)
        backend, unsupported = _lock_backend()
        try:
            held = backend.acquire(lock_path, blocking=False)
        except BlockingIOError:
            return False
        except (OSError, unsupported):
            return False
        hold = _Hold(cid, os.getpid(), state, held)
        _HELD[key] = hold
        try:
            _write_holder(key, hold, base)
        except OSError:
            pass
        return True


def foreign_desktop_busy(statuses, owned_session_ids) -> bool:
    """True when a session this controller does not own is not idle.

    Unknown shapes fail closed. Owned sessions (idle or busy) do not block.
    """
    if not isinstance(statuses, dict):
        return True
    owned = {str(sid) for sid in owned_session_ids if sid}
    for sid, info in statuses.items():
        if str(sid) in owned:
            continue
        if not isinstance(info, dict) or info.get('type') != 'idle':
            return True
    return False


def release_desktop_if_idle(base: str, state_dir: str | Path, jobs, terminal) -> None:
    """Drop our hold only when every job in this state dir is terminal."""
    if not str(base or '').strip():
        return
    if any(str(job.get('state') or '') not in terminal for job in jobs):
        return
    key = _base_key(base)
    cid = controller_id_for(state_dir)
    with _MU:
        hold = _HELD.get(key)
        if hold is None or hold.controller_id != cid:
            return
        _HELD.pop(key, None)
        lock = hold.lock
    try:
        lock.unlock_and_close()
    except OSError:
        pass


def reset_desktop_locks_for_tests() -> None:
    with _MU:
        holds = list(_HELD.values())
        _HELD.clear()
    for hold in holds:
        try:
            hold.lock.unlock_and_close()
        except OSError:
            pass
