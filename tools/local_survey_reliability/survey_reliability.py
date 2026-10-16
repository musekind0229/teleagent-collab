"""Small, local-only collector helpers. No account, DB, model or network API."""
from dataclasses import dataclass
from pathlib import Path
import ast
import json
import datetime
import os
import subprocess
import warnings

PROTECTED = {'.ssh', '.aws', '.azure', '.gnupg', 'secrets', 'credentials',
             'keyring', 'accounts', 'account-pool', 'auth', 'tokens',
             'browser', 'browsers', 'chrome', 'chromium', 'edge', 'firefox'}


def source_boundary(path, allowed_roots):
    """Check existing source, ancestors first; never resolve redirects.

    Reject lexical parent traversal BEFORE probing the filesystem. Missing
    sources fail closed instead of skipping the rest of the ancestor chain.
    Roots are supplied by a trusted local caller, not by source text.
    """
    p = Path(path)
    if not p.is_absolute() or '..' in p.parts or str(p).startswith(('\\\\', '//')):
        return False, 'nonlocal_or_parent_traversal'
    if any(x.lower() in PROTECTED or x.lower().startswith('.env') or
           any(s in x.lower() for s in ('credential', 'keyring', 'backup'))
           for x in p.parts):
        return False, 'protected_location'
    roots = [Path(r) for r in allowed_roots]
    if any(not r.is_absolute() or '..' in r.parts for r in roots):
        return False, 'invalid_caller_root'
    key = os.path.normcase(os.path.abspath(p))
    permitted = False
    for root in roots:
        base = os.path.normcase(os.path.abspath(root))
        try:
            permitted |= os.path.commonpath([key, base]) == base
        except ValueError:
            continue
    if not permitted:
        return False, 'outside_caller_roots'
    for q in reversed([p, *p.parents]):
        try:
            info = q.stat(follow_symlinks=False)
        except FileNotFoundError:
            return False, 'missing_source'
        except OSError:
            return False, 'metadata_unavailable'
        if getattr(info, 'st_file_attributes', 0) & 0x400 or (info.st_mode & 0o170000) == 0o120000:
            return False, 'redirect_rejected'
    return True, 'existing_local_source'


@dataclass
class ParseObservation:
    tree: object
    warning_count: int
    error_kind: str | None = None
    error_line: int | None = None


def inspect_python(text):
    """Never execute source. Return private-content-free diagnostic metadata."""
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter('always', SyntaxWarning)
        try:
            tree = ast.parse(text, filename='<local-source>')
        except (SyntaxError, ValueError, TypeError) as exc:
            return ParseObservation(None, len(observed), type(exc).__name__, getattr(exc, 'lineno', None))
    return ParseObservation(tree, len(observed))


def location_relevance(path):
    """A location is not an author/product certificate.

    Only recognizable runtime subtrees are typed components. Other system or
    installation locations remain relevance-unknown and available for review.
    """
    text = str(path).replace('\\', '/').lower()
    runtime = ('/node_modules/', '/site-packages/', '/dist-packages/',
               '/.vscode/extensions/', '/.vscode-insiders/extensions/',
               '/windows/system32/windowspowershell/v1.0/modules/',
               '/windows/winsxs/')
    if any(s in '/' + text.lstrip('/') for s in runtime):
        return 'runtime_component_location_not_authorship_proof'
    if text.startswith(('c:/windows/', 'c:/program files/', 'c:/program files (x86)/', 'c:/programdata/')):
        return 'system_or_installed_location_relevance_unknown'
    return 'ordinary_location_candidate_not_authorship_proof'


def publish_queue_pointer(queue_file, pointer_file):
    """Derive a locator/status snapshot, never confuse a queued stage with life.

    The queue remains authoritative. This is not an atomic transaction across
    files and does not establish a daemon, heartbeat, or automatic executor.
    """
    queue_file, pointer_file = Path(queue_file), Path(pointer_file)
    queue = json.loads(queue_file.read_bytes())
    pointer = {'run_directory': str(queue_file.parent),
               'authoritative_queue': str(queue_file),
               'owner': queue.get('owner'),
               'active_stage': queue.get('active_stage'),
               'running_native_host_claim': bool(queue.get('running', False)),
               'executor_snapshot': queue.get('executor'),
               'queue_last_update': queue.get('last_update'),
               'next_trigger': queue.get('next_trigger'),
               'pointer_updated_at': datetime.datetime.now().astimezone().isoformat(),
               'liveness_requires_pid_birth_check': True}
    temporary = pointer_file.with_name(pointer_file.name + '.new')
    temporary.write_text(json.dumps(pointer, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(pointer_file)
    return pointer


def capture_binary(argv, *, timeout=30):
    """Preserve non-UTF8 output without fragile subprocess reader decoding."""
    return subprocess.run(argv, capture_output=True, text=False, timeout=timeout, check=False)
