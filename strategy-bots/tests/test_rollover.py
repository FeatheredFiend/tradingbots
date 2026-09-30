"""
Tests for the CFD momentum scanners' overnight rule (shared/rollover.py):
when they close before the daily rollover and stop opening trades, and -
against fake brokers - that each closes only the trades it opened, never
another bot's or a hand-made one. No broker or dashboard is called.

    python -m unittest discover -s strategy-bots/tests
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

os.environ["DASHBOARD_URL"] = ""  # never report test runs to the real dashboard
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "shared"))
for bot in ("oanda", "capital", "ig"):
    sys.path.insert(0, os.path.join(HERE, "..", "..", f"{bot}-momentum-scanner-bot"))

import pandas as pd  # noqa: E402

import rollover  # noqa: E402
import capital_momentum_scanner_bot as capital_scanner  # noqa: E402
import ig_momentum_scanner_bot as ig_scanner  # noqa: E402
import oanda_momentum_scanner_bot as oanda_scanner  # noqa: E402


def utc(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def minutes(flat: int, last_entry: int):
    """The two settings, as if set in the environment."""
    return mock.patch.multiple(rollover, FLAT_MINUTES=flat, LAST_ENTRY_MINUTES=last_entry)


class RolloverTimingTests(unittest.TestCase):
    ROLLOVER = utc(2026, 9, 29, 21, 0)  # 17:00 New York = 22:00 UK in September

    def test_closes_in_the_last_quarter_hour(self):
        with minutes(15, 60):
            self.assertFalse(rollover.flat_due(self.ROLLOVER - 15 * 60 - 1))
            self.assertTrue(rollover.flat_due(self.ROLLOVER - 15 * 60))  # 21:45 UK
            self.assertTrue(rollover.flat_due(self.ROLLOVER - 60))
            self.assertFalse(rollover.flat_due(self.ROLLOVER + 60), "after the rollover the next is a day away")

    def test_no_entries_from_an_hour_before_to_45_minutes_after(self):
        with minutes(15, 60):
            self.assertFalse(rollover.entries_paused(self.ROLLOVER - 61 * 60))
            self.assertTrue(rollover.entries_paused(self.ROLLOVER - 60 * 60))   # 21:00 UK
            self.assertTrue(rollover.entries_paused(self.ROLLOVER + 44 * 60))
            self.assertFalse(rollover.entries_paused(self.ROLLOVER + 46 * 60))  # 22:46 UK
            self.assertFalse(rollover.entries_paused(utc(2026, 9, 29, 13, 0)))

    def test_follows_new_york_when_the_uk_clocks_change_first(self):
        # UK clocks go back on 25 October, US ones on 1 November: that week
        # the rollover is at 21:00 UK, so a fixed 21:45 would be too late.
        rollover_that_week = utc(2026, 10, 27, 21, 0)
        with minutes(15, 60):
            self.assertTrue(rollover.flat_due(rollover_that_week - 10 * 60))
            self.assertFalse(rollover.flat_due(utc(2026, 10, 27, 21, 45)))
            self.assertTrue(rollover.flat_due(utc(2026, 11, 10, 21, 50)), "22:00 UK again in November")

    def test_zero_switches_each_part_off(self):
        with minutes(0, 0):
            self.assertFalse(rollover.flat_due(self.ROLLOVER - 60))
            self.assertFalse(rollover.entries_paused(self.ROLLOVER - 60))
            self.assertIn("holds overnight", rollover.describe(self.ROLLOVER - 3600))
        with minutes(15, 0):  # entries still stop once it's closing
            self.assertTrue(rollover.entries_paused(self.ROLLOVER - 10 * 60))
            self.assertFalse(rollover.entries_paused(self.ROLLOVER - 30 * 60))

    def test_trading_day_starts_at_the_last_rollover(self):
        self.assertEqual(rollover.trading_day_start(utc(2026, 9, 30, 12, 0)), self.ROLLOVER)
        self.assertEqual(rollover.trading_day_start(self.ROLLOVER), self.ROLLOVER)
        self.assertEqual(rollover.trading_day_start(self.ROLLOVER - 1), utc(2026, 9, 28, 21, 0))
        # 1 November: US clocks go back, so that day's rollover is 22:00 UTC, 25 hours after the last.
        self.assertEqual(rollover.trading_day_start(utc(2026, 11, 1, 21, 30)), utc(2026, 10, 31, 21, 0))
        self.assertEqual(rollover.trading_day_start(utc(2026, 11, 1, 22, 30)), utc(2026, 11, 1, 22, 0))
        self.assertEqual(rollover.next_rollover_uk(utc(2026, 9, 30, 12, 0)), "22:00")

    def test_describe_gives_tonights_uk_times(self):
        with minutes(15, 60):
            self.assertEqual(rollover.describe(utc(2026, 9, 29, 12, 0)),
                             "Rollover 22:00 UK | closes its trades at 21:45 | no new trades 21:00-22:45")

    def test_settings(self):
        with mock.patch.dict(os.environ, {"X": "15.0", "Y": "", "Z": "-1"}):
            self.assertEqual(rollover._minutes("X", 5), 15)
            self.assertEqual(rollover._minutes("Y", 5), 5)
            self.assertRaises(ValueError, rollover._minutes, "Z", 5)


class OwnTradesTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "own_trades.json")

    def test_remembered_across_a_restart(self):
        own = rollover.OwnTrades(self.path)
        own.add("A1", None, "", 42)
        self.assertEqual(rollover.OwnTrades(self.path).ids, {"A1", "42"})
        self.assertIn(42, own)

    def test_forgets_closed_trades(self):
        own = rollover.OwnTrades(self.path)
        own.add("A1", "B2", "C3")
        own.keep_open(["B2", "someone-elses"])
        self.assertEqual(own.ids, {"B2"})
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), ["B2"])

    def test_unreadable_file_starts_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertLogs("rollover", "WARNING"):
            self.assertEqual(rollover.OwnTrades(self.path).ids, set())


class ScannerTestCase(unittest.TestCase):
    """Gives `scanner` a fresh OwnTrades in a temp dir holding `own_ids`."""
    scanner = None

    def own(self, *own_ids):
        own = rollover.OwnTrades(os.path.join(tempfile.mkdtemp(), "own_trades.json"))
        own.add(*own_ids)
        patcher = mock.patch.object(self.scanner, "own_trades", own)
        patcher.start()
        self.addCleanup(patcher.stop)
        return own


class OandaScannerTests(ScannerTestCase):
    scanner = oanda_scanner

    def test_closes_only_its_own_trades(self):
        own = self.own("101", "999")  # 999 closed on a stop-loss since
        calls = []

        def fake_oanda(method, path, **kwargs):
            calls.append((method, path))
            if path.endswith("/openTrades"):
                return {"trades": [{"id": "101", "instrument": "EUR_USD", "currentUnits": "25"},
                                   {"id": "202", "instrument": "XAU_USD", "currentUnits": "-0.1"}]}
            return {"orderFillTransaction": {"units": "-25", "price": "1.13"}}

        with mock.patch.object(oanda_scanner, "oanda", fake_oanda):
            oanda_scanner.close_before_rollover("acct")
        self.assertEqual(calls[1:], [("PUT", "/v3/accounts/acct/trades/101/close")])
        self.assertEqual(own.ids, {"101"})
        self.assertEqual(oanda_scanner.CLOSE_REASONS["101"], rollover.REASON)

    def test_remembers_what_it_opens_and_opens_nothing_near_the_rollover(self):
        own = self.own()
        instrument = {"tradeUnitsPrecision": 0, "minimumTradeSize": "1", "displayPrecision": 5}
        price = {"bid": 1.13, "ask": 1.1301, "unit_value": 0.75}
        filled = {"orderFillTransaction": {"units": "26", "price": "1.1301", "tradeOpened": {"tradeID": "303"}}}
        with mock.patch.object(oanda_scanner, "oanda", return_value=filled) as fake:
            with mock.patch.object(rollover, "entries_paused", return_value=True):
                self.assertFalse(oanda_scanner.open_position("acct", "EUR_USD", instrument, price, "BUY", "GBP"))
            fake.assert_not_called()
            with mock.patch.object(rollover, "entries_paused", return_value=False):
                self.assertTrue(oanda_scanner.open_position("acct", "EUR_USD", instrument, price, "BUY", "GBP"))
        self.assertEqual(own.ids, {"303"})


class CapitalScannerTests(ScannerTestCase):
    scanner = capital_scanner

    def test_closes_only_its_own_positions(self):
        own = self.own("DEAL1")
        deleted = []

        def fake_capital(method, path, **kwargs):
            if method == "GET":
                return {"positions": [
                    {"market": {"epic": "US500"}, "position": {"dealId": "DEAL1", "direction": "BUY", "size": 0.02}},
                    {"market": {"epic": "GOLD"}, "position": {"dealId": "TREND9", "direction": "SELL", "size": 0.03}},
                ]}
            deleted.append(path)
            return {"dealReference": "ref"}

        with mock.patch.object(capital_scanner, "capital", fake_capital), \
                mock.patch.object(capital_scanner, "confirm", return_value={"dealStatus": "ACCEPTED", "size": 0.02}):
            capital_scanner.close_before_rollover()
        self.assertEqual(deleted, ["/positions/DEAL1"])
        self.assertEqual(own.ids, {"DEAL1"})

    def test_remembers_the_position_deal_id(self):
        own = self.own()
        market = {"dealingRules": {"minSizeIncrement": {"value": 0.01}, "minDealSize": {"value": 0.01}},
                  "snapshot": {"offer": 100.0, "bid": 99.9, "decimalPlacesFactor": 2}}
        confirmation = {"dealStatus": "ACCEPTED", "dealId": "ORDER1", "size": 1.2, "level": 100.0,
                        "affectedDeals": [{"dealId": "POSITION1", "status": "OPENED"}]}
        with mock.patch.object(capital_scanner, "capital", return_value={"dealReference": "ref"}), \
                mock.patch.object(capital_scanner, "confirm", return_value=confirmation), \
                mock.patch.object(rollover, "entries_paused", return_value=False):
            self.assertTrue(capital_scanner.open_position("US500", market, 100.0, "BUY", "GBP"))
        self.assertEqual(own.ids, {"ORDER1", "POSITION1"})


class IGScannerTests(ScannerTestCase):
    scanner = ig_scanner
    POOL = [{"epic": "IX.D.DAX.IFD.IP", "name": "Germany 40"}, {"epic": "CS.D.EURUSD.MINI.IP", "name": "EUR/USD"}]

    def test_closes_only_its_own_positions(self):
        own = self.own("DIOWN", "DIGONE")
        positions = pd.DataFrame([
            {"epic": "IX.D.DAX.IFD.IP", "dealId": "DIOWN", "direction": "SELL", "size": 0.5},
            {"epic": "CS.D.EURUSD.MINI.IP", "dealId": "DIHAND", "direction": "BUY", "size": 1.0},
        ])
        service = mock.Mock()
        service.close_open_position.return_value = {"dealStatus": "ACCEPTED"}
        left = ig_scanner.close_before_rollover(service, positions, self.POOL)
        service.close_open_position.assert_called_once()
        self.assertEqual(service.close_open_position.call_args.kwargs["deal_id"], "DIOWN")
        self.assertEqual(service.close_open_position.call_args.kwargs["direction"], "BUY")
        self.assertEqual(list(left["dealId"]), ["DIHAND"])
        self.assertEqual(own.ids, {"DIOWN"})

    def test_no_positions_forgets_everything(self):
        own = self.own("DIOLD")
        self.assertTrue(ig_scanner.close_before_rollover(mock.Mock(), pd.DataFrame(), self.POOL).empty)
        self.assertEqual(own.ids, set())

    def test_remembers_what_it_opens(self):
        own = self.own()
        details = {"min_deal_size": 0.5, "contract_size": 1, "scaling_factor": 1, "currency": "GBP", "expiry": "-"}
        service = mock.Mock()
        service.create_open_position.return_value = {"dealStatus": "ACCEPTED", "dealId": "DINEW",
                                                     "affectedDeals": [{"dealId": "DINEW", "status": "OPENED"}]}
        with mock.patch.object(rollover, "entries_paused", return_value=False):
            ig_scanner.open_position(service, "IX.D.DAX.IFD.IP", "Germany 40", details, 25400.0, "BUY")
        self.assertEqual(own.ids, {"DINEW"})


if __name__ == "__main__":
    unittest.main()
