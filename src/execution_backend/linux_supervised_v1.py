"""Linux supervised TeleAgent backend.

Reuses ``WindowsSupervisedExecutionBackend`` (the same Engine, Store, external
input isolation, contamination gate, and permission scope). There is no
stdin_wrap path. The HTTP client is created on first dispatch, so the service
can start and answer ``/health`` and ``--ready`` while TeleAgent is logged out.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from execution_backend.windows_supervised_v1 import WindowsSupervisedExecutionBackend


def default_linux_client() -> Any:
    from win_collab.client import Client
    from win_collab.linux_discovery import discover_linux

    base, creds = discover_linux()
    return Client(base, creds)


class LinuxSupervisedExecutionBackend(WindowsSupervisedExecutionBackend):
    backend_id = "teleagent.linux.supervised_v1"
    capability_kind = "teleagent_linux"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        client: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
        max_parallel: int = 3,
        stdin_wrap: bool = False,
    ) -> None:
        if stdin_wrap:
            raise ValueError("stdin_wrap is not supported on teleagent-linux")
        if client is None and client_factory is None:
            client_factory = default_linux_client
        super().__init__(
            state_dir=state_dir,
            client=client,
            client_factory=client_factory,
            max_parallel=max_parallel,
            stdin_wrap=False,
        )


__all__ = ["LinuxSupervisedExecutionBackend", "default_linux_client"]
