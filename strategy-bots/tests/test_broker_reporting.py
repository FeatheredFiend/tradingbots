"""
Tests for what the bots tell the dashboard about their positions and
trades: IG positions' profit (IG's REST API has none), IG's transaction
names for markets dealt in another currency, Capital.com trades' profit by
deal ID and the history look-back, and reporting straight after a trade.
The figures are real ones from the IG and Capital.com demo accounts. No
broker or dashboard is called.

    python -m unittest discover -s strategy-bots/tests
"""

import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ["DASHBOARD_URL"] = ""  # never report test runs to the real dashboard
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "shared"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "ig-momentum-scanner-bot"))
sys.path.insert(0, os.path.join(HERE, ".."))

import pandas as pd  # noqa: E402

import dashboard_reporter  # noqa: E402
from dashboard_reporter import DashboardReporter  # noqa: E402
from engine.brokers.base import Market  # noqa: E402
from engine.brokers.capital import CapitalBroker  # noqa: E402
from engine.brokers.ig import IGBroker  # noqa: E402
import ig_momentum_scanner_bot as ig_scanner  # noqa: E402

USD_PER_GBP = 1 / 0.751009642373024  # IG's rate on the silver trade below

# Two closed IG trades, as fetch_transaction_history gives them (the second
# in a market dealt in dollars), and the same trades still open.
IG_HISTORY = pd.DataFrame([
    {"dateUtc": "2026-09-29T16:18:45", "openDateUtc": "2026-09-29T15:17:17", "instrumentName": "Germany 40 Cash (GBP1)",
     "profitAndLoss": "£19.95", "reference": "KFLVPAA8", "openLevel": "25413.6", "closeLevel": "25373.7", "size": "-0.50"},
    {"dateUtc": "2026-09-29T16:15:31", "openDateUtc": "2026-09-29T14:38:08",
     "instrumentName": "Mini Spot Silver (500oz) converted at 0.751009642373024", "profitAndLoss": "£10.70",
     "reference": "KFLZKZB2", "openLevel": "6114.1", "closeLevel": "6085.6", "size": "-0.10"},
])
IG_POSITIONS = pd.DataFrame([
    {"epic": "IX.D.DAX.IFD.IP", "dealId": "DIAAAAGER", "direction": "SELL", "size": 0.5, "level": 25413.6,
     "bid": 25372.7, "offer": 25373.7, "contractSize": 1.0, "currency": "GBP", "createdDateUTC": "2026-09-29T15:17:17"},
    {"epic": "CS.D.CFDSILVER.CFM.IP", "dealId": "DIAAAASILV", "direction": "SELL", "size": 0.1, "level": 6114.1,
     "bid": 6081.6, "offer": 6085.6, "contractSize": 5.0, "currency": "USD", "createdDateUTC": "2026-09-29T14:38:08"},
])


def ig_market(epic, name, currencies):
    return Market(symbol=epic, name=name, requested=name, min_size=0.1, size_step=0.1, digits=1,
                  raw={"instrument": {"currencies": currencies}, "snapshot": {}})


class IGScannerTests(unittest.TestCase):
    def test_position_profit_as_ig_books_it(self):
        with mock.patch.dict(ig_scanner._exchange_rates, clear=True):
            rows = list(IG_POSITIONS.iterrows())
            self.assertEqual(ig_scanner.position_pnl(rows[0][1]), 19.95)
            self.assertIsNone(ig_scanner.position_pnl(rows[1][1]), "no dollar rate seen yet")
            ig_scanner.note_exchange_rates({"currencies": [{"code": "USD", "baseExchangeRate": USD_PER_GBP}]})
            self.assertEqual(ig_scanner.position_pnl(rows[1][1]), 10.70)

    def test_trades_in_other_currencies_are_kept(self):
        service = mock.Mock()
        service.fetch_transaction_history.return_value = IG_HISTORY
        pool = [{"name": "Germany 40 Cash (GBP1)"}, {"name": "Mini Spot Silver (500oz)"}]
        with mock.patch.object(ig_scanner._rate_limiter, "wait"):
            trades = ig_scanner.fetch_closed_trades(service, pool)
        self.assertEqual([(t["symbol"], t["direction"], t["pnl"]) for t in trades],
                         [("Germany 40 Cash (GBP1)", "short", 19.95), ("Mini Spot Silver (500oz)", "short", 10.70)])


class IGBrokerTests(unittest.TestCase):
    def broker(self):
        broker = IGBroker(mock.Mock(strategy="index-reversion"), mock.Mock(), mock.Mock())
        broker._call = lambda what, fn, *args, **kwargs: {
            "open positions": IG_POSITIONS, "transaction history": IG_HISTORY}[what]
        broker.own_ids = {"DIAAAAGER", "DIAAAASILV"}
        return broker

    def markets(self):
        return {
            "IX.D.DAX.IFD.IP": ig_market("IX.D.DAX.IFD.IP", "Germany 40 Cash (GBP1)",
                                         [{"code": "GBP", "baseExchangeRate": 1.0}]),
            "CS.D.CFDSILVER.CFM.IP": ig_market("CS.D.CFDSILVER.CFM.IP", "Mini Spot Silver (500oz)",
                                               [{"code": "USD", "baseExchangeRate": USD_PER_GBP}]),
        }

    def test_positions_carry_their_profit(self):
        positions = self.broker().positions(self.markets())
        self.assertEqual(positions["IX.D.DAX.IFD.IP"].pnl, 19.95)
        self.assertAlmostEqual(positions["CS.D.CFDSILVER.CFM.IP"].pnl, 10.70)

    def test_trades_in_other_currencies_are_kept(self):
        trades = self.broker()._closed_trades(self.markets())
        self.assertEqual(sorted(t["symbol"] for t in trades), ["Germany 40 Cash (GBP1)", "Mini Spot Silver (500oz)"])


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 29, 17, 0, tzinfo=timezone.utc)


