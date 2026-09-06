#!/usr/bin/env python3
"""Unit/smoke tests for parallel scheduler (scan ≠ lead, isolation, serial short-poll)."""
from __future__ import annotations

import unittest

from scheduler import (
    choose_poll_interval,
    smoke_parallel_isolation,
    smoke_serial_short_poll,
)


class TestPollInterval(unittest.TestCase):
    def test_idle_range(self):
        for _ in range(20):
            iv = choose_poll_interval(
                any_busy=False, any_pending_recent=False, idle_min=10, idle_max=30, busy_min=1, busy_max=3
            )
            self.assertGreaterEqual(iv, 10)
            self.assertLessEqual(iv, 30)

    def test_busy_range(self):
        for _ in range(20):
            iv = choose_poll_interval(
                any_busy=True, any_pending_recent=False, idle_min=10, idle_max=30, busy_min=1, busy_max=3
            )
            self.assertGreaterEqual(iv, 1)
            self.assertLessEqual(iv, 3)

    def test_pending_recent_uses_busy(self):
        iv = choose_poll_interval(
            any_busy=False, any_pending_recent=True, idle_min=10, idle_max=30, busy_min=1, busy_max=1
        )
        self.assertEqual(iv, 1)


class TestSmoke(unittest.TestCase):
    def test_parallel_isolation(self):
        s = smoke_parallel_isolation(n=3, max_parallel=3)
        self.assertTrue(s["isolation_ok"])
        self.assertEqual(s["stats"]["lead_calls"], 0)
        self.assertEqual(len(s["jobs"]), 3)

    def test_serial_short_poll(self):
        s = smoke_serial_short_poll()
        self.assertTrue(s["serial_ok"])
        self.assertEqual(s["lead_calls"], 2)
        self.assertGreaterEqual(s["busy_interval_sample"], 1)
        self.assertLessEqual(s["busy_interval_sample"], 3)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
