"""Windows Job Object containment for lead and agy child processes.

NOT TESTED ON A REAL WINDOWS HOST (未在 Win 实测): only mock-tested on Linux.

``attach(proc)`` puts a just-started ``subprocess.Popen`` into a fresh Job
Object created with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. The job handle is
held by this (service) process, so when the service dies, even by
``TerminateProcess``/a hard kill, Windows closes the handle and kills every
process still in the job. ``terminate(proc)`` stops the whole job explicitly.

Every failure is swallowed and reported as ``False``/``None``; callers keep
``taskkill /F /T`` as the fallback. Known gap: a child spawned between
``Popen`` returning and ``AssignProcessToJobObject`` is not in the job (Popen
cannot start suspended), so taskkill still runs after a job terminate.
"""

from __future__ import annotations

import sys
from typing import Any

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
_ATTR = "_collab_job"


def _is_windows(platform: str | None = None) -> bool:
    return str(platform if platform is not None else sys.platform).startswith("win")


def _kernel32() -> Any:
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]


def _limit_info() -> Any:
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    return info


def attach(proc: Any, platform: str | None = None, *, kernel32: Any = None) -> Any:
    """Assign ``proc`` to a kill-on-close job. Returns the job handle or None."""
    if not _is_windows(platform):
        return None
    try:
        import ctypes

        k32 = kernel32 if kernel32 is not None else _kernel32()
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _limit_info()
        ok = k32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        )
        handle = getattr(proc, "_handle", None)
        if not ok or handle is None or not k32.AssignProcessToJobObject(job, int(handle)):
            k32.CloseHandle(job)
            return None
        setattr(proc, _ATTR, (k32, job))
        return job
    except Exception:  # noqa: BLE001
        return None


def terminate(proc: Any, exit_code: int = 1) -> bool:
    """Stop every process in ``proc``'s job. False when there is no job or it failed."""
    held = getattr(proc, _ATTR, None)
    if not held:
        return False
    try:
        k32, job = held
        return bool(k32.TerminateJobObject(job, int(exit_code)))
    except Exception:  # noqa: BLE001
        return False


def close(proc: Any) -> None:
    """Release the job handle (kills whatever is still in the job). Never raises."""
    held = getattr(proc, _ATTR, None)
    if not held:
        return
    try:
        setattr(proc, _ATTR, None)
        k32, job = held
        k32.CloseHandle(job)
    except Exception:  # noqa: BLE001
        pass


__all__ = ["attach", "terminate", "close", "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE"]
