"""
Tests for the IG momentum scanner's loss limits: the stop pulled in to
IG_MAX_TRADE_LOSS, the IG_MAX_POSITIONS cap and the IG_DAILY_LOSS_LIMIT
stop for the day - and its profit and time limits: IG_TAKE_PROFIT_PERCENT,
the take-profit pulled in to IG_MAX_TRADE_PROFIT, the IG_DAILY_GIVEBACK stop
and the IG_PAUSE_TIMES quiet hours. Against fakes - no broker or dashboard
is called.

    python -m unittest discover -s strategy-bots/tests
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, time as dtime, timezone
from unittest import mock

os.environ["DASHBOARD_URL"] = ""  # never report test runs to the real dashboard
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "shared"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "ig-momentum-scanner-bot"))

import pandas as pd  # noqa: E402

import rollover  # noqa: E402
import ig_momentum_scanner_bot as ig_scanner  # noqa: E402


def utc(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# Each market's IG minimum, as its market details give it (30 Sep 2026).
GOLD = {"min_deal_size": 10.0, "contract_size": 1.0, "scaling_factor": 1.0, "currency": "GBP", "expiry": "-",
        "min_stop": {"value": 1.0, "unit": "POINTS"}}
NIKKEI = {"min_deal_size": 0.5, "contract_size": 1.0, "scaling_factor": 1.0, "currency": "USD", "expiry": "-",
          "min_stop": {"value": 20.0, "unit": "POINTS"}}
EURUSD = {"min_deal_size": 0.1, "contract_size": 10000.0, "scaling_factor": 10000.0, "currency": "USD", "expiry": "-",
          "min_stop": {"value": 2.0, "unit": "POINTS"}}
ACCEPTED = {"dealStatus": "ACCEPTED", "dealId": "DINEW", "affectedDeals": []}


class LossLimitTestCase(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(ig_scanner, "STOP_LOSS_PCT", 0.005),
            mock.patch.object(ig_scanner, "MAX_TRADE_LOSS", 25.0),
            mock.patch.object(ig_scanner, "MAX_POSITIONS", 5),
            mock.patch.object(ig_scanner, "DAILY_LOSS_LIMIT", 250.0),
            # The launcher's settings on this PC mustn't leak in (they're read at import).
            mock.patch.object(ig_scanner, "TAKE_PROFIT_PCT", 0.015),
            mock.patch.object(ig_scanner, "MAX_TRADE_PROFIT", 0.0),
            mock.patch.object(ig_scanner, "DAILY_GIVEBACK", 0.0),
            mock.patch.object(ig_scanner, "PAUSE_TIMES", []),
            mock.patch.object(ig_scanner, "CURRENCY_CODE", "GBP"),
            mock.patch.dict(ig_scanner._exchange_rates, {"USD": 1.3264}, clear=True),
            mock.patch.object(ig_scanner, "loss_limits", ig_scanner._LossLimits()),
            mock.patch.object(ig_scanner, "own_trades",
                              rollover.OwnTrades(os.path.join(tempfile.mkdtemp(), "own_trades.json"))),
            mock.patch.object(ig_scanner._rate_limiter, "wait"),
            mock.patch.object(rollover, "entries_paused", return_value=False),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.service = mock.Mock()
        self.service.create_open_position.return_value = ACCEPTED

    def open(self, details, price, epic="EPIC", sent="stop_distance"):
        """The stop distance (or `sent`) the trade was sent with, or None if nothing was sent."""
        self.service.create_open_position.reset_mock()
        ig_scanner.open_position(self.service, epic, "Market", details, price, "BUY")
        if not self.service.create_open_position.called:
            return None
        return self.service.create_open_position.call_args.kwargs[sent]


class TradeLossTests(LossLimitTestCase):
    def test_pulls_a_big_contracts_stop_in(self):
        # 0.5% of gold is 20.8 points, £208 on IG's 10-contract minimum; £25 allows 2.5.
        self.assertEqual(self.open(GOLD, 4157.0), 2.5)
        # Nikkei in dollars: 0.5 contracts is $0.50 = £0.377 a point, so £25 is 66.3 points, not 336.9.
        self.assertEqual(self.open(NIKKEI, 67376.0), 66.3)

    def test_leaves_a_small_trades_stop_alone(self):
        # EUR/USD Mini at 0.1: 56.8 pips is about £4.28, well inside £25.
        self.assertEqual(self.open(EURUSD, 1.1352), 56.8)

    def test_skips_a_market_whose_closest_stop_loses_too_much(self):
        with mock.patch.object(ig_scanner, "MAX_TRADE_LOSS", 5.0):  # gold's 1-point minimum stop is £10
            self.assertIsNone(self.open(GOLD, 4157.0))

    def test_skips_until_the_exchange_rate_is_known(self):
        ig_scanner._exchange_rates.clear()
        self.assertIsNone(self.open(NIKKEI, 67376.0))

    def test_zero_is_off(self):
        with mock.patch.object(ig_scanner, "MAX_TRADE_LOSS", 0.0):
            self.assertEqual(self.open(GOLD, 4157.0), 20.8)

    def test_percentage_minimum_stop(self):
        details = dict(GOLD, min_stop={"value": 0.1, "unit": "PERCENTAGE"})  # 4.2 points, £42
        self.assertIsNone(self.open(details, 4157.0))


class TakeProfitTests(LossLimitTestCase):
    def test_sends_the_take_profit_in_points(self):
        with mock.patch.object(ig_scanner, "TAKE_PROFIT_PCT", 0.001):  # 0.1% of 69,700
            self.assertEqual(self.open(NIKKEI, 69700.0, sent="limit_distance"), 69.7)

    def test_pulls_a_big_contracts_take_profit_in(self):
        with mock.patch.object(ig_scanner, "MAX_TRADE_PROFIT", 30.0):
            # 1.5% of Nikkei is 1,045.5 points, about £394; £30 at £0.377 a point is 79.5.
            self.assertEqual(self.open(NIKKEI, 69700.0, sent="limit_distance"), 79.5)
            # Gold: 62.4 points at £10 a point; £30 is 3.
            self.assertEqual(self.open(GOLD, 4157.0, sent="limit_distance"), 3.0)
            # EUR/USD Mini: 170.3 pips is about £12.84, under £30 - left at 1.5%.
            self.assertEqual(self.open(EURUSD, 1.1352, sent="limit_distance"), 170.3)

    def test_never_closer_than_igs_minimum(self):
        details = dict(GOLD, min_stop={"value": 2.5, "unit": "POINTS"})  # the £25 stop just fits
        with mock.patch.object(ig_scanner, "MAX_TRADE_PROFIT", 20.0):  # 2 points
            self.assertEqual(self.open(details, 4157.0, sent="limit_distance"), 2.5)

    def test_zero_is_off(self):
        self.assertEqual(self.open(GOLD, 4157.0, sent="limit_distance"), 62.4)

    def test_its_own_setting_comes_before_the_shared_one(self):
        def setting(**env):
            with mock.patch.dict(os.environ, {"TAKE_PROFIT_PERCENT": "1.5"}):
                os.environ.pop("IG_TAKE_PROFIT_PERCENT", None)
                os.environ.update(env)
                return ig_scanner._take_profit_setting()

        self.assertAlmostEqual(setting(IG_TAKE_PROFIT_PERCENT="0.1"), 0.001)
        self.assertAlmostEqual(setting(), 0.015)
        self.assertAlmostEqual(setting(IG_TAKE_PROFIT_PERCENT=" "), 0.015, msg="the launcher saves an empty field")


class MaxPositionsTests(LossLimitTestCase):
    def test_opens_nothing_once_full(self):
        ig_scanner.loss_limits.open_positions = 4
        self.assertIsNotNone(self.open(EURUSD, 1.1352))
        self.assertEqual(ig_scanner.loss_limits.open_positions, 5)
        self.assertIsNone(self.open(EURUSD, 1.1352), "the fifth was the last")
        with mock.patch.object(ig_scanner, "MAX_POSITIONS", 0):
            self.assertIsNotNone(self.open(EURUSD, 1.1352), "0 is no cap")

    def test_counts_the_pools_positions(self):
        positions = pd.DataFrame([position("P1", "GOLD"), position("P2", "FTSE"), position("P3", "ELSEWHERE")])
        with mock.patch.object(ig_scanner, "DAILY_LOSS_LIMIT", 0.0):
            ig_scanner.loss_limits.update(self.service, positions, POOL)
        self.assertEqual(ig_scanner.loss_limits.open_positions, 2)


DAY_START = utc(2026, 9, 29, 21, 0)  # 22:00 UK
POOL = [{"epic": "GOLD", "name": "Spot Gold"}, {"epic": "FTSE", "name": "FTSE 100"}]


def position(deal_id, epic, level=100.0, bid=100.0, direction="BUY"):
    return {"epic": epic, "dealId": deal_id, "direction": direction, "size": 1.0, "level": level, "bid": bid,
            "offer": bid + 0.5, "contractSize": 1.0, "currency": "GBP"}


def trade(name, pnl, closed_at, ref="REF"):
    return {"instrumentName": name, "profitAndLoss": f"£{pnl:.2f}", "dateUtc": iso(closed_at),
            "openDateUtc": iso(closed_at - 600), "reference": ref, "size": "+1", "openLevel": 1, "closeLevel": 1}


class DayTestCase(LossLimitTestCase):
    def setUp(self):
        super().setUp()
        self.now = DAY_START + 12 * 3600
        for patch in (mock.patch.object(rollover, "trading_day_start", side_effect=lambda: self.day_start),
                      mock.patch.object(ig_scanner.time, "monotonic", side_effect=lambda: self.now)):
            patch.start()
            self.addCleanup(patch.stop)
        self.day_start = DAY_START
        self.service.close_open_position.return_value = {"dealStatus": "ACCEPTED"}

    def history(self, trades):
        self.service.fetch_transaction_history.return_value = pd.DataFrame(trades)

    def update(self, positions):
        return ig_scanner.loss_limits.update(self.service, pd.DataFrame(positions), POOL)


class DailyLossTests(DayTestCase):
    def setUp(self):
        super().setUp()
        self.history([
            trade("Spot Gold", -150.0, DAY_START + 3600),
            trade("FTSE 100", -50.0, DAY_START + 7200),
            trade("GBP/USD Mini converted at 0.75", -500.0, DAY_START + 3600),  # not the bot's market
            trade("Spot Gold", -900.0, DAY_START - 60),  # the day before
        ])

    def test_under_the_limit_carries_on(self):
        ig_scanner.own_trades.add("MINE")
        self.update([position("MINE", "GOLD", level=100.0, bid=60.0)])  # -40 open, -200 closed
        self.assertAlmostEqual(ig_scanner.loss_limits.day_pl, -240.0)
        self.assertIsNone(ig_scanner.loss_limits.why_no_new_trades())
        self.service.close_open_position.assert_not_called()

    def test_over_the_limit_closes_its_own_and_stops_for_the_day(self):
        ig_scanner.own_trades.add("MINE")
        left = self.update([position("MINE", "GOLD", level=100.0, bid=40.0),  # -60 open, -200 closed
                            position("BYHAND", "FTSE", level=100.0, bid=0.0)])  # not counted, not closed
        self.service.close_open_position.assert_called_once()
        self.assertEqual(self.service.close_open_position.call_args.kwargs["deal_id"], "MINE")
        self.assertEqual(list(left["dealId"]), ["BYHAND"])
        self.assertEqual(ig_scanner.loss_limits.open_positions, 1)
        self.assertIn("IG_DAILY_LOSS_LIMIT", ig_scanner.loss_limits.why_no_new_trades())
        self.assertIsNone(self.open(EURUSD, 1.1352))

        # Stays stopped when the day recovers, until the rollover starts a new one.
        self.history([])
        self.now += 3600
        self.update([])
        self.assertIsNotNone(ig_scanner.loss_limits.why_no_new_trades())
        self.day_start += 86400
        self.update([])
        self.assertIsNone(ig_scanner.loss_limits.why_no_new_trades())

    def test_reads_the_history_again_when_a_position_closes_or_it_gets_old(self):
        ig_scanner.own_trades.add("MINE")
        self.update([position("MINE", "GOLD")])
        self.update([position("MINE", "GOLD")])
        self.assertEqual(self.service.fetch_transaction_history.call_count, 1)
        self.update([])  # stopped out between passes
        self.assertEqual(self.service.fetch_transaction_history.call_count, 2)
        self.now += ig_scanner.DAY_TRADES_EVERY_SECONDS
        self.update([])
        self.assertEqual(self.service.fetch_transaction_history.call_count, 3)

    def test_a_failed_read_keeps_going(self):
        self.service.fetch_transaction_history.side_effect = Exception("403")
        with self.assertLogs("ig_momentum_bot", "WARNING"):
            self.update([])
        self.assertIsNone(ig_scanner.loss_limits.why_no_new_trades())

    def test_zero_is_off(self):
        with mock.patch.object(ig_scanner, "DAILY_LOSS_LIMIT", 0.0):
            self.update([])
        self.service.fetch_transaction_history.assert_not_called()
        self.assertIsNone(ig_scanner.loss_limits.why_no_new_trades())


class GivebackTests(DayTestCase):
    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(ig_scanner, "DAILY_GIVEBACK", 150.0),
                      mock.patch.object(ig_scanner, "DAILY_LOSS_LIMIT", 0.0)):  # works without it
            patch.start()
            self.addCleanup(patch.stop)
        ig_scanner.own_trades.add("MINE")

    def test_stops_once_the_day_gives_back_that_much_from_its_best(self):
        self.history([trade("Spot Gold", 150.0, DAY_START + 3600), trade("FTSE 100", 100.0, DAY_START + 7200),
                      trade("Spot Gold", -150.0, DAY_START + 10800)])  # up to +250, now +100
        self.update([position("MINE", "GOLD", level=100.0, bid=102.0)])  # +102: 148 below the best
        self.assertAlmostEqual(ig_scanner.loss_limits.best, 250.0)
        self.assertIsNone(ig_scanner.loss_limits.why_no_new_trades())
        self.service.close_open_position.assert_not_called()

        left = self.update([position("MINE", "GOLD", level=100.0, bid=99.0),  # +99: 151 below
                            position("BYHAND", "FTSE")])
        self.assertEqual(self.service.close_open_position.call_args.kwargs["deal_id"], "MINE")
        self.assertEqual(list(left["dealId"]), ["BYHAND"])
        self.assertIn("IG_DAILY_GIVEBACK", ig_scanner.loss_limits.why_no_new_trades())
        self.assertIsNone(self.open(EURUSD, 1.1352))

        # Stopped until the rollover starts a new day, which starts from 0 again.
        self.day_start += 86400
        self.history([])
        self.update([])
        self.assertIsNone(ig_scanner.loss_limits.why_no_new_trades())
        self.assertEqual(ig_scanner.loss_limits.best, 0.0)

    def test_not_until_the_day_has_been_that_far_up(self):
        self.history([trade("Spot Gold", 120.0, DAY_START + 3600), trade("Spot Gold", -140.0, DAY_START + 7200)])
        self.update([])  # 140 below a best of +120: a losing day is the daily loss limit's job
        self.assertIsNone(ig_scanner.loss_limits.why_no_new_trades())

    def test_counts_open_positions_highs(self):
        self.history([])
        self.update([position("MINE", "GOLD", level=100.0, bid=300.0)])  # +200 open
        self.assertIsNone(ig_scanner.loss_limits.why_no_new_trades())
        self.update([position("MINE", "GOLD", level=100.0, bid=150.0)])  # +50
        self.assertIn("IG_DAILY_GIVEBACK", ig_scanner.loss_limits.why_no_new_trades())

    def test_a_restart_remembers_the_closed_trades_best(self):
        # A fresh start mid-day: the history alone shows it went +250 and is back to +100.
        self.history([trade("FTSE 100", 100.0, DAY_START + 7200), trade("Spot Gold", 150.0, DAY_START + 3600),
                       trade("Spot Gold", -150.0, DAY_START + 10800)])
        self.update([])
        self.assertIn("IG_DAILY_GIVEBACK", ig_scanner.loss_limits.why_no_new_trades())


class PauseTimesTests(LossLimitTestCase):
    def test_reads_the_setting(self):
        self.assertEqual(ig_scanner._pause_times(""), [])
        self.assertEqual(ig_scanner._pause_times("13:00-17:00, 19:30-21:00"),
                         [(dtime(13, 0), dtime(17, 0)), (dtime(19, 30), dtime(21, 0))])
        for bad in ("13-17", "13:00", "13:00-13:00", "25:00-26:00", "13:00-17:00-18:00"):
            with self.assertRaises(ValueError, msg=bad):
                ig_scanner._pause_times(bad)

    def test_windows_are_uk_time(self):
        with mock.patch.object(ig_scanner, "PAUSE_TIMES", [(dtime(13, 0), dtime(17, 0))]):
            self.assertEqual(ig_scanner.paused_window(utc(2026, 10, 2, 12, 30)), "13:00-17:00")  # 13:30 BST
            self.assertIsNone(ig_scanner.paused_window(utc(2026, 10, 2, 16, 0)))  # 17:00 BST, the end
            self.assertIsNone(ig_scanner.paused_window(utc(2026, 10, 2, 11, 59)))
            self.assertEqual(ig_scanner.paused_window(utc(2026, 12, 2, 13, 30)), "13:00-17:00")  # 13:30 GMT
        with mock.patch.object(ig_scanner, "PAUSE_TIMES", [(dtime(23, 0), dtime(1, 0))]):
            self.assertEqual(ig_scanner.paused_window(utc(2026, 10, 2, 23, 30)), "23:00-01:00")  # 00:30 BST
            self.assertIsNone(ig_scanner.paused_window(utc(2026, 10, 2, 1, 0)))  # 02:00 BST

    def cycle(self, positions=None, paused="13:00-17:00"):
        details = dict(GOLD, market_status="TRADEABLE", bid=4157.0, offer=4157.4)
        with mock.patch.object(ig_scanner, "fetch_market_details", return_value=details), \
                mock.patch.object(ig_scanner, "STREAK_LENGTH", 3), \
                mock.patch.object(ig_scanner._bar_builder, "record", return_value=[1.0, 2.0, 3.0, 4.0]), \
                mock.patch.object(ig_scanner, "paused_window", return_value=paused):
            ig_scanner.trading_cycle(self.service, {"epic": "GOLD", "name": "Spot Gold"},
                                     pd.DataFrame(positions) if positions else None)

    def test_a_streak_opens_nothing_while_paused(self):
        self.cycle()
        self.service.create_open_position.assert_not_called()
        self.cycle(paused=None)
        self.service.create_open_position.assert_called_once()

    def test_a_reversal_still_closes_while_paused(self):
        self.service.close_open_position.return_value = {"dealStatus": "ACCEPTED"}
        self.cycle([{"epic": "GOLD", "dealId": "SHORT", "direction": "SELL", "size": 10.0}])
        self.assertEqual(self.service.close_open_position.call_args.kwargs["deal_id"], "SHORT")
        self.service.create_open_position.assert_not_called()


if __name__ == "__main__":
    unittest.main()
