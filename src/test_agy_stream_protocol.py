"""Tests for pure NDJSON stream protocol parser (agy_stream_protocol).

Verifies strict privacy separation between public diagnostic metadata
and private final payloads, error handling, normalization, usage allowlists,
and import restrictions.
"""
from __future__ import annotations

import ast
import json
import math
from pathlib import Path
import unittest

import agy_stream_protocol
from agy_stream_protocol import (
    USAGE_ALLOWLIST,
    VALID_EVENT_TYPES,
    VALID_SOURCE_TYPES,
    VALID_STATUS_TYPES,
    VALID_STEP_CATEGORIES,
    VALID_STEP_STATES,
    ParsedStreamLine,
    parse_ndjson_line,
    sanitize_usage_allowlist,
)


class AgyStreamProtocolAstTests(unittest.TestCase):
    """Verify that agy_stream_protocol imports only permitted standard modules."""

    def test_allowed_imports_only(self):
        protocol_path = Path(__file__).resolve().parent / "agy_stream_protocol.py"
        self.assertTrue(protocol_path.is_file(), f"Expected file at {protocol_path}")

        tree = ast.parse(protocol_path.read_text(encoding="utf-8"), filename=str(protocol_path))

        allowed_modules = frozenset({"__future__", "dataclasses", "json", "math", "typing"})
        found_imports: list[str] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.split(".")[0]
                    found_imports.append(root_mod)
                    self.assertIn(
                        root_mod,
                        allowed_modules,
                        f"Disallowed import '{alias.name}' found in agy_stream_protocol.py",
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root_mod = node.module.split(".")[0]
                    found_imports.append(root_mod)
                    self.assertIn(
                        root_mod,
                        allowed_modules,
                        f"Disallowed import-from '{node.module}' found in agy_stream_protocol.py",
                    )

        self.assertTrue(found_imports, "Expected at least one import in agy_stream_protocol.py")
        disallowed = set(found_imports) - allowed_modules
        self.assertEqual(disallowed, set(), f"Found disallowed imports: {disallowed}")


class ProtocolErrorTests(unittest.TestCase):
    """Verify malformed, non-object, non-string, and unknown events return fixed protocol_error."""

    def _assert_protocol_error(self, result: ParsedStreamLine):
        self.assertIsInstance(result, ParsedStreamLine)
        self.assertEqual(
            result.public_metadata,
            {
                "event": "unknown",
                "source": "runner",
                "error": "protocol_error",
            },
        )
        self.assertIsNone(result.private_payload)

    def test_non_string_inputs(self):
        for bad_input in (None, 123, True, False, 3.14, [], {}, object()):
            with self.subTest(bad_input=bad_input):
                res = parse_ndjson_line(bad_input)  # type: ignore[arg-type]
                self._assert_protocol_error(res)

    def test_malformed_and_non_object_strings(self):
        bad_lines = [
            "",
            "   ",
            "\n\t  ",
            "not json",
            "{",
            "}",
            "{unclosed json",
            "[1, 2, 3]",
            '"just a string"',
            "12345",
            "true",
            "null",
            "{\"foo\": ",
        ]
        for line in bad_lines:
            with self.subTest(line=line):
                res = parse_ndjson_line(line)
                self._assert_protocol_error(res)

    def test_missing_or_invalid_or_unknown_event(self):
        test_cases = [
            "{}",
            json.dumps({"key": "val"}),
            json.dumps({"event": None}),
            json.dumps({"event": 123}),
            json.dumps({"event": True}),
            json.dumps({"event": []}),
            json.dumps({"event": "unknown"}),
            json.dumps({"event": "INIT"}),  # case-sensitive: only lowercase "init" allowed
            json.dumps({"event": "STEP_UPDATE"}),
            json.dumps({"event": "RESULT"}),
            json.dumps({"event": "SYNTH_CUSTOM_EVENT"}),
        ]
        for line in test_cases:
            with self.subTest(line=line):
                res = parse_ndjson_line(line)
                self._assert_protocol_error(res)


class InitEventTests(unittest.TestCase):
    """Verify handling of the init event."""

    def test_valid_init_event(self):
        line = json.dumps({"event": "init"})
        res = parse_ndjson_line(line)
        self.assertEqual(
            res.public_metadata,
            {
                "event": "init",
                "source": "runner",
            },
        )
        self.assertIsNone(res.private_payload)

    def test_init_event_ignores_extra_keys_without_leak(self):
        line = json.dumps({
            "event": "init",
            "canary": "SYNTH_CANARY_INIT_SECRET_DATA",
            "step_index": 99,
        })
        res = parse_ndjson_line(line)
        self.assertEqual(
            res.public_metadata,
            {
                "event": "init",
                "source": "runner",
            },
        )
        self.assertIsNone(res.private_payload)
        self.assertNotIn("SYNTH_CANARY_INIT_SECRET_DATA", json.dumps(res.public_metadata))


class StepUpdateEventTests(unittest.TestCase):
    """Verify step_update parsing, step_index validation, state/category normalization, and usage."""

    def test_step_update_payload_must_be_dict(self):
        for bad_payload in (None, "not a dict", 123, []):
            line = json.dumps({"event": "step_update", "step_update": bad_payload})
            res = parse_ndjson_line(line)
            self.assertEqual(
                res.public_metadata,
                {
                    "event": "unknown",
                    "source": "runner",
                    "error": "protocol_error",
                },
            )
            self.assertIsNone(res.private_payload)

    def test_missing_step_update_payload_key(self):
        line = json.dumps({"event": "step_update"})
        res = parse_ndjson_line(line)
        self.assertEqual(
            res.public_metadata,
            {
                "event": "unknown",
                "source": "runner",
                "error": "protocol_error",
            },
        )
        self.assertIsNone(res.private_payload)

    def test_valid_step_index(self):
        for idx in (0, 1, 42, 1000):
            with self.subTest(step_index=idx):
                line = json.dumps({"event": "step_update", "step_update": {"step_index": idx}})
                res = parse_ndjson_line(line)
                self.assertEqual(res.public_metadata["event"], "step_update")
                self.assertEqual(res.public_metadata["source"], "runner")
                self.assertEqual(res.public_metadata["step_update"]["step_index"], idx)
                self.assertIsNone(res.private_payload)

    def test_invalid_step_index_omitted(self):
        invalid_indices = [
            True,        # bool
            False,       # bool
            1.0,         # float 1.0 rejected
            0.0,         # float 0.0 rejected
            -1,          # negative int rejected
            -42,         # negative int rejected
            "0",         # string rejected
            "1",         # string rejected
            "invalid",   # string rejected
            None,        # None rejected
            [],          # list rejected
            {},          # dict rejected
        ]
        for bad_idx in invalid_indices:
            with self.subTest(bad_idx=bad_idx):
                line = json.dumps({"event": "step_update", "step_update": {"step_index": bad_idx}})
                res = parse_ndjson_line(line)
                self.assertNotIn(
                    "step_index",
                    res.public_metadata["step_update"],
                    f"step_index {bad_idx!r} should be omitted",
                )

    def test_state_normalization_and_fallback(self):
        valid_cases = [
            ("ACTIVE", "ACTIVE"),
            ("active", "ACTIVE"),
            ("  ACTIVE  ", "ACTIVE"),
            ("DONE", "DONE"),
            ("done", "DONE"),
            ("  done  ", "DONE"),
        ]
        for input_state, expected_state in valid_cases:
            with self.subTest(state=input_state):
                line = json.dumps({"event": "step_update", "step_update": {"state": input_state}})
                res = parse_ndjson_line(line)
                self.assertEqual(res.public_metadata["step_update"]["state"], expected_state)

        invalid_cases = [
            "running",
            "PENDING",
            "failed",
            "",
            "   ",
            123,
            True,
            None,
            [],
        ]
        for bad_state in invalid_cases:
            with self.subTest(bad_state=bad_state):
                line = json.dumps({"event": "step_update", "step_update": {"state": bad_state}})
                res = parse_ndjson_line(line)
                self.assertEqual(res.public_metadata["step_update"]["state"], "unknown")

    def test_category_normalization_and_fallback(self):
        valid_cases = [
            ("user_input", "user_input"),
            ("USER_INPUT", "user_input"),
            ("  User_Input  ", "user_input"),
            ("agent_response", "agent_response"),
            ("AGENT_RESPONSE", "agent_response"),
            ("tool", "tool"),
            ("TOOL", "tool"),
            ("  Tool  ", "tool"),
            ("checkpoint", "checkpoint"),
            ("CHECKPOINT", "checkpoint"),
        ]
        for input_cat, expected_cat in valid_cases:
            with self.subTest(category=input_cat):
                line = json.dumps({"event": "step_update", "step_update": {"category": input_cat}})
                res = parse_ndjson_line(line)
                self.assertEqual(res.public_metadata["step_update"]["category"], expected_cat)

        invalid_cases = [
            "thought",
            "message",
            "system_prompt",
            "",
            "   ",
            999,
            False,
            None,
            {},
        ]
        for bad_cat in invalid_cases:
            with self.subTest(bad_cat=bad_cat):
                line = json.dumps({"event": "step_update", "step_update": {"category": bad_cat}})
                res = parse_ndjson_line(line)
                self.assertEqual(res.public_metadata["step_update"]["category"], "unknown")

    def test_usage_allowlist_filtering_under_step_update(self):
        usage_payload = {
            "input_tokens": 100,
            "output_tokens": 50,
            "thinking_tokens": 10.0,      # integral float accepted and converted to int
            "cache_read_tokens": 0,
            "total_tokens": 160,
            "unknown_metric": 42,         # unknown key dropped
            "cost_usd": 0.05,             # unknown key dropped
            "bad_bool": True,
        }
        line = json.dumps({"event": "step_update", "step_update": {"usage": usage_payload}})
        res = parse_ndjson_line(line)
        self.assertEqual(
            res.public_metadata["step_update"]["usage"],
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "thinking_tokens": 10,
                "cache_read_tokens": 0,
                "total_tokens": 160,
            },
        )
        self.assertNotIn("unknown_metric", res.public_metadata["step_update"]["usage"])
        self.assertNotIn("cost_usd", res.public_metadata["step_update"]["usage"])

    def test_usage_invalid_values_rejected(self):
        usage_payload = {
            "input_tokens": True,             # bool rejected
            "output_tokens": -5,              # negative rejected
            "thinking_tokens": "100",         # string rejected
            "cache_read_tokens": 10.5,        # non-integral float rejected
            "total_tokens": 50,               # valid nonnegative int
        }
        line = json.dumps({"event": "step_update", "step_update": {"usage": usage_payload}})
        res = parse_ndjson_line(line)
        self.assertEqual(
            res.public_metadata["step_update"]["usage"],
            {
                "total_tokens": 50,
            },
        )

    def test_usage_omitted_when_all_values_invalid_or_empty(self):
        for bad_usage in (
            {},
            {"unknown_key": 100},
            {"input_tokens": -1, "output_tokens": "abc"},
            "not a mapping",
            None,
        ):
            with self.subTest(bad_usage=bad_usage):
                line = json.dumps({"event": "step_update", "step_update": {"usage": bad_usage}})
                res = parse_ndjson_line(line)
                self.assertNotIn("usage", res.public_metadata["step_update"])

    def test_step_update_private_payload_is_always_none(self):
        line = json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "state": "ACTIVE",
                "category": "tool",
                "response": "SYNTH_PRIVATE_DATA",
                "conversation_id": "SYNTH_CID_DATA",
            },
        })
        res = parse_ndjson_line(line)
        self.assertIsNone(res.private_payload)
        self.assertNotIn("SYNTH_PRIVATE_DATA", json.dumps(res.public_metadata))
        self.assertNotIn("SYNTH_CID_DATA", json.dumps(res.public_metadata))


