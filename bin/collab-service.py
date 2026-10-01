#!/usr/bin/env python3
"""Loopback application API for submitting Goals without addressing agents."""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from framework.app_service import (  # noqa: E402
    CollabApplication,
    CollabHttpServer,
    CoordinatorLoop,
    DeterministicPlanner,
    LeadAdapterPlanner,
    write_running_tip,
)

_INPROCESS_LEAD_REFUSAL = (
    "refusing inprocess lead adapter: the HTTP service has no dialogue to "
    "answer file-protocol asks. The codex alias means inprocess; "
    "use COLLAB_LEAD_ADAPTER=codex_cli for the Codex CLI."
)


class InProcessLeadRefused(ValueError):
    """--planner lead cannot host an inprocess / file-protocol lead."""


def _positive_timeout(text: str) -> float:
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"lead timeout must be a float > 0, got {text!r}"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"lead timeout must be > 0, got {text!r}")
    return value


def _format_seconds(timeout_sec: float) -> str:
    value = float(timeout_sec)
    if math.isfinite(value) and value.is_integer():
        return str(int(value))
    return format(value, "g")


def _basename_only(raw: str) -> str:
    """File name only. Slashes and backslashes never survive into the log line."""
    text = str(raw).strip().replace("\\", "/").rstrip("/")
    if not text:
        return ""
    name = text.rsplit("/", 1)[-1].strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or ":" in name:
        return ""
    return name


def _codex_bin_basename(adapter: object) -> str:
    """Basename of the Codex binary, or "" if it cannot be named safely."""
    override = getattr(adapter, "_bin_override", None)
    if isinstance(override, str) and override.strip():
        return _basename_only(override)
    for var in ("COLLAB_CODEX_LEAD_BIN", "COLLAB_LEAD_BIN"):
        raw = (os.environ.get(var) or "").strip()
        if raw and os.path.isfile(raw):
            return _basename_only(raw)
    for candidate in ("codex", "codex.cmd", "codex.exe"):
        found = shutil.which(candidate)
        if found and os.path.isfile(found):
            return _basename_only(found)
    return _basename_only(os.environ.get("COLLAB_CODEX_LEAD_BIN") or "")


def _lead_planner_stderr_line(planner: object) -> str | None:
    if not isinstance(planner, LeadAdapterPlanner):
        return None
    adapter = getattr(planner, "adapter", None)
    name = str(getattr(adapter, "name", "") or type(adapter).__name__)
    line = f"lead planner: adapter={name} timeout={_format_seconds(planner.timeout_sec)}s"
    if name == "codex_cli":
        base = _codex_bin_basename(adapter)
        if base:
            line += f" bin={base}"
    return line


def _announce_lead_planner(planner: object) -> None:
    line = _lead_planner_stderr_line(planner)
    if line:
        print(line, file=sys.stderr, flush=True)


def _planner(name: str, persist: str | Path, timeout_sec: float = 180):
    persist = Path(persist)
    if name == "deterministic":
        return DeterministicPlanner()
    if name == "grok":
        from lead_adapter.grok_cli import GrokCliLeadAdapter

        lead_work = persist / "lead-work"
        lead_work.mkdir(parents=True, exist_ok=True)
        return LeadAdapterPlanner(
            GrokCliLeadAdapter(),
            cwd=lead_work,
            timeout_sec=timeout_sec,
        )
    if name == "lead":
        from lead_adapter import get_lead_adapter

        # exchange_dir is ignored by every adapter except inprocess. A refused
        # inprocess lead must not create the file-protocol exchange in the repo
        # or in COLLAB_LEAD_EXCHANGE — this process has no dialogue to answer it.
        probe = tempfile.mkdtemp(prefix="collab-inprocess-probe-")
        try:
            adapter = get_lead_adapter(exchange_dir=probe)
            if str(getattr(adapter, "name", "") or "") == "inprocess":
                raise InProcessLeadRefused(_INPROCESS_LEAD_REFUSAL)
        finally:
            shutil.rmtree(probe, ignore_errors=True)
        lead_work = persist / "lead-work"
        lead_work.mkdir(parents=True, exist_ok=True)
        return LeadAdapterPlanner(adapter, cwd=lead_work, timeout_sec=timeout_sec)
    raise ValueError(f"unknown planner {name!r}")


