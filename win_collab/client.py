"""Local TeleAgent HTTP adapter. Credentials stay in memory, never in reports.

Windows discovery reads only the environment block of the verified SAC process,
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


def windows_environment(pid: int) -> dict:
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
        expected = Path.home() / '.local/share/TeleAgent/runtimes/super-agent-code/bin/TeleAgent.exe'
        if Path(image.value).resolve() != expected.resolve():
            raise ValueError('PID is not the installed TeleAgent SAC executable')

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
        for offset in range(0, 1024 * 1024, 2):
            block.extend(read(env + offset, 2))
            if len(block) >= 4 and block[-4:] == b'\0\0\0\0':
                break
        else:
            raise RuntimeError('TeleAgent environment exceeds discovery limit')
        selected = {}
        for item in block.decode('utf-16-le').split('\0'):
            key, sep, value = item.partition('=')
            if sep and key in KEYS:
                selected[key] = value
        if not all(selected.get(k) for k in KEYS):
            raise RuntimeError('Local API keys are unavailable in this TeleAgent process. Bootstrap or explicit local credentials required.')
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
    # List just the SAC image, never command lines (which may contain credentials).
    script = "Get-Process TeleAgent -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*runtimes*super-agent-code*bin*TeleAgent.exe' } | Select-Object -ExpandProperty Id"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, text=True, timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    pids = [int(x) for x in result.stdout.split() if x.isdigit()]
    if len(pids) != 1:
        raise RuntimeError(f'Expected one running TeleAgent SAC process, found {len(pids)}')
    pid = pids[0]
    creds = windows_environment(pid)
    result = subprocess.run(['netstat.exe', '-ano', '-p', 'tcp'], capture_output=True,
                            text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    ports = []
    for line in result.stdout.splitlines():
        row = line.split()
        if len(row) == 5 and row[0] == 'TCP' and row[-1] == str(pid) and row[-2] == 'LISTENING':
            port = int(row[1].rsplit(':', 1)[1])
            if 4390 <= port <= 4410:
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
