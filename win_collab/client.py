"""Local TeleAgent HTTP adapter. Credentials stay in memory, never in reports.

Windows discovery reads only the environment block of verified TeleAgent runtime processes,
using normal OS read permissions. It does not elevate or dump process memory.
The three local API values can alternatively be supplied by environment variables.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

KEYS = ('OPENCODE_SERVER_USERNAME', 'OPENCODE_SERVER_PASSWORD', 'SUPER_AGENT_LOCAL_SESSION_KEY')
ALIASES = {
    'OPENCODE_SERVER_USERNAME': ('OPENCODE_SERVER_USERNAME', 'SUPER_AGENT_OPENCODE_USERNAME'),
    'OPENCODE_SERVER_PASSWORD': ('OPENCODE_SERVER_PASSWORD', 'SUPER_AGENT_OPENCODE_PASSWORD'),
    'SUPER_AGENT_LOCAL_SESSION_KEY': ('SUPER_AGENT_LOCAL_SESSION_KEY',),
}


def windows_process_image(pid: int, *, expected_images: tuple[Path, ...]) -> Path:
    """Resolve and verify a PID without reading its command line or environment."""
    if os.name != 'nt':
        raise RuntimeError('Windows required for TeleAgent process discovery')
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    # PROCESS_QUERY_LIMITED_INFORMATION is sufficient and avoids requesting VM_READ.
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise PermissionError(f'Cannot inspect TeleAgent process {pid}: WinError {ctypes.get_last_error()}')
    try:
        size = w.DWORD(32768)
        image = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
            raise PermissionError('Cannot verify TeleAgent process image')
        resolved = Path(image.value).resolve()
        if resolved not in {path.resolve() for path in expected_images}:
            raise ValueError('PID is not a verified installed TeleAgent runtime executable')
        return resolved
    finally:
        kernel.CloseHandle(handle)


def windows_environment(pid: int, *, expected_images: tuple[Path, ...] | None = None) -> dict:
    if os.name != 'nt' or struct.calcsize('P') != 8:
        raise RuntimeError('Windows x64 Python required for process environment discovery')
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    nt = ctypes.WinDLL('ntdll')
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.ReadProcessMemory.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    nt.NtQueryInformationProcess.argtypes = [w.HANDLE, w.ULONG, ctypes.c_void_p, w.ULONG, ctypes.c_void_p]
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        raise PermissionError(f'Cannot read TeleAgent process {pid}: WinError {ctypes.get_last_error()}')
    try:
        size = w.DWORD(32768)
        image = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
            raise PermissionError('Cannot verify TeleAgent process image')
        runtime = Path.home() / '.local/share/TeleAgent/runtimes'
        expected_images = expected_images or (
            runtime / 'super-agent-code/bin/TeleAgent.exe',
            runtime / 'node/node.exe',
        )
        if Path(image.value).resolve() not in {p.resolve() for p in expected_images}:
            raise ValueError('PID is not a verified installed TeleAgent runtime executable')

        def read(address, length):
            buf = ctypes.create_string_buffer(length)
            received = ctypes.c_size_t()
            if not kernel.ReadProcessMemory(handle, address, buf, length, ctypes.byref(received)):
                raise PermissionError(f'Cannot read TeleAgent environment: WinError {ctypes.get_last_error()}')
            return buf.raw[:received.value]

        pbi = ctypes.create_string_buffer(48)
        if nt.NtQueryInformationProcess(handle, 0, pbi, 48, None) != 0:
            raise RuntimeError('Cannot query TeleAgent PEB')
        peb = struct.unpack_from('<Q', pbi.raw, 8)[0]
        params = struct.unpack('<Q', read(peb + 0x20, 8))[0]
        env = struct.unpack('<Q', read(params + 0x80, 8))[0]
        # Environment block only; bounded to 1 MiB, never scan the process heap.
        block = bytearray()
        for offset in range(0, 1024 * 1024, 4096):
            chunk = read(env + offset, min(4096, 1024 * 1024 - offset))
            block.extend(chunk)
            scan_from = max(0, len(block) - len(chunk) - 4)
            if scan_from % 2:
                scan_from += 1
            end = next((i for i in range(scan_from, len(block) - 3, 2)
                        if block[i:i + 4] == b'\0\0\0\0'), -1)
            if end >= 0:
                block = block[:end + 4]
                break
        else:
            raise RuntimeError('TeleAgent environment exceeds discovery limit')
        raw_selected = {}
        for item in block.decode('utf-16-le').split('\0'):
            key, sep, value = item.partition('=')
            if sep and any(key in names for names in ALIASES.values()):
                raw_selected[key] = value
        selected = {
            canonical: next((raw_selected.get(name, '') for name in names if raw_selected.get(name)), '')
            for canonical, names in ALIASES.items()
        }
        if not all(selected.get(k) for k in KEYS):
            raise RuntimeError('Local API keys are unavailable in this verified TeleAgent runtime process.')
        return selected
    finally:
        kernel.CloseHandle(handle)


def discover() -> tuple[str, dict]:
    explicit = {k: os.environ.get(k, '') for k in KEYS}
    if all(explicit.values()):
        return os.environ.get('TELEAGENT_URL', 'http://127.0.0.1:4397'), explicit
    protected = Path(os.environ.get('COLLAB_AUTH_FILE', str(Path(__file__).resolve().parents[1] / '.collab-state/local-api.dpapi')))
    if protected.is_file():
        from .protected_auth import load
        return load(protected)
    if os.name != 'nt':
        raise RuntimeError('Supply TELEAGENT_URL and local API environment keys on non-Windows hosts')
    runtime = Path.home() / '.local/share/TeleAgent/runtimes'
    sac_image = runtime / 'super-agent-code/bin/TeleAgent.exe'
    node_image = runtime / 'node/node.exe'
    # List only exact installed runtime images; never command lines (which may contain credentials).
    script = (
        "$sac=[IO.Path]::GetFullPath('" + str(sac_image).replace("'", "''") + "');"
        "$node=[IO.Path]::GetFullPath('" + str(node_image).replace("'", "''") + "');"
        "Get-Process TeleAgent,node -ErrorAction SilentlyContinue | Where-Object {"
        "$_.Path -and ([IO.Path]::GetFullPath($_.Path) -ieq $sac -or "
        "[IO.Path]::GetFullPath($_.Path) -ieq $node)} | Select-Object -ExpandProperty Id"
    )
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    pids = [int(x) for x in result.stdout.split() if x.isdigit()]
    credential_sources = []
    for candidate in pids:
        try:
            credential_sources.append((candidate, windows_environment(
                candidate, expected_images=(sac_image, node_image))))
        except (PermissionError, RuntimeError, ValueError):
            continue
    if len(credential_sources) != 1:
        raise RuntimeError(
            f'Expected one verified TeleAgent credential source, found {len(credential_sources)}')
    _, creds = credential_sources[0]
    # Find the listener first, then require its process image to be the exact SAC executable.
    result = subprocess.run(['netstat.exe', '-ano', '-p', 'tcp'], capture_output=True,
                            text=True, encoding='mbcs', errors='replace', timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    ports = []
    for line in result.stdout.splitlines():
        row = line.split()
        if len(row) == 5 and row[0] == 'TCP' and row[-2] == 'LISTENING':
            try:
                port = int(row[1].rsplit(':', 1)[1])
                listener_pid = int(row[-1])
            except ValueError:
                continue
            if 4390 <= port <= 4410:
                try:
                    windows_process_image(listener_pid, expected_images=(sac_image,))
                except (PermissionError, RuntimeError, ValueError):
                    continue
                ports.append(port)
    if len(set(ports)) != 1:
        raise RuntimeError('Cannot identify unique TeleAgent API listener in 4390..4410')
    return os.environ.get('TELEAGENT_URL', f'http://127.0.0.1:{ports[0]}'), creds


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise RuntimeError('Local API redirects are forbidden')


class Client:
    def __init__(self, base=None, creds=None):
        if base is None or creds is None:
            base, creds = discover()
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path or parsed.username or parsed.query or parsed.fragment or not parsed.port:
            raise ValueError('TeleAgent URL must be http://127.0.0.1:<port>')
        self.base, self.creds = base.rstrip('/'), creds
        self.instance_id = hashlib.sha256((self.base + '\n' + self.creds[KEYS[2]]).encode()).hexdigest()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, method, path, body=None, workspace=None):
        if not path.startswith('/') or '\n' in path or '\r' in path:
            raise ValueError('Invalid API path')
        ts, nonce = str(int(time.time() * 1000)), secrets.token_hex(12)
        payload = '\n'.join(['local-v1', method.upper(), path, ts, nonce])
        sig = base64.urlsafe_b64encode(hmac.new(self.creds[KEYS[2]].encode(), payload.encode(), hashlib.sha256).digest()).decode().rstrip('=')
        basic = base64.b64encode(f'{self.creds[KEYS[0]]}:{self.creds[KEYS[1]]}'.encode()).decode()
        headers = {'Authorization': 'Basic ' + basic, 'X-SA-Sign-Version': 'local-v1',
                   'X-SA-Timestamp': ts, 'X-SA-Nonce': nonce, 'X-SA-Signature': sig,
                   'Content-Type': 'application/json', 'Accept': 'application/json'}
        if workspace:
            headers['x-opencode-directory'] = str(workspace)
        request = urllib.request.Request(self.base + path, method=method, headers=headers,
                  data=None if body is None else json.dumps(body, ensure_ascii=True).encode())
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise RuntimeError('API response exceeds size limit')
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            # Do not include response bodies/headers; upstream may return credentials.
            code = error.code
            error.close()
            raise RuntimeError(f'TeleAgent {method} {path.split("?")[0]} returned HTTP {code}') from None
