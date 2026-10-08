#!/usr/bin/env python3
"""Fake agy CLI for lead-review tests. Never calls a model or the network.

argv (agy shape): ``--output-format=json --model=<m> [--dangerously-skip-permissions] --print=<prompt>``.

Env:
  FAKE_AGY_SCRIPT  JSON list of steps, consumed in order across processes.
                   Counter file is ``<script>.n``.
  FAKE_AGY_LOG     jsonl, one object per invocation.

Steps: {"do": "ok"|"exit1"|"sleep"|"hang"|"echo_reason", "write": {rel: text},
"sec": N, "rel": "x.md", "transcript": "<jsonl path>"|true}. ``echo_reason``
writes the line after "Reason (verbatim):" into ``rel``. When ``transcript``
is present, that file (or a one-call default when the value is true) is copied
to ``$HOME/.gemini/antigravity-cli/brain/<conversation_id>/.system_generated/logs/transcript_full.jsonl``
before the JSON result is printed, using the same conversation_id. Absent key:
behaviour unchanged.
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


def _default_transcript() -> str:
    """One run_command plus its GENERIC output. Paths stay under /w/task."""
    rows = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "content": "<USER_REQUEST>work under /w/task</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "tool_calls": [
                {
                    "name": "run_command",
                    "args": {"CommandLine": "echo hello", "Cwd": "/w/task"},
                }
            ],
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "DONE",
            "content": "The command exited with code 0.\nOutput:\nhello\n",
        },
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "done",
        },
    ]
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def _copy_transcript(step: dict, conversation_id: str) -> None:
    """Copy a step transcript into the agy brain log for this conversation.

    No-op when the step has no ``transcript`` key. ``true`` writes a one-call
    default; any other value is a path to a jsonl file.
    """
    if "transcript" not in step:
        return
    home = (os.environ.get("HOME") or os.environ.get("USERPROFILE") or "").strip()
    if not home or not conversation_id:
        return
    spec = step.get("transcript")
    if spec is True or spec == "true":
        text = _default_transcript()
    else:
        text = Path(str(spec)).read_text(encoding="utf-8")
    dest = (
        Path(home)
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / conversation_id
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")


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
    conversation_id = f"conv_{n}"
    if do == "echo_reason":
        rel = str(step.get("rel") or "reason.txt")
        _write_map({rel: _verbatim_reason(prompt)})
    else:
        _write_map(step.get("write"))
    # Before any JSON on stdout, including exit1. Hang never prints JSON.
    _copy_transcript(step, conversation_id)
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
