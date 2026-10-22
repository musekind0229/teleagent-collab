#!/usr/bin/env python3
"""Tool-evidence tests. Red until read_tool_evidence and the new payload keys exist."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
SAMPLE = SRC / "testdata" / "agy_transcript_full_sample.jsonl"

_BEARER = "abc123SECRETtoken456"
_KEY = "sk-THISISFAKEKEY1234567890"
_SOURCE_NOTE = (
    "tools are reconstructed from the agy session transcript "
    "(last 30 calls, outputs truncated)."
)
_UNAVAILABLE_NOTE = (
    "agy provides no tool evidence for this run; judge by artifacts and output. "
    "Do not reject merely because tool evidence is empty."
)
_REVIEW = {"round": 1, "max_redos": 1, "history": []}
_SNAP = {"artifacts": {}, "artifact_hash": "abc"}
_ELLIPSIS = "\u2026"
_OMIT = object()


def _fn():
    from execution_backend import agy_review as mod

    fn = getattr(mod, "read_tool_evidence", None)
    if not callable(fn):
        raise AssertionError("read_tool_evidence is missing from execution_backend.agy_review")
    return fn


def _transcript_path(home: Path, conversation_id: str) -> Path:
    return (
        home
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / conversation_id
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )


def _install(home: Path, conversation_id: str, text: str) -> dict[str, str]:
    dest = _transcript_path(home, conversation_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return {"HOME": str(home)}


def _sample_lines() -> list[dict | None]:
    rows: list[dict | None] = []
    for line in SAMPLE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            rows.append(None)
            continue
        rows.append(obj if isinstance(obj, dict) else None)
    return rows


def _payload(tools=_OMIT):
    from execution_backend.agy_review import build_payload

    kwargs = {"acceptance_text": "prose acceptance", "response_excerpt": "worker-ok"}
    if tools is not _OMIT:
        kwargs["tools"] = tools
    try:
        return build_payload(_REVIEW, _SNAP, **kwargs)
    except TypeError as exc:
        raise AssertionError(f"build_payload rejected the tools argument: {exc}") from exc


def _review_prompt(payload: dict) -> str:
    """Same request shape as LeadAdapterPlanner.decide_action, then the lead prompt."""
    from framework.app_service import TASK_REVIEW_HINT, split_task_musts, task_acceptance_criteria
    from lead_adapter.schema import build_lead_request, format_lead_request_prompt

    task = {
        "task_id": "t1",
        "title": "notes",
        "status": "running",
        "run_id": "agy_0123456789ab",
        "expected_artifacts": ["notes.md"],
        "inputs": {"instruction": "Write notes.md under /w/task"},
        "done_when": {"artifacts": ["notes.md"], "text": "notes exist"},
    }
    goal_must = ["stay in /w/task"]
    scoped, deferred = split_task_musts(musts=goal_must, task=task, siblings=[task])
    accept = task_acceptance_criteria(task)
    instruction = "Write notes.md under /w/task"
    extra = {
        "worker_request": payload,
        "review_scope": "task",
        "task_acceptance": accept,
        "goal_acceptance": {"text": "notes exist"},
        "goal_must": goal_must,
        "deferred_must": deferred,
        "current_task": {
            "task_id": "t1",
            "title": "notes",
            "expected_artifacts": ["notes.md"],
            "instruction": instruction,
        },
        "sibling_tasks": [
            {
                "task_id": "t1",
                "title": "notes",
                "status": "running",
                "expected_artifacts": ["notes.md"],
            }
        ],
        "allow_hint": TASK_REVIEW_HINT,
    }
    request = build_lead_request(
        kind="review",
        goal=instruction,
        authorized_scope=scoped,
        prohibitions=["leak secrets"],
        acceptance_criteria=accept,
        current_application={
            "goal_id": "g_te",
            "task_id": "t1",
            "run_id": task["run_id"],
            "review_scope": "task",
        },
        extra=extra,
    )
    hint = str((request.get("extra") or {}).get("allow_hint") or "")
    return format_lead_request_prompt(request, allow_hint=hint)


def _many(n: int, long_in: str, long_out: str) -> str:
    lines = [json.dumps({"step_index": 0, "type": "USER_INPUT", "status": "DONE", "content": "go"})]
    step = 1
    for i in range(1, n + 1):
        cmd = long_in if i == 39 else f"echo n={i:02d}"
        lines.append(json.dumps({
            "step_index": step,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": cmd, "Cwd": "/w/task"}}],
        }))
        step += 1
        content = long_out if i == 40 else f"The command exited with code 0.\nOutput:\nn={i:02d}\n"
        lines.append(json.dumps({
            "step_index": step,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "DONE",
            "content": content,
        }))
        step += 1
    return "\n".join(lines) + "\n"


class AgyToolEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agy-te-")
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def _read(self, conversation_id: str, text: str | None = None, **kwargs):
        env = {"HOME": str(self.home)}
        if text is not None:
            _install(self.home, conversation_id, text)
        return _fn()(env, conversation_id, **kwargs)

    def test_te1_parse_sample(self) -> None:
        """asserts sample tool order, FIFO pairing, exit code 2, and a skipped malformed line"""
        rows = _sample_lines()
        self.assertEqual(sum(row is None for row in rows), 1)
        dual = next(row for row in rows if isinstance(row, dict) and len(row.get("tool_calls") or []) == 2)
        self.assertEqual(
            [call["name"] for call in dual["tool_calls"]],
            ["run_command", "view_file"],
        )
        self.assertEqual(dual["tool_calls"][0]["args"]["CommandLine"], "wc -c notes.md")
        final = rows[-1]
        self.assertIsInstance(final, dict)
        self.assertEqual(final["type"], "PLANNER_RESPONSE")
        self.assertTrue(final.get("content"))
        self.assertNotIn("tool_calls", final)
        tools = self._read("conv_sample", SAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(len(tools), 4)
        self.assertEqual(
            [row["tool"] for row in tools],
            ["run_command", "run_command", "view_file", "run_command"],
        )
        for row in tools:
            self.assertTrue({"tool", "status", "input", "output"} <= set(row))
            self.assertTrue(set(row) <= {"tool", "status", "input", "output", "exit_code"})
            self.assertIsInstance(row["input"], dict)
            self.assertIsInstance(row["output"], str)
        self.assertEqual(
            [row["status"] for row in tools],
            ["completed", "completed", "completed", "error"],
        )
        self.assertEqual(tools[0]["input"]["CommandLine"], "ls -la")
        self.assertEqual(tools[0]["input"]["Cwd"], "/w/task")
        self.assertEqual(tools[0]["input"]["WaitMsBeforeAsync"], 5000)
        self.assertEqual(tools[0]["exit_code"], 0)
        self.assertIn("total 0", tools[0]["output"])
        self.assertEqual(tools[1]["input"]["CommandLine"], "wc -c notes.md")
        self.assertIn("42 notes.md", tools[1]["output"])
        self.assertNotIn("hello from notes", tools[1]["output"])
        self.assertEqual(tools[1]["exit_code"], 0)
        self.assertEqual(set(tools[2]), {"tool", "status", "input", "output"})
        self.assertEqual(tools[2]["input"]["AbsolutePath"], "/w/task/notes.md")
        self.assertIn("hello from notes", tools[2]["output"])
        self.assertNotIn("42 notes.md", tools[2]["output"])
        self.assertNotIn("exit_code", tools[2])
        self.assertEqual(tools[3]["exit_code"], 2)
        self.assertIn("The command exited with code 2.", tools[3]["output"])
        self.assertEqual(tools[3]["input"]["Cwd"], "/w/task")

    def test_te2_redaction(self) -> None:
        """asserts neither the bearer token nor the api_key survives anywhere in the result"""
        raw = SAMPLE.read_text(encoding="utf-8")
        self.assertIn(_BEARER, raw)
        self.assertIn(_KEY, raw)
        tools = self._read("conv_redact", raw)
        blob = json.dumps(tools, ensure_ascii=False, default=str)
        leaked = _BEARER in blob or _KEY in blob
        self.assertFalse(leaked, "redaction left a bearer token or api_key in tool evidence")
        self.assertGreaterEqual(blob.count("[redacted]"), 2)

    def test_te3_caps(self) -> None:
        """asserts the last 30 of 40 calls are kept and long input/output strings are capped"""
        long_in = "HEADMARKER-" + ("x" * 2000) + "-TAILMARKER"
        long_out = "HEAD-" + ("y" * 3000) + "-OUTTAIL"
        output_cap = 80
        input_cap = 40
        tools = self._read(
            "conv_caps",
            _many(40, long_in, long_out),
            output_cap=output_cap,
            input_cap=input_cap,
        )
        cmds = [row["input"]["CommandLine"] for row in tools]
        self.assertEqual(len(tools), 30)
        self.assertNotIn("echo n=01", cmds)
        self.assertNotIn("echo n=10", cmds)
        self.assertEqual(cmds[0], "echo n=11")
        self.assertEqual(tools[0]["output"], "The command exited with code 0.\nOutput:\nn=11\n")
        self.assertEqual(cmds[-1], "echo n=40")
        capped_in = cmds[-2]
        self.assertEqual(capped_in, long_in[:input_cap])
        self.assertEqual(len(capped_in), input_cap)
        self.assertTrue(capped_in.startswith("HEADMARKER-"))
        self.assertNotIn("TAILMARKER", capped_in)
        self.assertEqual(tools[-2]["input"]["Cwd"], "/w/task")
        capped_out = tools[-1]["output"]
        self.assertEqual(capped_out, _ELLIPSIS + long_out[-output_cap:])
        self.assertTrue(capped_out.startswith(_ELLIPSIS))
        self.assertNotIn("HEAD-", capped_out)

    def test_te4_unavailable(self) -> None:
        """asserts no HOME, empty id, id with ../, or a missing file is None, and zero calls is []"""
        fn = _fn()
        self.assertIsNone(fn(None, "conv_ok"))
        self.assertIsNone(fn({}, "conv_ok"))
        self.assertIsNone(fn({"HOME": ""}, "conv_ok"))
        self.assertIsNone(fn({"PATH": "/usr/bin"}, "conv_ok"))
        self.assertIsNone(fn({"HOME": str(self.home)}, ""))
        self.assertIsNone(fn({"HOME": str(self.home)}, "../conv"))
        self.assertIsNone(fn({"HOME": str(self.home)}, "conv/../secret"))
        self.assertIsNone(fn({"HOME": str(self.home)}, "conv_missing"))
        zero = "\n".join([
            json.dumps({"type": "USER_INPUT", "status": "DONE", "content": "hi"}),
            json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "content": "no tools"}),
        ]) + "\n"
        self.assertEqual(self._read("conv_zero", zero), [])

    def test_te5_build_payload_with_tools(self) -> None:
        """asserts tools are copied with an agy_transcript note and no empty-evidence text"""
        from execution_backend import agy_review as mod

        tools = [{
            "tool": "run_command",
            "status": "completed",
            "input": {"CommandLine": "wc -c notes.md", "Cwd": "/w/task"},
            "output": "42 notes.md\n",
            "exit_code": 0,
        }]
        payload = _payload(tools)
        self.assertEqual(payload["tools"], tools)
        self.assertEqual(payload["tool_evidence"], {"source": "agy_transcript", "calls": 1})
        self.assertEqual(getattr(mod, "TOOL_EVIDENCE_SOURCE_NOTE", None), _SOURCE_NOTE)
        self.assertEqual(payload["review_note"], _SOURCE_NOTE)
        self.assertNotIn("no tool evidence", json.dumps(payload))
        self.assertEqual(payload["backend"], "antigravity.cli_v1")
        empty = _payload([])
        self.assertEqual(empty["tools"], [])
        self.assertEqual(empty["tool_evidence"], {"source": "agy_transcript", "calls": 0})
        self.assertEqual(empty["review_note"], _SOURCE_NOTE)
        self.assertNotIn("no tool evidence", json.dumps(empty))

    def test_te6_build_payload_unavailable(self) -> None:
        """asserts omitted tools and tools=None use the unavailable note"""
        from execution_backend import agy_review as mod

        for label, tools in (("omitted", _OMIT), ("none", None)):
            with self.subTest(form=label):
                payload = _payload(tools)
                self.assertEqual(payload.get("tools"), [])
                self.assertEqual(payload.get("tool_evidence"), {"source": "unavailable", "calls": 0})
                note = payload.get("review_note")
                self.assertEqual(getattr(mod, "TOOL_EVIDENCE_UNAVAILABLE_NOTE", None), _UNAVAILABLE_NOTE)
                self.assertEqual(note, _UNAVAILABLE_NOTE)
                self.assertIn("judge by artifacts and output", str(note))
                self.assertIn("Do not reject merely because tool evidence is empty", str(note))

    def test_te7_prompt_carries_note_and_command(self) -> None:
        """asserts the lead prompt contains the unavailable note and a sample tool command"""
        for tools in (_OMIT, None):
            prompt_u = _review_prompt(_payload(tools))
            self.assertTrue(
                _UNAVAILABLE_NOTE in prompt_u,
                "lead prompt is missing TOOL_EVIDENCE_UNAVAILABLE_NOTE",
            )
        parsed = self._read("conv_prompt", SAMPLE.read_text(encoding="utf-8"))
        prompt_a = _review_prompt(_payload(parsed))
        self.assertTrue(
            "wc -c notes.md" in prompt_a,
            "lead prompt is missing the sample tool command",
        )

def _round2_transcript() -> str:
    rows = [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "rework under /w/task"},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "tool_calls": [
            {"name": "run_command", "args": {"CommandLine": "wc -c other.md", "Cwd": "/w/task"}},
        ]},
        {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE", "content": "The command exited with code 0.\nOutput:\n7 other.md\n"},
        {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "updated other.md under /w/task"},
    ]
    return "".join(json.dumps(row) + "\n" for row in rows)


@unittest.skipIf(os.name == "nt", "POSIX shell shim; fake agy is spawned through /bin/sh")
class AgyToolEvidenceBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        from test_agy_lead_review import AgyLeadReviewBackendTests

        self.h = AgyLeadReviewBackendTests("test_t13_list_pending_alone")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def _arm(self, steps: list):
        from test_agy_lead_review import ACCEPTANCE

        pool = self.h._pool()
        be = self.h._backend(pool_path=pool)
        be.enable_lead_review()
        self.h._steps(steps)
        started = self.h._start(be, charter={"acceptance": ACCEPTANCE})
        self.assertTrue(started.get("ok"), started)
        run_id = started["run_id"]
        rows = self.h._wait_pending(be, run_id)
        self.assertEqual(len(rows), 1)
        return be, run_id, rows

    def _written(self) -> dict[str, str]:
        brain = self.h.root / "homeA" / ".gemini" / "antigravity-cli" / "brain"
        found: dict[str, str] = {}
        if not brain.is_dir():
            return found
        for path in brain.glob("*/.system_generated/logs/transcript_full.jsonl"):
            found[path.parents[2].name] = path.read_text(encoding="utf-8")
        return found

    def test_te8_backend_transcript_tools(self) -> None:
        """asserts the review payload tools come from the fake transcript"""
        from test_agy_lead_review import BODY

        _be, _run_id, rows = self._arm([
            {"do": "ok", "write": {"report.md": BODY}, "transcript": str(SAMPLE)},
        ])
        written = self._written()
        self.assertIn("conv_1", written)
        self.assertIn("wc -c notes.md", written["conv_1"])
        payload = rows[0]["payload"]
        blob = json.dumps(payload.get("tools"), ensure_ascii=False, default=str)
        self.assertTrue(payload.get("tools"), "payload.tools is empty")
        self.assertIn("wc -c notes.md", blob)
        self.assertEqual((payload.get("tool_evidence") or {}).get("source"), "agy_transcript")

    def test_te9_backend_without_transcript(self) -> None:
        """asserts a run with no transcript is unavailable and does not invent tools"""
        from execution_backend import agy_review as mod
        from test_agy_lead_review import BODY

        _be, _run_id, rows = self._arm([{"do": "ok", "write": {"report.md": BODY}}])
        payload = rows[0]["payload"]
        self.assertEqual(payload.get("tools"), [])
        self.assertEqual(payload.get("tool_evidence"), {"source": "unavailable", "calls": 0})
        self.assertEqual(payload.get("review_note"), getattr(mod, "TOOL_EVIDENCE_UNAVAILABLE_NOTE", None))
        self.assertEqual(payload.get("review_note"), _UNAVAILABLE_NOTE)

    def test_te10_rework_uses_round_transcript(self) -> None:
        """asserts round-2 tools come only from that round's transcript"""
        from test_agy_lead_review import BODY

        round2 = self.h.root / "round2.jsonl"
        text2 = _round2_transcript()
        self.assertNotIn("wc -c notes.md", text2)
        round2.write_text(text2, encoding="utf-8")
        be, run_id, rows = self._arm([
            {"do": "ok", "write": {"report.md": BODY}, "transcript": str(SAMPLE)},
            {"do": "ok", "write": {"report.md": BODY}, "transcript": str(round2)},
        ])
        rid1 = rows[0]["request_id"]
        failed = be.resolve_decision(rid1, verdict="fail", reason="redo the notes", answers=[{"by": "lead"}])
        self.assertEqual(failed["controller_state"], "running")
        self.assertEqual(failed["round"], 2)
        second = self.h._wait_pending(be, run_id)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["request_id"], f"agyrev:{run_id}:r2")
        written = self._written()
        self.assertEqual(set(written), {"conv_1", "conv_2"})
        self.assertIn("wc -c notes.md", written["conv_1"])
        self.assertNotIn("wc -c other.md", written["conv_1"])
        self.assertIn("wc -c other.md", written["conv_2"])
        self.assertNotIn("wc -c notes.md", written["conv_2"])
        payload = second[0]["payload"]
        blob = json.dumps(payload.get("tools"), ensure_ascii=False, default=str)
        self.assertIn("wc -c other.md", blob)
        self.assertNotIn("wc -c notes.md", blob)
        self.assertEqual((payload.get("tool_evidence") or {}).get("source"), "agy_transcript")


if __name__ == "__main__":
    unittest.main()
