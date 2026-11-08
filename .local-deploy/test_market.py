"""Boundary checks for real-price input integrity; no network requests."""

from copy import deepcopy
from datetime import datetime
import unittest

from market import SHANGHAI, normalize_symbol, parse_daily, parse_quote


class MarketInputTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 22, 12, 55, tzinfo=SHANGHAI)
        self.rows = [[day, "10", "11", "12", "9", "25", {}, "0.1", "2.5", ""]
                     for day in ["2026-10-20", "2026-10-21", "2026-10-22"]]
        self.body = {"code": 0, "data": {"sh600519": {"qfqday": self.rows}}}

    def parse(self, body=None, now=None, adjustment="qfq"):
        return parse_daily(body or self.body, "sh600519", now or self.now, 640, adjustment)

    def test_units_and_exclude_unclosed_day(self):
        frame, excluded = self.parse()
        self.assertEqual(len(frame), 2)
        self.assertEqual(excluded, ["2026-10-22"])
        self.assertEqual(frame.iloc[-1]["vol"], 2500)
        self.assertEqual(frame.iloc[-1]["amount"], 25000)

    def test_include_day_after_close_buffer(self):
        frame, excluded = self.parse(now=self.now.replace(hour=15, minute=10))
        self.assertEqual(len(frame), 3)
        self.assertEqual(excluded, [])

    def test_reject_unadjusted_fallback(self):
        body = {"code": 0, "data": {"sh600519": {"day": self.rows}}}
        with self.assertRaisesRegex(ValueError, "不会自动替换"):
            self.parse(body)
        self.assertEqual(len(self.parse(body, adjustment="none")[0]), 2)

    def test_reject_missing_amount(self):
        body = deepcopy(self.body)
        body["data"]["sh600519"]["qfqday"][0][8] = ""
        with self.assertRaises(ValueError):
            self.parse(body)

    def test_reject_duplicate_future_and_bad_prices(self):
        for change in [lambda r: r[1].__setitem__(0, r[0][0]),
                       lambda r: r[2].__setitem__(0, "2026-10-23"),
                       lambda r: r[0].__setitem__(3, "1")]:
            with self.subTest(change=change):
                body = deepcopy(self.body)
                change(body["data"]["sh600519"]["qfqday"])
                with self.assertRaises(ValueError):
                    self.parse(body)

    def test_symbol_formats_and_invalid_input(self):
        for value in ["600519", "sh600519", "600519.SH"]:
            self.assertEqual(normalize_symbol(value), "sh600519")
        self.assertEqual(normalize_symbol("sz000001"), "sz000001")
        self.assertEqual(normalize_symbol("sh000001"), "sh000001")
        for value in ["invalid", "../600519", "bj832982"]:
            with self.assertRaises(ValueError):
                normalize_symbol(value)

    def quote_text(self, code="600519", stamp="20261022125500"):
        fields = [""] * 38
        for index, value in {1: "贵州茅台", 2: code, 3: "1312.04", 4: "1309.69",
                             6: "17490", 30: stamp, 37: "228981"}.items():
            fields[index] = value
        return 'v_sh600519="' + "~".join(fields) + '";'

    def test_quote_identity_units_and_staleness(self):
        quote = parse_quote(self.quote_text(), "sh600519", self.now)
        self.assertEqual(quote["volume_shares"], 1749000)
        self.assertEqual(quote["amount_cny"], 2289810000)
        quote = parse_quote(self.quote_text(stamp="20261021150000"), "sh600519", self.now)
        self.assertEqual(quote["status"], "STALE_SNAPSHOT")
        with self.assertRaises(ValueError):
            parse_quote(self.quote_text(code="000001"), "sh600519", self.now)


if __name__ == "__main__":
    unittest.main(verbosity=2)
