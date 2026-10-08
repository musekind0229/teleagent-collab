#!/usr/bin/env python3
"""Client display tests for agy decision labels.

Red until bin/hermes-collab-request.py ``_decision_brief`` relabels agy rows.
Loads that script the same way src/test_client_lead_review_summary.py does.
Does not start a service and does not call a model. The server row stays unchanged.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "hermes-collab-request.py"

_DECISION_ID = "dec_75a651c7e078"
_TASK_ID = "task_f2e1eb857a0c767e"
_RUN_ID = "agy_8c73e8cd71da"
_AGY_REQUEST_ID = "agyrev:agy_8c73e8cd71da:r1"

_OLD_TITLE = "TeleAgent review"
_AGY_TITLE = "agy review"
_CONTAMINATED = "TeleAgent review (CONTAMINATED)"
_AGY_CONTAMINATED = "agy review (CONTAMINATED)"
_OLD_REASON = "TeleAgent worker requires a bounded decision"
_AGY_REASON = "agy run awaits a lead review decision"
_PAYLOAD_SUMMARY = "review: finish=stop; violations=0"

_LEAD = {
    "status": "running",
    "round": 1,
    "attempt": 1,
    "max_attempts": 2,
}


def _load_module():
    spec = importlib.util.spec_from_file_location("hermes_collab_request", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HCR = _load_module()


def _live_row() -> dict:
    """Trimmed agy pending row from a live run."""
    return {
        "decision_id": _DECISION_ID,
        "request_id": _AGY_REQUEST_ID,
        "kind": "artifact_review",
        "title": _OLD_TITLE,
        "reason": _OLD_REASON,
        "task_id": _TASK_ID,
        "run_id": _RUN_ID,
        "status": "pending",
        "backend_kind": "review",
        "backend_request_id": _AGY_REQUEST_ID,
        "awaiting": "lead",
        "details": {
            "backend_kind": "review",
            "backend_request_id": _AGY_REQUEST_ID,
            "context_hash": "x",
            "lead_review": dict(_LEAD),
            "payload": {
                "backend": "antigravity.cli_v1",
                "round": 1,
                "max_round": 3,
                "artifacts": {},
                "artifact_hash": "",
                "artifact_error": None,
                "tools": [],
                "tool_evidence": {"source": "unavailable", "calls": 0},
                "review_note": "n",
                "policy_violations": [],
                "finish": "stop",
                "acceptance_text": "a",
                "previous_rejections": [],
                "worker_response_excerpt": "done",
            },
        },
    }


def _expected(
    title: str,
    reason: str,
    summary: str,
    *,
    decision_id: str = _DECISION_ID,
    task_id: str = _TASK_ID,
    lead: dict | None = _LEAD,
    awaiting: str | None = "lead",
) -> dict:
    brief = {
        "decision_id": decision_id,
        "kind": "artifact_review",
        "title": title,
        "task_id": task_id,
        "status": "pending",
        "reason": reason,
        "summary": summary,
    }
    if awaiting is not None:
        brief["awaiting"] = awaiting
    if lead is not None:
        brief["lead_review"] = dict(lead)
    return brief


def _drop_payload(row: dict) -> dict:
    row = copy.deepcopy(row)
    details = row.get("details")
    if isinstance(details, dict):
        details.pop("payload", None)
    return row


def _plain_ids(row: dict, request_id: str = "req-plain-1") -> dict:
    """Point every id slot at a value that does not start with agyrev:."""
    row = copy.deepcopy(row)
    row["request_id"] = request_id
    row["backend_request_id"] = request_id
    details = row.get("details")
    if isinstance(details, dict):
        details["backend_request_id"] = request_id
    return row


def _set_backend(row: dict, backend: object) -> dict:
    row = copy.deepcopy(row)
    details = row.get("details")
    if isinstance(details, dict) and isinstance(details.get("payload"), dict):
        details["payload"]["backend"] = backend
    return row


def _status_payload(row: dict) -> dict:
    return {
        "ok": True,
        "state": "running",
        "request_id": "goal_agy_label",
        "tasks": [
            {
                "task_id": row.get("task_id") or _TASK_ID,
                "title": "implementation",
                "status": "running",
            }
        ],
        "pending_decisions": [row],
    }


class ClientAgyDecisionLabelTests(unittest.TestCase):
    def _brief(self, row: dict) -> dict:
        original = copy.deepcopy(row)
        brief = HCR._decision_brief(row)
        self.assertEqual(row, original)
        return brief

    def _assert_no_teleagent(self, brief: dict) -> None:
        for key in ("title", "reason", "summary"):
            self.assertNotIn("TeleAgent", brief[key], brief)

    def test_tl_live_agy_row_relabels_title_reason_and_keeps_other_fields(self) -> None:
        """asserts the live agy row brief renames title to agy review and reason to agy run awaits a lead review decision, keeps the payload summary and the other keys, has no TeleAgent in title/reason/summary, and does not mutate the row."""
        row = _live_row()
        brief = self._brief(row)
        expected = _expected(_AGY_TITLE, _AGY_REASON, _PAYLOAD_SUMMARY)
        self.assertEqual(brief, expected)
        self._assert_no_teleagent(brief)
        self.assertEqual(row["title"], _OLD_TITLE)
        self.assertEqual(row["reason"], _OLD_REASON)
        self.assertEqual(row["details"]["payload"]["backend"], "antigravity.cli_v1")

    def test_tl_contaminated_title_relabels(self) -> None:
        """asserts a leading TeleAgent review (CONTAMINATED) title and the matching summary become agy review (CONTAMINATED) on an agy row."""
        row = _live_row()
        row["title"] = _CONTAMINATED
        brief = self._brief(row)
        expected = _expected(_AGY_CONTAMINATED, _AGY_REASON, _AGY_CONTAMINATED)
        self.assertEqual(brief, expected)
        self._assert_no_teleagent(brief)

    def test_tl_summary_starting_with_teleagent_review_relabels(self) -> None:
        """asserts a summary that starts with TeleAgent review is relabelled on its own when the title is the plain TeleAgent review string."""
        row = _live_row()
        row["details"]["summary"] = _CONTAMINATED
        brief = self._brief(row)
        expected = _expected(_AGY_TITLE, _AGY_REASON, _AGY_CONTAMINATED)
        self.assertEqual(brief, expected)
        self._assert_no_teleagent(brief)
        self.assertEqual(row["details"]["summary"], _CONTAMINATED)

    def test_tl_summary_equal_to_worker_reason_relabels(self) -> None:
        """asserts a summary equal to TeleAgent worker requires a bounded decision becomes agy run awaits a lead review decision, and a title without that prefix stays."""
        row = {
            "decision_id": "dec_reason_summary",
            "request_id": "agyrev:agy_8c73e8cd71da:r2",
            "kind": "artifact_review",
            "title": "artifact_review",
            "reason": _OLD_REASON,
            "task_id": "task_x",
            "status": "pending",
            "awaiting": "lead",
            "details": {
                "lead_review": {
                    "status": "running",
                    "round": 2,
                    "attempt": 1,
                    "max_attempts": 2,
                }
            },
        }
        brief = self._brief(row)
        expected = _expected(
            "artifact_review",
            _AGY_REASON,
            _AGY_REASON,
            decision_id="dec_reason_summary",
            task_id="task_x",
            lead={
                "status": "running",
                "round": 2,
                "attempt": 1,
                "max_attempts": 2,
            },
        )
        self.assertEqual(brief, expected)
        self._assert_no_teleagent(brief)

    def test_tl_custom_title_and_reason_on_agy_row_stay(self) -> None:
        """asserts an agy row keeps a title and reason that are not the TeleAgent display strings, including a lowercase teleagent review title."""
        custom_title = "请复核 hello.txt"
        custom_reason = "look at the diff"
        row = _drop_payload(_live_row())
        row["title"] = custom_title
        row["reason"] = custom_reason
        brief = self._brief(row)
        self.assertEqual(brief, _expected(custom_title, custom_reason, custom_title))
        self._assert_no_teleagent(brief)

        lower = _drop_payload(_live_row())
        lower["title"] = "teleagent review"
        lower["reason"] = custom_reason
        lower_brief = self._brief(lower)
        self.assertEqual(
            lower_brief,
            _expected("teleagent review", custom_reason, "teleagent review"),
        )

    def test_tl_nonmatching_reason_stays_while_title_relabels(self) -> None:
        """asserts an agy row relabels TeleAgent review in the title and summary and leaves a different reason unchanged."""
        row = _drop_payload(_live_row())
        row["reason"] = "quota exceeded"
        brief = self._brief(row)
        expected = _expected(_AGY_TITLE, "quota exceeded", _AGY_TITLE)
        self.assertEqual(brief, expected)
        self._assert_no_teleagent(brief)

    def test_tl_antigravity_backend_prefix_alone_relabels(self) -> None:
        """asserts payload.backend starting with antigravity. relabels the brief when no id starts with agyrev:."""
        for backend in ("antigravity.cli_v1", "antigravity.api", "antigravity."):
            with self.subTest(backend=backend):
                row = _plain_ids(_set_backend(_live_row(), backend))
                brief = self._brief(row)
                expected = _expected(_AGY_TITLE, _AGY_REASON, _PAYLOAD_SUMMARY)
                self.assertEqual(brief, expected)
                self._assert_no_teleagent(brief)

    def test_tl_no_payload_details_backend_request_id_agyrev_relabels(self) -> None:
        """asserts a row with no details.payload still relabels when details.backend_request_id starts with agyrev:."""
        row = _drop_payload(_live_row())
        row.pop("backend_request_id", None)
        row.pop("request_id", None)
        self.assertTrue(str(row["details"]["backend_request_id"]).startswith("agyrev:"))
        brief = self._brief(row)
        expected = _expected(_AGY_TITLE, _AGY_REASON, _AGY_TITLE)
        self.assertEqual(brief, expected)
        self._assert_no_teleagent(brief)

    def test_tl_no_payload_top_level_backend_request_id_agyrev_relabels(self) -> None:
        """asserts a row with no details.payload still relabels when the top-level backend_request_id starts with agyrev:."""
        row = _drop_payload(_live_row())
        row["details"].pop("backend_request_id", None)
        row.pop("request_id", None)
        row["backend_request_id"] = _AGY_REQUEST_ID
        brief = self._brief(row)
        expected = _expected(_AGY_TITLE, _AGY_REASON, _AGY_TITLE)
        self.assertEqual(brief, expected)
        self._assert_no_teleagent(brief)

    def test_tl_no_payload_request_id_agyrev_relabels(self) -> None:
        """asserts a row with no details.payload still relabels when request_id starts with agyrev:."""
        row = _drop_payload(_live_row())
        row["details"].pop("backend_request_id", None)
        row.pop("backend_request_id", None)
        self.assertTrue(str(row["request_id"]).startswith("agyrev:"))
        brief = self._brief(row)
        expected = _expected(_AGY_TITLE, _AGY_REASON, _AGY_TITLE)
        self.assertEqual(brief, expected)
        self._assert_no_teleagent(brief)

    def test_tl_request_id_agyrev_without_details_relabels(self) -> None:
        """asserts request_id starting with agyrev: relabels the brief when the row has no details object."""
        row = {
            "decision_id": "dec_bare",
            "request_id": _AGY_REQUEST_ID,
            "kind": "artifact_review",
            "title": _OLD_TITLE,
            "reason": _OLD_REASON,
            "task_id": _TASK_ID,
            "status": "pending",
            "awaiting": "lead",
        }
        brief = self._brief(row)
        expected = _expected(
            _AGY_TITLE,
            _AGY_REASON,
            _AGY_TITLE,
            decision_id="dec_bare",
            lead=None,
        )
        self.assertEqual(brief, expected)
        self._assert_no_teleagent(brief)
        self.assertNotIn("lead_review", brief)

    def test_tl_agyrev_id_relabels_even_with_windows_backend(self) -> None:
        """asserts an agyrev: id relabels the brief when payload.backend is windows_supervised.v1."""
        row = _plain_ids(_set_backend(_live_row(), "windows_supervised.v1"), "winreq:1")
        row["details"]["backend_request_id"] = _AGY_REQUEST_ID
        brief = self._brief(row)
        expected = _expected(_AGY_TITLE, _AGY_REASON, _PAYLOAD_SUMMARY)
        self.assertEqual(brief, expected)
        self._assert_no_teleagent(brief)
        self.assertEqual(row["details"]["payload"]["backend"], "windows_supervised.v1")

    def test_tl_windows_supervised_row_stays_teleagent_review(self) -> None:
        """asserts a windows_supervised.v1 row with non-agy ids keeps TeleAgent review and the worker reason byte-for-byte and is not mutated."""
        row = _plain_ids(_set_backend(_live_row(), "windows_supervised.v1"), "winreq:1")
        brief = self._brief(row)
        expected = _expected(_OLD_TITLE, _OLD_REASON, _PAYLOAD_SUMMARY)
        self.assertEqual(brief, expected)
        self.assertEqual(brief["title"], _OLD_TITLE)
        self.assertEqual(row["title"], _OLD_TITLE)

    def test_tl_teleagent_backend_row_stays_teleagent_review(self) -> None:
        """asserts a teleagent.cli_v1 payload backend with non-agy ids keeps TeleAgent review byte-for-byte."""
        row = _plain_ids(_set_backend(_live_row(), "teleagent.cli_v1"), "teleagent-req-1")
        brief = self._brief(row)
        self.assertEqual(brief, _expected(_OLD_TITLE, _OLD_REASON, _PAYLOAD_SUMMARY))
        self.assertEqual(brief["title"], _OLD_TITLE)

    def test_tl_missing_payload_backend_stays_teleagent_review(self) -> None:
        """asserts a row with no payload.backend and no agyrev: id keeps TeleAgent review byte-for-byte, with or without a payload object."""
        missing_key = _plain_ids(_live_row(), "req-plain-1")
        missing_key["details"]["payload"].pop("backend")
        brief = self._brief(missing_key)
        self.assertEqual(brief, _expected(_OLD_TITLE, _OLD_REASON, _PAYLOAD_SUMMARY))
        self.assertEqual(brief["title"], _OLD_TITLE)

        absent = _plain_ids(_drop_payload(_live_row()), "req-plain-1")
        absent_brief = self._brief(absent)
        self.assertEqual(absent_brief, _expected(_OLD_TITLE, _OLD_REASON, _OLD_TITLE))
        self.assertEqual(absent_brief["title"], _OLD_TITLE)

    def test_tl_near_miss_prefixes_stay_teleagent_review(self) -> None:
        """asserts antigravity without the dot, a non-string backend, and ids that do not start with agyrev: keep TeleAgent review."""
        cases = {
            "backend-without-dot": _plain_ids(_set_backend(_live_row(), "antigravity")),
            "backend-prefix-not-at-start": _plain_ids(
                _set_backend(_live_row(), "xantigravity.cli_v1")
            ),
            "empty-backend": _plain_ids(_set_backend(_live_row(), "")),
            "backend-none": _plain_ids(_set_backend(_live_row(), None)),
            "backend-list": _plain_ids(_set_backend(_live_row(), ["antigravity.cli_v1"])),
            "id-without-colon": _plain_ids(
                _set_backend(_live_row(), "windows_supervised.v1"),
                "agyrev",
            ),
            "id-not-at-start": _plain_ids(
                _set_backend(_live_row(), "windows_supervised.v1"),
                "xxagyrev:agy_8c73e8cd71da:r1",
            ),
            "id-uppercase": _plain_ids(
                _set_backend(_live_row(), "windows_supervised.v1"),
                "AGYREV:agy_8c73e8cd71da:r1",
            ),
        }
        expected = _expected(_OLD_TITLE, _OLD_REASON, _PAYLOAD_SUMMARY)
        for name, row in cases.items():
            with self.subTest(name=name):
                brief = self._brief(row)
                self.assertEqual(brief, expected)
                self.assertEqual(brief["title"], _OLD_TITLE)

        bad_details = _plain_ids(_live_row(), "req-plain-1")
        bad_details["details"] = "nope"
        bad_brief = self._brief(bad_details)
        self.assertEqual(
            bad_brief,
            _expected(_OLD_TITLE, _OLD_REASON, _OLD_TITLE, lead=None),
        )
        self.assertEqual(bad_brief["title"], _OLD_TITLE)

    def test_tl_summarize_status_agy_row_relabels_and_hides_teleagent(self) -> None:
        """asserts summarize_status pending_decisions shows the relabelled agy title and reason, and json.dumps of the result contains no TeleAgent."""
        row = _live_row()
        payload = _status_payload(row)
        original = copy.deepcopy(payload)
        result = HCR.summarize_status(payload)
        brief = result["pending_decisions"][0]
        expected = _expected(_AGY_TITLE, _AGY_REASON, _PAYLOAD_SUMMARY)
        self.assertEqual(brief, expected)
        self._assert_no_teleagent(brief)
        self.assertNotIn("TeleAgent", json.dumps(result))
        self.assertEqual(payload, original)
        self.assertEqual(payload["pending_decisions"][0]["title"], _OLD_TITLE)
        self.assertEqual(payload["pending_decisions"][0]["reason"], _OLD_REASON)

    def test_tl_summarize_status_non_agy_row_keeps_teleagent_review(self) -> None:
        """asserts summarize_status on a non-agy windows_supervised.v1 row still shows TeleAgent review."""
        row = _plain_ids(_set_backend(_live_row(), "windows_supervised.v1"), "winreq:1")
        payload = _status_payload(row)
        original = copy.deepcopy(payload)
        result = HCR.summarize_status(payload)
        brief = result["pending_decisions"][0]
        expected = _expected(_OLD_TITLE, _OLD_REASON, _PAYLOAD_SUMMARY)
        self.assertEqual(brief, expected)
        self.assertEqual(brief["title"], _OLD_TITLE)
        self.assertIn("TeleAgent review", json.dumps(result))
        self.assertEqual(payload, original)


if __name__ == "__main__":
    unittest.main()
