"""Read-only readiness gate for the antigravity / agy CLI worker backend.

The TeleAgent gate (``linux_ready``) asks for :4399 and TeleAgent credentials,
which mean nothing to an agy worker. This gate checks what an agy run needs:
the binary, its version, a signed-in account (``agy models`` says "Please sign
in" otherwise) and the running tip. Probes are injectable so tests never start
a real agy. Output is trimmed; no credentials are read or copied.
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from framework.linux_ready import _check

LOGIN_STEP = (
    "agy is not signed in for this user/HOME: run `agy` (no arguments) as the service user "
    "over SSH, open the printed URL in a local browser, sign in, paste the code back"
)
_INSTALL_STEP = "install agy for the service user (curl -fsSL https://antigravity.google/cli/install.sh | bash) or set AGY_BIN"

Runner = Callable[[list[str], Mapping[str, str], float], tuple[int | None, str]]


def _run(argv: list[str], env: Mapping[str, str], timeout: float) -> tuple[int | None, str]:
    try:
        proc = subprocess.run(
            argv,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout:g}s"
    except OSError as exc:
        return -1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()


def _short(text: str, limit: int = 240) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 3] + "..."


def classify_login(returncode: int | None, output: str) -> dict[str, str]:
    """Map ``agy models`` output to logged_in / not_logged_in / unknown."""
    low = str(output or "").lower()
    if "sign in" in low or "signin" in low or "not signed" in low or "log in" in low:
        return {"state": "not_logged_in", "detail": _short(output)}
    if returncode is None:
        return {"state": "unknown", "detail": _short(output) or "agy models timed out"}
    if returncode == 0:
        lines = [x for x in str(output).splitlines() if x.strip() and not x.lower().startswith("fetching")]
        return {"state": "logged_in", "detail": f"agy models listed {len(lines)} line(s)"}
    return {"state": "unknown", "detail": _short(output) or f"agy models exit={returncode}"}


def assess_agy_readiness(
    *,
    environ: Mapping[str, str] | None = None,
    bin_path: str | None = None,
    runner: Runner | None = None,
    tip_path: str | Path | None = None,
    running_tip: str | None = None,
    head_tip: str | None = None,
    running_tip_fn: Callable[[], Any] | None = None,
    head_tip_fn: Callable[[], Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    from execution_backend.antigravity_cli_v1 import resolve_agy_bin
    from framework.app_service import assess_tip_consistency, tip_mismatch_lines

    env = dict(environ if environ is not None else os.environ)
    run = runner or _run
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []
    checks.append(_check("python", True, blocks=True, detail=f"Python {sys.version.split()[0]}"))

    agy = resolve_agy_bin(explicit=bin_path, environ=env)
    resolved = agy if os.path.isabs(agy) else (__import__("shutil").which(agy, path=env.get("PATH")) or "")
    bin_ok = bool(resolved) and os.path.isfile(resolved) and os.access(resolved, os.X_OK)
    checks.append(
        _check(
            "agy_bin",
            bin_ok,
            blocks=True,
            detail=resolved if bin_ok else f"agy binary not found ({agy})",
            next_step="" if bin_ok else _INSTALL_STEP,
            code="" if bin_ok else "agy_missing",
        )
    )

    version = ""
    login = {"state": "unknown", "detail": "agy binary missing"}
    if bin_ok:
        rc, out = run([resolved, "--version"], env, 15.0)
        version = _short(out, 80) if rc == 0 else ""
        checks.append(
            _check(
                "agy_version",
                rc == 0,
                blocks=False,
                detail=version or _short(out) or f"exit={rc}",
                code="" if rc == 0 else "agy_version_failed",
            )
        )
        rc, out = run([resolved, "models"], env, float(timeout))
        login = classify_login(rc, out)
    logged_in = login["state"] == "logged_in"
    if bin_ok:
        step = "" if logged_in else (LOGIN_STEP if login["state"] == "not_logged_in" else "run `agy models` as the service user and check the error")
        checks.append(
            _check(
                "agy_login",
                logged_in,
                blocks=True,
                detail=f"{login['state']}: {login['detail']}" if not logged_in else login["detail"],
                next_step=step,
                code="" if logged_in else ("not_logged_in" if login["state"] == "not_logged_in" else "login_unknown"),
            )
        )
    pool = str(env.get("COLLAB_AGY_ACCOUNT_POOL") or "").strip()
    if pool:
        checks.append(
            _check(
                "agy_account_pool",
                True,
                blocks=False,
                detail="account pool configured; login above is for this process HOME only, each pool HOME signs in separately",
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
        lines = tip_mismatch_lines(tip)
        reasons.extend(lines)
        checks.append(
            _check(
                "running_tip",
                False,
                blocks=True,
                detail=str(tip.get("reason") or "running tip mismatch"),
                next_step=lines[0] if lines else "Restart collab-service so the running tip matches HEAD.",
                code="running_tip_mismatch",
            )
        )

    hints: list[str] = []
    for item in checks:
        step = str(item.get("next_step") or "").strip()
        if not item["ok"] and step and step not in hints:
            hints.append(step)
    for line in reasons:
        if line and line not in hints:
            hints.append(line)
    dispatch_allowed = all(item["ok"] or not item["blocks_dispatch"] for item in checks)
    return {
        "ready": dispatch_allowed,
        "dispatch_allowed": dispatch_allowed,
        "backend": "antigravity.cli_v1",
        "agy": {"bin": resolved or agy, "version": version, "login": login["state"]},
        "tip_ok": bool(tip["ok"]),
        "running_tip": tip["running_tip"],
        "head_tip": tip["head_tip"],
        "checks": checks,
        "hints": hints,
        "reasons": reasons,
    }


__all__ = ["assess_agy_readiness", "classify_login", "LOGIN_STEP"]
