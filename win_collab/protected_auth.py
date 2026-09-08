"""Windows DPAPI storage for app-local API connection data (never account tokens)."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path


def _crypt(raw: bytes, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise RuntimeError('DPAPI credentials require Windows')
    from ctypes import wintypes as w

    class Blob(ctypes.Structure):
        _fields_=[('length',w.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]

    crypt=ctypes.WinDLL('crypt32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes=[ctypes.c_void_p]
    kernel.LocalFree.restype=ctypes.c_void_p
    buf=(ctypes.c_ubyte*len(raw)).from_buffer_copy(raw)
    source=Blob(len(raw),buf);target=Blob()
    if decrypt:
        fn=crypt.CryptUnprotectData
        fn.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,
                     ctypes.c_void_p,w.DWORD,ctypes.POINTER(Blob)]
        ok=fn(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target))
    else:
        fn=crypt.CryptProtectData
        fn.argtypes=[ctypes.POINTER(Blob),w.LPCWSTR,ctypes.c_void_p,ctypes.c_void_p,
                     ctypes.c_void_p,w.DWORD,ctypes.POINTER(Blob)]
        ok=fn(ctypes.byref(source),'TeleAgent collab local API',None,None,None,1,ctypes.byref(target))
    if not ok:
        raise RuntimeError(f'Windows credential protection failed ({ctypes.get_last_error()})')
    try:
        return ctypes.string_at(target.data,target.length)
    finally:
        kernel.LocalFree(target.data)


def save(path:Path, connection:dict):
    from .client import KEYS
    if set(connection) != {'base','creds'} or set(connection['creds']) != set(KEYS):
        raise ValueError('Only local API connection fields may be saved')
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    encrypted=_crypt(json.dumps(connection).encode())
    temp=path.with_suffix('.tmp')
    temp.write_bytes(encrypted)
    os.replace(temp,path)


def load(path:Path):
    data=json.loads(_crypt(Path(path).read_bytes(),decrypt=True))
    return data['base'],data['creds']
