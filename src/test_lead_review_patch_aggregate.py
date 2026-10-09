"""Tests for LeadReviewRunner._patch_processes aggregate events byte cap."""
from __future__ import annotations

import json
import unittest
from typing import Any

from framework.lead_review_runner import LeadReviewRunner
try:
    from framework.lead_review_runner import _trim_events
except ImportError:
    _trim_events = None

from test_lead_review_event_cap import _MemoryLayer, _stopped_decision

_EPOCH = 1700000000.0
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


class LeadReviewPatchAggregateTests(unittest.TestCase):
    def test_patch_processes_trims_events_to_max_bytes(self) -> None:
        """asserts 5 stopped events appended then patched with large processes list stays <= 4096 bytes, keeps patched event and drops oldest."""
        decision = {
            "decision_id": "dec-stop",
            "details": {"lead_review": {"events": []}},
        }
        layer = _MemoryLayer(decision)
        runner = _runner(layer)
        lead = layer.decision["details"]["lead_review"]
        for i in range(5):
            runner._append_event(
                lead,
                "lead_review_stopped",
                job_id="job-stop",
                request_id=f"req-stop-{i}",
                reason=f"stop_{i}_" + ("a" * (300 - len(f"stop_{i}_"))),
            )
        self.assertEqual(len(lead["events"]), 5)
        self.assertLessEqual(_bytes(lead["events"]), _MAX)

        proc_row = {f"field_{j:02d}": "z" * 300 for j in range(20)}
        runner._patch_processes(
            {"goal_id": "goal-1", "decision_id": "dec-stop", "job_id": "job-stop"},
            [proc_row],
        )

        self.assertIsNotNone(layer.written)
        written_lead = (layer.written or {})["details"]["lead_review"]
        written_events = written_lead["events"]

        self.assertLessEqual(_bytes(written_events), _MAX)
        patched = written_events[-1]
        self.assertEqual(patched.get("request_id"), "req-stop-4")
        self.assertIn("lead_processes", patched)
        self.assertTrue(len(patched["lead_processes"]) > 0)

        # Trimmed events are the oldest
        self.assertLess(len(written_events), 5)
        remaining_requests = [ev.get("request_id") for ev in written_events]
        self.assertNotIn("req-stop-0", remaining_requests)
        self.assertIn("req-stop-4", remaining_requests)
        expected_tail = [f"req-stop-{k}" for k in range(5 - len(written_events), 5)]
        self.assertEqual(remaining_requests, expected_tail)

    def test_patch_processes_small_events_nothing_trimmed(self) -> None:
        """asserts small events and small process row leave list unchanged apart from lead_processes."""
        decision = {
            "decision_id": "dec-stop",
            "details": {"lead_review": {"events": []}},
        }
        layer = _MemoryLayer(decision)
        runner = _runner(layer)
        lead = layer.decision["details"]["lead_review"]
        runner._append_event(
            lead,
            "lead_review_stale_result",
            job_id="job-other",
            request_id="req-other",
            error="SYNTH_ERROR",
        )
        runner._append_event(
            lead,
            "lead_review_stopped",
            job_id="job-stop",
            request_id="req-stop",
            reason="SYNTH_REASON",
        )
        events_before = [dict(ev) for ev in lead["events"]]
        self.assertEqual(len(events_before), 2)

        proc_row = {"pid": 42, "cmd": "SYNTH_CMD"}
        runner._patch_processes(
            {"goal_id": "goal-1", "decision_id": "dec-stop", "job_id": "job-stop"},
            [proc_row],
        )

        self.assertIsNotNone(layer.written)
        written_lead = (layer.written or {})["details"]["lead_review"]
        written_events = written_lead["events"]

        self.assertEqual(len(written_events), 2)
        self.assertLessEqual(_bytes(written_events), _MAX)
        self.assertEqual(written_events[0], events_before[0])
        expected_second = dict(events_before[1])
        expected_second["lead_processes"] = [proc_row]
        self.assertEqual(written_events[1], expected_second)

    def test_append_event_trimming_behaviour_unchanged(self) -> None:
        """asserts _append_event trimming behavior keeps total <= 4096 and keeps newest events while dropping oldest."""
        runner = _runner()
        lead: dict[str, Any] = {}
        for i in range(15):
            runner._append_event(
                lead,
                "lead_review_stopped",
                job_id="job-append",
                request_id=f"req-append-{i}",
                reason=f"reason_{i:02d}_" + ("r" * 280),
            )
        events = lead.get("events") or []
        self.assertLessEqual(_bytes(events), _MAX)
        self.assertLess(len(events), 15)
        self.assertEqual(events[-1].get("request_id"), "req-append-14")
        remaining_reqs = [ev.get("request_id") for ev in events]
        self.assertNotIn("req-append-0", remaining_reqs)
        self.assertNotIn("req-append-1", remaining_reqs)
        expected_tail = [f"req-append-{k}" for k in range(15 - len(events), 15)]
        self.assertEqual(remaining_reqs, expected_tail)

    def test_trim_events_helper_preserves_keep_identity(self) -> None:
        """asserts _trim_events helper never deletes keep item even if keep is not the newest event."""
        if _trim_events is None:
            self.fail("_trim_events helper is not available")
        keep_item = {"event": "lead_review_stopped", "job_id": "job-keep", "reason": "k" * 300}
        events = [
            {"event": "lead_review_stopped", "job_id": "job-0", "reason": "0" * 300},
            keep_item,
            {"event": "lead_review_stopped", "job_id": "job-2", "reason": "2" * 300},
            {"event": "lead_review_stopped", "job_id": "job-3", "reason": "3" * 300},
            {"event": "lead_review_stopped", "job_id": "job-4", "reason": "4" * 300},
            {"event": "lead_review_stopped", "job_id": "job-5", "reason": "5" * 300},
            {"event": "lead_review_stopped", "job_id": "job-6", "reason": "6" * 300},
            {"event": "lead_review_stopped", "job_id": "job-7", "reason": "7" * 300},
            {"event": "lead_review_stopped", "job_id": "job-8", "reason": "8" * 300},
            {"event": "lead_review_stopped", "job_id": "job-9", "reason": "9" * 300},
            {"event": "lead_review_stopped", "job_id": "job-10", "reason": "a" * 300},
            {"event": "lead_review_stopped", "job_id": "job-11", "reason": "b" * 300},
            {"event": "lead_review_stopped", "job_id": "job-12", "reason": "c" * 300},
        ]
        trimmed = _trim_events(events, keep=keep_item)
        self.assertLessEqual(_bytes(trimmed), _MAX)
        self.assertTrue(any(ev is keep_item for ev in trimmed))
        self.assertFalse(any(ev.get("job_id") == "job-0" for ev in trimmed))


if __name__ == "__main__":
    unittest.main()
