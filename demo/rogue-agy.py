"""Scripted stand-in for agy, used only by demo/agy-review-demo.ps1 -Mode rogue.

It is not a worker. Only a ``--print`` call counts as a dispatch; probes such
as ``--version`` or ``models`` answer and exit without touching files. Dispatch
n runs steps[min(n, last)] from AGY_FAKE_STEPS_FILE: write the listed files into
the task workspace (cwd) and print agy's JSON result line.
"""
import json
import os
import sys
from pathlib import Path

if not any(a == "-p" or a.startswith("--print") for a in sys.argv[1:]):
    sys.stdout.write("1.3.4\n")
    raise SystemExit(0)

state = Path(os.environ["AGY_FAKE_STATE"])
n = int(state.read_text()) if state.exists() else 0
state.write_text(str(n + 1))
steps = json.loads(Path(os.environ["AGY_FAKE_STEPS_FILE"]).read_text(encoding="utf-8"))
step = steps[min(n, len(steps) - 1)]
for name, body in (step.get("write") or {}).items():
    p = Path.cwd() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
sys.stdout.write(json.dumps({"conversation_id": "demo_%d" % n, "status": "ok",
                             "response": step.get("say", "done"),
                             "usage": {"input_tokens": 1, "output_tokens": 1}},
                            ensure_ascii=False) + "\n")