def _backend(
    name: str,
    persist: Path,
    *,
    teleagent_stdin_wrap: bool = False,
    agy_account_pool: str | None = None,
):
    if name == "inprocess":
        return None
    if name == "teleagent-windows":
        from execution_backend.windows_supervised_v1 import WindowsSupervisedExecutionBackend

        return WindowsSupervisedExecutionBackend(
            state_dir=persist / "windows-controller",
            stdin_wrap=teleagent_stdin_wrap,
        )
    if name in ("antigravity", "agy"):
        # Reuse run-job pool path (quota / 503 cooldown / mutex). Account is
        # selected+reserved per start_run (per-dispatch); collect_result applies
        # pool state and releases the lease so the next Goal can switch accounts.
        # In-memory agy run handles are NOT recoverable across restart — the
        # coordinator fails the Task with backend resume failed, never silent
        # redispatch. reply_permission stays 501 (no TA permission channel).
        from execution_backend import get_execution_backend
        from execution_backend.agy_account_pool import AccountPoolError

        kw: dict = {}
        pool = (agy_account_pool or "").strip()
        if pool:
            kw["account_pool_path"] = pool
        try:
            return get_execution_backend("antigravity", **kw)
        except AccountPoolError as e:
            raise ValueError(f"antigravity account pool unavailable: {e}") from e
    raise ValueError(f"unknown backend {name!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local Goal API: ingress -> lead planning -> worker backend",
    )
    parser.add_argument("--persist", default=str(ROOT / ".collab-app"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--planner",
        choices=("deterministic", "grok", "lead"),
        default="deterministic",
        help=(
            "deterministic: bootstrap planner. "
            "grok: Grok CLI lead; ignores COLLAB_LEAD_ADAPTER. "
            "lead: LeadAdapter from COLLAB_LEAD_ADAPTER (default grok_cli; "
            "inprocess and the codex alias are refused). "
            "The lead decides the plan and routine permission/review gates; "
            "anything it cannot decide (lead error, illegal JSON, timeout, "
            "non-auto kinds like question/system_action) stays a pending human decision."
        ),
    )
    parser.add_argument(
        "--lead-timeout",
        type=_positive_timeout,
        default=180.0,
        help=(
            "Timeout in seconds for grok and lead planner decisions "
            "(float, must be > 0). Default: 180."
        ),
    )
    parser.add_argument(
        "--backend",
        choices=("inprocess", "teleagent-windows", "antigravity", "agy"),
        default="inprocess",
        help=(
            "Worker backend. antigravity/agy reuses run-job pool + CLI worker; "
            "in-memory run handles are not recoverable after service restart; "
            "reply_permission is 501 (unlike teleagent-windows supervision)."
        ),
    )
    parser.add_argument(
        "--agy-account-pool",
        default="",
        help=(
            "Optional path to agy account pool JSON (else COLLAB_AGY_ACCOUNT_POOL). "
            "Each start_run selects+reserves one HOME (per-dispatch); not pinned "
            "for the whole service lifetime."
        ),
    )
    parser.add_argument(
        "--teleagent-stdin-wrap",
        action="store_true",
        help=(
            "DIAGNOSTIC ONLY: spawn controlled loopback TeleAgent kernel. "
            "Not the production entry — prefer logged-in desktop GUI "
            "(discover 4399/4397/4398; default desktop :4397). "
            "Wrap model-auth reuse is not live-verified."
        ),
    )
    parser.add_argument(
        "--ready",
        action="store_true",
        help=(
            "read-only daily gate: GUI doctor plus /session/status occupancy "
            "and lock-holder metadata. Exit 0 only when ready and "
            "dispatch_allowed. Does not start the HTTP service, stdin_wrap, "
            "a session, or the desktop lock"
        ),
    )
    parser.add_argument(
        "--check-gui",
        action="store_true",
        help=(
            "doctor-only probe of desktop GUI TeleAgent ports (4399/4397/4398) "
            "and exit; does not read session occupancy, start the HTTP service, "
            "or use stdin_wrap. Daily dispatch gate is --ready"
        ),
    )
    parser.add_argument("--token-env", default="COLLAB_API_TOKEN")
    parser.add_argument("--once", action="store_true", help="process all queued Goals once and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.ready:
        from framework.app_service import assess_win_gui_readiness

        result = assess_win_gui_readiness(
            tip_path=Path(args.persist).resolve() / "running_tip.json",
        )
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8")
            except Exception:
                pass
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0 if result.get("ready") and result.get("dispatch_allowed") else 1

    if args.check_gui:
        from framework.app_service import probe_win_gui_connection

        result = probe_win_gui_connection()
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0 if result.get("ok") else 1

    if args.teleagent_stdin_wrap:
        print(
            json.dumps(
                {
                    "warning": (
                        "teleagent-stdin-wrap is diagnostic-only; "
                        "preferred entry is logged-in desktop GUI TeleAgent "
                        "(ports 4399/4397/4398, default :4397). "
                        "Do not use wrap for production."
                    )
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )

    persist = Path(args.persist).resolve()
    try:
        planner = _planner(args.planner, persist, args.lead_timeout)
    except ValueError as exc:
        # inprocess (including the codex alias) and unknown COLLAB_LEAD_ADAPTER.
        # grok / deterministic errors are not argparse failures.
        if args.planner != "lead":
            raise
        parser.error(str(exc))
    _announce_lead_planner(planner)
    backend = _backend(
        args.backend,
        persist,
        teleagent_stdin_wrap=args.teleagent_stdin_wrap,
        agy_account_pool=args.agy_account_pool or None,
    )
    app = CollabApplication(persist, planner=planner, backend=backend)
    if args.once:
        try:
            print(json.dumps(app.coordinator.process_all(), ensure_ascii=False, indent=2, default=str))
        finally:
            close_backend = getattr(app.coordinator.backend, "close", None)
            if callable(close_backend):
                close_backend()
        return 0

    token = (os.environ.get(args.token_env) or "").strip()
    server = CollabHttpServer((args.host, args.port), app, api_token=token)
    write_running_tip(persist / "running_tip.json")
    loop = CoordinatorLoop(app)
    loop.start()
    auth = f"Bearer token required from {args.token_env}" if token else "loopback only; no bearer token configured"
    print(
        json.dumps(
            {
                "ok": True,
                "listen": f"http://{args.host}:{server.server_address[1]}",
                "planner": planner.name,
                "backend": app.coordinator.backend.backend_id,
                "auth": auth,
                "persist": str(persist),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        loop.stop()
        server.server_close()
        close_backend = getattr(app.coordinator.backend, "close", None)
        if callable(close_backend):
            close_backend()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
