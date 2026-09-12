"""TeleAgent platform adapter layer (Linux local-v1 / Windows blocked)."""
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
from teleagent_adapter.windows_blocked import WindowsBlockedAdapter

__all__ = [
    "AdapterError",
    "AdapterStatus",
    "TeleAgentAdapter",
    "LinuxLocalV1Adapter",
    "WindowsBlockedAdapter",
    "DoctorReport",
    "doctor",
    "get_adapter",
    "creds_refresh_policy",
    "reconnect_policy",
    "resume_policy",
    "session_id_of",
    "wrap_session_handle",
]


def get_adapter(*, platform: str | None = None, base_url: str | None = None, **kwargs: Any) -> TeleAgentAdapter:
    """Factory: Linux → local-v1 HMAC :4399; Windows → blocked stub."""
    plat = (platform or sys.platform).lower()
    if plat.startswith("win"):
        return WindowsBlockedAdapter(teleagent_version=kwargs.get("teleagent_version", "2.4.1"))
    return LinuxLocalV1Adapter(base_url=base_url or "http://127.0.0.1:4399", **{
        k: v for k, v in kwargs.items() if k in ("find_creds_fn", "lazy_creds")
    })
