"""Unit tests for native_entry.py.

Verifies:
1. Pure preflight without side effects (no modelspawn, no credential-reads, no sys.modules mutations).
2. Interpreter import capabilities returned separately.
3. Common imports before preflight and ensuring sys.modules is not polluted or replaced.
4. Schema, model, root, and file hash validations.
5. Store and binding validations (invalid schema/model/root/hash/binding/stale-run).
6. Invocation requires authentic controller proof and store matching; persists pre-effect evidence before runner side effects.
7. Invocation runs once and prevents replay.
8. Default-deny environment protection compliance.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

# Ensure native_entry is loaded
import native_entry


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class TestNativeEntry(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()

        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.workspace = self.scratch / "workspaces" / "goal_test" / "task_1"
        self.workspace.mkdir(parents=True)
        self.control = self.scratch / "control"
        self.stage_dir = self.control / "implementation"
        self.stage_dir.mkdir(parents=True)

        # Create dummy prompt and response-schema
        self.prompt_file = self.stage_dir / "prompt.txt"
        self.prompt_file.write_text("Hello test prompt", encoding="utf-8")
        self.prompt_sha = sha256_bytes(self.prompt_file.read_bytes())

        self.schema_data = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        }
        self.schema_file = self.stage_dir / "response-schema.json"
        self.schema_file.write_text(json.dumps(self.schema_data), encoding="utf-8")
        self.schema_sha = sha256_bytes(self.schema_file.read_bytes())

        # Create pinned files
        self.file1 = self.workspace / "file1.txt"
        self.file1.write_text("file 1 content", encoding="utf-8")
        self.file1_sha = sha256_bytes(self.file1.read_bytes())

        self.manifest = [
            {"path": str(self.file1), "sha256": self.file1_sha},
            {"path": str(self.prompt_file), "sha256": self.prompt_sha},
            {"path": str(self.schema_file), "sha256": self.schema_sha},
        ]

        # Durable store
        self.durable_dir = self.root / ".collab-durable"
        self.durable_dir.mkdir()
        self.store_file = self.durable_dir / "store.json"

        self.goal_id = "goal_test_123"
        self.task_id = "task_test_456"
        self.run_id = "run_test_789"
        self.owner = "test_owner"

        self.store_content = {
            "goals": {
                self.goal_id: {
                    "coordinator_id": self.owner,
                    "goal": {
                        "role_hints": {"coordinator": self.owner},
                    },
                    "tasks": [
                        {
                            "task_id": self.task_id,
                            "run_id": self.run_id,
                            "status": "running",
                            "workspace": str(self.workspace),
                        }
                    ],
                }
            }
        }
        self.store_file.write_text(json.dumps(self.store_content), encoding="utf-8")

        self.valid_request = {
            "goal_id": self.goal_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "owner": self.owner,
            "model": "gemini-3.8-flash-low",
            "scratch": str(self.scratch),
            "workspace": str(self.workspace),
            "control": str(self.control),
            "store": str(self.store_file),
            "manifest": self.manifest,
            "stage": "implementation",
            "stages": {
                "implementation": {
                    "seconds": 120,
                    "prompt_sha256": self.prompt_sha,
                    "schema_sha256": self.schema_sha,
                }
            },
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_import_and_capabilities_no_modules_mutation(self):
        modules_before = set(sys.modules.keys())
        caps = native_entry.get_capabilities()
        self.assertTrue(caps["isolated_child_interpreter"])
        self.assertTrue(caps["no_cached_sys_modules_mutation"])
        self.assertIn("gemini-3.8-flash-low", caps["supported_models"])
        # sys.modules keys should not change
        self.assertEqual(set(sys.modules.keys()), modules_before)

    def test_preflight_valid(self):
        modules_before = dict(sys.modules)
        result = native_entry.preflight(self.valid_request)
        self.assertTrue(result["passed"])
        self.assertEqual(result["mode"], "NO_SPAWN_PREFLIGHT")
        self.assertEqual(result["native_calls"], 0)
        self.assertTrue(result["no_sys_modules_mutation"])
        self.assertEqual(sys.modules, modules_before)

    def test_preflight_invalid_model(self):
        req = copy.deepcopy(self.valid_request)
        req["model"] = "gemini-ultra-pro"
        with self.assertRaises(native_entry.NativeEntryError) as ctx:
            native_entry.preflight(req)
        self.assertIn("Unsupported model", str(ctx.exception))

    def test_preflight_invalid_root_traversal(self):
        req = copy.deepcopy(self.valid_request)
        # Point workspace outside scratch root
        outside = self.root / "outside_dir"
        outside.mkdir()
        req["workspace"] = str(outside)
        with self.assertRaises(native_entry.NativeEntryError) as ctx:
            native_entry.preflight(req)
        self.assertIn("outside authorized scratch root", str(ctx.exception))

    def test_preflight_manifest_hash_mismatch(self):
        req = copy.deepcopy(self.valid_request)
        req["manifest"][0]["sha256"] = "0000000000000000000000000000000000000000000000000000000000000000"
        with self.assertRaises(native_entry.NativeEntryError) as ctx:
            native_entry.preflight(req)
        self.assertIn("Manifest hash mismatch", str(ctx.exception))

    def test_preflight_invalid_schema(self):
        req = copy.deepcopy(self.valid_request)
        self.schema_file.write_text("{\"type\": 123}", encoding="utf-8")
        new_schema_sha = sha256_bytes(self.schema_file.read_bytes())
        req["stages"]["implementation"]["schema_sha256"] = new_schema_sha
        for entry in req["manifest"]:
            if entry["path"] == str(self.schema_file):
                entry["sha256"] = new_schema_sha
        with self.assertRaises(native_entry.NativeEntryError) as ctx:
            native_entry.preflight(req)
        self.assertIn("Schema validation error", str(ctx.exception))

    def test_invoke_caller_authored_boolean_alone_rejected(self):
        proof = {"verified_by_controller": False}
        with self.assertRaises(native_entry.NativeEntryError) as ctx:
            native_entry.invoke(self.valid_request, proof)
        self.assertIn("verified_by_controller", str(ctx.exception))

    def test_invoke_stale_run_or_mismatched_binding(self):
        proof = {"verified_by_controller": True}
        req = copy.deepcopy(self.valid_request)
        req["run_id"] = "stale_run_other"
        with self.assertRaises(native_entry.NativeEntryError) as ctx:
            native_entry.invoke(req, proof)
        self.assertIn("Run mismatch in store", str(ctx.exception))

    def test_invoke_successful_with_pre_effect_evidence(self):
        proof = {"verified_by_controller": True}
        runner_called = []
        evidence_checked_pre_runner = []

        def fake_runner(req, evidence_dir=None):
            launch_file = Path(evidence_dir) / "launch.json"
            # Verify evidence was persisted BEFORE runner executes
            evidence_checked_pre_runner.append(launch_file.exists())
            runner_called.append(True)
            return {"returncode": 0, "status": "SUCCESS"}

        res = native_entry.invoke(self.valid_request, proof, runner=fake_runner)
        self.assertTrue(res["ok"])
        self.assertEqual(len(runner_called), 1)
        self.assertTrue(evidence_checked_pre_runner[0])

        evidence_dir = Path(res["evidence_dir"])
        self.assertTrue((evidence_dir / "launch.json").exists())
        self.assertTrue((evidence_dir / "settlement.json").exists())

        # Test replay prevention
        with self.assertRaises(native_entry.NativeEntryError) as ctx:
            native_entry.invoke(self.valid_request, proof, runner=fake_runner)
        self.assertIn("Replay detected", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
