"""
Tests for intraday momentum (engine/strategies.py IntradayMomentum): the
NYSE calendar it trades by, the noise area and its signals on made-up
30-minute bars, its exits, its settings and the runner waking for each bar.
No broker is contacted.

    python -m unittest discover -s strategy-bots/tests
"""

import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, time as dtime, timedelta, timezone
from unittest import mock

os.environ["DASHBOARD_URL"] = ""  # never report test runs to the real dashboard
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from engine import clock, runner  # noqa: E402
from engine.brokers.base import Market, Position, Quote  # noqa: E402
from engine.indicators import Bar  # noqa: E402
from engine.settings import SettingsError, bot_settings, strategy_params  # noqa: E402
from engine.strategies import IndexReversion, IntradayMomentum  # noqa: E402

from test_engine import FakeBroker, settings  # noqa: E402

HALF_HOUR = 1800


def session_bars(day: date, closes: list, day_open: float = 100.0) -> list:
    """One NYSE session's 30-minute bars from 09:30: each opens where the last closed."""
    start = clock.at("new_york", day, dtime(9, 30))
    bars, last = [], day_open
    for i, close in enumerate(closes):
        bars.append(Bar(start + i * HALF_HOUR, last, max(last, close) + 0.01, min(last, close) - 0.01, close, 100))
        last = close
    return bars


def nyse_days(end: date, count: int) -> list:
    """The `count` NYSE sessions up to and including `end`, oldest first."""
    days, day = [], end
    while len(days) < count:
        if clock.nyse_hours(day) is not None:
            days.append(day)
        day -= timedelta(days=1)
    return days[::-1]


TODAY = date(2026, 10, 6)  # a Tuesday


def history(sessions: int = 15) -> list:
    """`sessions` calm sessions before TODAY: every half hour 0.4% from a 100 open,
    up one day and down the next - so the noise is 0.4% at every time of day.
    The last one (5 Oct) closes at 99.6."""
    bars = []
    days = nyse_days(TODAY - timedelta(days=1), sessions)
    for i, day in enumerate(days):
        level = 99.6 if (len(days) - 1 - i) % 2 == 0 else 100.4
        bars += session_bars(day, [level] * 13)
    return bars


