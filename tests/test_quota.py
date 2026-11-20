from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fanfictl.config import Settings
from fanfictl.keystore import RuntimeAPIKey
from fanfictl.quota import DailyQuotaExceeded, QuotaTracker


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class QuotaTests(unittest.TestCase):
    def test_tracker_counts_requests_and_waits_for_rpm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.output_dir = Path(tmp) / "output"
            settings.rpm_limit = 2
            settings.rpd_limit = 10
            clock = FakeClock()
            tracker = QuotaTracker(
                settings,
                [RuntimeAPIKey(id="k1", key="a", source="env", is_default=True)],
                now_func=clock.time,
                sleep_func=clock.sleep,
            )

            tracker.acquire_request_slot()
            tracker.acquire_request_slot()
            before = clock.now
            tracker.acquire_request_slot()

            snapshot = tracker.snapshot()
            self.assertGreaterEqual(clock.now - before, 60)
            self.assertEqual(snapshot.daily_used, 3)

    def test_tracker_blocks_after_daily_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.output_dir = Path(tmp) / "output"
            settings.rpm_limit = 100
            settings.rpd_limit = 2
            clock = FakeClock()
            tracker = QuotaTracker(
                settings,
                [RuntimeAPIKey(id="k1", key="a", source="env", is_default=True)],
                now_func=clock.time,
                sleep_func=clock.sleep,
            )

            tracker.acquire_request_slot()
            tracker.acquire_request_slot()

            with self.assertRaises(DailyQuotaExceeded):
                tracker.acquire_request_slot()

    def test_tracker_falls_back_to_second_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.output_dir = Path(tmp) / "output"
            settings.rpm_limit = 1
            settings.rpd_limit = 1
            clock = FakeClock()
            tracker = QuotaTracker(
                settings,
                [
                    RuntimeAPIKey(id="k1", key="a", source="env", is_default=True),
                    RuntimeAPIKey(id="k2", key="b", source="stored", is_default=False),
                ],
                now_func=clock.time,
                sleep_func=clock.sleep,
            )

            first = tracker.acquire_request_slot()
            second = tracker.acquire_request_slot()

            self.assertEqual(first.id, "k1")
            self.assertEqual(second.id, "k2")

    def test_quota_error_cools_key_down_and_switches_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.output_dir = Path(tmp) / "output"
            settings.rpm_limit = 5
            settings.rpd_limit = 100
            clock = FakeClock()
            tracker = QuotaTracker(
                settings,
                [
                    RuntimeAPIKey(id="k1", key="a", source="env", is_default=True),
                    RuntimeAPIKey(id="k2", key="b", source="stored", is_default=False),
                ],
                now_func=clock.time,
                sleep_func=clock.sleep,
            )

            self.assertEqual(tracker.acquire_request_slot().id, "k1")
            tracker.record_quota_error("k1", "429 RESOURCE_EXHAUSTED")
            self.assertEqual(tracker.acquire_request_slot().id, "k2")


if __name__ == "__main__":
    unittest.main()
