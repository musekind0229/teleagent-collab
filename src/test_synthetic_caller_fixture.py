"""Explicit TEST-ONLY caller contract for synthetic AGY executables. Not OS isolation.
Production defaults remain deny. This helper never accepts installed agy/grok.
"""
from pathlib import Path
import hashlib,uuid,sys
from unittest.mock import Mock
from execution_backend.antigravity_cli_v1 import agy_auto_approve_enabled,artifacts_from_charter

def fixture_backend(backend,charter=None):
 p=Path(backend.bin_path)
 if p.name.lower()in {'agy.exe','grok.exe','agy','grok'}:raise AssertionError('real model executable forbidden by synthetic test helper')
 mode='skip_permissions'if agy_auto_approve_enabled(charter,backend._base_env())else'strict_headless'
 backend.configure_caller_permissions(allowed_operations=['read','write','command'],native_mode=mode)
 if p.is_file():
  # This is coverage for an explicitly local fake executable, not verified coverage of real native CLI.
  if p.suffix.lower()in ['.exe']and p.resolve()!=Path(sys.executable).resolve():raise AssertionError('non-Python executable not a fixture')
  backend.configure_native_tool_coverage(native_mode=mode,operations=['read','write','command'],executable_sha256=hashlib.sha256(p.read_bytes()).hexdigest())
 return backend

def fixture_start(backend,**kw):
 fixture_backend(backend,kw.get('charter'));kw.setdefault('caller_task_id','synthetic-test-'+uuid.uuid4().hex)
 grant=backend.authorize_caller_task(task_id=kw['caller_task_id'],title=kw['title'],directory=kw['directory'],instruction=kw.get('instruction',''),artifacts=artifacts_from_charter(kw.get('charter'),kw.get('artifacts')));assert grant['ok']
 return backend.start_run(**kw)
