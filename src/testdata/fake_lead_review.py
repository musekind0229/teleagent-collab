#!/usr/bin/env python3
"""Fake lead CLI for lead-review tests. Never calls grok, codex, or the network.

Grok shape (default): read ``-p <prompt>`` and write one JSON object to stdout.
Codex shape (``FAKE_LEAD_SHAPE=codex`` or argv ``--codex``): read the prompt on
stdin and write the decision to the ``-o`` path (codex ``exec -o last.json -``).

The prompt is the text from ``format_lead_request_prompt``. application_id and
context_summary are copied from the Full request JSON, byte-for-byte.

Env:
  FAKE_LEAD_SCRIPT  JSON list of review steps, consumed in order. Counter file
                    is ``<script>.n``. Plan calls do not consume a step.
  FAKE_LEAD_LOG     jsonl ``{"n", "kind", "pid"}`` per invocation.
  FAKE_LEAD_SHAPE   ``codex`` selects the codex argv/stdin/``-o`` convention.

Review steps: ``pass:<reason>``, ``fail:<reason>``, ``invalid_json``,
``mismatch_id``, ``sleep:<sec>``, ``block_until:<path>``,
``block_until:<path>|<sec>``, ``block_until_fail:<path>|<reason>``,
``exit1_401``. ``block_until`` still waits 30s then passes when no ``|sec``
is given. ``block_until_fail`` waits up to 120s, then returns fail.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _flag(name: str) -> str:
    argv = sys.argv[1:]
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
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


def _extract_request(prompt: str) -> dict:
    marker = "Full request JSON:"
    idx = prompt.rfind(marker)
    decoder = json.JSONDecoder()
    if idx >= 0:
        blob = prompt[idx + len(marker):].lstrip()
        try:
            obj, _end = decoder.raw_decode(blob)
            if isinstance(obj, dict) and "application_id" in obj:
                return obj
        except json.JSONDecodeError:
            pass
    found = None
    i = 0
    while True:
        j = prompt.find("{", i)
        if j < 0:
            break
        try:
            obj, _end = decoder.raw_decode(prompt[j:])
        except json.JSONDecodeError:
            i = j + 1
            continue
        if isinstance(obj, dict) and "application_id" in obj and "kind" in obj:
            found = obj
        i = j + 1
    if found is not None:
        return found
    app_id = ""
    summary = ""
    kind = "review"
    for line in prompt.splitlines():
        if line.startswith("application_id: "):
            app_id = line[len("application_id: "):]
        elif line.startswith("context_summary: "):
            summary = line[len("context_summary: "):]
    return {
        "application_id": app_id,
        "context_summary": summary,
        "kind": kind,
        "acceptance_criteria": {},
        "task_goal": "",
    }


def _exact(req: dict, key: str) -> str:
    val = req.get(key)
    if isinstance(val, str):
        return val
    if val is None:
        return ""
    return str(val)


def _log(path: str, row: dict) -> None:
    if not path:
        return
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _emit(text: str, *, codex: bool, out_path: str) -> None:
    body = text if text.endswith("\n") else text + "\n"
    if codex and out_path:
        Path(out_path).write_text(body, encoding="utf-8")
        return
    sys.stdout.write(body)
    sys.stdout.flush()


def _decision(req: dict, *, verdict: str | None = None, decision: str | None = None, reason: str) -> str:
    body = {
        "application_id": _exact(req, "application_id"),
        "context_summary": _exact(req, "context_summary"),
        "reason": reason,
    }
    if verdict is not None:
        body["verdict"] = verdict
    if decision is not None:
        body["decision"] = decision
    return json.dumps(body, ensure_ascii=False)


def _plan(req: dict) -> str:
    acc = req.get("acceptance_criteria")
    arts: list[str] = []
    if isinstance(acc, dict):
        raw = acc.get("artifacts") or []
        if isinstance(raw, str):
            raw = [raw]
        if isinstance(raw, list):
            arts = [a.strip() for a in raw if isinstance(a, str) and a.strip()]
    goal = req.get("task_goal")
    instruction = goal.strip() if isinstance(goal, str) and goal.strip() else "Complete the requested outcome"
    body = {
        "application_id": _exact(req, "application_id"),
        "context_summary": _exact(req, "context_summary"),
        "summary": "single task",
        "tasks": [
            {
                "task_key": "implement",
                "title": "implementation",
                "instruction": instruction,
                "depends_on": [],
                "artifacts": arts,
            }
        ],
    }
    return json.dumps(body, ensure_ascii=False)


def _split_step(step: str) -> tuple[str, str]:
    for name in ("block_until_fail", "pass", "fail", "sleep", "block_until"):
        prefix = name + ":"
        if step.startswith(prefix):
            return name, step[len(prefix):]
    return step, ""


def _wait_gate(path: str, seconds: float) -> None:
    gate = Path(path)
    deadline = time.time() + seconds
    while not gate.exists():
        if time.time() >= deadline:
            return
        time.sleep(0.05)


def _read_prompt(*, codex: bool) -> str:
    if codex:
        if sys.stdin.isatty():
            return _flag("-p")
        return sys.stdin.read()
    return _flag("-p")


def main() -> None:
    codex = os.environ.get("FAKE_LEAD_SHAPE", "").strip().lower() == "codex" or "--codex" in sys.argv[1:]
    prompt = _read_prompt(codex=codex)
    req = _extract_request(prompt)
    kind = str(req.get("kind") or "review")
    script_path = os.environ.get("FAKE_LEAD_SCRIPT", "")
    log_path = os.environ.get("FAKE_LEAD_LOG", "")
    if log_path:
        n = _bump(log_path + ".calls")
    elif script_path:
        n = _bump(script_path + ".calls")
    else:
        n = 1
    _log(log_path, {"n": n, "kind": kind, "pid": os.getpid()})
    out_path = _flag("-o")
    if kind == "plan":
        _emit(_plan(req), codex=codex, out_path=out_path)
        return
    if kind == "permission":
        _emit(
            _decision(req, decision="once", reason="in scope"),
            codex=codex,
            out_path=out_path,
        )
        return
    steps = _load_steps(script_path)
    index = _bump(script_path + ".n") if script_path else 1
    step = steps[index - 1] if 0 <= index - 1 < len(steps) else "pass:ok"
    if not isinstance(step, str):
        step = "pass:ok"
    op, arg = _split_step(step)
    if op == "sleep":
        time.sleep(float(arg or "0"))
        op, arg = "pass", "slept"
    elif op == "block_until":
        # ``path`` alone keeps the historical 30s wait. ``path|sec`` is optional.
        path_arg, sep, extra = arg.partition("|")
        wait = 30.0
        if sep:
            try:
                wait = float(extra)
            except ValueError:
                wait = 30.0
        _wait_gate(path_arg, wait)
        op, arg = "pass", "unblocked"
    elif op == "block_until_fail":
        path_arg, sep, reason = arg.partition("|")
        _wait_gate(path_arg, 120.0)
        op, arg = "fail", (reason if sep else "") or "fail"
    if op == "invalid_json":
        _emit("not json {{{", codex=codex, out_path=out_path)
        return
    if op == "mismatch_id":
        wrong = dict(req)
        wrong["application_id"] = _exact(req, "application_id") + "_MISMATCH"
        _emit(_decision(wrong, verdict="pass", reason="wrong id"), codex=codex, out_path=out_path)
        return
    if op == "exit1_401":
        sys.stderr.write("401 unauthorized\n")
        sys.stderr.flush()
        raise SystemExit(1)
    if op == "fail":
        _emit(
            _decision(req, verdict="fail", reason=arg or "fail"),
            codex=codex,
            out_path=out_path,
        )
        return
    _emit(
        _decision(req, verdict="pass", reason=arg or "pass"),
        codex=codex,
        out_path=out_path,
    )


if __name__ == "__main__":
    main()
