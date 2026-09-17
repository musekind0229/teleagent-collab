"""TeleAgent adapter doctor: not_running / version_incompatible / missing_creds / auth_failed / api_incompatible / ok.

Windows uses the same classification as Linux (no automatic ``blocked``).
``blocked`` is only reported for an explicit ``WindowsBlockedAdapter`` (or
``simulate_status``). Windows 真机未验收 — extras keep
``windows_live_verified=false`` until workshop live doctor/hello on
DESKTOP-TBB531F. Extra keys may include ``creds_source``
(process_env|foreign_process_environ|missing) and loopback ports 4399/4397;
never secret values.
"""
from __future__ import annotations

import json
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from teleagent_adapter.base import AdapterStatus


# Known-compatible SAC HTTP API surface (Linux verified 2.5.0; Win unverified).
_MIN_APP = (2, 5, 0)
_MIN_LINUX_APP = _MIN_APP  # alias — do not break callers that imported the old name
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
    port_open_fn: Callable[[str, int], bool] | None = None,
) -> DoctorReport:
    """Probe TeleAgent readiness. Pass simulated=True + simulate_status for unit tests.

    Classification (Linux and Windows): not_running / version_incompatible /
    missing_creds / auth_failed / api_incompatible / ok. ``blocked`` only when
    the adapter itself is an explicit blocked stub (or simulated).

    ``port_open_fn(host, port)`` injects the TCP probe for tests.
    Windows 真机未验收.
    """
    plat = (platform or sys.platform).lower()
    report = DoctorReport(
        status=AdapterStatus.OK.value,
        platform=plat,
        base_url=base_url,
        simulated=simulated,
    )
    if plat.startswith("win"):
        report.extras["windows_live_verified"] = False
        report.extras["note"] = (
            "Windows 真机未验收; live doctor/hello on DESKTOP-TBB531F is workshop-owned"
        )

    if simulated and simulate_status:
        report.status = simulate_status
        report.details.append(f"simulated status={simulate_status}")
        return report

    # Prefer adapter-discovered base_url when present (Win live often lands on :4397).
    if adapter is not None:
        ad_url = getattr(adapter, "base_url", None)
        if isinstance(ad_url, str) and ad_url.strip():
            base_url = ad_url.rstrip("/")
            report.base_url = base_url

    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    # Prefer explicit 4399 default
    if parsed.port is None and "4399" in base_url:
        port = 4399

    check_port = port_open_fn or _port_open
    port_cache: dict[tuple[str, int], bool] = {}

    def _checked(h: str, p: int) -> bool:
        key = (h, p)
        if key not in port_cache:
            port_cache[key] = bool(check_port(h, p))
        return port_cache[key]

    if plat.startswith("win"):
        report.extras["ports"] = {
            "4399": _checked(host, 4399),
            "4397": _checked(host, 4397),
        }
        try:
            from teleagent_adapter.windows_process_environ import probe_windows_creds_presence

            presence = probe_windows_creds_presence()
            report.extras["creds_source"] = presence.source
            report.extras["password_present"] = bool(presence.password_present)
            report.extras["session_key_present"] = bool(presence.session_key_present)
        except Exception:
            report.extras["creds_source"] = "missing"
            report.extras["password_present"] = False
            report.extras["session_key_present"] = False

    # Explicit blocked/degraded adapter only — not the win32 factory default.
    if adapter is not None:
        hint_fn = getattr(adapter, "doctor_hint", None)
        if callable(hint_fn):
            try:
                hint = hint_fn()
            except Exception:
                hint = None
            if isinstance(hint, dict) and str(hint.get("status", "")).lower() == AdapterStatus.BLOCKED.value:
                report.status = AdapterStatus.BLOCKED.value
                report.details.append(str(hint.get("reason") or "adapter blocked"))
                report.extras["teleagent_version"] = hint.get("teleagent_version") or teleagent_version or ""
                return report

    if not _checked(host, port):
        # Windows workers frequently bind :4397 only; do not fail solely on closed :4399.
        if plat.startswith("win") and port != 4397 and _checked(host, 4397):
            port = 4397
            base_url = f"http://{host}:4397"
            report.base_url = base_url
            report.details.append(
                f"preferred port closed; using discovered http://{host}:4397"
            )
            if adapter is not None and hasattr(adapter, "base_url"):
                try:
                    adapter.base_url = base_url
                except Exception:
                    pass
        else:
            report.status = AdapterStatus.NOT_RUNNING.value
            report.details.append(f"{host}:{port} not accepting TCP connections")
            return report

    if teleagent_version:
        ver = _parse_version(teleagent_version)
        if ver < _MIN_APP:
            report.status = AdapterStatus.VERSION_INCOMPATIBLE.value
            report.details.append(
                f"teleagent_version={teleagent_version} < minimum {'.'.join(map(str, _MIN_APP))}"
            )
            return report

    # Creds / auth / API probe via adapter when provided
    if adapter is not None:
        try:
            adapter.refresh_creds()
        except Exception as e:
            from teleagent_adapter.base import AdapterError as _AE

            if isinstance(e, _AE) and e.status == AdapterStatus.AUTH_FAILED:
                report.status = AdapterStatus.AUTH_FAILED.value
            elif isinstance(e, _AE) and e.status == AdapterStatus.NOT_RUNNING:
                report.status = AdapterStatus.NOT_RUNNING.value
            else:
                report.status = AdapterStatus.MISSING_CREDS.value
            report.details.append(f"refresh_creds failed: {e}")
            return report
        try:
            code, body = adapter.call("GET", "/version")
        except Exception as e:
            from teleagent_adapter.base import AdapterError as _AE

            err = str(e).lower()
            if isinstance(e, _AE) and e.status in (
                AdapterStatus.AUTH_FAILED,
                AdapterStatus.MISSING_CREDS,
                AdapterStatus.NOT_RUNNING,
                AdapterStatus.API_INCOMPATIBLE,
            ):
                report.status = e.status.value
            elif "auth" in err or "401" in err or "403" in err:
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
        # Question API probe (P3) — gap marked in extras, does not fail doctor alone
        try:
            try:
                from question_api import probe_question_api
            except ImportError:
                import sys as _sys
                from pathlib import Path as _P
                _src = str(_P(__file__).resolve().parents[1])
                if _src not in _sys.path:
                    _sys.path.insert(0, _src)
                from question_api import probe_question_api
            qp = probe_question_api(adapter)
            report.extras["question_api"] = qp.to_dict()
            if not qp.available:
                report.details.append("question_api gap: " + "; ".join(qp.details))
            else:
                report.details.append("question_api ok")
        except Exception as e:
            report.extras["question_api"] = {"available": False, "status": "error", "details": [str(e)]}
            report.details.append(f"question_api probe error: {e}")

    report.status = AdapterStatus.OK.value
    report.details.append("port open" + ("; adapter probe ok" if adapter is not None else ""))
    return report


def doctor_json(**kwargs: Any) -> str:
    return json.dumps(doctor(**kwargs).to_dict(), ensure_ascii=False, indent=2)


__all__ = ["DoctorReport", "doctor", "doctor_json"]
