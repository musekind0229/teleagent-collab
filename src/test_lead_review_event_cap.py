"""Per-event byte cap for lead_review events.

One event whose fields are already capped at 300 characters can still exceed
the documented 4096-byte events budget. ``_patch_processes`` can push a stopped
event over that budget again, and it stores process rows with no redaction.
"""
from __future__ import annotations

import json
import unittest
from typing import Any

from framework.lead_review_runner import LeadReviewRunner

_EPOCH = 1700000000.0
_SECRET = "SYNTH_SECRET_VALUE_XXXX"
_PROC_SECRET = "SYNTH_PROC_SECRET_ABCDEF"
_BEARER = "SYNTHBEARERTOKENVALUE"
_MAX = 4096


def _runner(layer: Any = None) -> LeadReviewRunner:
    return LeadReviewRunner(
        layer=layer if layer is not None else object(),
        planner=object(),
        backend=object(),
        clock=lambda: _EPOCH,
    )


def _bytes(value: Any) -> int:
    return len(json.dumps(value))


class _MemoryLayer:
    """In-process decision layer. Stores the lead_review blob ``_patch_processes`` writes."""

    def __init__(self, decision: dict[str, Any]) -> None:
        self.decision = decision
        self.written: dict[str, Any] | None = None

    def get_goal(self, goal_id: str) -> dict[str, Any]:
        return {"goal": {"goal_id": goal_id, "pending_decisions": [self.decision]}}

    def annotate_decision(
        self, goal_id: str, decision_id: str | None = None, details: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.written = {"goal_id": goal_id, "decision_id": decision_id, "details": details}
        merged = dict(self.decision)
        merged["details"] = dict(details or {})
        self.decision = merged
        return {"ok": True}


def _stopped_decision() -> dict[str, Any]:
    return {
        "decision_id": "dec-stop",
        "details": {
            "lead_review": {
                "events": [
                    {
                        "event": "lead_review_stopped",
                        "at": _EPOCH,
                        "at_iso": "2023-11-14T22:13:20Z",
                        "job_id": "job-stop",
                        "request_id": "req-stop",
                        "reason": "stop",
                    }
                ]
            }
        },
    }


class LeadReviewEventCapTests(unittest.TestCase):
    def test_one_wide_event_stays_within_4096(self) -> None:
        """asserts one event with 30 fields of 300 chars is at most 4096 bytes, keeps core ids, and marks truncation."""
        runner = _runner()
        lead: dict[str, Any] = {}
        fields = {f"field_{index:02d}": "x" * 300 for index in range(30)}
        fields["field_00"] = f"token={_SECRET} " + ("y" * 270)
        self.assertEqual(len(fields["field_00"]), 300)
        runner._append_event(
            lead,
            "lead_review_stale_result",
            job_id="job-fat",
            request_id="agyrev:agy_0123456789ab:r1",
            **fields,
        )
        events = lead.get("events") or []
        self.assertEqual(len(events), 1, events)
        event = events[0]
        self.assertLessEqual(_bytes(event), _MAX, _bytes(event))
        self.assertLessEqual(_bytes(events), _MAX, _bytes(events))
        self.assertEqual(event.get("event"), "lead_review_stale_result")
        self.assertEqual(event.get("at"), _EPOCH)
        self.assertEqual(event.get("job_id"), "job-fat")
        self.assertEqual(event.get("request_id"), "agyrev:agy_0123456789ab:r1")
        self.assertTrue(event.get("truncated") is True or int(event.get("dropped_fields") or 0) > 0)
        self.assertLess(sum(1 for key in event if str(key).startswith("field_")), 30)
        blob = json.dumps(events)
        self.assertNotIn(_SECRET, blob)
        if "field_00" in event:
            self.assertIn("[redacted]", str(event["field_00"]))
            self.assertNotIn(_SECRET, str(event["field_00"]))
            self.assertLessEqual(len(str(event["field_00"])), 300)

    def test_field_redaction_still_applies(self) -> None:
        """asserts a secret-looking event field is redacted and capped at 300 characters."""
        runner = _runner()
        lead: dict[str, Any] = {}
        raw = f"token={_SECRET} Bearer {_BEARER} " + ("z" * 400)
        runner._append_event(
            lead,
            "lead_review_stale_result",
            job_id="job-redact",
            request_id="req-redact",
            error=raw,
        )
        event = lead["events"][-1]
        stored = str(event["error"])
        self.assertNotIn(_SECRET, stored)
        self.assertNotIn(_BEARER, stored)
        self.assertIn("[redacted]", stored)
        self.assertLessEqual(len(stored), 300)
        self.assertLessEqual(_bytes(lead["events"]), _MAX)

    def test_small_events_are_unchanged(self) -> None:
        """asserts a normal small event keeps its fields and gains no truncation marker."""
        runner = _runner()
        lead: dict[str, Any] = {}
        runner._append_event(
            lead,
            "lead_review_stopped",
            job_id="job-small",
            request_id="req-small",
            reason="done",
        )
        runner._append_event(
            lead,
            "lead_review_stale_result",
            job_id="job-small",
            request_id="req-small",
            error="plain failure",
        )
        events = lead["events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(
            events[0],
            {
                "event": "lead_review_stopped",
                "at": _EPOCH,
                "at_iso": "2023-11-14T22:13:20Z",
                "job_id": "job-small",
                "request_id": "req-small",
                "reason": "done",
            },
        )
        self.assertEqual(events[1]["event"], "lead_review_stale_result")
        self.assertEqual(events[1]["error"], "plain failure")
        self.assertEqual(events[1]["job_id"], "job-small")
        self.assertEqual(events[1]["request_id"], "req-small")
        for event in events:
            self.assertNotIn("truncated", event)
            self.assertNotIn("dropped_fields", event)
        self.assertLessEqual(_bytes(events), _MAX)

    def test_patch_processes_caps_and_redacts_large_entries(self) -> None:
        """asserts _patch_processes with large secret-looking rows keeps the stopped event within 4096 bytes and redacted."""
        layer = _MemoryLayer(_stopped_decision())
        runner = _runner(layer)
        procs = []
        for index in range(8):
            entry: dict[str, Any] = {
                "pid": index,
                "argv": f"token={_PROC_SECRET} Bearer {_BEARER} " + ("Q" * 280),
            }
            for blob in range(15):
                entry[f"blob_{blob:02d}"] = "Z" * 300
            procs.append(entry)
        runner._patch_processes(
            {"goal_id": "goal-1", "decision_id": "dec-stop", "job_id": "job-stop"},
            procs,
        )
        self.assertIsNotNone(layer.written)
        lead = (layer.written or {})["details"]["lead_review"]
        events = lead["events"]
        event = events[-1]
        self.assertEqual(event.get("event"), "lead_review_stopped")
        self.assertEqual(event.get("at"), _EPOCH)
        self.assertEqual(event.get("job_id"), "job-stop")
        self.assertEqual(event.get("request_id"), "req-stop")
        self.assertLessEqual(_bytes(event), _MAX, _bytes(event))
        self.assertLessEqual(_bytes(events), _MAX, _bytes(events))
        blob = json.dumps(events)
        self.assertNotIn(_PROC_SECRET, blob)
        self.assertNotIn(_BEARER, blob)
        self.assertIn("[redacted]", blob)
        self.assertEqual(event.get("reason"), "stop")

    def test_patch_processes_small_entries_stay_a_redacted_list(self) -> None:
        """asserts a small process list stays a list, with secret-looking strings redacted and no truncation marker."""
        layer = _MemoryLayer(_stopped_decision())
        runner = _runner(layer)
        procs = [
            {
                "pid": 7,
                "method": "kill",
                "result": "killed",
                "note": f"token={_PROC_SECRET}",
                "auth": f"Bearer {_BEARER}",
            }
        ]
        runner._patch_processes(
            {"goal_id": "goal-1", "decision_id": "dec-stop", "job_id": "job-stop"},
            procs,
        )
        event = layer.decision["details"]["lead_review"]["events"][-1]
        self.assertEqual(
            event["lead_processes"],
            [
                {
                    "pid": 7,
                    "method": "kill",
                    "result": "killed",
                    "note": "[redacted]",
                    "auth": "[redacted]",
                }
            ],
        )
        self.assertNotIn("truncated", event)
        self.assertNotIn("dropped_fields", event)
        self.assertLessEqual(_bytes(layer.decision["details"]["lead_review"]["events"]), _MAX)
        self.assertNotIn(_PROC_SECRET, json.dumps(event))
        self.assertNotIn(_BEARER, json.dumps(event))


if __name__ == "__main__":
    unittest.main()
