#!/usr/bin/env python3
"""Backend unit tests for agy lead review. Red until agy_review lands.

The review modules and AntigravityCliExecutionBackend hooks are not implemented.
Imports of those modules happen inside helpers so each test fails on its own.
"""
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

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_backend.antigravity_cli_v1 import (  # noqa: E402
    ACCEPTANCE_UNVERIFIED_WARNING,
    AntigravityCliExecutionBackend,
    apply_agy_acceptance_gate,
)
from execution_backend.base import BackendError, BackendStatus  # noqa: E402

FAKE_AGY = SRC / "testdata" / "fake_agy_review.py"
ACCEPTANCE = "Prose acceptance: a lead must judge whether the report explains the change."
BODY = "hello review\n"
RESPONSE = "worker-ok"
REASON_MARK = "Lead review rejected the previous attempt. Reason (verbatim):"
REWORK_TAIL = (
    "Modify the existing files in the current working directory so the acceptance is "
    "met, then stop. Do not wait for further input."
)
INTERRUPTED_NEXT = "open a NEW request citing this request_id"
_FORBIDDEN_ERROR_BITS = (
    "timed_out",
    "wall clock",
    "wall_sec",
    "budget_exhausted",
    "deadline exhausted",
)


def _review_store_mod():
    import execution_backend.agy_review_store as mod

    return mod


def _saved(path: Path, run_id: str) -> dict:
    rec = _review_store_mod().AgyReviewStore(path).get(run_id)
    if rec is None:
        raise AssertionError(f"no review record for {run_id} in {path}")
    return rec


