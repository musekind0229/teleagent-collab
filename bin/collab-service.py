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


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"must be an integer >= 1, got {text!r}") from exc
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be an integer >= 1, got {text!r}")
    return value


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"must be a number > 0, got {text!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a number > 0, got {text!r}")
    return value


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
    if name in ("teleagent-linux", "teleagent_linux"):
        # Lazy client: constructing the backend does not dial :4399.
        # stdin_wrap is a Windows diagnostic and is not used here.
        from execution_backend.linux_supervised_v1 import LinuxSupervisedExecutionBackend

        return LinuxSupervisedExecutionBackend(state_dir=persist / "linux-controller")
    if name in ("antigravity", "agy"):
        # Reuse run-job pool path (quota / 503 cooldown / mutex). Account is
        # selected+reserved per start_run (per-dispatch); collect_result applies
        # pool state and releases the lease so the next Goal can switch accounts.
        # In-memory agy run handles are NOT recoverable across restart — the
        # coordinator fails the Task with backend resume failed, never silent
        # redispatch. agy-runs.json records worker pids; a restarted service
        # stops orphaned workers (process group / taskkill /T) at startup. reply_permission stays 501 (no TA permission channel).
        from execution_backend import get_execution_backend
        from execution_backend.agy_account_pool import AccountPoolError

        kw: dict = {"run_registry_path": str(persist / "agy-runs.json")}
        pool = (agy_account_pool or "").strip()
        if pool:
            kw["account_pool_path"] = pool
        try:
            return get_execution_backend("antigravity", **kw)
        except AccountPoolError as e:
            raise ValueError(f"antigravity account pool unavailable: {e}") from e
    raise ValueError(f"unknown backend {name!r}")


def _use_linux_readiness(args) -> bool:
    """Linux assessor on Linux, or when the worker backend is teleagent-linux.

    ``--backend teleagent-windows`` on Linux still uses the Linux assessor,
    because ``sys.platform`` is linux. Windows uses the Windows assessor unless
    ``--backend teleagent-linux`` (or ``teleagent_linux``) is set.
    """
    backend = str(getattr(args, "backend", "") or "")
    if backend in ("teleagent-linux", "teleagent_linux"):
        return True
    return sys.platform.startswith("linux")


def _uses_agy_backend(args) -> bool:
    return str(getattr(args, "backend", "") or "") in ("antigravity", "agy")


def _emit_json(payload) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


def _note_previous_exit(persist: Path) -> str:
    """Log why the previous service process ended and feed it to reap reasons."""
    from execution_backend.agy_run_registry import set_previous_exit_note
    from framework.service_exit import consume_previous_exit, previous_exit_note

    row = consume_previous_exit(persist)
    note = previous_exit_note(row)
    if row is not None:
        print(
            f"previous service process ended: result={row.get('service_result') or '?'} "
            f"code={row.get('exit_code') or '?'} status={row.get('exit_status') or '?'}",
            file=sys.stderr,
            flush=True,
        )
    set_previous_exit_note(note)
    try:  # baseline for ExecStopPost: oom_kills from here on are this process's to explain
        from platform_services import cgroup_oom

        cgroup_oom.start_ledger(persist)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not block startup
        print(f"oom ledger unavailable: {exc}", file=sys.stderr, flush=True)
    return note


def _install_lead_registry(persist: Path) -> list[dict]:
    """Record lead (planner / decision) processes in ``<persist>/lead-runs.json``.

    A service killed with SIGKILL leaves its lead behind (it runs in its own
    process group). On the next start, every lead recorded by a dead service
    is stopped with its whole group (Windows: ``taskkill /F /T``); a pid that
    now belongs to another process (start token differs) is left alone.
    """
    from execution_backend.agy_run_registry import AgyRunRegistry
    from lead_adapter.cancel import set_process_registry

    registry = AgyRunRegistry(persist / "lead-runs.json")
    rows: list[dict] = []
    try:
        rows = registry.reap_orphans()
    except Exception as exc:  # noqa: BLE001 — never block startup on bookkeeping
        print(f"lead orphan reap failed: {type(exc).__name__}", file=sys.stderr, flush=True)
    for row in rows:
        print(
            f"lead orphan from previous service: run={row.get('run_id')} pid={row.get('pid')} outcome={row.get('outcome')}",
            file=sys.stderr,
            flush=True,
        )
    set_process_registry(registry)
    return rows


