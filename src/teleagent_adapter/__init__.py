"""TeleAgent platform adapter layer (Linux local-v1 / Windows local-v1).

Windows 真机未验收 — win32 factory returns WindowsLocalV1Adapter (mock-tested);
WindowsBlockedAdapter is an explicit blocked/degraded path, not the default.
"""
from __future__ import annotations

import sys
from typing import Any

from teleagent_adapter.base import (
    AdapterError,
    AdapterStatus,
    TeleAgentAdapter,
    creds_refresh_policy,
    reconnect_policy,
    resume_policy,
)
from teleagent_adapter.doctor import DoctorReport, doctor
from teleagent_adapter.linux_local_v1 import LinuxLocalV1Adapter
from teleagent_adapter.native_handle import session_id_of, wrap_session_handle
from teleagent_adapter.permission_view import list_public_permissions, to_public_permission, to_native_for_rules, prepare_permission
from teleagent_adapter.run_observe import build_run_observation, fetch_run_observation
from teleagent_adapter.windows_blocked import WindowsBlockedAdapter
from teleagent_adapter.windows_local_v1 import WindowsLocalV1Adapter

__all__ = [
    "AdapterError",
    "AdapterStatus",
    "TeleAgentAdapter",
    "LinuxLocalV1Adapter",
    "WindowsLocalV1Adapter",
    "WindowsBlockedAdapter",
    "DoctorReport",
    "doctor",
    "get_adapter",
    "creds_refresh_policy",
    "reconnect_policy",
    "resume_policy",
    "session_id_of",
    "wrap_session_handle",
    "list_public_permissions",
    "to_public_permission",
    "to_native_for_rules",
    "prepare_permission",
    "build_run_observation",
    "fetch_run_observation",
]


def get_adapter(
    *,
    platform: str | None = None,
    base_url: str | None = None,
    blocked: bool = False,
    **kwargs: Any,
) -> TeleAgentAdapter:
    """Factory: Linux → local-v1 HMAC :4399; Windows → local-v1 (not blocked).

    Pass ``blocked=True`` on win32 to get the explicit ``WindowsBlockedAdapter``
    degrade path. Windows 真机未验收.
    """
    plat = (platform or sys.platform).lower()
    if plat.startswith("win"):
        if blocked:
            return WindowsBlockedAdapter(teleagent_version=kwargs.get("teleagent_version", "2.4.1"))
        allowed = {
            k: v
            for k, v in kwargs.items()
            if k in (
                "find_creds_fn",
                "lazy_creds",
                "allow_non_loopback",
                "discover",
                "probe_fn",
                "env",
                "host",
            )
        }
        return WindowsLocalV1Adapter(base_url=base_url, **allowed)
    linux_kw = {k: v for k, v in kwargs.items() if k in ("find_creds_fn", "lazy_creds", "allow_non_loopback")}
    return LinuxLocalV1Adapter(base_url=base_url or "http://127.0.0.1:4399", **linux_kw)