def _write_shim(directory: Path, script: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "agy"
    shim.write_text(
        "#!/bin/sh\n" + f'exec "{sys.executable}" "{script}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return shim


@unittest.skipIf(os.name == "nt", "POSIX shell shim; fake agy is spawned through /bin/sh")
class AgyLeadReviewBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self._backends: list[AntigravityCliExecutionBackend] = []
        self._tmp = tempfile.TemporaryDirectory(prefix="agy-lr-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.registry = self.root / "persist" / "agy-runs.json"
        self.registry.parent.mkdir(parents=True)
        self.store = self.root / "explicit" / "agy-reviews.json"
        self.store.parent.mkdir()
        self.beside = self.registry.parent / "agy-reviews.json"
        self.log = self.root / "fake-agy.jsonl"
        self.script = self.root / "agy-steps.json"
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.shim = _write_shim(self.root / "bin", FAKE_AGY)
        self.assertTrue(FAKE_AGY.is_file(), FAKE_AGY)

    def tearDown(self) -> None:
        for be in self._backends:
            try:
                be.close()
            except Exception:
                pass

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key in (
            "AGY_BIN",
            "COLLAB_AGY_ACCOUNT_POOL",
            "COLLAB_AGY_REVIEW_STORE",
            "COLLAB_AGY_POOL_PRECHECK",
            "AGY_AUTO_APPROVE",
            "COLLAB_AGY_AUTO_APPROVE",
            "COLLAB_AGY_RUN_REGISTRY",
        ):
            env.pop(key, None)
        env["FAKE_AGY_SCRIPT"] = str(self.script)
        env["FAKE_AGY_LOG"] = str(self.log)
        env["PATH"] = os.environ.get("PATH", "")
        return env

    def _backend(self, *, explicit_store: bool = True, pool_path: Path | None = None):
        kwargs = {
            "bin_path": str(self.shim),
            "environ": self._env(),
            "poll_sec": 0.05,
            "timeout_sec": 60,
            "run_registry_path": str(self.registry),
        }
        if explicit_store:
            kwargs["review_store_path"] = str(self.store)
        if pool_path is not None:
            kwargs["account_pool_path"] = str(pool_path)
        be = AntigravityCliExecutionBackend(**kwargs)
        self._backends.append(be)
        return be

    def _steps(self, steps: list) -> None:
        self.script.write_text(json.dumps(steps), encoding="utf-8")

    def _start(self, be, *, charter: dict | None = None, artifacts: list[str] | None = None):
        ch = {
            "acceptance": ACCEPTANCE,
            "max_redos": 1,
            "timeout_sec": 60,
            "goal": "Write the report",
        }
        if charter:
            ch.update(charter)
        return be.start_run(
            title="lead-review",
            directory=str(self.ws),
            instruction="Write report.md",
            artifacts=["report.md"] if artifacts is None else artifacts,
            charter=ch,
        )

    def _log_rows(self) -> list[dict]:
        if not self.log.is_file():
            return []
        rows = []
        for line in self.log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def _assert_fake_used(self, rows: list[dict]) -> None:
        self.assertGreaterEqual(len(rows), 1)
        for row in rows:
            self.assertEqual(Path(row["cwd"]).resolve(), self.ws.resolve())
            self.assertIsInstance(row["pid"], int)
            self.assertGreater(row["pid"], 0)
            prompt = row["prompt"]
            self.assertEqual(row["prompt_sha"], hashlib.sha256(prompt.encode("utf-8")).hexdigest())
            self.assertEqual(row["rework"], "REWORK " in prompt)
            self.assertIsInstance(row["n"], int)

    def _wait_pending(self, be, run_id: str, timeout: float = 8.0) -> list:
        deadline = time.monotonic() + timeout
        last_code = None
        last_rows = None
        while time.monotonic() < deadline:
            last_code, last_rows = be.list_pending_actions(session_id=run_id)
            if last_code == 200 and last_rows:
                return last_rows
            time.sleep(0.05)
        self.fail(f"no review action within {timeout}s code={last_code} rows={last_rows}")

    def _wait_log(self, n: int, timeout: float = 8.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        rows: list[dict] = []
        while time.monotonic() < deadline:
            rows = self._log_rows()
            if len(rows) >= n:
                return rows
            time.sleep(0.05)
        self.fail(f"fake agy log has {len(rows)} lines, want {n}")

    def _wait_idle(self, be, run_id: str, timeout: float = 8.0) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = be.observe_run(run_id)
            if not last.get("busy"):
                return last
            time.sleep(0.05)
        self.fail(f"still busy: {None if last is None else last.get('activity')}")

    def _assert_clean_error(self, text: str) -> None:
        for bit in _FORBIDDEN_ERROR_BITS:
            self.assertNotIn(bit, text)

    def _assert_action(self, row: dict, *, run_id: str, round_no: int, rejections: list[str]) -> str:
        self.assertEqual(row["kind"], "review")
        rid = row["request_id"]
        self.assertEqual(rid, f"agyrev:{run_id}:r{round_no}")
        payload = row["payload"]
        self.assertEqual(payload["acceptance_text"], ACCEPTANCE)
        self.assertEqual(payload["round"], round_no)
        self.assertEqual(payload["max_round"], 2)
        self.assertEqual(payload["previous_rejections"], rejections)
        self.assertEqual(payload["backend"], "antigravity.cli_v1")
        self.assertEqual(payload["finish"], "stop")
        self.assertIsNone(payload["artifact_error"])
        self.assertEqual(payload["tools"], [])
        self.assertEqual(payload["policy_violations"], [])
        self.assertEqual(payload["worker_response_excerpt"], RESPONSE)
        art = payload["artifacts"]["report.md"]
        self.assertEqual(art["sha256"], hashlib.sha256(BODY.encode()).hexdigest())
        self.assertEqual(art["bytes"], len(BODY.encode()))
        self.assertEqual(art["preview"], BODY)
        self.assertFalse(art["truncated"])
        self.assertIsNone(art["contamination"])
        self.assertEqual(len(payload["artifact_hash"]), 64)
        self.assertEqual(
            row["context_hash"],
            hashlib.sha256(f"{payload['artifact_hash']}:{round_no}".encode()).hexdigest(),
        )
        return rid

    def _pool(self) -> Path:
        home = self.root / "homeA"
        home.mkdir()
        path = self.root / "pool.json"
        path.write_text(
            json.dumps(
                {
                    "accounts": [
                        {
                            "id": "A",
                            "home": str(home),
                            "state": "available",
                            "email_mask": "a***@example.com",
                            "notes": "fake primary",
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def _pool_states(self, pool_path: Path) -> dict[str, str]:
        from execution_backend.agy_account_pool import load_pool

        pool = load_pool(pool_path)
        return {acc.id: acc.state for acc in pool.accounts}

    def test_t1_pass(self) -> None:
        """T1: asserts one review on :r1, observe busy, pass collects lead_review and rework.used 0
        red on 49fa3a36: review_store_path is rejected and list_pending_actions stays empty"""
        be = self._backend(explicit_store=True)
        be.enable_lead_review()
        self.assertEqual(be.lead_review_mode, "async_v1")
        caps = be.capabilities()
        self.assertTrue(caps["channels"]["review"])
        self.assertTrue(caps["acceptance"]["lead_review"])
        self.assertFalse(caps["channels"]["permission"])
        self._steps([{"do": "ok", "write": {"report.md": BODY}}])
        started = self._start(be)
        self.assertTrue(started.get("ok"), started)
        run_id = started["run_id"]
        self.assertRegex(run_id, r"^agy_[0-9a-f]{12}$")
        rows = self._wait_pending(be, run_id)
        self.assertEqual(len(rows), 1)
        rid = self._assert_action(rows[0], run_id=run_id, round_no=1, rejections=[])
        obs = be.observe_run(run_id)
        self.assertTrue(obs["busy"])
        self.assertEqual(obs["activity"], "awaiting_review")
        self.assertIsNone(obs["finish"])
        self.assertFalse(obs["finish_successful"])
        self.assertFalse(obs["errored"])
        saved = _saved(self.store, run_id)
        self.assertEqual(saved["state"], "awaiting_review")
        self.assertEqual(saved["request_id"], rid)
        self.assertFalse(self.beside.is_file())
        reason = "looks good"
        resolved = be.resolve_decision(
            rid, verdict="pass", reason=reason, answers=[{"by": "lead"}]
        )
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["kind"], "review")
        self.assertEqual(resolved["controller_state"], "accepted")
        self.assertEqual(resolved["round"], 1)
        self.assertNotIn("idempotent", resolved)
        out = be.collect_result(run_id)
        self.assertTrue(out["ok"])
        self.assertEqual(out["state"], "ok")
        self.assertEqual(out["review"]["status"], "passed")
        self.assertEqual(out["review"]["source"], "lead_review")
        self.assertEqual(out["review"]["evidence"], reason[:240])
        self.assertEqual(out["rework"]["used"], 0)
        self.assertEqual(out["rework"]["max"], 1)
        self.assertEqual(out["rework"]["rounds"], 1)
        self.assertEqual(out["rework"]["history"][0]["verdict"], "pass")
        self.assertEqual(out["rework"]["history"][0]["reason"], reason)
        self.assertEqual(out["rework"]["history"][0]["by"], "lead")
        self.assertEqual(out["rework"]["history"][0]["round"], 1)
        self.assertEqual(out["rework"]["history"][0]["conversation_id"], "conv_1")
        self.assertEqual(out["usage"]["input_tokens"], 10)
        self.assertEqual(out["usage"]["output_tokens"], 5)
        self.assertEqual(out["conversation_id"], "conv_1")
        rows = self._log_rows()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["rework"])
        self._assert_fake_used(rows)

    def test_t2_reject_rework(self) -> None:
        """T2: asserts fail respawns the same run_id with REWORK 1/1, :r2, summed usage, two fake spawns
        red on 49fa3a36: no resolve_decision and list_pending_actions stays empty"""
        reason = 'need "exact" reason: alpha'
        be = self._backend()
        be.enable_lead_review()
        self._steps(
            [
                {"do": "ok", "write": {"report.md": BODY}},
                {"do": "ok", "write": {"report.md": BODY}},
            ]
        )
        started = self._start(be)
        run_id = started["run_id"]
        first = self._wait_pending(be, run_id)
        rid1 = self._assert_action(first[0], run_id=run_id, round_no=1, rejections=[])
        pid1 = self._log_rows()[0]["pid"]
        failed = be.resolve_decision(
            rid1, verdict="fail", reason=reason, answers=[{"by": "lead"}]
        )
        self.assertEqual(failed["controller_state"], "running")
        self.assertEqual(failed["round"], 2)
        second = self._wait_pending(be, run_id)
        self.assertEqual(len(second), 1)
        rid2 = self._assert_action(second[0], run_id=run_id, round_no=2, rejections=[reason])
        self.assertNotEqual(rid2, rid1)
        rows = self._log_rows()
        self.assertEqual(len(rows), 2)
        self._assert_fake_used(rows)
        self.assertNotEqual(rows[1]["pid"], pid1)
        self.assertFalse(rows[0]["rework"])
        self.assertTrue(rows[1]["rework"])
        prompt = rows[1]["prompt"]
        self.assertIn(rows[0]["prompt"], prompt)
        self.assertTrue(any(line.startswith("REWORK 1/1") for line in prompt.splitlines()), prompt)
        self.assertIn(REASON_MARK, prompt)
        self.assertIn(reason, prompt)
        self.assertIn(REWORK_TAIL, prompt)
        passed = be.resolve_decision(
            rid2, verdict="pass", reason="fixed", answers=[{"by": "lead"}]
        )
        self.assertEqual(passed["controller_state"], "accepted")
        self.assertEqual(passed["round"], 2)
        out = be.collect_result(run_id)
        self.assertEqual(out["run_id"], run_id)
        self.assertTrue(out["ok"])
        self.assertEqual(out["review"]["source"], "lead_review")
        self.assertEqual(out["review"]["status"], "passed")
        self.assertEqual(out["rework"]["used"], 1)
        self.assertEqual(out["rework"]["max"], 1)
        self.assertEqual(out["rework"]["rounds"], 2)
        self.assertEqual(out["usage"]["input_tokens"], 20)
        self.assertEqual(out["usage"]["output_tokens"], 10)
        self.assertEqual([item["verdict"] for item in out["rework"]["history"]], ["fail", "pass"])
        self.assertEqual(out["rework"]["history"][0]["conversation_id"], "conv_1")
        self.assertEqual(out["rework"]["history"][1]["conversation_id"], "conv_2")
        self.assertEqual(len(self._log_rows()), 2)

    def test_t3_exhausted(self) -> None:
        """T3: asserts a second fail is rejected_final with budget exhausted (1/1) and agy_err_class is not quota
        red on 49fa3a36: review_store_path is rejected; no rework and no acceptance error_source"""
        reason2 = "quota exceeded but the worker output was fine"
        pool = self._pool()
        be = self._backend(pool_path=pool)
        be.enable_lead_review()
        self._steps(
            [
                {"do": "ok", "write": {"report.md": BODY}},
                {"do": "ok", "write": {"report.md": BODY}},
            ]
        )
        started = self._start(be)
        run_id = started["run_id"]
        rid1 = self._wait_pending(be, run_id)[0]["request_id"]
        be.resolve_decision(rid1, verdict="fail", reason="missing citation", answers=[{"by": "lead"}])
        rid2 = self._wait_pending(be, run_id)[0]["request_id"]
        self.assertEqual(rid2, f"agyrev:{run_id}:r2")
        done = be.resolve_decision(
            rid2, verdict="fail", reason=reason2, answers=[{"by": "lead"}]
        )
        self.assertEqual(done["controller_state"], "rejected_final")
        out = be.collect_result(run_id)
        expected = (
            "acceptance_failed: lead_review_rejected; rework budget exhausted (1/1); "
            f"last reason: {reason2}"
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["state"], "fail")
        self.assertTrue(out["acceptance_failed"])
        self.assertEqual(out["error_source"], "acceptance")
        self.assertEqual(out["error"], expected)
        self.assertEqual(out["review"]["status"], "failed")
        self.assertEqual(out["review"]["source"], "lead_review")
        self.assertEqual(out["review"]["evidence"], expected[:240])
        self.assertEqual(out["rework"]["used"], 1)
        self.assertEqual(out["rework"]["max"], 1)
        self.assertEqual(out["rework"]["rounds"], 2)
        self.assertEqual(out.get("agy_err_class"), "ok")
        self.assertNotIn("quota", str(out.get("agy_err_class") or ""))
        self._assert_clean_error(out["error"])
        rows = self._log_rows()
        self.assertEqual(len(rows), 2)
        self._assert_fake_used(rows)

    def test_t5a_duplicate_verdict_idempotent(self) -> None:
        """T5a: asserts a second identical pass is idempotent and does not spawn again
        red on 49fa3a36: resolve_decision does not exist"""
        be = self._backend()
        be.enable_lead_review()
        self._steps([{"do": "ok", "write": {"report.md": BODY}}])
        started = self._start(be)
        run_id = started["run_id"]
        rid = self._wait_pending(be, run_id)[0]["request_id"]
        first = be.resolve_decision(rid, verdict="pass", reason="ok", answers=[{"by": "lead"}])
        self.assertEqual(first["controller_state"], "accepted")
        self.assertNotIn("idempotent", first)
        again = be.resolve_decision(rid, verdict="approve", reason="ok", answers=[{"by": "lead"}])
        self.assertTrue(again["ok"])
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["request_id"], rid)
        self.assertEqual(again["kind"], "review")
        self.assertEqual(again["controller_state"], "accepted")
        self.assertEqual(len(self._log_rows()), 1)
        out = be.collect_result(run_id)
        self.assertTrue(out["ok"])
        self.assertEqual(out["review"]["source"], "lead_review")
        self.assertEqual(out["review"]["status"], "passed")
        self._assert_fake_used(self._log_rows())

    def test_t5b_old_request_after_rework(self) -> None:
        """T5b: asserts resolving :r1 after rework raises ValueError, does not spawn, and leaves state unchanged
        red on 49fa3a36: resolve_decision does not exist"""
        be = self._backend()
        be.enable_lead_review()
        self._steps(
            [
                {"do": "ok", "write": {"report.md": BODY}},
                {"do": "sleep", "sec": 3, "write": {"report.md": BODY}},
            ]
        )
        started = self._start(be)
        run_id = started["run_id"]
        rid1 = self._wait_pending(be, run_id)[0]["request_id"]
        be.resolve_decision(rid1, verdict="fail", reason="redo", answers=[{"by": "lead"}])
        self._wait_log(2)
        before = _saved(self.store, run_id)
        self.assertEqual(before["state"], "running")
        self.assertEqual(before["round"], 2)
        obs = be.observe_run(run_id)
        self.assertTrue(obs["busy"])
        with self.assertRaises(ValueError) as ctx:
            be.resolve_decision(rid1, verdict="pass", reason="late", answers=[{"by": "lead"}])
        message = str(ctx.exception)
        self.assertTrue(
            "stale" in message or "already resolved" in message,
            message,
        )
        after = _saved(self.store, run_id)
        self.assertEqual(after["state"], "running")
        self.assertEqual(after["round"], 2)
        self.assertEqual(after["redos"], before["redos"])
        self.assertEqual(len(self._log_rows()), 2)
        self.assertTrue(be.observe_run(run_id)["busy"])
        self._assert_fake_used(self._log_rows())

    def test_t5f_conflicting_verdict(self) -> None:
        """T5f: asserts a conflicting verdict raises ValueError and leaves the accepted state unchanged
        red on 49fa3a36: resolve_decision does not exist"""
        be = self._backend()
        be.enable_lead_review()
        self._steps([{"do": "ok", "write": {"report.md": BODY}}])
        started = self._start(be)
        run_id = started["run_id"]
        rid = self._wait_pending(be, run_id)[0]["request_id"]
        be.resolve_decision(rid, verdict="pass", reason="ok", answers=[{"by": "lead"}])
        with self.assertRaises(ValueError) as ctx:
            be.resolve_decision(rid, verdict="fail", reason="nope", answers=[{"by": "lead"}])
        self.assertIn("already resolved as pass", str(ctx.exception))
        saved = _saved(self.store, run_id)
        self.assertEqual(saved["state"], "accepted")
        self.assertEqual(saved["round"], 1)
        self.assertEqual(len(self._log_rows()), 1)
        out = be.collect_result(run_id)
        self.assertTrue(out["ok"])
        self.assertEqual(out["review"]["status"], "passed")
        self.assertEqual(out["review"]["source"], "lead_review")
        self._assert_fake_used(self._log_rows())

    def test_t5g_unavailable_on_stale_request(self) -> None:
        """T5g: asserts unavailable on a stale request raises ValueError and leaves the terminal round untouched
        red on 49fa3a36: resolve_decision does not exist"""
        be = self._backend()
        be.enable_lead_review()
        self._steps([{"do": "ok", "write": {"report.md": BODY}}])
        started = self._start(be)
        run_id = started["run_id"]
        rid = self._wait_pending(be, run_id)[0]["request_id"]
        be.resolve_decision(rid, verdict="pass", reason="ok", answers=[{"by": "lead"}])
        stale = f"agyrev:{run_id}:r2"
        with self.assertRaises(ValueError) as ctx:
            be.resolve_decision(
                stale,
                verdict="unavailable",
                reason="lead_review_unavailable: timeout after 2 attempt(s)",
                answers=[{"by": "system"}],
            )
        message = str(ctx.exception)
        self.assertIn(f"stale review request {stale}", message)
        self.assertIn("current round r1", message)
        self.assertIn("accepted", message)
        saved = _saved(self.store, run_id)
        self.assertEqual(saved["state"], "accepted")
        self.assertEqual(saved["round"], 1)
        self.assertFalse(saved.get("unavailable_reason"))
        recorded = be.review_outcome(rid)
        self.assertEqual(recorded["verdict"], "pass")
        self.assertIsNone(be.review_outcome(stale))
        out = be.collect_result(run_id)
        self.assertTrue(out["ok"])
        self.assertEqual(out["review"]["status"], "passed")
        self.assertEqual(out["review"]["source"], "lead_review")
        self.assertNotIn("lead_review", out)
        self.assertEqual(len(self._log_rows()), 1)
        self._assert_fake_used(self._log_rows())

    def test_t8_deterministic_not_enabled(self) -> None:
        """T8: asserts lead_review_mode is "" and, as guardrail, caps stay off, no pending review, no store file
        red on 49fa3a36: lead_review_mode property does not exist"""
        be = self._backend(explicit_store=False)
        self.assertEqual(be.lead_review_mode, "")
        caps = be.capabilities()
        self.assertFalse(caps["channels"]["review"])
        self.assertFalse(caps["channels"]["permission"])
        self.assertFalse(caps["acceptance"]["lead_review"])
        self.assertTrue(caps["acceptance"]["exact_content"])
        self._steps([{"do": "ok", "write": {"report.md": BODY}}])
        started = self._start(be)
        self.assertTrue(started.get("ok"), started)
        run_id = started["run_id"]
        self._wait_idle(be, run_id)
        code, rows = be.list_pending_actions(session_id=run_id)
        self.assertEqual(code, 200)
        self.assertEqual(rows, [])
        self.assertFalse(self.beside.is_file())
        self.assertFalse(self.store.is_file())
        out = be.collect_result(run_id)
        self.assertTrue(out["ok"])
        self.assertNotIn("rework", out)
        self.assertNotEqual((out.get("review") or {}).get("source"), "lead_review")
        logged = self._log_rows()
        self.assertEqual(len(logged), 1)
        self._assert_fake_used(logged)

    def test_t9_permission_and_unknown_ids(self) -> None:
        """T9: asserts resolve_decision on permission and unknown ids raises BackendError; reply_permission stays 501 (guardrail)
        red on 49fa3a36: resolve_decision does not exist"""
        be = self._backend(explicit_store=False)
        with self.assertRaises(BackendError) as ctx:
            be.resolve_decision("perm_req_1", verdict="once", reason="no")
        self.assertEqual(ctx.exception.capability, "resolve_decision")
        self.assertEqual(ctx.exception.status, BackendStatus.FAILED)
        be.enable_lead_review()
        code, body = be.reply_permission("perm_req_1", "once")
        self.assertEqual(code, 501)
        self.assertFalse(body["ok"])
        self.assertEqual(body["capability"], "reply_permission")
        for request_id in (
            "perm_req_1",
            "agyrev:not-valid",
            "agyrev:agy_0123456789ab:r1",
        ):
            with self.assertRaises(BackendError) as ctx:
                be.resolve_decision(request_id, verdict="pass", reason="no")
            self.assertEqual(ctx.exception.capability, "resolve_decision")
            self.assertEqual(ctx.exception.status, BackendStatus.FAILED)
        self.assertFalse(self.log.is_file())

    def test_t12_lease_released_at_review(self) -> None:
        """T12: asserts the pool lease is free at awaiting_review, busy again during rework, and free at the end
        red on 49fa3a36: no awaiting_review release; the lease stays busy until collect"""
        pool = self._pool()
        be = self._backend(pool_path=pool)
        be.enable_lead_review()
        self._steps(
            [
                {"do": "ok", "write": {"report.md": BODY}},
                {"do": "sleep", "sec": 2, "write": {"report.md": BODY}},
            ]
        )
        started = self._start(be)
        run_id = started["run_id"]
        self.assertEqual(started.get("agy_profile"), "A")
        rid1 = self._wait_pending(be, run_id)[0]["request_id"]
        self.assertEqual(self._pool_states(pool), {"A": "available"})
        failed = be.resolve_decision(
            rid1, verdict="fail", reason="redo", answers=[{"by": "lead"}]
        )
        self.assertEqual(failed["controller_state"], "running")
        self.assertEqual(self._pool_states(pool), {"A": "busy"})
        rid2 = self._wait_pending(be, run_id)[0]["request_id"]
        self.assertEqual(rid2, f"agyrev:{run_id}:r2")
        self.assertEqual(self._pool_states(pool), {"A": "available"})
        be.resolve_decision(rid2, verdict="pass", reason="fixed", answers=[{"by": "lead"}])
        out = be.collect_result(run_id)
        self.assertTrue(out["ok"])
        self.assertEqual(out["rework"]["used"], 1)
        self.assertNotIn("busy", self._pool_states(pool).values())
        rows = self._log_rows()
        self.assertEqual(len(rows), 2)
        self._assert_fake_used(rows)

    def test_t13_list_pending_alone(self) -> None:
        """T13: asserts list_pending_actions alone, with no observe_run from the test, produces the review
        red on 49fa3a36: list_pending_actions is always empty"""
        be = self._backend()
        be.enable_lead_review()
        self._steps([{"do": "ok", "write": {"report.md": BODY}}])
        started = self._start(be)
        run_id = started["run_id"]
        rows = self._wait_pending(be, run_id)
        self.assertEqual(len(rows), 1)
        self._assert_action(rows[0], run_id=run_id, round_no=1, rejections=[])
        self.assertEqual(len(self._log_rows()), 1)
        self._assert_fake_used(self._log_rows())

    def test_t6c_restart_during_rework(self) -> None:
        """T6c: asserts restart while rework is running becomes interrupted_worker, does not respawn, and keeps redos
        red on 49fa3a36: review state is not persisted and the run id does not survive a new backend"""
        be = self._backend()
        be.enable_lead_review()
        self._steps(
            [
                {"do": "ok", "write": {"report.md": BODY}},
                {"do": "hang", "write": {"report.md": BODY}},
            ]
        )
        started = self._start(be)
        run_id = started["run_id"]
        rid1 = self._wait_pending(be, run_id)[0]["request_id"]
        be.resolve_decision(rid1, verdict="fail", reason="redo", answers=[{"by": "lead"}])
        self._wait_log(2)
        before = _saved(self.store, run_id)
        self.assertEqual(before["redos"], 1)
        self.assertEqual(before["round"], 2)
        self.assertEqual(before["state"], "running")
        be.close()
        be2 = self._backend()
        be2.enable_lead_review()
        self.assertEqual(len(self._log_rows()), 2)
        obs = be2.observe_run(run_id)
        self.assertFalse(obs["busy"])
        out = be2.collect_result(run_id)
        expected = (
            "lead_review_interrupted: round 2 worker lost in service restart; "
            "used reworks 1/1 are kept"
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["state"], "fail")
        self.assertEqual(out["error"], expected)
        self._assert_clean_error(out["error"])
        self.assertEqual(out["review"]["status"], "failed")
        self.assertEqual(out["review"]["source"], "lead_review")
        self.assertEqual(out["review"]["evidence"], "lead_review_interrupted")
        self.assertEqual(out["lead_review"]["outcome"], "interrupted")
        self.assertEqual(out["lead_review"]["code"], "lead_review_interrupted")
        self.assertEqual(out["lead_review"]["next_step"], INTERRUPTED_NEXT)
        self.assertTrue(out["lead_review"]["human_action_required"])
        self.assertEqual(out["rework"]["used"], 1)
        self.assertEqual(out["rework"]["max"], 1)
        self.assertEqual(out["rework"]["rounds"], 2)
        self.assertEqual(len(self._log_rows()), 2)
        self._assert_fake_used(self._log_rows())

    def test_t6_restore_awaiting_review(self) -> None:
        """T6-restore: asserts a restarted backend restores awaiting_review with the same request_id from the beside-registry store
        red on 49fa3a36: enable_lead_review does not exist and a new backend does not restore the run"""
        be = self._backend(explicit_store=False)
        be.enable_lead_review()
        self._steps([{"do": "ok", "write": {"report.md": BODY}}])
        started = self._start(be)
        run_id = started["run_id"]
        rows = self._wait_pending(be, run_id)
        rid = self._assert_action(rows[0], run_id=run_id, round_no=1, rejections=[])
        self.assertTrue(self.beside.is_file())
        self.assertFalse(self.store.is_file())
        be.close()
        be2 = self._backend(explicit_store=False)
        be2.enable_lead_review()
        restored = be2.list_pending_actions(session_id=run_id)
        self.assertEqual(restored[0], 200)
        self.assertEqual(len(restored[1]), 1)
        self.assertEqual(restored[1][0]["request_id"], rid)
        self._assert_action(restored[1][0], run_id=run_id, round_no=1, rejections=[])
        obs = be2.observe_run(run_id)
        self.assertTrue(obs["busy"])
        self.assertEqual(obs["activity"], "awaiting_review")
        self.assertIsNone(obs["finish"])
        resolved = be2.resolve_decision(
            rid, verdict="pass", reason="still good", answers=[{"by": "lead"}]
        )
        self.assertEqual(resolved["controller_state"], "accepted")
        out = be2.collect_result(run_id)
        self.assertTrue(out["ok"])
        self.assertEqual(out["review"]["source"], "lead_review")
        self.assertEqual(out["review"]["evidence"], "still good")
        self.assertEqual(len(self._log_rows()), 1)
        self._assert_fake_used(self._log_rows())

    def test_c4_backend_restart_after_fail_applied(self) -> None:
        """C4-backend: asserts a stored round-2 running fail restarts as interrupted, review_outcome(:r1) is fail, and fake agy is not spawned again
        red on 49fa3a36: the review store and review_outcome do not exist, so restart cannot keep the fail"""
        reason = "redo exactly"
        be = self._backend()
        be.enable_lead_review()
        self._steps(
            [
                {"do": "ok", "write": {"report.md": BODY}},
                {"do": "hang"},
            ]
        )
        started = self._start(be)
        run_id = started["run_id"]
        rid1 = self._wait_pending(be, run_id)[0]["request_id"]
        failed = be.resolve_decision(
            rid1, verdict="fail", reason=reason, answers=[{"by": "lead"}]
        )
        self.assertEqual(failed["controller_state"], "running")
        self._wait_log(2)
        saved = _saved(self.store, run_id)
        self.assertEqual(saved["state"], "running")
        self.assertEqual(saved["round"], 2)
        self.assertEqual(saved["redos"], 1)
        self.assertEqual(saved["resolutions"][rid1]["verdict"], "fail")
        self.assertEqual(saved["resolutions"][rid1]["reason"], reason)
        count = len(self._log_rows())
        be.close()
        be2 = self._backend()
        be2.enable_lead_review()
        time.sleep(0.2)
        self.assertEqual(len(self._log_rows()), count)
        outcome = be2.review_outcome(rid1)
        self.assertEqual(outcome["verdict"], "fail")
        self.assertEqual(outcome["reason"], reason)
        self.assertEqual(outcome["by"], "lead")
        self.assertEqual(outcome["controller_state"], "interrupted_worker")
        out = be2.collect_result(run_id)
        self.assertIn("lead_review_interrupted", out["error"])
        self.assertIn("used reworks 1/1 are kept", out["error"])
        self.assertEqual(len(self._log_rows()), count)
        self._assert_fake_used(self._log_rows())

    def test_d11_missing_artifact_worker_failed(self) -> None:
        """D11: asserts a clean exit with a missing artifact does not open a review and records worker_failed
        red on 49fa3a36: enable_lead_review does not exist and no worker_failed review record is stored"""
        be = self._backend()
        be.enable_lead_review()
        self._steps([{"do": "ok"}])
        started = self._start(be)
        run_id = started["run_id"]
        self._wait_idle(be, run_id)
        code, rows = be.list_pending_actions(session_id=run_id)
        self.assertEqual(code, 200)
        self.assertEqual(rows, [])
        saved = _saved(self.store, run_id)
        self.assertEqual(saved["state"], "worker_failed")
        self.assertIsNone(saved.get("request_id"))
        out = be.collect_result(run_id)
        self.assertFalse(out["ok"])
        self.assertIn("report.md", out["missing"])
        self.assertNotEqual((out.get("review") or {}).get("source"), "lead_review")
        self.assertNotIn("rework", out)
        logged = self._log_rows()
        self.assertEqual(len(logged), 1)
        self._assert_fake_used(logged)


class AgyLeadReviewGateTests(unittest.TestCase):
    def test_t11_gate_keeps_lead_review_record(self) -> None:
        """T11: asserts apply_agy_acceptance_gate keeps a lead_review record and adds no unverified warning
        red on 49fa3a36: prose acceptance overwrites review to unsupported"""
        work = tempfile.mkdtemp(prefix="agy-lr-gate-")
        self.addCleanup(lambda: __import__("shutil").rmtree(work, ignore_errors=True))
        result = {
            "ok": True,
            "state": "ok",
            "review": {"status": "passed", "source": "lead_review", "evidence": "ship it"},
            "rework": {"used": 0, "max": 1, "rounds": 1, "history": []},
        }
        charter = {"acceptance": ACCEPTANCE, "force_lead_review": False}
        out = apply_agy_acceptance_gate(charter=charter, workdir=work, result=result)
        self.assertIs(out, result)
        self.assertEqual(out["review"]["source"], "lead_review")
        self.assertEqual(out["review"]["status"], "passed")
        self.assertEqual(out["review"]["evidence"], "ship it")
        self.assertTrue(out["ok"])
        self.assertFalse(out["force_lead_review"])
        self.assertNotIn(ACCEPTANCE_UNVERIFIED_WARNING, out.get("warnings") or [])
        self.assertNotIn(ACCEPTANCE_UNVERIFIED_WARNING, out.get("notes") or [])


if __name__ == "__main__":
    unittest.main()
