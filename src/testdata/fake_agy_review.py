#!/usr/bin/env python3
"""Fake agy CLI for lead-review tests. Never calls a model or the network.

argv (agy shape): ``--output-format=json --model=<m> [--dangerously-skip-permissions] --print=<prompt>``.

Env:
  FAKE_AGY_SCRIPT  JSON list of steps, consumed in order across processes.
                   Counter file is ``<script>.n``.
  FAKE_AGY_LOG     jsonl, one object per invocation.

Steps: {"do": "ok"|"exit1"|"sleep"|"hang"|"echo_reason", "write": {rel: text},
"sec": N, "rel": "x.md"}. ``echo_reason`` writes the line after
"Reason (verbatim):" into ``rel``.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path


def _prefixed(prefix: str) -> str:
    for arg in sys.argv[1:]:
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return ""


def _bump(counter: str) -> int:
    path = Path(counter)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(counter + ".lock")
    fd = None
    for _ in range(200):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(0.01)
    try:
        try:
            n = int(path.read_text(encoding="utf-8")) + 1
        except (OSError, ValueError):
            n = 1
        path.write_text(str(n), encoding="utf-8")
        return n
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock.unlink()
        except OSError:
            pass


def _load_steps(path: str) -> list:
    if not path:
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write_map(mapping) -> None:
    if not isinstance(mapping, dict):
        return
    for rel, text in mapping.items():
        dest = Path(str(rel))
        if not dest.is_absolute():
            dest = Path.cwd() / dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        body = text if isinstance(text, str) else str(text)
        dest.write_text(body, encoding="utf-8")


def _verbatim_reason(prompt: str) -> str:
    marker = "Reason (verbatim):"
    idx = prompt.find(marker)
    if idx < 0:
        return ""
    rest = prompt[idx + len(marker):]
    for line in rest.splitlines():
        if line.strip():
            return line
    return ""


def _emit(n: int, status: str, response: str = "worker-ok") -> None:
    payload = {
        "conversation_id": f"conv_{n}",
        "status": status,
        "response": response,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _log(path: str, row: dict) -> None:
    if not path:
        return
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    if not any(arg.startswith("--print=") for arg in sys.argv[1:]):
        sys.stderr.write("fake_agy_review: missing --print=\n")
        raise SystemExit(2)
    prompt = _prefixed("--print=")
    script_path = os.environ.get("FAKE_AGY_SCRIPT", "")
    steps = _load_steps(script_path)
    n = _bump(script_path + ".n") if script_path else 1
    step = steps[n - 1] if 0 <= n - 1 < len(steps) else {"do": "ok"}
    if not isinstance(step, dict):
        step = {"do": "ok"}
    do = str(step.get("do") or "ok")
    _log(
        os.environ.get("FAKE_AGY_LOG", ""),
        {
            "n": n,
            "pid": os.getpid(),
            "cwd": os.getcwd(),
            "rework": "REWORK " in prompt,
            "prompt_sha": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt": prompt,
        },
    )
    if do == "echo_reason":
        rel = str(step.get("rel") or "reason.txt")
        _write_map({rel: _verbatim_reason(prompt)})
        _emit(n, "ok")
        return
    _write_map(step.get("write"))
    if do == "hang":
        while True:
            time.sleep(3600)
    if do == "sleep":
        time.sleep(float(step.get("sec") or 0))
        _emit(n, "ok")
        return
    if do == "exit1":
        _emit(n, "error", "exit1")
        raise SystemExit(1)
    _emit(n, "ok")


if __name__ == "__main__":
    main()
