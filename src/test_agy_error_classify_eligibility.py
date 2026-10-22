"""Tests for agy error classification with transient eligibility 503 envelope.

Verifies:
1. Genuine location / product eligibility blocks take precedence and map to
   eligibility_blocked (even if 503, auth, or quota strings appear).
2. The known transient envelope:
   "Eligibility check failed: UNAVAILABLE (code 503): The service is currently unavailable"
   maps to auth_invalid if AUTH matches, quota_exhausted if QUOTA matches,
   else temporary_no_capacity (and NOT eligibility_blocked).
3. Remaining / unknown "Eligibility check failed..." texts remain eligibility_blocked,
   model "UNAVAILABLE (code 503): No capacity" remains temporary_no_capacity, and all
   other classes remain unchanged.
4. Channel verification: stderr, stdout JSON {"status":"ERROR","error":"..."},
   classify_agy_error(error=...), and classify_result({"ok": False, "error": "..."}).
5. Regression coverage across all classifier classes.
6. agy_account_pool.apply_class_to_state mapping transient envelope to short-duration
   cooldown and NOT unavailable.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from execution_backend.agy_account_pool import (
    _parse_cooldown_until,
    Account,
    AccountPool,
    apply_class_to_state,
    load_pool,
    save_pool,
)
from execution_backend.agy_error_classify import (
    CLASS_AUTH_INVALID,
    CLASS_ELIGIBILITY_BLOCKED,
    CLASS_EMPTY_FAILURE,
    CLASS_OK,
    CLASS_ORDINARY,
    CLASS_QUOTA_EXHAUSTED,
    CLASS_RATE_LIMIT,
    CLASS_TEMPORARY_NO_CAPACITY,
    classify,
    classify_agy_error,
    classify_result,
)

TRANSIENT_503_ENVELOPE = (
    "Eligibility check failed: UNAVAILABLE (code 503): The service is currently unavailable"
)


class TestAgyErrorClassifyEligibility(unittest.TestCase):
    """Test eligibility rules across all input channels."""

    def _assert_classified_all_channels(self, message: str, expected_class: str) -> None:
        """Assert message maps to expected_class via stderr, stdout JSON, classify_agy_error, and classify_result."""
        # Channel 1: via stderr
        res_stderr = classify(stderr=message, rc=1)
        self.assertEqual(
            res_stderr,
            expected_class,
            f"Failed on stderr channel for message {message!r}",
        )

        # Channel 2: via stdout JSON
        res_stdout_json = classify(
            stdout=json.dumps({"status": "ERROR", "error": message}),
            rc=1,
        )
        self.assertEqual(
            res_stdout_json,
            expected_class,
            f"Failed on stdout JSON channel for message {message!r}",
        )

        # Channel 3: via classify_agy_error(error=...)
        res_agy_err = classify_agy_error(error=message, rc=1)
        self.assertEqual(
            res_agy_err,
            expected_class,
            f"Failed on classify_agy_error(error=...) for message {message!r}",
        )

        # Channel 4: via classify_result({"ok": False, "error": "..."})
        res_collect = classify_result({"ok": False, "error": message})
        self.assertEqual(
            res_collect,
            expected_class,
            f"Failed on classify_result for message {message!r}",
        )

    # --- Rule 1: Genuine location / product eligibility block ---

    def test_rule1_location_not_available(self) -> None:
        msg = "Error: not currently available in your location"
        self._assert_classified_all_channels(msg, CLASS_ELIGIBILITY_BLOCKED)

    def test_rule1_not_eligible_for_antigravity(self) -> None:
        msg = "Error: Your account is not eligible for Antigravity"
        self._assert_classified_all_channels(msg, CLASS_ELIGIBILITY_BLOCKED)

    def test_rule1_case_insensitive(self) -> None:
        msg1 = "NOT CURRENTLY AVAILABLE IN YOUR LOCATION"
        self._assert_classified_all_channels(msg1, CLASS_ELIGIBILITY_BLOCKED)
        msg2 = "NOT ELIGIBLE FOR ANTIGRAVITY"
        self._assert_classified_all_channels(msg2, CLASS_ELIGIBILITY_BLOCKED)

    def test_rule1_location_mixed_with_503(self) -> None:
        msg = (
            "Eligibility check failed: UNAVAILABLE (code 503): The service is currently unavailable. "
            "not currently available in your location"
        )
        self._assert_classified_all_channels(msg, CLASS_ELIGIBILITY_BLOCKED)

    def test_rule1_location_mixed_with_auth(self) -> None:
        msg = "not currently available in your location. Please authentication required."
        self._assert_classified_all_channels(msg, CLASS_ELIGIBILITY_BLOCKED)

    def test_rule1_not_eligible_mixed_with_quota(self) -> None:
        msg = "not eligible for Antigravity. RESOURCE_EXHAUSTED quota exceeded."
        self._assert_classified_all_channels(msg, CLASS_ELIGIBILITY_BLOCKED)

    # --- Rule 2: Transient 503 eligibility envelope ---

    def test_rule2_transient_envelope_alone(self) -> None:
        self._assert_classified_all_channels(
            TRANSIENT_503_ENVELOPE,
            CLASS_TEMPORARY_NO_CAPACITY,
        )

    def test_rule2_transient_envelope_flexible_whitespace(self) -> None:
        msg = "Eligibility check failed:   UNAVAILABLE  (code 503):  The service is currently unavailable"
        self._assert_classified_all_channels(msg, CLASS_TEMPORARY_NO_CAPACITY)

    def test_rule2_transient_envelope_case_insensitive(self) -> None:
        msg = "eligibility check failed: unavailable (code 503): the service is currently unavailable"
        self._assert_classified_all_channels(msg, CLASS_TEMPORARY_NO_CAPACITY)

    def test_rule2_transient_envelope_mixed_with_auth(self) -> None:
        msg = f"{TRANSIENT_503_ENVELOPE}\nauthentication required"
        self._assert_classified_all_channels(msg, CLASS_AUTH_INVALID)

    def test_rule2_transient_envelope_mixed_with_quota(self) -> None:
        msg = f"{TRANSIENT_503_ENVELOPE}\nMODEL_CAPACITY_EXHAUSTED: quota exceeded"
        self._assert_classified_all_channels(msg, CLASS_QUOTA_EXHAUSTED)

    def test_rule2_transient_envelope_mixed_with_auth_and_quota_auth_wins(self) -> None:
        msg = f"{TRANSIENT_503_ENVELOPE}\nauthentication failed\nMODEL_CAPACITY_EXHAUSTED"
        self._assert_classified_all_channels(msg, CLASS_AUTH_INVALID)

    # --- Rule 3: Remaining eligibility blocks and other classes unchanged ---

    def test_rule3_other_eligibility_block_remains_eligibility_blocked(self) -> None:
        msg = "Eligibility check failed: Account under review"
        self._assert_classified_all_channels(msg, CLASS_ELIGIBILITY_BLOCKED)

    def test_rule3_other_eligibility_block_mixed_with_auth(self) -> None:
        msg = "Eligibility check failed: Unknown reason\nauthentication required"
        self._assert_classified_all_channels(msg, CLASS_ELIGIBILITY_BLOCKED)

    def test_rule3_other_eligibility_block_mixed_with_quota(self) -> None:
        msg = "Eligibility check failed: Unknown reason\nRESOURCE_EXHAUSTED"
        self._assert_classified_all_channels(msg, CLASS_ELIGIBILITY_BLOCKED)

    def test_rule3_model_503_remains_temporary_no_capacity(self) -> None:
        msg = "API error: UNAVAILABLE (code 503): No capacity available for model on server"
        self._assert_classified_all_channels(msg, CLASS_TEMPORARY_NO_CAPACITY)


class TestAgyErrorClassifyRegression(unittest.TestCase):
    """Regression test rows for every existing class."""

    def test_regression_auth_invalid(self) -> None:
        cases = [
            "authentication required. Please sign in.",
            "error: authentication failed or timed out",
            "not signed in",
            "unauthenticated request",
            "unauthorized access",
            "please login to continue",
        ]
        for msg in cases:
            self.assertEqual(classify(stderr=msg, rc=1), CLASS_AUTH_INVALID, f"Failed on {msg!r}")

    def test_regression_quota_exhausted(self) -> None:
        cases = [
            "MODEL_CAPACITY_EXHAUSTED: capacity exhausted",
            "RESOURCE_EXHAUSTED: Quota exceeded",
            "out of credits for current billing period",
            "no credits available",
            "failed at fetchQuotaStatus",
        ]
        for msg in cases:
            self.assertEqual(classify(stderr=msg, rc=1), CLASS_QUOTA_EXHAUSTED, f"Failed on {msg!r}")

    def test_regression_temporary_no_capacity(self) -> None:
        cases = [
            "no capacity available for model",
            "UNAVAILABLE (code 503)",
            "(code 503): service dropped request",
        ]
        for msg in cases:
            self.assertEqual(classify(stderr=msg, rc=1), CLASS_TEMPORARY_NO_CAPACITY, f"Failed on {msg!r}")

    def test_regression_rate_limit(self) -> None:
        cases = [
            "HTTP 429 Too Many Requests",
            "rate limit exceeded",
            "The model API is currently overloaded",
            "Service temporarily unavailable, please try again later",
        ]
        for msg in cases:
            self.assertEqual(classify(stderr=msg, rc=1), CLASS_RATE_LIMIT, f"Failed on {msg!r}")

    def test_regression_ok(self) -> None:
        # JSON status ok
        for st in ("SUCCESS", "OK", "COMPLETED", "STOP", "DONE"):
            out = json.dumps({"status": st, "response": "SYNTH_PONG"})
            self.assertEqual(classify(stdout=out, rc=0), CLASS_OK, f"Failed on status {st}")
        # Plain model list with rc=0
        models_out = "gemini-3.8-flash\ngemini-2.5-pro\n"
        self.assertEqual(classify(stdout=models_out, rc=0), CLASS_OK)
        # classify_result ok
        self.assertEqual(classify_result({"ok": True, "response": "SYNTH_OK"}), CLASS_OK)

    def test_regression_empty_failure(self) -> None:
        self.assertEqual(classify(stdout="", stderr=""), CLASS_EMPTY_FAILURE)
        self.assertEqual(classify(stdout="", stderr="", rc=0), CLASS_EMPTY_FAILURE)
        self.assertEqual(classify_result(None), CLASS_EMPTY_FAILURE)
        self.assertEqual(classify_result({}), CLASS_EMPTY_FAILURE)
        self.assertEqual(classify_result({"ok": True, "stdout": "", "stderr": "", "error": ""}), CLASS_OK)

    def test_regression_ordinary_task_failure(self) -> None:
        # Non-zero rc with non-matching error
        self.assertEqual(
            classify(stderr="SYNTH_ERR: syntax error in user script", rc=1),
            CLASS_ORDINARY,
        )
        # JSON status ERROR
        self.assertEqual(
            classify(stdout=json.dumps({"status": "ERROR", "error": "file not found"}), rc=0),
            CLASS_ORDINARY,
        )


class TestAccountPoolApplyClassTransientEnvelope(unittest.TestCase):
    """Assert agy_account_pool.apply_class_to_state maps transient envelope to cooldown."""

    def test_transient_envelope_maps_to_short_cooldown_not_unavailable(self) -> None:
        err_cls = classify(stderr=TRANSIENT_503_ENVELOPE, rc=1)
        self.assertEqual(err_cls, CLASS_TEMPORARY_NO_CAPACITY)

        with tempfile.TemporaryDirectory() as tmp_dir:
            synth_home = Path(tmp_dir) / "SYNTH_HOME_1"
            synth_home.mkdir(parents=True, exist_ok=True)
            acc = Account(
                id="SYNTH_ACC_1",
                home=str(synth_home),
                state="available",
            )

            import time
            now_ts = time.time()
            temp_cooldown = 300.0
            changed = apply_class_to_state(
                acc,
                err_cls,
                now=now_ts,
                temp_cooldown_sec=temp_cooldown,
            )

            self.assertTrue(changed)
            # Must be cooldown, NOT unavailable
            self.assertEqual(acc.state, "cooldown")
            self.assertNotEqual(acc.state, "unavailable")
            self.assertIsNotNone(acc.cooldown_until)
            # Short duration cooldown (temp_cooldown_sec), never a day_boundary wait.
            self.assertAlmostEqual(_parse_cooldown_until(acc.cooldown_until), now_ts + temp_cooldown, delta=2.0)

            # Persisted pool test with synthetic pool state
            pool_path = Path(tmp_dir) / "synth_pool.json"
            pool = AccountPool(accounts=[acc], path=pool_path)
            save_pool(pool_path, pool)

            reloaded_pool = load_pool(pool_path)
            reloaded_acc = reloaded_pool.by_id("SYNTH_ACC_1")
            self.assertEqual(reloaded_acc.state, "cooldown")
            self.assertNotEqual(reloaded_acc.state, "unavailable")

    def test_genuine_eligibility_block_maps_to_unavailable(self) -> None:
        genuine_msg = "Error: not currently available in your location"
        err_cls = classify(stderr=genuine_msg, rc=1)
        self.assertEqual(err_cls, CLASS_ELIGIBILITY_BLOCKED)

        with tempfile.TemporaryDirectory() as tmp_dir:
            synth_home = Path(tmp_dir) / "SYNTH_HOME_2"
            synth_home.mkdir(parents=True, exist_ok=True)
            acc = Account(
                id="SYNTH_ACC_2",
                home=str(synth_home),
                state="available",
            )

            changed = apply_class_to_state(acc, err_cls)
            self.assertTrue(changed)
            self.assertEqual(acc.state, "unavailable")
            self.assertIsNone(acc.cooldown_until)


if __name__ == "__main__":
    unittest.main()
