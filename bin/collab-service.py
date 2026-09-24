#!/usr/bin/env python3
"""Loopback application API for submitting Goals without addressing agents."""
from __future__ import annotations

import argparse
import json
import os
import sys
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
)


def _planner(name: str, persist: Path):
    if name == "deterministic":
        return DeterministicPlanner()
    if name == "grok":
        from lead_adapter.grok_cli import GrokCliLeadAdapter

        lead_work = persist / "lead-work"
        lead_work.mkdir(parents=True, exist_ok=True)
        return LeadAdapterPlanner(GrokCliLeadAdapter(), cwd=lead_work)
    raise ValueError(f"unknown planner {name!r}")


def _backend(name: str, persist: Path, *, teleagent_stdin_wrap: bool = False):
    if name == "inprocess":
        return None
    if name == "teleagent-windows":
        from execution_backend.windows_supervised_v1 import WindowsSupervisedExecutionBackend

        return WindowsSupervisedExecutionBackend(
            state_dir=persist / "windows-controller",
            stdin_wrap=teleagent_stdin_wrap,
        )
    raise ValueError(f"unknown backend {name!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local Goal API: ingress -> lead planning -> worker backend",
    )
    parser.add_argument("--persist", default=str(ROOT / ".collab-app"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--planner", choices=("deterministic", "grok"), default="deterministic")
    parser.add_argument("--backend", choices=("inprocess", "teleagent-windows"), default="inprocess")
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
        "--check-gui",
        action="store_true",
        help=(
            "run fail-closed doctor against desktop GUI TeleAgent ports "
            "(4399/4397/4398) and exit; does not start the HTTP service "
            "and never uses stdin_wrap"
        ),
    )
    parser.add_argument("--token-env", default="COLLAB_API_TOKEN")
    parser.add_argument("--once", action="store_true", help="process all queued Goals once and exit")
    args = parser.parse_args(argv)

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
    planner = _planner(args.planner, persist)
    backend = _backend(args.backend, persist, teleagent_stdin_wrap=args.teleagent_stdin_wrap)
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
