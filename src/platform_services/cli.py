"""CLI launch contract: argv-only LaunchSpec, never a shell string.

POSIX executes directly (exec bit) or via an explicit interpreter.
Windows .cmd/.bat need a platform wrapper; that implementation is owned by
the Windows side (platform_services.windows_cli). Without it we raise
PlatformWrapperRequired rather than falling back to shell=True.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol, Sequence

WRAPPER_DIRECT = "direct"
WRAPPER_INTERPRETER = "interpreter"
WRAPPER_WINDOWS_CMD = "windows_cmd"

_WIN_CMD_SUFFIXES = (".cmd", ".bat")


class CliLaunchError(ValueError):
    """Binary/args cannot be turned into a safe argv launch."""


class PlatformWrapperRequired(CliLaunchError, NotImplementedError):
    """Launch needs a platform wrapper that is not available on this side."""


@dataclass(frozen=True)
class LaunchSpec:
    argv: tuple[str, ...]
    cwd: str | None
    env: Mapping[str, str] | None
    wrapper_kind: str
    shell: bool = field(default=False, init=False)  # always False by contract


class CmdWrapper(Protocol):
    """Windows-owned hook: turn a .cmd/.bat + args into a safe LaunchSpec."""

    def wrap(
        self,
        script: str,
        args: tuple[str, ...],
        cwd: str | None,
        env: Mapping[str, str] | None,
    ) -> LaunchSpec: ...


def _check_args(args: Sequence[str]) -> tuple[str, ...]:
    if isinstance(args, (str, bytes)):
        raise CliLaunchError("args must be a list of str, not a joined string")
    out = tuple(args)
    for a in out:
        if not isinstance(a, str):
            raise CliLaunchError(f"arg must be str, got {type(a).__name__}")
        if "\x00" in a:
            raise CliLaunchError("arg contains NUL")
    return out


def _resolve(binary: str, env: Mapping[str, str] | None) -> str:
    if not binary or not isinstance(binary, str):
        raise CliLaunchError("binary must be a non-empty str")
    if os.sep in binary or (os.altsep and os.altsep in binary):
        p = binary
    else:
        # env given -> only env's PATH is searched (no PATH => not found);
        # env None -> the parent process PATH.
        if env is None:
            p = shutil.which(binary) or ""
        else:
            path = env.get("PATH")
            p = (shutil.which(binary, path=path) or "") if path else ""
    if not p or not Path(p).is_file():
        raise CliLaunchError(f"binary not found: {binary!r}")
    return str(Path(p).absolute())


def prepare_cli(
    binary: str,
    args: Sequence[str],
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    *,
    interpreter: str | None = None,
    platform: str | None = None,
    cmd_wrapper: CmdWrapper | None = None,
) -> LaunchSpec:
    """Build an argv-only LaunchSpec. Raises; never degrades to a shell.

    Bare names (no path separator) are looked up on ``env["PATH"]`` when
    ``env`` is given (missing PATH => not found) and on the parent PATH only
    when ``env`` is None. Paths containing a separator are used as-is.
    """
    argv_args = _check_args(args)
    key = (platform or sys.platform).lower()
    if interpreter is not None:
        interp = _resolve(interpreter, env)
        script = _resolve(binary, env)
        return LaunchSpec((interp, script, *argv_args), cwd, env, WRAPPER_INTERPRETER)
    exe = _resolve(binary, env)
    if key.startswith("win"):
        if exe.lower().endswith(_WIN_CMD_SUFFIXES):
            if cmd_wrapper is None:
                raise PlatformWrapperRequired(
                    f"{exe!r} is a .cmd/.bat; needs platform_services.windows_cli wrapper"
                )
            spec = cmd_wrapper.wrap(exe, argv_args, cwd, env)
            if spec.wrapper_kind != WRAPPER_WINDOWS_CMD:
                raise CliLaunchError("cmd wrapper returned wrong wrapper_kind")
            return spec
        if not exe.lower().endswith(".exe"):
            raise CliLaunchError(f"{exe!r} is not .exe; pass an explicit interpreter")
        return LaunchSpec((exe, *argv_args), cwd, env, WRAPPER_DIRECT)
    # POSIX: must be executable; scripts must carry a shebang.
    if not os.access(exe, os.X_OK):
        raise CliLaunchError(f"{exe!r} is not executable; pass an explicit interpreter")
    try:
        with open(exe, "rb") as fh:
            head = fh.read(4)
    except OSError as e:
        raise CliLaunchError(f"cannot read {exe!r}: {e}") from e
    if head[:2] != b"#!" and head != b"\x7fELF" and head[:4] not in (
        b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf",
    ):
        raise CliLaunchError(f"{exe!r} has no shebang/known binary header")
    return LaunchSpec((exe, *argv_args), cwd, env, WRAPPER_DIRECT)


__all__ = [
    "CliLaunchError", "CmdWrapper", "LaunchSpec", "PlatformWrapperRequired",
    "WRAPPER_DIRECT", "WRAPPER_INTERPRETER", "WRAPPER_WINDOWS_CMD", "prepare_cli",
]