class ResultEventTests(unittest.TestCase):
    """Verify result event status validation, error mapping, and strict privacy boundary."""

    def test_result_payload_must_be_dict(self):
        for bad_payload in (None, "not a dict", 123, []):
            line = json.dumps({"event": "result", "result": bad_payload})
            res = parse_ndjson_line(line)
            self.assertEqual(
                res.public_metadata,
                {
                    "event": "unknown",
                    "source": "runner",
                    "error": "protocol_error",
                },
            )
            self.assertIsNone(res.private_payload)

    def test_missing_result_payload_key(self):
        line = json.dumps({"event": "result"})
        res = parse_ndjson_line(line)
        self.assertEqual(
            res.public_metadata,
            {
                "event": "unknown",
                "source": "runner",
                "error": "protocol_error",
            },
        )
        self.assertIsNone(res.private_payload)

    def test_invalid_status_yields_protocol_error(self):
        invalid_statuses = [
            "RUNNING",
            "PENDING",
            "OK",
            "ERROR",
            "",
            "   ",
            123,
            True,
            None,
            [],
            "SYNTH_STATUS",
        ]
        for bad_status in invalid_statuses:
            with self.subTest(bad_status=bad_status):
                line = json.dumps({"event": "result", "result": {"status": bad_status}})
                res = parse_ndjson_line(line)
                self.assertEqual(
                    res.public_metadata,
                    {
                        "event": "unknown",
                        "source": "runner",
                        "error": "protocol_error",
                    },
                )
                self.assertIsNone(res.private_payload)

    def test_valid_statuses_without_error(self):
        valid_statuses = [
            ("SUCCESS", "SUCCESS"),
            ("success", "SUCCESS"),
            ("  SUCCESS  ", "SUCCESS"),
            ("FAILURE", "FAILURE"),
            ("failure", "FAILURE"),
            ("CANCELLED", "CANCELLED"),
            ("cancelled", "CANCELLED"),
            ("TIMEOUT", "TIMEOUT"),
            ("timeout", "TIMEOUT"),
        ]
        for input_status, expected_status in valid_statuses:
            with self.subTest(status=input_status):
                line = json.dumps({"event": "result", "result": {"status": input_status}})
                res = parse_ndjson_line(line)
                self.assertEqual(
                    res.public_metadata,
                    {
                        "event": "result",
                        "source": "runner",
                        "status": expected_status,
                    },
                )
                self.assertIsNotNone(res.private_payload)
                self.assertEqual(res.private_payload["status"], expected_status)
                self.assertNotIn("error", res.public_metadata)
                self.assertNotIn("error", res.private_payload)

    def test_success_with_error_maps_to_failure_and_protocol_failure(self):
        canary_err = "SYNTH_CANARY_RAW_ERROR_MESSAGE_REPRESENTING_INTERNAL_EXCEPTION"
        line = json.dumps({
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "error": canary_err,
            },
        })
        res = parse_ndjson_line(line)
        self.assertEqual(res.public_metadata["status"], "FAILURE")
        self.assertEqual(res.public_metadata["error"], "protocol_failure")
        self.assertIsNotNone(res.private_payload)
        self.assertEqual(res.private_payload["status"], "FAILURE")
        self.assertEqual(res.private_payload["error"], "protocol_failure")
        # Verify raw error canary never leaks into public metadata
        self.assertNotIn(canary_err, json.dumps(res.public_metadata))

    def test_failure_or_timeout_or_cancelled_with_error(self):
        for status in ("FAILURE", "TIMEOUT", "CANCELLED"):
            canary_err = f"SYNTH_ERR_ON_{status}"
            line = json.dumps({
                "event": "result",
                "result": {
                    "status": status,
                    "error": canary_err,
                },
            })
            res = parse_ndjson_line(line)
            self.assertEqual(res.public_metadata["status"], status)
            self.assertEqual(res.public_metadata["error"], "protocol_failure")
            self.assertIsNotNone(res.private_payload)
            self.assertEqual(res.private_payload["status"], status)
            self.assertEqual(res.private_payload["error"], "protocol_failure")
            self.assertNotIn(canary_err, json.dumps(res.public_metadata))

    def test_response_and_conversation_id_isolated_in_private_payload(self):
        canary_response = "SYNTH_CANARY_SENSITIVE_MODEL_OUTPUT_XYZ987"
        canary_conversation_id = "SYNTH_CANARY_CONVERSATION_ID_ABC123"
        canary_error = "SYNTH_CANARY_ERROR_TRACEBACK_DEF456"

        line = json.dumps({
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "error": canary_error,
                "response": canary_response,
                "conversation_id": f"  {canary_conversation_id}  ",
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 45,
                },
            },
        })

        res = parse_ndjson_line(line)

        # 1. Assert canaries NEVER appear anywhere in public_metadata json serialization
        public_json = json.dumps(res.public_metadata)
        self.assertNotIn(canary_response, public_json)
        self.assertNotIn(canary_conversation_id, public_json)
        self.assertNotIn(canary_error, public_json)

        # 2. Public metadata contains only closed allowed fields
        self.assertEqual(
            res.public_metadata,
            {
                "event": "result",
                "source": "runner",
                "status": "FAILURE",
                "error": "protocol_failure",
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 45,
                },
            },
        )

        # 3. Private payload contains the private fields
        self.assertIsNotNone(res.private_payload)
        self.assertEqual(res.private_payload["status"], "FAILURE")
        self.assertEqual(res.private_payload["error"], "protocol_failure")
        self.assertEqual(res.private_payload["response"], canary_response)
        self.assertEqual(res.private_payload["conversation_id"], canary_conversation_id)
        self.assertEqual(
            res.private_payload["usage"],
            {
                "input_tokens": 120,
                "output_tokens": 45,
            },
        )

    def test_result_usage_allowlist(self):
        line = json.dumps({
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "unknown_key": 999,
                },
            },
        })
        res = parse_ndjson_line(line)
        expected_usage = {"input_tokens": 10, "output_tokens": 20}
        self.assertEqual(res.public_metadata.get("usage"), expected_usage)
        self.assertIsNotNone(res.private_payload)
        self.assertEqual(res.private_payload.get("usage"), expected_usage)