class CalendarTests(unittest.TestCase):
    def test_holidays_2025(self):
        self.assertEqual(clock.nyse_holidays(2025), {
            date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18), date(2025, 5, 26),
            date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25)})

    def test_holidays_2026_with_independence_day_on_a_saturday(self):
        self.assertEqual(clock.nyse_holidays(2026), {
            date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
            date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)})

    def test_weekend_holidays_move(self):
        holidays = clock.nyse_holidays(2022)
        self.assertNotIn(date(2021, 12, 31), clock.nyse_holidays(2021))  # Saturday New Year: not moved back
        self.assertIn(date(2022, 6, 20), holidays)    # Juneteenth on a Sunday -> Monday
        self.assertIn(date(2022, 12, 26), holidays)   # Christmas on a Sunday -> Monday
        self.assertIn(date(2021, 12, 24), clock.nyse_holidays(2021))  # Christmas on a Saturday -> Friday
        self.assertNotIn(date(2021, 6, 18), clock.nyse_holidays(2021))  # no Juneteenth before 2022

    def test_early_closes(self):
        self.assertEqual(clock.nyse_early_closes(2024), {date(2024, 7, 3), date(2024, 11, 29), date(2024, 12, 24)})
        self.assertEqual(clock.nyse_early_closes(2025), {date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)})
        self.assertEqual(clock.nyse_early_closes(2026), {date(2026, 11, 27), date(2026, 12, 24)})

    def test_hours(self):
        self.assertIsNone(clock.nyse_hours(date(2026, 10, 3)))   # Saturday
        self.assertIsNone(clock.nyse_hours(date(2026, 11, 26)))  # Thanksgiving
        opens, closes = clock.nyse_hours(date(2026, 10, 6))
        self.assertEqual(opens, datetime(2026, 10, 6, 13, 30, tzinfo=timezone.utc).timestamp())  # 09:30 EDT
        self.assertEqual(closes, datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc).timestamp())
        _, closes = clock.nyse_hours(date(2026, 11, 27))
        self.assertEqual(closes, datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc).timestamp())  # 13:00 EST


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.strategy = IntradayMomentum(strategy_params("intraday-momentum"))
        self.market = Market("NAS100", "US Tech 100", "NAS100", 0.1, 0.1)

    def assess(self, bars, strategy=None):
        return (strategy or self.strategy).assess(self.market, {"exec": bars}, bars[-1].time + HALF_HOUR)

    def test_noise_area(self):
        levels, why = self.strategy.levels(history() + session_bars(TODAY, [100.1]))
        self.assertEqual(why, "")
        self.assertAlmostEqual(levels.sigma, 0.004)
        self.assertAlmostEqual(levels.upper, 100 * 1.004)         # max(open 100, previous close 99.6)
        self.assertAlmostEqual(levels.lower, 99.6 * 0.996)        # min(...)
        self.assertEqual(levels.checked_at, clock.at("new_york", TODAY, dtime(10, 0)))

    def test_band_multiplier(self):
        wide = IntradayMomentum({**strategy_params("intraday-momentum"), "band_multiplier": 2.0})
        levels, _ = wide.levels(history() + session_bars(TODAY, [100.1]))
        self.assertAlmostEqual(levels.sigma, 0.008)

    def test_breakout_up_is_a_long_with_its_stop_at_the_far_band(self):
        result = self.assess(history() + session_bars(TODAY, [101.0]))
        self.assertIsNotNone(result.signal, result.note)
        self.assertEqual(result.signal.direction, "long")
        self.assertAlmostEqual(result.signal.stop, 99.6 * 0.996)
        self.assertIsNone(result.signal.take_profit)
        self.assertIsNone(result.signal.reward_risk)

    def test_breakout_down_is_a_short(self):
        result = self.assess(history() + session_bars(TODAY, [98.5]))
        self.assertEqual(result.signal.direction, "short")
        self.assertAlmostEqual(result.signal.stop, 100 * 1.004)

    def test_inside_the_noise_area(self):
        result = self.assess(history() + session_bars(TODAY, [100.2]))
        self.assertIsNone(result.signal)
        self.assertIn("inside the noise area", result.note)

    def test_vwap_must_agree(self):
        # A spike to 103 lifts the VWAP; back to 100.6 is over the band but under the VWAP.
        bars = history() + session_bars(TODAY, [103.0, 100.6])
        result = self.assess(bars)
        self.assertIsNone(result.signal)
        self.assertIn("wrong side of the VWAP", result.note)
        no_trail = IntradayMomentum({**strategy_params("intraday-momentum"), "vwap_trail": False})
        self.assertEqual(self.assess(bars, no_trail).signal.direction, "long")

    def test_first_check(self):
        later = IntradayMomentum({**strategy_params("intraday-momentum"), "first_check": "10:30"})
        result = self.assess(history() + session_bars(TODAY, [101.0]), later)
        self.assertIsNone(result.signal)
        self.assertIn("before the first check", result.note)
        self.assertEqual(self.assess(history() + session_bars(TODAY, [100.5, 101.0]), later).signal.direction, "long")

    def test_last_entry(self):
        # The 15:00 bar closes at 15:30, the last check with entries; the 15:30 bar closes at 16:00.
        self.assertEqual(self.assess(history() + session_bars(TODAY, [100.1] * 11 + [101.0])).signal.direction,
                         "long")
        result = self.assess(history() + session_bars(TODAY, [100.1] * 12 + [101.0]))
        self.assertIsNone(result.signal)
        self.assertIn("no entries in the last 30 min", result.note)

    def test_needs_enough_history(self):
        result = self.assess(history(sessions=4) + session_bars(TODAY, [101.0]))
        self.assertIsNone(result.signal)
        self.assertIn("only 4 of the last 14 sessions", result.note)

    def test_needs_yesterday_s_session(self):
        # A gap in the history (MetaTrader's first read on 7 Oct 2026 had no 6 Oct): no previous close, no trade.
        bars = [b for b in history() if clock.local_date("new_york", b.time) != date(2026, 10, 5)]
        result = self.assess(bars + session_bars(TODAY, [101.0]))
        self.assertIsNone(result.signal)
        self.assertIn("no Mon 05 Oct session", result.note)

    def test_previous_nyse_day(self):
        self.assertEqual(clock.previous_nyse_day(date(2026, 10, 6)), date(2026, 10, 5))
        self.assertEqual(clock.previous_nyse_day(date(2026, 10, 5)), date(2026, 10, 2))    # over the weekend
        self.assertEqual(clock.previous_nyse_day(date(2026, 11, 27)), date(2026, 11, 25))  # over Thanksgiving

    def test_needs_the_opening_bar(self):
        result = self.assess(history() + session_bars(TODAY, [100.5, 101.0])[1:])
        self.assertIn("no bar from the 09:30 open", result.note)

    def test_outside_the_session_and_on_holidays(self):
        evening = Bar(clock.at("new_york", TODAY, dtime(16, 30)), 100, 101, 99, 101, 100)
        self.assertIn("outside the NYSE session", self.assess(history() + [evening]).note)
        thanksgiving = Bar(clock.at("new_york", date(2026, 11, 26), dtime(10, 0)), 100, 101, 99, 101, 100)
        self.assertIn("the NYSE is shut today", self.assess(history() + [thanksgiving]).note)

    def test_cfd_bars_outside_the_session_are_ignored(self):
        # A CFD trades nearly round the clock: the evening and early-morning bars don't count.
        bars = []
        for bar in history():
            bars.append(bar)
            if clock.local("new_york", bar.time).time() == dtime(15, 30):
                bars.append(Bar(bar.time + HALF_HOUR, bar.close, 120, 80, 120, 5))  # wild after-hours
        day_before = clock.at("new_york", TODAY, dtime(9, 0))
        bars.append(Bar(day_before, 120, 121, 119, 120, 5))
        levels, _ = self.strategy.levels(bars + session_bars(TODAY, [100.1]))
        self.assertAlmostEqual(levels.sigma, 0.004)
        self.assertAlmostEqual(levels.upper, 100 * 1.004)

    def test_half_day_noise_uses_the_sessions_that_reach_that_time(self):
        # 27 Nov 2026 closes at 13:00: its bars end at 12:30-13:00 and the 14:00 noise skips it.
        black_friday = date(2026, 11, 27)
        bars = []
        for day in nyse_days(date(2026, 12, 1), 16)[:-1]:
            bars += session_bars(day, [100.4] * (7 if day == black_friday else 13))
        self.assertTrue(any(clock.local_date("new_york", b.time) == black_friday for b in bars))
        today = session_bars(date(2026, 12, 1), [100.1] * 9)
        levels, why = self.strategy.levels(bars + today)
        self.assertEqual(why, "")
        self.assertAlmostEqual(levels.sigma, 0.004)

    def test_trailing_exit(self):
        position = Position("NAS100", "long", 1.0, 101.0, opened_at=clock.at("new_york", TODAY, dtime(10, 1)))
        # Still above the band and the VWAP: hold.
        bars = history() + session_bars(TODAY, [101.0, 101.5])
        self.assertIsNone(self.strategy.exit_on_bar(self.market, {"exec": bars}, position, {}, bars[-1].time))
        # Back inside the noise area: out.
        bars = history() + session_bars(TODAY, [101.0, 100.2])
        reason = self.strategy.exit_on_bar(self.market, {"exec": bars}, position, {}, bars[-1].time)
        self.assertIn("momentum gone", reason)
        # Over the band but under the VWAP: out with the trail, held without it.
        bars = history() + session_bars(TODAY, [103.0, 100.6])
        self.assertIn("VWAP", self.strategy.exit_on_bar(self.market, {"exec": bars}, position, {}, bars[-1].time))
        no_trail = IntradayMomentum({**strategy_params("intraday-momentum"), "vwap_trail": False})
        self.assertIsNone(no_trail.exit_on_bar(self.market, {"exec": bars}, position, {}, bars[-1].time))

    def test_short_trailing_exit(self):
        position = Position("NAS100", "short", 1.0, 99.0, opened_at=clock.at("new_york", TODAY, dtime(10, 1)))
        bars = history() + session_bars(TODAY, [99.0, 99.5])  # back inside the area, the VWAP above the band
        self.assertIn("above the band",
                      self.strategy.exit_on_bar(self.market, {"exec": bars}, position, {}, bars[-1].time))

    def test_flat_before_the_close(self):
        opened = clock.at("new_york", TODAY, dtime(10, 1))
        position = Position("NAS100", "long", 1.0, 101.0, opened_at=opened)
        state = {"opened_at": opened}
        self.assertIsNone(self.strategy.exit_on_time(self.market, position, state,
                                                     clock.at("new_york", TODAY, dtime(15, 57, 59))))
        self.assertIn("before the NYSE close", self.strategy.exit_on_time(
            self.market, position, state, clock.at("new_york", TODAY, dtime(15, 58))))
        self.assertIn("before the NYSE close", self.strategy.exit_on_time(
            self.market, position, state, clock.at("new_york", TODAY + timedelta(days=1), dtime(9, 45))))

    def test_flat_before_a_half_day_close(self):
        day = date(2026, 11, 27)
        opened = clock.at("new_york", day, dtime(10, 1))
        position = Position("NAS100", "long", 1.0, 101.0, opened_at=opened)
        self.assertIsNone(self.strategy.exit_on_time(self.market, position, {"opened_at": opened},
                                                     clock.at("new_york", day, dtime(12, 57))))
        self.assertIsNotNone(self.strategy.exit_on_time(self.market, position, {"opened_at": opened},
                                                        clock.at("new_york", day, dtime(12, 58))))

    def test_trade_day_is_new_york_s(self):
        self.assertEqual(self.strategy.trade_day(self.market, clock.at("new_york", TODAY, dtime(19, 30))),
                         "2026-10-06")  # 00:30 in London the next day

    def test_only_us_indices(self):
        log = mock.Mock()
        markets = {s: Market(s, s, s, 0.1, 0.1) for s in ("NAS100", "QQQ", "UK100", "XAUUSD")}
        markets["Tech"] = Market("Tech", "Tech", "Tech", 0.1, 0.1, session_hint="us")
        self.assertEqual(set(self.strategy.prepare(markets, log)), {"NAS100", "QQQ", "Tech"})
        self.assertEqual(log.warning.call_count, 2)


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in [n for n in os.environ if n.startswith("INTRADAY_") or n.startswith("ALPACA_INTRADAY")]:
                del os.environ[name]
            s = bot_settings("alpaca", "intraday-momentum")
        self.assertEqual(s.markets, ["QQQ"])
        self.assertEqual(s.max_positions, 1)
        self.assertEqual(s.budget, 1000)
        self.assertEqual(s.slug, "alpaca-intraday-momentum")
        self.assertEqual(s.name, "Alpaca intraday momentum")
        self.assertEqual(s.params["timeframe"], "M30")
        self.assertEqual((s.params["risk_percent"], s.params["max_leverage"]), (2.0, 1.0))
        self.assertEqual(s.params["lookback_days"], 14)

    def test_lookback_fits_capital_com(self):
        with mock.patch.dict(os.environ, {"INTRADAY_LOOKBACK_DAYS": "19"}):
            with self.assertRaises(SettingsError):
                strategy_params("intraday-momentum")
        strategy = IntradayMomentum({**strategy_params("intraday-momentum"), "lookback_days": 18})
        self.assertLessEqual(strategy.feeds()["exec"][1], 1000)

    def test_first_check_inside_the_session(self):
        with mock.patch.dict(os.environ, {"INTRADAY_FIRST_CHECK": "09:00"}):
            with self.assertRaises(SettingsError):
                strategy_params("intraday-momentum")

    def test_every_broker_has_a_bot(self):
        for broker in ("oanda", "pepperstone", "capital", "ig", "alpaca"):
            self.assertEqual(bot_settings(broker, "intraday-momentum").slug, f"{broker}-intraday-momentum")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(runner, "STATE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_wakes_for_the_next_bar(self):
        s = settings("intraday-momentum", markets=["NAS100"], max_positions=1)
        bot = runner.StrategyBot(s, IntradayMomentum(s.params), FakeBroker(s, {}, None), mock.Mock(), mock.Mock())
        feed = mock.Mock(next_check=time.time() + 5)
        bot.feeds = {"NAS100": {"exec": feed}}
        self.assertAlmostEqual(bot.wait_seconds(), 5, delta=0.5)
        feed.next_check = time.time() + 600
        self.assertEqual(bot.wait_seconds(), runner.LOOP_SECONDS)
        feed.next_check = time.time() - 100  # overdue: look again shortly, not in a busy loop
        self.assertEqual(bot.wait_seconds(), 1.0)

    def test_other_strategies_keep_their_pace(self):
        s = settings("index-reversion", markets=["US500"])
        bot = runner.StrategyBot(s, IndexReversion(s.params), FakeBroker(s, {}, None), mock.Mock(), mock.Mock())
        bot.feeds = {"US500": {"exec": mock.Mock(next_check=time.time() + 5)}}
        self.assertEqual(bot.wait_seconds(), runner.LOOP_SECONDS)

    def test_entry_is_sized_to_the_budget(self):
        # Stop 1% away: 2% risk would allow twice the budget, so the 1x leverage cap decides.
        s = settings("intraday-momentum", markets=["NAS100"], max_positions=1, budget=1000.0)
        quote = Quote(99.99, 100.01, True, unit_value=100.0)  # one unit = 100 GBP
        bot = runner.StrategyBot(s, IntradayMomentum(s.params), FakeBroker(s, {}, quote), mock.Mock(), mock.Mock())
        bot.markets = bot.broker.resolve(["NAS100"])
        market = Market("NAS100", "NAS100", "NAS100", 0.01, 0.01)
        size, text = bot.size(market, quote, 1.0)
        self.assertAlmostEqual(size, 10.0)  # 10 x 100 GBP = the 1,000 budget
        self.assertIn("leverage cap", text)


if __name__ == "__main__":
    unittest.main()
