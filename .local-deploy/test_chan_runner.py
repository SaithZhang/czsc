"""Offline operational boundaries and a recorded CZSC signal regression."""
import copy
from datetime import datetime
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from czsc import CZSC, Freq, format_standard_kline
import chan_runner as cr

REFERENCE = cr.ROOT / "20261022_223901"


class CalendarAndPublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calendar = cr.read_json(cr.ROOT / "calendar/2026-10.json")["raw"]

    def test_holiday_is_not_weekday(self):
        days = cr.validate_calendar(self.calendar, 2026, 10)
        with patch.object(cr, "get_calendar", return_value=days):
            self.assertIsNone(cr.expected_session(None, datetime(2026, 10, 1, 16, 30, tzinfo=cr.SHANGHAI), True))
            self.assertIsNone(cr.expected_session(None, datetime(2026, 10, 24, 16, 30, tzinfo=cr.SHANGHAI), True))
            self.assertEqual(cr.expected_session(None, datetime(2026, 10, 22, 16, 30, tzinfo=cr.SHANGHAI), True), "2026-10-22")

    def test_incomplete_and_duplicate_calendar_rejected(self):
        broken = copy.deepcopy(self.calendar)
        broken["data"].pop()
        with self.assertRaises(ValueError):
            cr.validate_calendar(broken, 2026, 10)
        broken["data"].append(broken["data"][0])
        with self.assertRaises(ValueError):
            cr.validate_calendar(broken, 2026, 10)

    def test_intraday_uses_previous_close_and_month_boundary(self):
        days = cr.validate_calendar(self.calendar, 2026, 10)
        with patch.object(cr, "get_calendar", return_value=days):
            self.assertEqual(cr.expected_session(None, datetime(2026, 10, 22, 14, 0, tzinfo=cr.SHANGHAI)), "2026-10-21")
            with self.assertRaises(ValueError):
                cr.expected_session(None, datetime(2026, 10, 22, 16, 29, tzinfo=cr.SHANGHAI), True)
        with patch.object(cr, "get_calendar", side_effect=[days, {"2026-09-30": True}]):
            self.assertEqual(cr.expected_session(None, datetime(2026, 10, 8, 14, 0, tzinfo=cr.SHANGHAI)), "2026-09-30")

    def test_partial_failure_keeps_published_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = {"path": "previous-good-run", "session": "2026-10-21"}
            cr.atomic_json(root / "latest.json", old)
            with patch.object(cr, "expected_session", return_value="2026-10-22"), \
                 patch.object(cr, "analyze_symbol", side_effect=[{"symbol": "sz399006"}, ValueError("second index incomplete")]):
                self.assertEqual(cr.run(root=root), 1)
            self.assertEqual(cr.read_json(root / "latest.json"), old)
            self.assertEqual(cr.read_json(root / "state.json")["status"], "error")

    def test_concurrent_run_does_not_touch_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cr.atomic_json(root / "state.json", {"status": "running", "pid": 123})
            with (root / "run.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(cr.run(root=root), 2)
            self.assertEqual(cr.read_json(root / "state.json"), {"status": "running", "pid": 123})


class RecordedSignalTests(unittest.TestCase):
    def test_missing_amount_and_incomplete_close(self):
        raw = cr.read_json(REFERENCE / "sh000688/m30_raw.json")
        frame = cr.parse_m30(raw, "sh000688", "2026-10-22")
        self.assertTrue(frame["amount"].isna().all())
        broken = copy.deepcopy(raw)
        broken["data"]["sh000688"]["m30"].pop()
        with self.assertRaises(ValueError):
            cr.parse_m30(broken, "sh000688", "2026-10-22")

    def test_first_buy_known_only_after_bottom(self):
        raw = cr.read_json(REFERENCE / "sh000688/m30_raw.json")
        frame = cr.parse_m30(raw, "sh000688", "2026-10-22")
        bars = format_standard_kline(frame, Freq.F30)
        timeline = cr.confirmation_window(bars, Freq.F30, 24)
        current = cr.active_signals(CZSC(bars), timeline)
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["kind"], "first_buy")
        self.assertEqual(current[0]["price"], 1302.39)
        self.assertEqual(current[0]["structural_time"], "2026-10-22 14:00:00")
        self.assertEqual(current[0]["observed_at"], "2026-10-22 14:30:00")
        self.assertEqual(current[0]["confirmation_close"], 1314.30)
        before = next(r for r in timeline if r["as_of"] == "2026-10-22 14:00:00")
        self.assertTrue(before["signals"]["first_buy"][0]["value"].startswith("其他"))

    def test_revoked_intraday_buy_not_shown_at_close(self):
        raw = cr.read_json(REFERENCE / "sz399006/m30_raw.json")
        bars = format_standard_kline(cr.parse_m30(raw, "sz399006", "2026-10-22"), Freq.F30)
        timeline = cr.confirmation_window(bars, Freq.F30, 8)
        at1430 = next(r for r in timeline if r["as_of"] == "2026-10-22 14:30:00")
        self.assertTrue(at1430["signals"]["first_buy"][0]["value"].startswith("一买"))
        self.assertEqual(cr.active_signals(CZSC(bars), timeline), [])


if __name__ == "__main__":
    unittest.main()