class SanitizeUsageAllowlistUnitTests(unittest.TestCase):
    """Direct unit tests for sanitize_usage_allowlist helper."""

    def test_non_mapping_returns_none(self):
        for val in (None, 123, "not a dict", [], True, 3.14):
            self.assertIsNone(sanitize_usage_allowlist(val))

    def test_empty_or_no_valid_keys_returns_none(self):
        self.assertIsNone(sanitize_usage_allowlist({}))
        self.assertIsNone(sanitize_usage_allowlist({"unknown": 10}))
        self.assertIsNone(sanitize_usage_allowlist({"input_tokens": -1}))
        self.assertIsNone(sanitize_usage_allowlist({"output_tokens": True}))

    def test_all_allowlist_keys_supported(self):
        all_keys = {k: 10 for k in USAGE_ALLOWLIST}
        sanitized = sanitize_usage_allowlist(all_keys)
        self.assertEqual(sanitized, all_keys)

    def test_integral_float_accepted_and_converted(self):
        sanitized = sanitize_usage_allowlist({"input_tokens": 15.0})
        self.assertEqual(sanitized, {"input_tokens": 15})
        self.assertIsInstance(sanitized["input_tokens"], int)

    def test_non_finite_floats_rejected(self):
        sanitized = sanitize_usage_allowlist({
            "input_tokens": float("nan"),
            "output_tokens": float("inf"),
            "thinking_tokens": float("-inf"),
            "total_tokens": 5,
        })
        self.assertEqual(sanitized, {"total_tokens": 5})

    def test_non_integral_floats_rejected(self):
        sanitized = sanitize_usage_allowlist({
            "input_tokens": 15.5,
            "output_tokens": 20,
        })
        self.assertEqual(sanitized, {"output_tokens": 20})


if __name__ == "__main__":
    unittest.main()
