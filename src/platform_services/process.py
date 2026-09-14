"""Process identity used by occupancy records. Loaded with the platform factory."""
from __future__ import annotations

import os
import threading
from typing import Protocol


class ProcessBackend(Protocol):
    name: str

    def current_pid(self) -> int: ...

    def current_thread_id(self) -> int: ...


class StdlibProcess:
    """pid/tid via the interpreter. Same on POSIX and Windows."""

    name = "stdlib.process"

    def current_pid(self) -> int:
        return os.getpid()

    def current_thread_id(self) -> int:
        return threading.get_ident()
