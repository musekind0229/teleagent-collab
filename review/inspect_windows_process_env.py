"""Report only selected TeleAgent environment variable names and value lengths.

This diagnostic never prints values and only accepts verified TeleAgent install/runtime
images. It is intentionally Windows x64 specific.
"""
from __future__ import annotations

import ctypes
import json
import os
import struct
import sys
from pathlib import Path

NAMES = {
    "SCHEDULER_API_TOKEN",
    "SUPER_AGENT_LOCAL_SESSION_KEY",
    "SUPER_AGENT_OPENCODE_USERNAME",
    "SUPER_AGENT_OPENCODE_PASSWORD",
    "OPENCODE_SERVER_USERNAME",
    "OPENCODE_SERVER_PASSWORD",
    "OPENCODE_BASE_URL",
    "SCHEDULER_DAEMON_STATE_FILE",
}


def selected_environment(pid: int) -> dict[str, int]:
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise RuntimeError("Windows x64 Python required")
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    nt = ctypes.WinDLL("ntdll")
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.ReadProcessMemory.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    nt.NtQueryInformationProcess.argtypes = [w.HANDLE, w.ULONG, ctypes.c_void_p, w.ULONG, ctypes.c_void_p]
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        raise PermissionError(f"OpenProcess failed: {ctypes.get_last_error()}")
    try:
        size = w.DWORD(32768)
        image = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
            raise PermissionError("Cannot verify process image")
        resolved = Path(image.value).resolve()
        allowed = [Path("G:/teleagent").resolve(),
                   (Path.home() / ".local/share/TeleAgent/runtimes").resolve()]
        if not any(resolved == root or root in resolved.parents for root in allowed):
            raise ValueError("Process is outside verified TeleAgent paths")

        def read(address: int, length: int) -> bytes:
            buf = ctypes.create_string_buffer(length)
            got = ctypes.c_size_t()
            if not kernel.ReadProcessMemory(handle, address, buf, length, ctypes.byref(got)):
                raise PermissionError(f"ReadProcessMemory failed: {ctypes.get_last_error()}")
            return buf.raw[:got.value]

        pbi = ctypes.create_string_buffer(48)
        if nt.NtQueryInformationProcess(handle, 0, pbi, 48, None) != 0:
            raise RuntimeError("NtQueryInformationProcess failed")
        peb = struct.unpack_from("<Q", pbi.raw, 8)[0]
        params = struct.unpack("<Q", read(peb + 0x20, 8))[0]
        env_ptr = struct.unpack("<Q", read(params + 0x80, 8))[0]
        block = bytearray()
        for offset in range(0, 1024 * 1024, 4096):
            chunk = read(env_ptr + offset, min(4096, 1024 * 1024 - offset))
            block.extend(chunk)
            end = block.find(b"\0\0\0\0")
            if end >= 0:
                block = block[: end + 4]
                break
        found: dict[str, int] = {}
        for item in block.decode("utf-16-le", "replace").split("\0"):
            key, sep, value = item.partition("=")
            if sep and key in NAMES:
                found[key] = len(value)
        return {"pid": pid, "image": str(resolved), "variables": found}
    finally:
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    print(json.dumps([selected_environment(int(arg)) for arg in sys.argv[1:]], indent=2))
