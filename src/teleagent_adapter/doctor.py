"""TeleAgent adapter doctor: not_running / version_incompatible / missing_creds / auth_failed / api_incompatible."""
from __future__ import annotations

import json
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

from teleagent_adapter.base import AdapterStatus


# Known-compatible Linux SAC HTTP API surface (from discovery notes).
_MIN_LINUX_APP = (2, 5, 0)
_KNOWN_API_ROUTES = ("/session", "/permission", "/version")


@dataclass
class DoctorReport:
    status: str
    platform: str
    base_url: str
    details: list[str] = field(default_factory=list)
    simulated: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _parse_version(s: str) -> tuple[int, ...]:
    parts = []
    for bit in (s or "").replace("-", ".").split("."):
        if bit.isdigit():
            parts.append(int(bit))
        else:
            break
    return tuple(parts) if parts else (0,)


def _port_open(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def doctor(
    *,
    base_url: str = "http://127.0.0.1:4399",
    platform: str | None = None,
    teleagent_version: str | None = None,
    adapter: Any = None,
    simulated: bool = False,
    simulate_status: str | None = None,
) -> DoctorReport:
    """Probe TeleAgent readiness. Pass simulated=True + simulate_status for unit tests."""
    plat = (platform or sys.platform).lower()
    report = DoctorReport(
        status=AdapterStatus.OK.value,
        platform=plat,
        base_url=base_url,
        simulated=simulated,
    )

    if simulated and simulate_status:
        report.status = simulate_status
        report.details.append(f"simulated status={simulate_status}")
        return report

    if plat.startswith("win"):
        report.status = AdapterStatus.BLOCKED.value
        report.details.append(
            "Windows TeleAgent 2.4.1 has no supported auth entry for workers → blocked"
        )
        report.extras["teleagent_version"] = teleagent_version or "2.4.1"
        return report

    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    # Prefer explicit 4399 default
    if parsed.port is None and "4399" in base_url:
        port = 4399

    if not _port_open(host, port):
        report.status = AdapterStatus.NOT_RUNNING.value
        report.details.append(f"{host}:{port} not accepting TCP connections")
        return report

    if teleagent_version:
        ver = _parse_version(teleagent_version)
        if ver < _MIN_LINUX_APP:
            report.status = AdapterStatus.VERSION_INCOMPATIBLE.value
            report.details.append(
                f"teleagent_version={teleagent_version} < minimum {'.'.join(map(str, _MIN_LINUX_APP))}"
            )
            return report

    # Creds / auth / API probe via adapter when provided
    if adapter is not None:
        try:
            adapter.refresh_creds()
        except Exception as e:
            report.status = AdapterStatus.MISSING_CREDS.value
            report.details.append(f"refresh_creds failed: {e}")
            return report
        try:
            code, body = adapter.call("GET", "/version")
        except Exception as e:
            err = str(e).lower()
            if "auth" in err or "401" in err or "403" in err:
                report.status = AdapterStatus.AUTH_FAILED.value
            elif "cred" in err or "missing" in err:
                report.status = AdapterStatus.MISSING_CREDS.value
            else:
                report.status = AdapterStatus.NOT_RUNNING.value
            report.details.append(str(e))
            return report
        if code in (401, 403):
            report.status = AdapterStatus.AUTH_FAILED.value
            report.details.append(f"/version HTTP {code}")
            return report
        if code >= 500 or code == 404:
            report.status = AdapterStatus.API_INCOMPATIBLE.value
            report.details.append(f"/version HTTP {code} body={body!r}")
            return report
        report.extras["version_body"] = body
        # Spot-check permission route exists
        try:
            pc, _ = adapter.call("GET", "/permission")
            if pc in (401, 403):
                report.status = AdapterStatus.AUTH_FAILED.value
                report.details.append(f"/permission HTTP {pc}")
                return report
            if pc >= 400:
                report.status = AdapterStatus.API_INCOMPATIBLE.value
                report.details.append(f"/permission HTTP {pc}")
                return report
        except Exception as e:
            report.status = AdapterStatus.API_INCOMPATIBLE.value
            report.details.append(str(e))
            return report

    report.status = AdapterStatus.OK.value
    report.details.append("port open" + ("; adapter probe ok" if adapter is not None else ""))
    return report


def doctor_json(**kwargs: Any) -> str:
    return json.dumps(doctor(**kwargs).to_dict(), ensure_ascii=False, indent=2)


__all__ = ["DoctorReport", "doctor", "doctor_json"]
