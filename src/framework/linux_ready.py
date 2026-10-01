"""Read-only Linux TeleAgent readiness gate.

Same dispatch rule as the Windows gate: exit-ready only when the GUI can be
used, the desktop is idle, and the running tip matches HEAD. Probes are
injectable so tests never dial a live TeleAgent or read real credentials.
Secret values are not copied into the result.
"""
from __future__ import annotations

import os
import socket
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_LOGIN_STEP = (
    "TeleAgent GUI not logged in: open the GUI (VNC to the Xvfb display) "
    "and log in; :4399 starts after login"
)
_START_STEP = (
    "TeleAgent is not running: install TeleAgent (/opt/TeleAgent or "
    "~/.local/share/TeleAgent/runtimes), start the GUI on the Xvfb display, "
    "then log in via VNC; :4399 starts after login"
)
_JSONSCHEMA_STEP = "pip install -r requirements.txt"


def _check(
    name: str,
    ok: bool,
    *,
    blocks: bool,
    detail: str,
    next_step: str = "",
    code: str = "",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name,
        "ok": bool(ok),
        "blocks_dispatch": bool(blocks),
        "detail": detail,
    }
    if code:
        item["code"] = code
    if not ok:
        item["next_step"] = next_step or detail
    return item


def _jsonschema_importable() -> bool:
    try:
        import jsonschema  # noqa: F401
    except Exception:
        return False
    return True


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    if host != "127.0.0.1":
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _x_display_status(environ: Mapping[str, str]) -> dict[str, Any]:
    display = str(environ.get("DISPLAY") or "").strip()
    sockets = False
    xdir = Path("/tmp/.X11-unix")
    try:
        sockets = xdir.is_dir() and any(xdir.iterdir())
    except OSError:
        sockets = False
    if display:
        return {"present": True, "detail": f"DISPLAY={display}"}
    if sockets:
        return {
            "present": True,
            "detail": "X11 socket present but DISPLAY is unset in this process",
        }
    return {
        "present": False,
        "detail": "DISPLAY is unset and no X11 socket was found",
    }


def _creds_next_step(reason: str) -> str:
    if reason == "not_readable":
        return (
            "TeleAgent processes found but environ not readable: "
            "run collab-service as the TeleAgent user or root"
        )
    if reason == "conflict":
        return (
            "Multiple TeleAgent processes disagree on local API credentials. "
            "Stop the extra process, or set the three local API keys in the "
            "EnvironmentFile (mode 600). Values are not shown."
        )
    if reason == "bad_url":
        return "Set TELEAGENT_URL to http://127.0.0.1:<port> or unset it to use http://127.0.0.1:4399."
    if reason == "not_logged_in":
        return _LOGIN_STEP
    return "no TeleAgent process. " + _LOGIN_STEP


def _default_session_status(base_url: str, scan: Any) -> Any:
    if scan is None or not getattr(scan, "creds", None):
        raise RuntimeError("creds unavailable")
    from win_collab.client import Client

    client = Client(base=base_url, creds=scan.creds)
    return client.call("GET", "/session/status")


