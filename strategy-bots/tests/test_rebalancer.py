"""
Tests for the portfolio bots (engine/rebalancer.py): the slow trend's
signal, volatility and sizing, the ETF rotation's month-end average, the
schedules, the orders that take a position to its target, and a rebalance
against a fake broker. No broker is contacted.

    python -m unittest discover -s strategy-bots/tests
"""

import math
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

os.environ["DASHBOARD_URL"] = ""  # never report test runs to the real dashboard
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from engine import rebalancer, runner  # noqa: E402
from engine.brokers.base import Account, Broker, Market, Position, Quote  # noqa: E402
from engine.indicators import Bar  # noqa: E402
from engine.rebalancer import (EtfRotation, RebalanceBot, SlowTrend, Target, above_average,  # noqa: E402
                               annual_volatility, month_end_closes, plan, trend_signal)
from engine.settings import BotSettings, SettingsError, strategy_params  # noqa: E402

DAY = 86400


def utc(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def daily(closes: list, start: float = None) -> list:
    """One bar a calendar day, from 1 Jan 2024 unless said."""
    start = utc(2024, 1, 1) if start is None else start
    return [Bar(start + i * DAY, c, c, c, c) for i, c in enumerate(closes)]


def ramp(start: float, end: float, count: int) -> list:
    return [start + (end - start) * i / (count - 1) for i in range(count)]


def alternating(count: int, move: float, price: float = 100.0) -> list:
    """Closes whose daily returns are exactly +move, -move, +move, ..."""
    closes = [price]
    for i in range(1, count):
        closes.append(closes[-1] * (1 + (move if i % 2 else -move)))
    return closes


# ---------------------------------------------------------------------------
class SignalTests(unittest.TestCase):
    def test_trend_long_short_and_split(self):
        up = daily(ramp(100, 200, 400))
        self.assertEqual(trend_signal(up, 50, 200, 365)[0], 1)
        self.assertEqual(trend_signal(daily(ramp(200, 100, 400)), 50, 200, 365)[0], -1)
        # up on the year, but falling hard for the last ten weeks: the EMAs say down, the year says up -> flat
        split = daily(ramp(100, 200, 330) + ramp(198, 140, 70))
        signal, why = trend_signal(split, 50, 200, 365)
        self.assertEqual(signal, 0, why)
        self.assertIn("below", why)

    def test_trend_needs_history(self):
        self.assertIsNone(trend_signal(daily(ramp(100, 200, 150)), 50, 200, 365)[0])
        self.assertIsNone(trend_signal(daily(ramp(100, 200, 300)), 50, 200, 365)[0])  # under a year

    def test_volatility_of_known_moves(self):
        vol = annual_volatility(daily(alternating(300, 0.01)), 60)
        self.assertAlmostEqual(vol, 0.01 * math.sqrt(365.25), places=6)  # one bar a calendar day
        self.assertIsNone(annual_volatility(daily(alternating(30, 0.01)), 60))

    def test_month_ends_exclude_the_current_month(self):
        bars = daily(list(range(1, 100)), start=utc(2026, 7, 1, 4))  # 1 Jul - 7 Oct 2026, New York midnight
        months = month_end_closes(bars, now=utc(2026, 10, 7, 15))
        self.assertEqual([m for m, _ in months], ["2026-07", "2026-08", "2026-09"])
        self.assertEqual(months[-1][1], 92)  # 30 September is day 92
        self.assertEqual(above_average(months, 3)[0], True)
        self.assertEqual(above_average(list(reversed(months)), 3)[0], False)
        self.assertIsNone(above_average(months, 4)[0])


# ---------------------------------------------------------------------------
class PlanTests(unittest.TestCase):
    def setUp(self):
        self.market = Market("X", "X", "X", 1.0, 1.0)
        self.broker = Broker(None, mock.Mock(), mock.Mock())

    def plan(self, current, want, band=0.0):
        return plan(current, Target("X", size=want, band=band), self.market, self.broker)

    def test_open_hold_add_and_reduce(self):
        self.assertEqual(self.plan(0, 10), [("open", "long", 10)])
        self.assertEqual(self.plan(0, -10), [("open", "short", 10)])
        self.assertEqual(self.plan(10, 12, band=2.5), [])                  # inside the band
        self.assertEqual(self.plan(10, 14, band=2.5), [("open", "long", 4)])
        self.assertEqual(self.plan(-10, -6, band=2.5), [("close", 4)])
        self.assertEqual(self.plan(10, 0.4), [("close", 9.0)])             # whole steps only
        self.assertEqual(self.plan(0, 0), [])

    def test_flat_and_flip_close_everything_first(self):
        self.assertEqual(self.plan(10, 0), [("close", None)])
        self.assertEqual(self.plan(10, -8), [("close", None), ("open", "short", 8)])
        self.assertEqual(self.plan(-3, 5), [("close", None), ("open", "long", 5)])


# ---------------------------------------------------------------------------
def settings(strategy, markets, budget=10000.0, dry_run=False, **env):
    with mock.patch.dict(os.environ, env):
        params = strategy_params(strategy)
    return BotSettings(broker="fake", strategy=strategy, slug=f"test-{strategy}", name="Test bot", markets=markets,
                       budget=budget, max_positions=50, account_id="", dry_run=dry_run, params=params)


class SettingsTests(unittest.TestCase):
    def test_defaults_and_checks(self):
        p = strategy_params("slow-trend")
        self.assertEqual((p["fast_ema"], p["slow_ema"], p["trade_time"]), (50, 200, "15:00"))
        self.assertEqual(strategy_params("etf-rotation")["cash_fund"], "SHY")
        with mock.patch.dict(os.environ, {"SLOW_TREND_FAST_EMA": "200"}), self.assertRaises(SettingsError):
            strategy_params("slow-trend")
        with mock.patch.dict(os.environ, {"SLOW_TREND_TRADE_TIME": "22:30"}), self.assertRaises(SettingsError):
            strategy_params("slow-trend")


class ScheduleTests(unittest.TestCase):
    def test_slow_trend_trades_weekdays_from_the_trade_time_to_before_the_rollover(self):
        s = SlowTrend(strategy_params("slow-trend"))
        self.assertEqual(s.period(None, utc(2026, 9, 30, 14, 30)), "2026-09-30")  # Wed 15:30 London
        self.assertIsNone(s.period(None, utc(2026, 9, 30, 13, 0)))                # 14:00 London
        self.assertIsNone(s.period(None, utc(2026, 9, 30, 20, 50)))               # 21:50, rollover at 22:00
        self.assertIsNone(s.period(None, utc(2026, 10, 3, 14, 30)))               # Saturday

    def test_rotation_trades_once_the_session_has_run_a_while(self):
        s = EtfRotation(strategy_params("etf-rotation"))
        opened = utc(2026, 10, 1, 13, 30)
        broker = mock.Mock(session_opened_at=mock.Mock(return_value=opened))
        self.assertIsNone(s.period(broker, opened + 10 * 60))
        self.assertEqual(s.period(broker, opened + 31 * 60), "2026-10")
        broker.session_opened_at.return_value = None  # shut
        self.assertIsNone(s.period(broker, opened + 31 * 60))


# ---------------------------------------------------------------------------
class FakeBroker(Broker):
    key, name = "fake", "Fake"

    def __init__(self, settings, history, quotes, positions=None, min_size=1.0, step=1.0):
        super().__init__(settings, mock.Mock(), mock.Mock())
        self.history, self._quotes, self._positions = history, quotes, positions or {}
        self.min_size, self.step = min_size, step
        self.orders = []
        self.opened_at = None

    def connect(self):
        return Account("1", "GBP", 10000, 10000, "fake account")

    def resolve(self, names):
        return {n: Market(n, n, n, self.min_size, self.step, digits=4) for n in names}

    def daily_bars(self, market, count):
        return self.history.get(market.symbol, [])[-count:]

    def session_opened_at(self, now):
        return self.opened_at

    def quotes(self, markets):
        return {m.symbol: self._quotes[m.symbol] for m in markets if m.symbol in self._quotes}

    def positions(self, markets):
        return {s: p for s, p in self._positions.items() if s in markets}

    def open(self, market, direction, size, stop, take_profit, quote):
        self.orders.append(("open", market.symbol, direction, size, stop))
        held = self._positions.get(market.symbol)
        self._positions[market.symbol] = Position(market.symbol, direction, (held.size if held else 0) + size, quote.mid,
                                                  own=True)
        return True

    def close(self, market, position, reason, size=None):
        self.orders.append(("close", market.symbol, size))
        if size is None:
            self._positions.pop(market.symbol, None)
        else:
            position.size -= size
        return True


def quote(price, spread=0.0, tradeable=True):
    return Quote(price - spread / 2, price + spread / 2, tradeable, unit_value=price)


class SlowTrendBotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(runner, "STATE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        # UP rises steadily, DOWN falls, both with 1% daily wiggles (~19% a year)
        up = [c * (1 + i * 0.002) for i, c in enumerate(alternating(600, 0.01))]
        down = [c * (1 - i * 0.001) for i, c in enumerate(alternating(600, 0.01))]
        self.history = {"UP": daily(up), "DOWN": daily(down)}
        self.quotes = {"UP": quote(up[-1], 0.02), "DOWN": quote(down[-1], 0.02)}

    def bot(self, positions=None, dry_run=False, **env):
        s = settings("slow-trend", ["UP", "DOWN"], dry_run=dry_run, **env)
        broker = FakeBroker(s, self.history, self.quotes, positions)
        bot = RebalanceBot(s, SlowTrend(s.params), broker, mock.Mock(), mock.Mock())
        bot.markets = broker.resolve(s.markets)
        return bot, broker

    def targets(self, bot):
        return bot.strategy.targets(bot.markets, self.history, self.quotes, bot.settings.budget, bot.broker, 0)

    def test_sizing_by_volatility(self):
        bot, _ = self.bot()
        t = self.targets(bot)
        vol = annual_volatility(self.history["UP"], 60)
        # 10% a year across 2 markets -> 7.07% of the 10,000 budget each, / its volatility, / the unit's value
        self.assertEqual(t["UP"].size, math.floor(10000 * 0.10 / math.sqrt(2) / vol / self.quotes["UP"].mid))
        self.assertLess(t["DOWN"].size, 0)
        self.assertAlmostEqual(t["UP"].band, abs(t["UP"].size) * 0.25)

    def test_caps_and_a_smallest_trade_too_big(self):
        bot, _ = self.bot(SLOW_TREND_MAX_MARKET_LEVERAGE="0.1")      # at most 1,000 exposure a market
        self.assertEqual(self.targets(bot)["UP"].size, math.floor(1000 / self.quotes["UP"].mid))
        bot, _ = self.bot(SLOW_TREND_MAX_LEVERAGE="0.1")             # at most 1,000 in all, shared out
        t = self.targets(bot)
        self.assertLessEqual(abs(t["UP"].size) * self.quotes["UP"].mid + abs(t["DOWN"].size) * self.quotes["DOWN"].mid,
                             1000)
        bot, _ = self.bot()
        bot.settings.budget = 50                                     # can't afford one unit
        t = self.targets(bot)
        self.assertEqual(t["UP"].size, 0)
        self.assertIn("raise the budget", t["UP"].note)

    def test_first_rebalance_opens_with_a_safety_stop_then_waits_for_tomorrow(self):
        bot, broker = self.bot()
        with mock.patch.object(bot.strategy, "period", return_value="2026-10-01"):
            bot.cycle()
            opened = {o[1]: o for o in broker.orders}
            self.assertEqual(opened["UP"][2], "long")
            self.assertEqual(opened["DOWN"][2], "short")
            entry, vol = self.quotes["UP"].ask, annual_volatility(self.history["UP"], 60)
            self.assertAlmostEqual(opened["UP"][4], round(entry * (1 - 3 * vol / math.sqrt(12)), 4))
            self.assertGreater(opened["DOWN"][4], self.quotes["DOWN"].bid)
            broker.orders.clear()
            bot.cycle()  # the same day again: already done
            self.assertEqual(broker.orders, [])
        self.assertEqual(bot.state.positions["UP"]["direction"], "long")

    def test_within_band_no_trade_and_flip_closes_first(self):
        t = self.targets(self.bot()[0])
        up_now = abs(t["UP"].size) * 0.9                    # 10% under target: inside the 25% band
        down_now = Position("DOWN", "long", 5, 1.0, own=True)  # long, but it should be short
        bot, broker = self.bot({"UP": Position("UP", "long", up_now, 1.0, own=True), "DOWN": down_now})
        with mock.patch.object(bot.strategy, "period", return_value="2026-10-01"):
            bot.cycle()
        self.assertEqual([o for o in broker.orders if o[1] == "UP"], [])
        self.assertEqual([o[:3] for o in broker.orders], [("close", "DOWN", None), ("open", "DOWN", "short")])

    def test_leaves_other_positions_alone(self):
        other = Position("UP", "long", 3, 1.0, own=False)
        bot, broker = self.bot({"UP": other})
        with mock.patch.object(bot.strategy, "period", return_value="2026-10-01"):
            bot.cycle()
        self.assertEqual([o[1] for o in broker.orders], ["DOWN"])
        self.assertEqual(bot.state.trades_on("UP", "2026-10-01"), 1)  # tried again tomorrow, not every loop

    def test_shut_market_or_wide_spread_is_retried_later(self):
        self.quotes["UP"] = quote(self.quotes["UP"].mid, tradeable=False)
        self.quotes["DOWN"] = quote(self.quotes["DOWN"].mid, spread=self.quotes["DOWN"].mid * 0.01)  # 1% > 0.3%
        bot, broker = self.bot()
        with mock.patch.object(bot.strategy, "period", return_value="2026-10-01"):
            bot.cycle()
            self.assertEqual(broker.orders, [])
            self.assertEqual(set(bot.retry_at), {"UP", "DOWN"})
            self.quotes["UP"] = quote(self.quotes["UP"].mid)
            bot.retry_at.clear()
            bot.cycle()
        self.assertEqual([o[1] for o in broker.orders], ["UP"])

    def test_dry_run_sends_nothing(self):
        bot, broker = self.bot(dry_run=True)
        with mock.patch.object(bot.strategy, "period", return_value="2026-10-01"):
            bot.cycle()
        self.assertEqual(broker.orders, [])
        self.assertEqual(bot.state.trades_on("UP", "2026-10-01"), 1)


class RotationBotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(runner, "STATE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        start = utc(2025, 8, 1, 4)
        self.history = {"RISE": daily(ramp(50, 100, 420), start), "FALL": daily(ramp(100, 50, 420), start),
                        "SHY": daily([80.0] * 420, start)}
        self.quotes = {"RISE": quote(100.0), "FALL": quote(50.0), "SHY": quote(80.0)}
        self.now = utc(2026, 10, 1, 14, 30)

    def bot(self, positions=None):
        s = settings("etf-rotation", ["RISE", "FALL"], budget=100.0)
        broker = FakeBroker(s, self.history, self.quotes, positions, min_size=0.0, step=1e-6)
        bot = RebalanceBot(s, EtfRotation(s.params), broker, mock.Mock(), mock.Mock())
        bot.markets = broker.resolve(s.markets + ["SHY"])
        return bot, broker

    def test_targets_put_a_falling_fund_in_cash(self):
        bot, _ = self.bot()
        t = bot.strategy.targets(bot.markets, self.history, self.quotes, 100.0, bot.broker, self.now)
        self.assertAlmostEqual(t["RISE"].size, 0.5)           # $50 at $100
        self.assertEqual(t["FALL"].size, 0)
        self.assertAlmostEqual(t["SHY"].size, 0.625)          # FALL's $50 at $80
        self.assertAlmostEqual(t["RISE"].band, 2.5 / 100)     # 5% of $50, in shares

    def test_monthly_rebalance_sells_before_it_buys(self):
        mine = {"FALL": Position("FALL", "long", 1.0, 50.0), "RISE": Position("RISE", "long", 0.49, 100.0)}
        bot, broker = self.bot(mine)
        bot.state.positions = {"FALL": {"direction": "long"}, "RISE": {"direction": "long"}}  # the bot bought them
        with mock.patch.object(bot.strategy, "period", return_value="2026-10"), \
                mock.patch.object(rebalancer.time, "time", return_value=self.now):
            bot.cycle()
        # RISE is $1 under its $50 - inside the $2.50 band; FALL is sold, its money goes to SHY
        self.assertEqual([o[:2] for o in broker.orders], [("close", "FALL"), ("open", "SHY")])
        self.assertAlmostEqual(broker.orders[1][3], 0.625)
        self.assertIsNone(broker.orders[1][4])                 # no stop on a fund


if __name__ == "__main__":
    unittest.main()
