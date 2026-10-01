#!/usr/bin/env python3
"""Structured worker contract reaches the agy prompt and fails closed."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from charter import build_instruction  # noqa: E402
from execution_backend.antigravity_cli_v1 import (  # noqa: E402
    AntigravityCliExecutionBackend,
    build_agy_prompt,
)
from framework.app_service import (  # noqa: E402
    AppError,
    CollabApplication,
    build_planning_request,
    validate_plan,
    worker_charter_for_task,
)
from framework.contract_render import (  # noqa: E402
    ContractRenderError,
    contract_fingerprint,
    contract_fields_present,
    normalize_worker_contract,
    render_contract_section,
)

_FAKE_AGY = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

argv_path = os.environ.get("AGY_FAKE_ARGV", "")
if argv_path:
    Path(argv_path).write_text(json.dumps(sys.argv), encoding="utf-8")
art = os.environ.get("AGY_FAKE_ARTIFACT", "")
if art:
    p = Path(art)
    if not p.is_absolute():
        p = Path.cwd() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(os.environ.get("AGY_FAKE_ARTIFACT_BODY", "hello from agy fake\n"), encoding="utf-8")
payload = {
    "conversation_id": "conv_contract",
    "status": "ok",
    "response": "wrote artifact",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
sys.stdout.write(json.dumps(payload))
sys.stdout.write("\n")
raise SystemExit(0)
'''

_OLD_TAIL = "Stay inside the working directory. When finished, stop. Do not wait for further input."
_PINNED_TAIL = (
    "Write only inside the working directory. Outside it, you may only read the pinned "
    "external inputs listed above. When finished, stop. Do not wait for further input."
)


def _write_fake_agy(dirpath: str | Path) -> Path:
    root = Path(dirpath)
    if os.name == "nt":
        script = root / "fake-agy.py"
        script.write_text(_FAKE_AGY, encoding="utf-8")
        cmd = root / "fake-agy.cmd"
        cmd.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return cmd
    path = root / "fake-agy"
    path.write_text(_FAKE_AGY, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _plan_request() -> dict:
    return {"application_id": "plan_contract", "context_summary": "ctx-contract"}


def _plan(tasks: list[dict]) -> dict:
    return {
        "application_id": "plan_contract",
        "context_summary": "ctx-contract",
        "summary": "contract plan",
        "tasks": tasks,
    }


def _task(**extra) -> dict:
    row = {
        "task_key": "implement",
        "title": "Implement",
        "instruction": "do the task",
        "depends_on": [],
        "artifacts": ["inside.txt"],
    }
    row.update(extra)
    return row


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class TestNormalizeAndRender(unittest.TestCase):
    def test_docstring_says_prompt_is_not_a_sandbox(self):
        import framework.contract_render as mod

        text = (mod.__doc__ or "") + (render_contract_section.__doc__ or "")
        self.assertIn("not an OS sandbox", text)

    def test_error_code(self):
        err = ContractRenderError("bad")
        self.assertIsInstance(err, ValueError)
        self.assertEqual(err.code, "contract_render_error")

    def test_acceptance_mapping_keeps_must_not(self):
        charter = {
            "must_not": ["NEVER_DELETE_SOURCE"],
            "acceptance": {"text": "ACCEPTANCE_MUST_SURVIVE", "allow_aigc_marks": False},
            "task_kind": "file_task",
        }
        normalized = normalize_worker_contract(charter)
        self.assertEqual(normalized["must_not"], ["NEVER_DELETE_SOURCE"])
        self.assertEqual(normalized["acceptance"], "ACCEPTANCE_MUST_SURVIVE")
        self.assertEqual(normalized["task_kind"], "file_task")
        self.assertIn("must_not", contract_fields_present(normalized))
        self.assertIn("acceptance", contract_fields_present(normalized))
        prompt = build_agy_prompt(instruction="DO_TASK", charter=charter)
        self.assertIn("DO_TASK", prompt)
        self.assertIn("NEVER_DELETE_SOURCE", prompt)
        self.assertIn("ACCEPTANCE_MUST_SURVIVE", prompt)
        self.assertIn("Must not:", prompt)
        self.assertIn("Acceptance:", prompt)
        self.assertIn(_OLD_TAIL, prompt)

    def test_bad_acceptance_and_must_not_raise(self):
        with self.assertRaises(ContractRenderError) as acc:
            build_agy_prompt(
                instruction="DO_TASK",
                charter={"acceptance": {"text": 5}, "must_not": ["KEEP"]},
            )
        self.assertEqual(acc.exception.code, "contract_render_error")
        with self.assertRaises(ContractRenderError):
            build_agy_prompt(instruction="DO_TASK", charter={"must_not": 5})
        with self.assertRaises(ContractRenderError):
            normalize_worker_contract({"acceptance": {"text": 5, "mystery": 1}})

    def test_each_single_field_survives_nonempty_instruction(self):
        digest = "ab" * 32
        cases = [
            ({"forbidden_tools": ["powershell"]}, "powershell", False),
            ({"input_files": ["notes.md"]}, "notes.md", False),
            (
                {"done_when": {"artifacts": ["out.txt"], "text": "OUT_READY"}},
                "OUT_READY",
                False,
            ),
            ({"must_not": ["NEVER_DELETE_SOURCE"]}, "NEVER_DELETE_SOURCE", False),
            (
                {"external_inputs": [{"path": "/tmp/pinned-contract.bin", "sha256": digest}]},
                "/tmp/pinned-contract.bin",
                True,
            ),
        ]
        for charter, needle, external in cases:
            prompt = build_agy_prompt(instruction="DO_TASK", charter=charter)
            self.assertIn("DO_TASK", prompt, charter)
            self.assertIn(needle, prompt, charter)
            if external:
                self.assertIn(digest, prompt)
                self.assertNotIn("Stay inside the working directory", prompt)
                self.assertIn(_PINNED_TAIL, prompt)
                self.assertIn("Pinned external inputs", prompt)
            else:
                self.assertIn(_OLD_TAIL, prompt)
        done = build_agy_prompt(
            instruction="DO_TASK",
            charter={"done_when": {"artifacts": ["out.txt"], "text": "OUT_READY"}},
        )
        self.assertIn("out.txt", done)
        self.assertIn("Done when:", done)

    def test_unknown_keys_and_fingerprint(self):
        rollback = {"steps": ["undo"]}
        charter = {
            "must_not": ["NEVER_DELETE_SOURCE"],
            "task_kind": "file_task",
            "network_allow": ["example.com"],
            "rollback": rollback,
            "agy_auto_approve": False,
        }
        normalized = normalize_worker_contract(charter)
        self.assertEqual(normalized["network_allow"], ["example.com"])
        self.assertIs(normalized["rollback"], rollback)
        self.assertIs(normalized["agy_auto_approve"], False)
        digest = contract_fingerprint(normalized)
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, contract_fingerprint(normalize_worker_contract(dict(charter))))
        self.assertNotEqual(
            digest,
            contract_fingerprint(normalize_worker_contract({"must_not": ["OTHER"]})),
        )
        self.assertEqual(render_contract_section({}), "")

    def test_build_instruction_accepts_acceptance_mapping(self):
        text = build_instruction(
            {
                "goal": "G",
                "must": [],
                "must_not": ["NEVER_DELETE_SOURCE"],
                "acceptance": {"text": "ACCEPT_MAP"},
                "allow_paths": [],
            }
        )
        self.assertIn("ACCEPT_MAP", text)
        self.assertIn("NEVER_DELETE_SOURCE", text)
        with self.assertRaises(ContractRenderError):
            build_instruction(
                {
                    "goal": "G",
                    "must": [],
                    "must_not": [],
                    "acceptance": {"text": 5},
                    "allow_paths": [],
                }
            )

    def test_legacy_done_when_string_and_list(self):
        as_text = normalize_worker_contract({"done_when": "ship the file"})
        self.assertEqual(as_text["done_when"], {"text": "ship the file"})
        as_list = normalize_worker_contract({"done_when": ["out/x.md"]})
        self.assertEqual(as_list["done_when"]["artifacts"], ["out/x.md"])
        with self.assertRaises(ContractRenderError):
            normalize_worker_contract({"done_when": ["../OUTSIDE_WORKSPACE.txt"]})


class TestDispatchFailsClosed(unittest.TestCase):
    def test_bad_contract_does_not_spawn(self):
        with tempfile.TemporaryDirectory() as td:
            be = AntigravityCliExecutionBackend(bin_path=str(Path(td) / "missing-agy"))
            with patch("execution_backend.antigravity_cli_v1.subprocess.Popen") as popen:
                with patch.object(
                    AntigravityCliExecutionBackend,
                    "_start_run_locked",
                    wraps=be._start_run_locked,
                ) as locked:
                    started = be.start_run(
                        title="t",
                        directory=td,
                        instruction="DO_TASK",
                        charter={"must_not": 5, "acceptance": {"text": "ACCEPTANCE_MUST_SURVIVE"}},
                    )
            popen.assert_not_called()
            locked.assert_not_called()
            self.assertFalse(started["ok"])
            self.assertTrue(started["error"].startswith("contract_render_error:"))
            self.assertNotIn("run_id", started)

    def test_patched_renderer_fails_task_without_start_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = _write_fake_agy(root)
            argv_path = root / "argv.json"
            env = os.environ.copy()
            env.pop("AGY_AUTO_APPROVE", None)
            env.pop("COLLAB_AGY_AUTO_APPROVE", None)
            env["AGY_FAKE_ARGV"] = str(argv_path)
            env["AGY_FAKE_ARTIFACT"] = "delivery.txt"
            be = AntigravityCliExecutionBackend(bin_path=str(fake), environ=env, timeout_sec=5)
            app = CollabApplication(root / "app", backend=be)
            opened = app.submit(
                {
                    "idempotency_key": "render-boom",
                    "goal": "DO_TASK",
                    "acceptance": {"artifacts": ["delivery.txt"], "text": "ACCEPTANCE_MUST_SURVIVE"},
                    "boundaries": {"must": ["write the file"], "must_not": ["NEVER_DELETE_SOURCE"]},
                }
            )
            with patch(
                "framework.contract_render.render_contract_section",
                side_effect=ContractRenderError("boom"),
            ):
                with patch.object(AntigravityCliExecutionBackend, "start_run") as start:
                    outcome = app.coordinator.process_goal(opened["goal_id"])
            start.assert_not_called()
            self.assertFalse(argv_path.exists())
            status = app.status(opened["goal_id"])
            self.assertEqual(status["state"], "failed", outcome)
            error = status["tasks"][0]["result"]["error"]
            self.assertTrue(error.startswith("contract_render_error:"), error)
            self.assertIn("boom", error)


class TestPlanDoneWhen(unittest.TestCase):
    def _raises(self, row: dict) -> AppError:
        with self.assertRaises(AppError) as caught:
            validate_plan(_plan([row]), _plan_request())
        self.assertEqual(caught.exception.code, "invalid_plan")
        return caught.exception

    def test_rejects_bad_shapes_and_paths(self):
        for bad in ("just text", ["a.txt"], 5, 1.5, True):
            self._raises(_task(done_when=bad))
        self._raises(_task(done_when={"artifacts": ["inside.txt"], "nope": 1}))
        self._raises(_task(done_when={"artifacts": ["inside.txt"], "text": 5}))
        self._raises(_task(done_when={"text": None}))
        for bad_path in (
            "../OUTSIDE_WORKSPACE.txt",
            "/abs",
            "C:/x",
            "..\\x",
            "foo\\..\\bar",
            ".",
            "",
        ):
            self._raises(_task(done_when={"artifacts": [bad_path]}))

    def test_subset_and_scoped_text(self):
        with self.assertRaises(AppError) as caught:
            validate_plan(
                _plan([_task(artifacts=["a.txt", "b.txt"], done_when={"artifacts": ["c.txt"]})]),
                _plan_request(),
            )
        self.assertEqual(caught.exception.code, "invalid_plan")
        clean = validate_plan(
            _plan(
                [
                    _task(
                        artifacts=["a.txt", "b.txt"],
                        done_when={"artifacts": ["a.txt"], "text": "scoped"},
                    )
                ]
            ),
            _plan_request(),
        )
        row = clean["tasks"][0]
        self.assertEqual(row["artifacts"], ["a.txt", "b.txt"])
        self.assertEqual(row["done_when"]["artifacts"], ["a.txt"])
        self.assertEqual(row["done_when"]["text"], "scoped")

    def test_missing_artifacts_uses_validated_done_when(self):
        row = _task(done_when={"artifacts": ["from_done.txt"], "text": "via done"})
        del row["artifacts"]
        clean = validate_plan(_plan([row]), _plan_request())
        self.assertEqual(clean["tasks"][0]["artifacts"], ["from_done.txt"])
        self.assertEqual(clean["tasks"][0]["done_when"]["text"], "via done")
        bad = _task(done_when={"artifacts": ["../OUTSIDE_WORKSPACE.txt"]})
        del bad["artifacts"]
        self._raises(bad)

    def test_two_tasks_do_not_share_done_when(self):
        clean = validate_plan(
            _plan(
                [
                    _task(
                        task_key="a",
                        title="A",
                        instruction="ia",
                        artifacts=["a.txt"],
                        done_when={"artifacts": ["a.txt"], "text": "TEXT_A"},
                    ),
                    _task(
                        task_key="b",
                        title="B",
                        instruction="ib",
                        artifacts=["b.txt"],
                        done_when={"artifacts": ["b.txt"], "text": "TEXT_B"},
                    ),
                ]
            ),
            _plan_request(),
        )
        first, second = clean["tasks"]
        self.assertEqual(first["done_when"], {"artifacts": ["a.txt"], "text": "TEXT_A"})
        self.assertEqual(second["done_when"], {"artifacts": ["b.txt"], "text": "TEXT_B"})
        first["done_when"]["artifacts"].append("leaked.txt")
        first["done_when"]["text"] = "mutated"
        self.assertEqual(second["done_when"]["artifacts"], ["b.txt"])
        self.assertEqual(second["done_when"]["text"], "TEXT_B")
        self.assertNotIn("TEXT_A", second["done_when"]["text"])

    def test_backslash_relative_normalizes(self):
        clean = validate_plan(
            _plan([_task(artifacts=["out\\x.md"], done_when={"artifacts": ["out\\x.md"], "text": "ok"})]),
            _plan_request(),
        )
        self.assertEqual(clean["tasks"][0]["artifacts"], ["out/x.md"])
        self.assertEqual(clean["tasks"][0]["done_when"]["artifacts"], ["out/x.md"])


class TestApplicationContract(unittest.TestCase):
    def test_submit_rejects_non_string_acceptance_text(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            body = {
                "idempotency_key": "bad-text",
                "goal": "DO_TASK",
                "acceptance": {"artifacts": ["delivery.txt"], "text": 5},
            }
            with self.assertRaises(AppError) as caught:
                app.submit(body)
            self.assertEqual(caught.exception.status, 400)
            self.assertIn("acceptance.text", str(caught.exception))
            self.assertEqual(app.list_requests()["requests"], [])

    def test_worker_charter_copies_acceptance_and_done_when_text(self):
        charter = worker_charter_for_task(
            goal={
                "desired_outcome": "DO_TASK",
                "boundaries": {"must": [], "must_not": ["NEVER_DELETE_SOURCE"]},
                "acceptance": {"artifacts": ["a.txt"], "text": "ACCEPTANCE_MUST_SURVIVE"},
            },
            task={
                "title": "t",
                "inputs": {"instruction": "DO_TASK"},
                "done_when": {"artifacts": ["a.txt"], "text": "DONE_TEXT"},
            },
        )
        self.assertEqual(charter["acceptance"], "ACCEPTANCE_MUST_SURVIVE")
        self.assertEqual(charter["done_when"]["text"], "DONE_TEXT")
        self.assertEqual(charter["must_not"], ["NEVER_DELETE_SOURCE"])
        with self.assertRaises(AppError):
            worker_charter_for_task(
                goal={"acceptance": {"text": 5, "artifacts": ["a.txt"]}},
                task={"title": "t", "inputs": {"instruction": "x"}, "done_when": {}},
            )

    def test_coordinator_keeps_per_task_done_when(self):
        class _TwoPlanner:
            name = "two.done_when"

            def plan(self, snap):
                request = build_planning_request(snap)
                return validate_plan(
                    {
                        "application_id": request["application_id"],
                        "context_summary": request["context_summary"],
                        "summary": "two",
                        "tasks": [
                            {
                                "task_key": "a",
                                "title": "A",
                                "instruction": "ia",
                                "depends_on": [],
                                "artifacts": ["a.txt", "extra.txt"],
                                "done_when": {"artifacts": ["a.txt"], "text": "TEXT_A"},
                            },
                            {
                                "task_key": "b",
                                "title": "B",
                                "instruction": "ib",
                                "depends_on": ["a"],
                                "artifacts": ["b.txt"],
                                "done_when": {"artifacts": ["b.txt"], "text": "TEXT_B"},
                            },
                        ],
                    },
                    request,
                )

        class _Recording:
            backend_id = "fake.recording_v1"

            def __init__(self) -> None:
                self.starts: list[dict] = []

            def start_run(self, *, title, directory, instruction="", artifacts=None, charter=None):
                root = Path(directory)
                for rel in artifacts or []:
                    path = root / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("ok\n", encoding="utf-8")
                self.starts.append({"title": title, "charter": dict(charter or {})})
                run_id = f"rec-{len(self.starts)}"
                return {"ok": True, "run_id": run_id, "native_handle": run_id, "backend": self.backend_id}

            def observe_run(self, run_id, **kwargs):
                return {"busy": False, "finish_successful": True}

            def collect_result(self, run_id):
                return {"ok": True, "run_id": run_id}

            def list_pending_actions(self, *, session_id=None):
                return 200, []

            def cancel(self, run_id):
                return 200, {"ok": True}

        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, planner=_TwoPlanner(), backend=_Recording())
            opened = app.submit(
                {
                    "idempotency_key": "two-done",
                    "goal": "two files",
                    "acceptance": {"artifacts": ["a.txt"], "text": "GOAL_TEXT"},
                }
            )
            first = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(first["action"], "task_finished", first)
            tasks = app.status(opened["goal_id"])["tasks"]
            by_title = {row["title"]: row for row in tasks}
            self.assertEqual(by_title["A"]["done_when"]["artifacts"], ["a.txt", "extra.txt"])
            self.assertEqual(by_title["A"]["done_when"]["text"], "TEXT_A")
            self.assertEqual(by_title["B"]["done_when"]["artifacts"], ["b.txt"])
            self.assertEqual(by_title["B"]["done_when"]["text"], "TEXT_B")
            self.assertNotIn("TEXT_B", json.dumps(by_title["A"]["done_when"]))
            self.assertNotIn("TEXT_A", json.dumps(by_title["B"]["done_when"]))

    def test_agy_application_prompt_carries_contract(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pin_a = root / "pin-a.bin"
            pin_b = root / "pin-b.bin"
            pin_a.write_bytes(b"alpha-pin")
            pin_b.write_bytes(b"beta-pin")
            sha_a = _sha(pin_a.read_bytes())
            sha_b = _sha(pin_b.read_bytes())
            fake = _write_fake_agy(root)
            argv_path = root / "argv.json"
            env = os.environ.copy()
            env.pop("AGY_AUTO_APPROVE", None)
            env.pop("COLLAB_AGY_AUTO_APPROVE", None)
            env["AGY_FAKE_ARGV"] = str(argv_path)
            env["AGY_FAKE_ARTIFACT"] = "delivery.txt"
            be = AntigravityCliExecutionBackend(
                bin_path=str(fake),
                environ=env,
                timeout_sec=10,
                poll_sec=0.05,
            )
            app = CollabApplication(root / "app", backend=be)
            opened = app.submit(
                {
                    "idempotency_key": "agy-contract",
                    "title": "Pinned read",
                    "goal": "DO_THE_PINNED_READ",
                    "boundaries": {
                        "must": ["Write delivery.txt"],
                        "must_not": ["NEVER_DELETE_SOURCE"],
                    },
                    "acceptance": {
                        "artifacts": ["delivery.txt"],
                        "text": "ACCEPTANCE_MUST_SURVIVE",
                    },
                    "external_inputs": [
                        {"path": str(pin_a.resolve()), "sha256": sha_a},
                        {"path": str(pin_b.resolve()), "sha256": sha_b},
                    ],
                    "budget": {"wall_sec": 30, "max_reworks": 0},
                }
            )
            deadline = time.time() + 5
            status = app.status(opened["goal_id"])
            while time.time() < deadline and status["state"] not in {"completed", "failed", "cancelled"}:
                app.coordinator.process_goal(opened["goal_id"])
                status = app.status(opened["goal_id"])
                if status["state"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.05)
            self.assertEqual(status["state"], "completed", status)
            argv = json.loads(argv_path.read_text(encoding="utf-8"))
            prompt = next(item for item in argv if str(item).startswith("--print="))
            prompt = prompt.split("=", 1)[1]
            self.assertIn("DO_THE_PINNED_READ", prompt)
            self.assertIn("NEVER_DELETE_SOURCE", prompt)
            self.assertIn("ACCEPTANCE_MUST_SURVIVE", prompt)
            self.assertIn(str(pin_a.resolve()), prompt)
            self.assertIn(str(pin_b.resolve()), prompt)
            self.assertIn(sha_a, prompt)
            self.assertIn(sha_b, prompt)
            self.assertNotIn("Stay inside the working directory", prompt)
            task = status["tasks"][0]
            result = task["result"]
            charter = worker_charter_for_task(goal=status["goal"], task=task)
            expected = contract_fingerprint(normalize_worker_contract(charter))
            self.assertEqual(result.get("contract_sha256"), expected)
            self.assertEqual(len(expected), 64)
            self.assertIn("must_not", result.get("contract_fields") or [])
            self.assertIn("acceptance", result.get("contract_fields") or [])
            self.assertIn("external_inputs", result.get("contract_fields") or [])
            rec = be._runs[task["run_id"]]
            self.assertEqual(rec["contract_sha256"], expected)
            self.assertNotIn("prompt", rec)
            self.assertTrue(any(flag.endswith("<redacted>") for flag in rec["argv_flags"]))


if __name__ == "__main__":
    unittest.main()
