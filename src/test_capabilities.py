#!/usr/bin/env python3
"""Backend capability documents, caller requirements, and the agy acceptance gate."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_backend.antigravity_cli_v1 import (  # noqa: E402
    SKIP_PERMISSIONS_WARNING,
    AntigravityCliExecutionBackend,
    run_antigravity_job_via_public_api,
)
from execution_backend.base import (  # noqa: E402
    CAPABILITIES_API_VERSION,
    SKIP_PERMISSIONS_WARNING as BASE_SKIP_WARNING,
    default_capabilities,
    unmet_capabilities,
)
from execution_backend.inprocess_v1 import InProcessExecutionBackend  # noqa: E402
from execution_backend.linux_supervised_v1 import LinuxSupervisedExecutionBackend  # noqa: E402
from execution_backend.windows_supervised_v1 import WindowsSupervisedExecutionBackend  # noqa: E402
from framework.app_service import (  # noqa: E402
    API_VERSION,
    AppError,
    CollabApplication,
    CollabHttpServer,
    compose_capabilities,
)
from test_contract_render import _write_fake_agy  # noqa: E402

ACCEPTANCE_UNVERIFIED = "acceptance text was not independently verified"
_SCHEMA_KEYS = {
    "api_version",
    "backend",
    "planner",
    "channels",
    "external_inputs",
    "isolation",
    "skip_permissions",
    "resume",
    "acceptance",
    "progress",
    "metering",
    "budget_enforcement",
    "usage",
    "warnings",
}


def _goal(**overrides) -> dict:
    body = {
        "idempotency_key": "cap-1",
        "client_id": "test-suite",
        "title": "capability probe",
        "goal": "Write delivery.txt inside the assigned workspace.",
        "boundaries": {
            "must": ["Stay inside the assigned workspace"],
            "must_not": ["Do not use network or system tools"],
        },
        "acceptance": {"artifacts": ["delivery.txt"], "text": "delivery.txt exists"},
        "budget": {"wall_sec": 30, "max_reworks": 0},
    }
    body.update(overrides)
    return body


def _pin() -> dict:
    # Absolute on every OS (a POSIX "/tmp/..." is not absolute on Windows).
    return {"path": str(Path(tempfile.gettempdir()).resolve() / "pinned-input.txt"), "sha256": "ab" * 32}


def _agy_env(*, body: str, auto: bool) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "AGY_AUTO_APPROVE",
        "COLLAB_AGY_AUTO_APPROVE",
        "COLLAB_AGY_ACCOUNT_POOL",
        "AGY_PROFILE",
        "AGY_BIN",
    ):
        env.pop(key, None)
    env["AGY_FAKE_ARTIFACT"] = "delivery.txt"
    env["AGY_FAKE_ARTIFACT_BODY"] = body
    if auto:
        env["AGY_AUTO_APPROVE"] = "true"
    return env


def _assert_shape(testcase: unittest.TestCase, caps: dict) -> None:
    testcase.assertTrue(_SCHEMA_KEYS <= set(caps))
    testcase.assertEqual(set(caps["backend"]), {"id", "kind"})
    testcase.assertEqual(set(caps["planner"]), {"name", "decomposes", "lead_review"})
    testcase.assertEqual(set(caps["channels"]), {"permission", "question", "review"})
    testcase.assertEqual(caps["external_inputs"]["max"], 8)
    testcase.assertIn(
        caps["external_inputs"]["enforcement"],
        {"permission_gate", "prompt_only", "none"},
    )
    testcase.assertEqual(
        set(caps["isolation"]),
        {"os_sandbox", "access_audit", "prompt_constraints"},
    )
    testcase.assertIs(caps["isolation"]["os_sandbox"], False)
    testcase.assertEqual(
        set(caps["acceptance"]),
        {"artifact_presence", "exact_content", "lead_review", "executable_checks"},
    )
    testcase.assertIs(caps["acceptance"]["executable_checks"], False)
    testcase.assertIs(caps["progress"]["percent"], False)
    testcase.assertIn(caps["progress"]["subagent_observability"], (False, "unknown"))
    # Never a fabricated numeric count (note False == 0 in Python, so check the type).
    value = caps["progress"]["subagent_observability"]
    testcase.assertFalse(isinstance(value, int) and not isinstance(value, bool))
    testcase.assertIsInstance(caps["warnings"], list)


class CapabilityDocumentTests(unittest.TestCase):
    def test_default_document_is_unknown_or_false(self) -> None:
        caps = default_capabilities()
        _assert_shape(self, caps)
        self.assertEqual(caps["api_version"], CAPABILITIES_API_VERSION)
        self.assertEqual(caps["backend"], {"id": "unknown", "kind": "unknown"})
        self.assertFalse(caps["planner"]["decomposes"])
        self.assertFalse(caps["planner"]["lead_review"])
        self.assertEqual(caps["channels"], {"permission": False, "question": False, "review": False})
        self.assertEqual(caps["external_inputs"]["enforcement"], "none")
        self.assertEqual(caps["isolation"]["access_audit"], "unknown")
        self.assertIs(caps["isolation"]["prompt_constraints"], False)
        self.assertEqual(caps["skip_permissions"], "unknown")
        self.assertEqual(caps["resume"], "unknown")
        self.assertFalse(caps["acceptance"]["artifact_presence"])
        self.assertEqual(caps["usage"]["source"], "unknown")
        self.assertEqual(caps["warnings"], [])
        self.assertIs(caps["progress"]["available"], False)
        self.assertIs(caps["progress"]["heartbeat"], False)
        self.assertIs(caps["progress"]["artifact_checkpoint"], False)
        self.assertEqual(caps["progress"]["subagent_observability"], "unknown")
        self.assertIs(caps["metering"]["live_usage"], False)
        self.assertEqual(caps["metering"]["source"], "none")
        self.assertEqual(caps["budget_enforcement"]["wall_sec"], "enforced")
        self.assertEqual(caps["budget_enforcement"]["max_tokens"], "unsupported")
        self.assertEqual(caps["budget_enforcement"]["max_tool_calls"], "unsupported")
        self.assertEqual(caps["budget_enforcement"]["no_progress_sec"], "unsupported")
        self.assertEqual(unmet_capabilities(caps, ["no_skip_permissions", "permission_gate"]), [
            "no_skip_permissions",
            "permission_gate",
        ])

    def test_inprocess_is_local_writer_without_a_gate(self) -> None:
        caps = InProcessExecutionBackend().capabilities()
        _assert_shape(self, caps)
        self.assertEqual(caps["backend"], {"id": "inprocess.local_v1", "kind": "inprocess"})
        self.assertEqual(caps["channels"], {"permission": False, "question": False, "review": False})
        self.assertEqual(caps["external_inputs"]["enforcement"], "none")
        self.assertIs(caps["isolation"]["access_audit"], False)
        self.assertIs(caps["isolation"]["prompt_constraints"], True)
        self.assertIs(caps["skip_permissions"], False)
        self.assertIs(caps["resume"], False)
        self.assertIs(caps["acceptance"]["artifact_presence"], True)
        self.assertIs(caps["acceptance"]["exact_content"], False)
        self.assertIs(caps["acceptance"]["lead_review"], False)
        self.assertEqual(caps["usage"]["source"], "unknown")
        self.assertEqual(caps["warnings"], [])
        self.assertIs(caps["progress"]["available"], True)
        self.assertIs(caps["progress"]["heartbeat"], False)
        self.assertIs(caps["progress"]["artifact_checkpoint"], True)
        self.assertIs(caps["progress"]["subagent_observability"], False)
        self.assertIs(caps["metering"]["live_usage"], False)
        self.assertIs(caps["metering"]["usage_at_end"], False)
        self.assertIs(caps["metering"]["tool_calls"], False)
        self.assertEqual(caps["metering"]["source"], "none")
        self.assertEqual(caps["budget_enforcement"]["max_tokens"], "unsupported")
        self.assertEqual(caps["budget_enforcement"]["max_tool_calls"], "unsupported")
        self.assertEqual(caps["budget_enforcement"]["no_progress_sec"], "enforced")

    def test_agy_skip_follows_backend_env_not_process_env(self) -> None:
        with mock.patch.dict(os.environ, {"AGY_AUTO_APPROVE": "true"}, clear=False):
            quiet = AntigravityCliExecutionBackend(
                bin_path="agy-test",
                environ={"PATH": os.environ.get("PATH", "")},
            )
            loud = AntigravityCliExecutionBackend(
                bin_path="agy-test",
                environ={"AGY_AUTO_APPROVE": "yes"},
            )
        off = quiet.capabilities()
        on = loud.capabilities()
        for caps in (off, on):
            _assert_shape(self, caps)
            self.assertEqual(caps["backend"]["id"], "antigravity.cli_v1")
            self.assertEqual(caps["backend"]["kind"], "antigravity_cli")
            self.assertEqual(caps["channels"], {"permission": False, "question": False, "review": False})
            self.assertEqual(caps["external_inputs"]["enforcement"], "prompt_only")
            self.assertIs(caps["isolation"]["os_sandbox"], False)
            self.assertIs(caps["isolation"]["access_audit"], False)
            self.assertIs(caps["isolation"]["prompt_constraints"], True)
            self.assertIs(caps["resume"], False)
            self.assertIs(caps["acceptance"]["artifact_presence"], True)
            self.assertIs(caps["acceptance"]["exact_content"], True)
            self.assertIs(caps["acceptance"]["lead_review"], False)
            self.assertEqual(caps["usage"]["source"], "worker_self_reported")
            self.assertIs(caps["progress"]["available"], True)
            self.assertEqual(caps["progress"]["heartbeat"], "runner_process")
            self.assertIs(caps["progress"]["artifact_checkpoint"], True)
            self.assertIs(caps["progress"]["percent"], False)
            self.assertIs(caps["progress"]["subagent_observability"], False)
            self.assertIs(caps["metering"]["live_usage"], False)
            self.assertIs(caps["metering"]["usage_at_end"], True)
            self.assertIs(caps["metering"]["tool_calls"], False)
            self.assertEqual(
                caps["metering"]["fields"],
                [
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                ],
            )
            self.assertEqual(caps["metering"]["source"], "worker_self_reported")
            self.assertEqual(caps["budget_enforcement"]["max_tokens"], "post_hoc")
            self.assertEqual(caps["budget_enforcement"]["max_tool_calls"], "unsupported")
            self.assertEqual(caps["budget_enforcement"]["no_progress_sec"], "enforced")
        self.assertIs(off["skip_permissions"], False)
        self.assertEqual(off["warnings"], [])
        self.assertIs(on["skip_permissions"], True)
        self.assertEqual(on["warnings"], [SKIP_PERMISSIONS_WARNING])
        self.assertEqual(SKIP_PERMISSIONS_WARNING, BASE_SKIP_WARNING)

    def test_supervised_backends_expose_native_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            windows = WindowsSupervisedExecutionBackend(
                state_dir=root / "win",
                client=object(),
            )
            linux = LinuxSupervisedExecutionBackend(
                state_dir=root / "linux",
                client=object(),
            )
            for backend, kind in (
                (windows, "teleagent_windows"),
                (linux, "teleagent_linux"),
            ):
                caps = backend.capabilities()
                _assert_shape(self, caps)
                self.assertEqual(caps["backend"]["id"], backend.backend_id)
                self.assertEqual(caps["backend"]["kind"], kind)
                self.assertEqual(caps["channels"], {"permission": True, "question": True, "review": True})
                self.assertEqual(caps["external_inputs"]["enforcement"], "permission_gate")
                self.assertEqual(caps["isolation"]["access_audit"], "decision_log")
                self.assertIs(caps["isolation"]["prompt_constraints"], True)
                self.assertIs(caps["skip_permissions"], False)
                self.assertIs(caps["resume"], True)
                self.assertIs(caps["acceptance"]["artifact_presence"], True)
                self.assertIs(caps["progress"]["available"], True)
                self.assertEqual(caps["progress"]["heartbeat"], "engine")
                self.assertIs(caps["progress"]["artifact_checkpoint"], True)
                self.assertEqual(caps["progress"]["subagent_observability"], "unknown")
                self.assertIs(caps["metering"]["live_usage"], False)
                self.assertEqual(caps["metering"]["source"], "none")
                self.assertEqual(caps["budget_enforcement"]["max_tokens"], "unsupported")
                self.assertEqual(caps["budget_enforcement"]["max_tool_calls"], "unsupported")
                self.assertEqual(caps["budget_enforcement"]["no_progress_sec"], "enforced")
                self.assertIs(caps["acceptance"]["exact_content"], False)
                self.assertIs(caps["acceptance"]["lead_review"], True)
                self.assertEqual(caps["usage"]["source"], "unknown")
                self.assertEqual(caps["warnings"], [])

    def test_compose_overlays_planner_and_ands_lead_review(self) -> None:
        class _Named:
            def __init__(self, name: str) -> None:
                self.name = name

        with tempfile.TemporaryDirectory() as td:
            windows = WindowsSupervisedExecutionBackend(state_dir=Path(td) / "win", client=object())
        det = compose_capabilities(windows, _Named("deterministic.single_task"))
        self.assertEqual(det["planner"], {
            "name": "deterministic.single_task",
            "decomposes": False,
            "lead_review": False,
        })
        self.assertIs(det["acceptance"]["lead_review"], False)
        lead = compose_capabilities(windows, _Named("lead_adapter.plan_v1"))
        self.assertEqual(lead["planner"], {
            "name": "lead_adapter.plan_v1",
            "decomposes": True,
            "lead_review": True,
        })
        self.assertIs(lead["acceptance"]["lead_review"], True)
        inproc = compose_capabilities(InProcessExecutionBackend(), _Named("lead_adapter.plan_v1"))
        self.assertIs(inproc["planner"]["lead_review"], True)
        self.assertIs(inproc["acceptance"]["lead_review"], False)


class CapabilityHttpTests(unittest.TestCase):
    def test_health_and_capabilities_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            server = CollabHttpServer(("127.0.0.1", 0), app, api_token="test-token")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(f"{base}/v1/capabilities", timeout=5)
                self.assertEqual(denied.exception.code, 401)
                health = json.loads(urllib.request.urlopen(f"{base}/health", timeout=5).read())
                self.assertEqual(health["ok"], True)
                self.assertEqual(health["api_version"], API_VERSION)
                self.assertEqual(health["backend"], "inprocess.local_v1")
                self.assertEqual(health["planner"], "deterministic.single_task")
                self.assertNotIn("token", json.dumps(health).lower())
                req = urllib.request.Request(
                    f"{base}/v1/capabilities",
                    headers={"Authorization": "Bearer test-token"},
                )
                caps = json.loads(urllib.request.urlopen(req, timeout=5).read())
                self.assertTrue(caps["ok"])
                _assert_shape(self, caps)
                self.assertEqual(caps["api_version"], API_VERSION)
                self.assertEqual(caps["backend"]["id"], "inprocess.local_v1")
                self.assertEqual(caps["planner"]["name"], "deterministic.single_task")
                self.assertIs(caps["planner"]["decomposes"], False)
                self.assertIs(caps["planner"]["lead_review"], False)
                blocked = urllib.request.Request(
                    f"{base}/v1/capabilities",
                    data=b"{}",
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer test-token",
                    },
                )
                with self.assertRaises(urllib.error.HTTPError) as method:
                    urllib.request.urlopen(blocked, timeout=5)
                self.assertEqual(method.exception.code, 405)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class CapabilitySubmitTests(unittest.TestCase):
    def test_unmet_required_is_409_and_does_not_persist(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            payload = _goal(
                idempotency_key="reuse-me",
                required_capabilities=["permission_gate", "permission_gate", "os_sandbox"],
            )
            with self.assertRaises(AppError) as caught:
                app.submit(payload)
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(caught.exception.code, "capability_unavailable")
            self.assertEqual(caught.exception.extra["missing"], ["permission_gate", "os_sandbox"])
            self.assertEqual(caught.exception.extra["capabilities"]["backend"]["id"], "inprocess.local_v1")
            self.assertEqual(app.layer.list_goals()["goal_count"], 0)
            opened = app.submit(_goal(idempotency_key="reuse-me"))
            self.assertTrue(opened["created"])
            self.assertFalse(opened["duplicate"])
            status = app.status(opened["goal_id"])
            self.assertEqual(status["warnings"], [])
            self.assertEqual(status["capabilities_ref"], {
                "backend": "inprocess.local_v1",
                "planner": "deterministic.single_task",
            })

    def test_unknown_capability_is_400(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            for bad in (["not_a_capability"], "permission_gate", [""], [1]):
                with self.assertRaises(AppError) as caught:
                    app.submit(_goal(required_capabilities=bad))
                self.assertEqual(caught.exception.status, 400)
                self.assertEqual(caught.exception.code, "invalid_request")
            self.assertEqual(app.layer.list_goals()["goal_count"], 0)

    def test_http_409_body_and_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            server = CollabHttpServer(("127.0.0.1", 0), app, api_token="test-token")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            headers = {
                "Content-Type": "application/json",
                "Authorization": "Bearer test-token",
            }
            try:
                raw = json.dumps(_goal(required_capabilities=["access_audit"])).encode("utf-8")
                req = urllib.request.Request(
                    f"{base}/v1/requests",
                    data=raw,
                    method="POST",
                    headers=headers,
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(caught.exception.code, 409)
                body = json.loads(caught.exception.read().decode("utf-8"))
                self.assertEqual(body["code"], "capability_unavailable")
                self.assertEqual(body["missing"], ["access_audit"])
                self.assertIn("capabilities", body)
                listed = json.loads(
                    urllib.request.urlopen(
                        urllib.request.Request(f"{base}/v1/requests", headers=headers),
                        timeout=5,
                    ).read()
                )
                self.assertEqual(listed["requests"], [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def _agy_app(self, root: Path, *, auto: bool) -> CollabApplication:
        backend = AntigravityCliExecutionBackend(
            bin_path="agy-test",
            environ=_agy_env(body="unused\n", auto=auto),
            timeout_sec=5,
        )
        return CollabApplication(root / "app", backend=backend)

    def test_agy_external_inputs_with_skip_need_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = self._agy_app(root, auto=True)
            payload = _goal(idempotency_key="pin-1", external_inputs=[_pin()])
            with self.assertRaises(AppError) as caught:
                app.submit(payload)
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(caught.exception.code, "capability_unavailable")
            self.assertEqual(caught.exception.extra["missing"], ["external_input_enforcement"])
            self.assertEqual(app.layer.list_goals()["goal_count"], 0)
            still = dict(payload)
            still["acknowledge_prompt_only_inputs"] = True
            still["required_capabilities"] = ["external_input_enforcement"]
            with self.assertRaises(AppError) as explicit:
                app.submit(still)
            self.assertEqual(explicit.exception.status, 409)
            self.assertIn("external_input_enforcement", explicit.exception.extra["missing"])
            self.assertEqual(app.layer.list_goals()["goal_count"], 0)
            opened = app.submit({
                **payload,
                "acknowledge_prompt_only_inputs": True,
            })
            self.assertTrue(opened["created"])
            status = app.status(opened["goal_id"])
            self.assertIn(SKIP_PERMISSIONS_WARNING, status["warnings"])
            self.assertIn(SKIP_PERMISSIONS_WARNING, status["goal"]["warnings"])
            self.assertEqual(status["capabilities_ref"]["backend"], "antigravity.cli_v1")

    def test_agy_prompt_only_without_skip_allows_pins(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = self._agy_app(Path(td), auto=False)
            opened = app.submit(_goal(external_inputs=[_pin()]))
            self.assertTrue(opened["created"])
            status = app.status(opened["goal_id"])
            self.assertNotIn(SKIP_PERMISSIONS_WARNING, status["warnings"])


class AgyAcceptanceGateApiTests(unittest.TestCase):
    def _backend(self, root: Path, body: str) -> AntigravityCliExecutionBackend:
        fake = _write_fake_agy(root)
        return AntigravityCliExecutionBackend(
            bin_path=str(fake),
            environ=_agy_env(body=body, auto=False),
            timeout_sec=15,
            poll_sec=0.05,
        )

    def _drive(self, app: CollabApplication, goal_id: str) -> dict:
        last = None
        for _ in range(40):
            last = app.coordinator.process_goal(goal_id)
            status = app.status(goal_id)
            if status["state"] in {"completed", "failed", "cancelled"}:
                return status
            time.sleep(0.05)
        self.fail(f"goal did not finish: {last}")

    def _public(self, backend: AntigravityCliExecutionBackend, workdir: Path, text: str) -> dict:
        charter = {
            "name": "gate",
            "goal": "Write delivery.txt",
            "must": ["Stay inside the assigned workspace"],
            "must_not": ["Do not use network or system tools"],
            "done_when": {"artifacts": ["delivery.txt"]},
            "acceptance": text,
            "timeout_sec": 15,
        }
        return run_antigravity_job_via_public_api(
            workdir=workdir,
            charter=charter,
            instruction="Write delivery.txt",
            name="gate",
            timeout_sec=15,
            backend=backend,
        )

    def test_wrong_exact_content_fails_on_both_entries(self) -> None:
        text = "delivery.txt must contain exactly RIGHT"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backend = self._backend(root, "WRONG\n")
            app = CollabApplication(root / "app", backend=backend)
            opened = app.submit(_goal(
                acceptance={"artifacts": ["delivery.txt"], "text": text},
            ))
            status = self._drive(app, opened["goal_id"])
            self.assertEqual(status["state"], "failed", status)
            task = status["tasks"][0]
            self.assertEqual(task["status"], "failed")
            result = task["result"]
            self.assertFalse(result.get("ok"))
            self.assertTrue(result.get("acceptance_failed"))
            self.assertEqual(result["review"]["status"], "failed")
            self.assertEqual(result["review"]["source"], "agy_exact_content")
            self.assertNotEqual(status["state"], "completed")
            public = self._public(backend, root / "public", text)
            self.assertFalse(public.get("ok"), public)
            self.assertTrue(public.get("acceptance_failed"), public)
            self.assertEqual(public["review"]["status"], "failed")
            self.assertEqual(public["review"]["source"], "agy_exact_content")
            self.assertNotEqual(public.get("state"), "ok")

    def test_correct_exact_content_passes_on_both_entries(self) -> None:
        text = "delivery.txt must contain exactly RIGHT"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backend = self._backend(root, "RIGHT\n")
            app = CollabApplication(root / "app", backend=backend)
            opened = app.submit(_goal(
                acceptance={"artifacts": ["delivery.txt"], "text": text},
            ))
            status = self._drive(app, opened["goal_id"])
            self.assertEqual(status["state"], "completed", status)
            result = status["tasks"][0]["result"]
            self.assertTrue(result.get("ok"), result)
            self.assertEqual(result["review"], {
                "status": "passed",
                "source": "agy_exact_content",
                "evidence": "exact-content criteria passed",
            })
            public = self._public(backend, root / "public", text)
            self.assertTrue(public.get("ok"), public)
            self.assertEqual(public["review"]["status"], "passed")
            self.assertEqual(public["review"]["source"], "agy_exact_content")

    def test_prose_acceptance_is_unsupported_not_passed(self) -> None:
        text = "delivery.txt should read well"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backend = self._backend(root, "RIGHT\n")
            app = CollabApplication(root / "app", backend=backend)
            opened = app.submit(_goal(
                acceptance={"artifacts": ["delivery.txt"], "text": text},
            ))
            status = self._drive(app, opened["goal_id"])
            self.assertEqual(status["state"], "completed", status)
            result = status["tasks"][0]["result"]
            self.assertTrue(result.get("ok"), result)
            self.assertEqual(result["review"]["status"], "unsupported")
            self.assertNotEqual(result["review"]["status"], "passed")
            self.assertIn(ACCEPTANCE_UNVERIFIED, result.get("warnings") or [])
            self.assertIn(ACCEPTANCE_UNVERIFIED, status["warnings"])
            self.assertIn(ACCEPTANCE_UNVERIFIED, status["goal"].get("warnings") or [])
            public = self._public(backend, root / "public", text)
            self.assertTrue(public.get("ok"), public)
            self.assertEqual(public["review"]["status"], "unsupported")
            self.assertNotEqual(public["review"]["status"], "passed")
            self.assertIn(ACCEPTANCE_UNVERIFIED, public.get("warnings") or [])


if __name__ == "__main__":
    unittest.main()