def _install_stop_signals() -> None:
    """SIGTERM (and SIGBREAK on Windows) take the same finally path as Ctrl-C.

    Without it a plain ``kill`` skips backend.close(), so live agy workers
    outlive the service and keep writing until the next start reaps them.
    """
    import signal

    def _stop(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _stop)
            except (OSError, ValueError):
                pass


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
        choices=(
            "inprocess",
            "teleagent-windows",
            "teleagent-linux",
            "teleagent_linux",
            "antigravity",
            "agy",
        ),
        default="inprocess",
        help=(
            "Worker backend. teleagent-linux reuses the supervised controller "
            "against loopback TeleAgent :4399 (no stdin_wrap). "
            "antigravity/agy reuses run-job pool + CLI worker; "
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
            "dispatch_allowed. With --backend antigravity/agy this is the agy gate "
            "(agy binary, version, signed-in account via `agy models`, running tip). "
            "Otherwise on Linux (or --backend teleagent-linux) this is "
            "assess_linux_gui_readiness; otherwise the Windows gate. "
            "Does not start the HTTP service, stdin_wrap, a session, or the desktop lock"
        ),
    )
    parser.add_argument(
        "--check-gui",
        action="store_true",
        help=(
            "doctor-only probe of desktop GUI TeleAgent ports (4399/4397/4398) "
            "and exit; does not read session occupancy, start the HTTP service, "
            "or use stdin_wrap. On Linux this runs the Linux readiness report "
            "(same exit rule as --ready). Daily dispatch gate is --ready"
        ),
    )
    parser.add_argument(
        "--max-parallel-per-goal",
        type=_positive_int,
        default=2,
        help=(
            "How many independent tasks of one Goal may run at once "
            "(integer >= 1). Default: 2. The effective cap is the minimum of "
            "this, --max-parallel-global, and the backend concurrency limit."
        ),
    )
    parser.add_argument(
        "--stale-after",
        type=_positive_seconds,
        default=120.0,
        help=(
            "While a task is running, a heartbeat older than this many seconds "
            "is stale (number > 0). Default: 120. Unknown progress stays unknown."
        ),
    )
    parser.add_argument(
        "--max-parallel-global",
        type=_positive_int,
        default=4,
        help=(
            "How many tasks may run at once across all Goals "
            "(integer >= 1). Default: 4. Supervised TeleAgent desktops stay "
            "serial because of the desktop session lock."
        ),
    )
    parser.add_argument("--token-env", default="COLLAB_API_TOKEN")
    parser.add_argument("--once", action="store_true", help="process all queued Goals once and exit")
    parser.add_argument(
        "--record-exit",
        action="store_true",
        help=(
            "for systemd ExecStopPost: write $SERVICE_RESULT/$EXIT_CODE/$EXIT_STATUS to "
            "<persist>/last-exit.json; the next start reports e.g. an OOM kill in reap reasons"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.record_exit:
        # systemd ExecStopPost: remember why this service process ended.
        from framework.service_exit import record_exit

        row = record_exit(Path(args.persist).resolve())
        print(f"recorded service exit: result={row.get('service_result')}", file=sys.stderr, flush=True)
        return 0

    if args.ready and _uses_agy_backend(args):
        # agy workers need agy itself, not TeleAgent :4399 / GUI credentials.
        from framework.agy_ready import assess_agy_readiness

        result = assess_agy_readiness(
            tip_path=Path(args.persist).resolve() / "running_tip.json",
            pool_path=args.agy_account_pool or None,
        )
        _emit_json(result)
        return 0 if result.get("ready") and result.get("dispatch_allowed") else 1

    linux_ready = _use_linux_readiness(args)
    if args.ready or (args.check_gui and linux_ready):
        tip_path = Path(args.persist).resolve() / "running_tip.json"
        if linux_ready:
            from framework.app_service import assess_linux_gui_readiness

            result = assess_linux_gui_readiness(tip_path=tip_path)
        else:
            from framework.app_service import assess_win_gui_readiness

            result = assess_win_gui_readiness(tip_path=tip_path)
        _emit_json(result)
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
    _note_previous_exit(persist)
    _install_lead_registry(persist)
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
    app = CollabApplication(
        persist,
        planner=planner,
        backend=backend,
        max_parallel_per_goal=args.max_parallel_per_goal,
        max_parallel_global=args.max_parallel_global,
        stale_after_sec=args.stale_after,
    )
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
    # agy: refresh GET /v1/capabilities models.known from `agy models` in the
    # background (built-in list until then); per-Goal `model` is checked against it.
    enable_probe = getattr(app.coordinator.backend, "enable_model_probe", None)
    if callable(enable_probe):
        try:
            enable_probe()
        except Exception as exc:  # noqa: BLE001 - never block startup on the model list
            print(f"agy model list probe unavailable: {type(exc).__name__}", file=sys.stderr, flush=True)
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
                "max_parallel_per_goal": args.max_parallel_per_goal,
                "max_parallel_global": args.max_parallel_global,
                "stale_after_sec": args.stale_after,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    _install_stop_signals()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        loop.stop()
        server.server_close()
        try:  # a lead still planning must not outlive the service
            app.coordinator.stop_all_planning("service shutdown")
        except Exception:  # noqa: BLE001
            pass
        close_backend = getattr(app.coordinator.backend, "close", None)
        if callable(close_backend):
            close_backend()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