def assess_linux_gui_readiness(
    *,
    environ: Mapping[str, str] | None = None,
    proc_root: str | Path | None = None,
    image_specs: list | None = None,
    home: Path | None = None,
    include_system_homes: bool = True,
    base_url: str | None = None,
    process_probe: Callable[[], Mapping[str, Any]] | None = None,
    port_open_fn: Callable[[str, int], bool] | None = None,
    creds_probe: Callable[[], Mapping[str, Any]] | None = None,
    session_status_fn: Callable[[str], Any] | None = None,
    jsonschema_probe: Callable[[], bool] | None = None,
    display_probe: Callable[[], Mapping[str, Any]] | None = None,
    python_version: tuple[int, ...] | None = None,
    running_tip: str | None = None,
    head_tip: str | None = None,
    running_tip_fn: Callable[[], Any] | None = None,
    head_tip_fn: Callable[[], Any] | None = None,
    tip_path: str | Path | None = None,
    lock_holder_fn: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Linux daily gate. Does not create a session or claim the desktop lock.

    ``jsonschema`` and the X display check are informational. Everything else
    listed below blocks dispatch when it fails. Credential values are not
    returned.
    """
    # Lazy: framework.app_service re-exports this function. Importing those
    # helpers at module import time would cycle.
    from framework.app_service import (
        _classify_desktop_occupancy,
        _occupancy_block_reasons,
        _public_lock_holder,
        assess_tip_consistency,
        tip_mismatch_lines,
    )

    env = dict(os.environ if environ is None else environ)
    if base_url:
        env["TELEAGENT_URL"] = str(base_url)

    version = tuple(python_version or sys.version_info[:3])
    while len(version) < 3:
        version = version + (0,)
    python_ok = version >= (3, 10)
    python_text = f"Python {version[0]}.{version[1]}.{version[2]}"
    checks: list[dict[str, Any]] = [
        _check(
            "python",
            python_ok,
            blocks=True,
            detail=python_text,
            next_step="Install Python 3.10 or newer.",
        )
    ]

    if jsonschema_probe is not None:
        try:
            has_jsonschema = bool(jsonschema_probe())
        except Exception:
            has_jsonschema = False
    else:
        has_jsonschema = _jsonschema_importable()
    checks.append(
        _check(
            "jsonschema",
            has_jsonschema,
            blocks=False,
            detail=(
                "jsonschema importable"
                if has_jsonschema
                else "jsonschema is not importable (tests only; collab-service runtime is stdlib)"
            ),
            next_step=_JSONSCHEMA_STEP,
            code="" if has_jsonschema else "missing_dependency",
        )
    )

    if process_probe is not None:
        try:
            process = dict(process_probe())
        except Exception as exc:
            process = {"present": False, "detail": f"process probe failed: {type(exc).__name__}"}
    else:
        from win_collab.linux_discovery import linux_process_report

        process = linux_process_report(
            environ=env,
            proc_root=proc_root,
            image_specs=image_specs,
            home=home,
            include_system_homes=include_system_homes,
        )
    process_ok = bool(process.get("present"))
    checks.append(
        _check(
            "teleagent_process",
            process_ok,
            blocks=True,
            detail=str(process.get("detail") or ("TeleAgent process present" if process_ok else "no TeleAgent process")),
            next_step=_START_STEP,
            code="" if process_ok else "no_process",
        )
    )

    if display_probe is not None:
        try:
            display = dict(display_probe())
        except Exception as exc:
            display = {"present": False, "detail": f"display probe failed: {type(exc).__name__}"}
    else:
        display = _x_display_status(env)
    display_ok = bool(display.get("present"))
    checks.append(
        _check(
            "x_display",
            display_ok,
            blocks=False,
            detail=str(display.get("detail") or ""),
            next_step=(
                "Start Xvfb (or another X server) and set DISPLAY before opening "
                "the TeleAgent GUI. Informational only; this process does not need DISPLAY to dispatch."
            ),
        )
    )

    from win_collab.linux_discovery import linux_base_url, scan_linux_credentials

    normalized = ""
    port_detail = ""
    port_step = _LOGIN_STEP
    port_ok = False
    port_code = "port_closed"
    try:
        normalized = linux_base_url(env)
    except RuntimeError as exc:
        port_detail = str(exc)
        port_step = _creds_next_step("bad_url")
        port_code = "bad_url"
    else:
        parsed = urlsplit(normalized)
        opener = port_open_fn or _port_open
        try:
            port_ok = bool(opener("127.0.0.1", int(parsed.port or 0)))
        except Exception as exc:
            port_detail = f"port probe failed: {type(exc).__name__}"
        else:
            port_detail = (
                f"127.0.0.1:{parsed.port} is listening"
                if port_ok
                else f"127.0.0.1:{parsed.port} is not listening"
            )
    checks.append(
        _check(
            "port",
            port_ok,
            blocks=True,
            detail=port_detail,
            next_step=port_step,
            code="" if port_ok else port_code,
        )
    )

    scan = None
    if creds_probe is not None:
        try:
            creds_view = dict(creds_probe())
        except Exception as exc:
            creds_view = {
                "ok": False,
                "reason": "missing",
                "detail": f"creds probe failed: {type(exc).__name__}",
            }
    else:
        scan = scan_linux_credentials(
            environ=env,
            proc_root=proc_root,
            image_specs=image_specs,
            home=home,
            include_system_homes=include_system_homes,
        )
        creds_view = scan.public_dict()
    creds_ok = bool(creds_view.get("ok"))
    creds_reason = str(creds_view.get("reason") or ("ok" if creds_ok else "missing"))
    checks.append(
        _check(
            "creds",
            creds_ok,
            blocks=True,
            detail=str(creds_view.get("detail") or creds_reason),
            next_step=_creds_next_step(creds_reason),
            code="" if creds_ok else creds_reason,
        )
    )

    reasons: list[str] = []
    occupancy: dict[str, Any]
    if not (creds_ok and port_ok and normalized):
        occupancy = {"state": "unknown", "session_count": 0, "session_types": {}}
        occ_detail = str(creds_view.get("detail") or port_detail or "")
    else:
        fetch = session_status_fn
        try:
            if fetch is not None:
                status_obj = fetch(normalized)
            else:
                status_obj = _default_session_status(normalized, scan)
        except Exception as exc:
            occupancy = {"state": "unknown", "session_count": 0, "session_types": {}}
            occ_detail = f"session status read failed: {type(exc).__name__}"
        else:
            occupancy = _classify_desktop_occupancy(status_obj)
            occ_detail = ""
    occ_ok = occupancy["state"] == "idle"
    if occ_ok:
        checks.append(_check("occupancy", True, blocks=True, detail="idle"))
    else:
        occ_lines = _occupancy_block_reasons(str(occupancy["state"]), detail=occ_detail)
        reasons.extend(occ_lines)
        checks.append(
            _check(
                "occupancy",
                False,
                blocks=True,
                detail=str(occupancy["state"]),
                next_step=occ_lines[0] if occ_lines else "DO NOT DISPATCH",
                code=str(occupancy["state"]),
            )
        )

    tip = assess_tip_consistency(
        running_tip=running_tip,
        head_tip=head_tip,
        running_tip_fn=running_tip_fn,
        head_tip_fn=head_tip_fn,
        tip_path=tip_path,
    )
    if tip["ok"]:
        checks.append(_check("running_tip", True, blocks=True, detail="running tip matches HEAD"))
    else:
        tip_lines = tip_mismatch_lines(tip)
        reasons.extend(tip_lines)
        checks.append(
            _check(
                "running_tip",
                False,
                blocks=True,
                detail=str(tip.get("reason") or "running tip mismatch"),
                next_step=tip_lines[0] if tip_lines else "Restart collab-service so the running tip matches HEAD.",
                code="running_tip_mismatch",
            )
        )

    lock_holder = None
    if normalized:
        holder_fn = lock_holder_fn
        if holder_fn is None:
            from win_collab.desktop_lock import read_holder

            holder_fn = read_holder
        try:
            lock_holder = _public_lock_holder(holder_fn(normalized))
        except Exception:
            lock_holder = None

    hints: list[str] = []
    for item in checks:
        if item["ok"]:
            continue
        step = str(item.get("next_step") or "").strip()
        if step and step not in hints:
            hints.append(step)
    for line in reasons:
        if line and line not in hints:
            hints.append(line)

    dispatch_allowed = all(item["ok"] or not item["blocks_dispatch"] for item in checks)
    gui_ok = process_ok and port_ok and creds_ok
    return {
        "ready": dispatch_allowed,
        "dispatch_allowed": dispatch_allowed,
        "occupancy": occupancy,
        "session_count": occupancy["session_count"],
        "session_types": occupancy["session_types"],
        "tip_ok": bool(tip["ok"]),
        "checks": checks,
        "hints": hints,
        "reasons": reasons,
        "base_url": normalized,
        "running_tip": tip["running_tip"],
        "head_tip": tip["head_tip"],
        "lock_holder": lock_holder,
        "gui": {
            "ok": gui_ok,
            "status": "ok" if gui_ok else "not_ready",
            "reason": "" if gui_ok else (hints[0] if hints else "not_ready"),
        },
    }


__all__ = ["assess_linux_gui_readiness"]