def capital_activity(deal_id, when, direction, level, source="USER"):
    return {"dateUTC": when, "epic": "EURUSD", "dealId": deal_id, "source": source, "type": "POSITION",
            "status": "ACCEPTED", "details": {"marketName": "EUR/USD", "currency": "USD", "size": 200,
                                              "direction": direction, "level": level}}


class CapitalBrokerTests(unittest.TestCase):
    # Two EUR/USD trades that closed within a second of each other: only the
    # deal ID says which profit is whose.
    ACTIVITIES = [
        capital_activity("A", "2026-09-29T15:47:00.000", "SELL", 1.13400),
        capital_activity("A", "2026-09-29T15:48:00.330", "BUY", 1.13415, source="SL"),
        capital_activity("B", "2026-09-29T15:47:10.000", "BUY", 1.13390),
        capital_activity("B", "2026-09-29T15:48:00.900", "SELL", 1.13420, source="TP"),
    ]
    TRANSACTIONS = [
        {"dateUtc": "2026-09-29T15:48:00.950", "instrumentName": "EURUSD", "size": "0.04", "dealId": "B"},
        {"dateUtc": "2026-09-29T15:48:00.400", "instrumentName": "EURUSD", "size": "-0.03", "dealId": "A"},
    ]

    def setUp(self):
        self.calls = []
        self.broker = CapitalBroker(mock.Mock(account_id=None), mock.Mock(), mock.Mock())
        self.broker.own_ids = {"A", "B"}

        def call(method, path, params=None, **kwargs):
            self.calls.append((path, params))
            # Every trade here closed at about 15:48 on the 29th.
            if "lastPeriod" in params or params["from"] < "2026-09-29T15:48" < params["to"]:
                return {"activities": self.ACTIVITIES, "transactions": self.TRANSACTIONS}
            return {}
        self.broker._call = call
        patcher = mock.patch("engine.brokers.capital.datetime", FrozenDateTime)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_profit_goes_by_deal_id(self):
        trades = {t["ref"]: t for t in self.broker._closed_trades({"EURUSD": None}, set())}
        self.assertEqual((trades["A"]["direction"], trades["A"]["pnl"], trades["A"]["closeReason"]),
                         ("short", -0.03, "stop-loss"))
        self.assertEqual((trades["B"]["direction"], trades["B"]["pnl"], trades["B"]["closeReason"]),
                         ("long", 0.04, "take-profit"))

    def test_first_report_looks_back_a_week_a_day_at_a_time(self):
        self.assertEqual(len(self.broker._closed_trades({"EURUSD": None}, set())), 2)
        activity_windows = [params for path, params in self.calls if path == "/history/activity"]
        self.assertEqual(len(activity_windows), 7)
        self.assertEqual((activity_windows[0]["from"], activity_windows[0]["to"]),
                         ("2026-09-28T17:00:00", "2026-09-29T17:00:00"))
        self.assertEqual(activity_windows[-1]["from"], "2026-09-22T17:00:00")

        self.calls.clear()
        self.assertEqual(len(self.broker._closed_trades({"EURUSD": None}, set())), 2)
        self.assertEqual([params.get("lastPeriod") for _, params in self.calls], [86400, 86400])


class ReportSoonTests(unittest.TestCase):
    def reporter(self):
        with mock.patch.object(dashboard_reporter, "URL", ""):
            reporter = DashboardReporter("test-bot", "Test bot", broker="Fake", strategy="Test")
        reporter.enabled = True  # no thread, no posts
        return reporter

    def test_due_comes_round_early_and_the_report_goes_out_at_once(self):
        reporter = self.reporter()
        self.assertTrue(reporter.due())
        self.assertFalse(reporter.due(), "then not for another 15 s")

        reporter.report_soon(0, 0.05)
        self.assertTrue(reporter.due())
        reporter.update(positions=[])
        self.assertTrue(reporter._wake.is_set(), "sent now, not with the next 10-second report")
        reporter._wake.clear()
        self.assertFalse(reporter.due())
        time.sleep(0.06)
        self.assertTrue(reporter.due(), "the second, later look")
        self.assertFalse(reporter.due())

        reporter.update(positions=[])
        reporter._wake.clear()
        reporter.update(positions=[])
        self.assertFalse(reporter._wake.is_set(), "an ordinary snapshot waits for the next report")

    def test_off_when_reporting_is_off(self):
        reporter = self.reporter()
        reporter.enabled = False
        reporter.report_soon()
        self.assertFalse(reporter.due())


if __name__ == "__main__":
    unittest.main()
