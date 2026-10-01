"""
Tests for the strategy bots' engine: the clock, the indicators, each
strategy's signals on made-up bars, and the runner's risk rules and sizing
against a fake broker. No broker is contacted.

    python -m unittest discover -s strategy-bots/tests
"""

import os
import sys
import tempfile
import unittest
from datetime import date, datetime, time as dtime, timezone
from unittest import mock

os.environ["DASHBOARD_URL"] = ""  # never report test runs to the real dashboard
for _name in [n for n in os.environ if "STAGNANT" in n]:  # the stagnancy timeout at its defaults
    del os.environ[_name]
os.environ["STAGNANT_FILE"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "no-stagnancy.json")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from engine import clock, runner  # noqa: E402
from engine.brokers.base import Account, Broker, Market, Position, Quote  # noqa: E402
from engine.indicators import Bar, adx, atr, ema, rsi, session_vwap  # noqa: E402
from engine.settings import BotSettings, strategy_params  # noqa: E402
from engine.strategies import CommodityTrend, IndexReversion, SessionBreakout, guess_session  # noqa: E402


def utc(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def flat_bars(start: float, count: int, price: float, seconds: int = 900, wiggle: float = 0.0) -> list:
    bars = []
    for i in range(count):
        drift = wiggle if i % 2 else -wiggle
        bars.append(Bar(start + i * seconds, price, price + abs(wiggle) + 1e-4 * price, price - abs(wiggle) - 1e-4 * price,
                        price + drift, 100))
    return bars


# ---------------------------------------------------------------------------
class ClockTests(unittest.TestCase):
    def test_london_summer_and_winter(self):
        self.assertEqual(clock.utc_offset_hours("london", utc(2026, 7, 1, 12)), 1)
        self.assertEqual(clock.utc_offset_hours("london", utc(2026, 12, 1, 12)), 0)
        # UK clocks change at 01:00 UTC on the last Sunday of October (25 Oct 2026)
        self.assertEqual(clock.utc_offset_hours("london", utc(2026, 10, 25, 0, 59)), 1)
        self.assertEqual(clock.utc_offset_hours("london", utc(2026, 10, 25, 1, 0)), 0)

    def test_new_york_dst(self):
        # US clocks change on the second Sunday of March (8 Mar 2026) and first Sunday of November (1 Nov 2026)
        self.assertEqual(clock.utc_offset_hours("new_york", utc(2026, 3, 8, 6, 59)), -5)
        self.assertEqual(clock.utc_offset_hours("new_york", utc(2026, 3, 8, 7, 0)), -4)
        self.assertEqual(clock.utc_offset_hours("new_york", utc(2026, 11, 1, 5, 59)), -4)
        self.assertEqual(clock.utc_offset_hours("new_york", utc(2026, 11, 1, 6, 0)), -5)

    def test_to_utc_round_trip(self):
        ts = clock.at("london", date(2026, 9, 29), dtime(13, 0))
        self.assertEqual(ts, utc(2026, 9, 29, 12, 0))
        self.assertEqual(clock.at("new_york", date(2026, 9, 29), dtime(9, 30)), utc(2026, 9, 29, 13, 30))
        self.assertEqual(clock.at("frankfurt", date(2026, 1, 5), dtime(9, 0)), utc(2026, 1, 5, 8, 0))

    def test_rollover_window(self):
        rollover = utc(2026, 9, 29, 21, 0)  # 17:00 New York in September
        self.assertEqual(clock.next_rollover(utc(2026, 9, 29, 12, 0)), rollover)
        self.assertTrue(clock.near_rollover(rollover - 10 * 60))
        self.assertTrue(clock.near_rollover(rollover + 30 * 60))
        self.assertFalse(clock.near_rollover(rollover + 60 * 60))
        self.assertFalse(clock.near_rollover(utc(2026, 9, 29, 13, 0)))

    def test_sessions(self):
        opens, closes = clock.INDEX_SESSIONS["us"].bounds(date(2026, 9, 29))
        self.assertEqual((opens, closes), (utc(2026, 9, 29, 13, 30), utc(2026, 9, 29, 20, 0)))
        self.assertIsNone(clock.INDEX_SESSIONS["uk"].bounds(date(2026, 10, 3)))  # Saturday


# ---------------------------------------------------------------------------
class IndicatorTests(unittest.TestCase):
    def test_ema(self):
        values = ema([1, 2, 3, 4, 5, 6], 3)
        self.assertEqual(values[:2], [None, None])
        self.assertAlmostEqual(values[2], 2.0)
        self.assertAlmostEqual(values[3], 3.0)   # 2 + 0.5 * (4 - 2)
        self.assertAlmostEqual(values[5], 5.0)

    def test_rsi_extremes(self):
        self.assertEqual(rsi(list(range(1, 30)), 14)[-1], 100.0)
        self.assertLess(rsi(list(range(30, 1, -1)), 14)[-1], 1.0)

    def test_atr_of_constant_range(self):
        bars = [Bar(i, 10, 11, 9, 10) for i in range(30)]
        self.assertAlmostEqual(atr(bars, 14)[-1], 2.0)

    def test_adx_trend_vs_range(self):
        trending = [Bar(i, 100 + i, 101 + i, 99.5 + i, 100.8 + i) for i in range(80)]
        ranging = [Bar(i, 100, 101 + (i % 2), 99 - (i % 2), 100 + (1 if i % 2 else -1)) for i in range(80)]
        self.assertGreater(adx(trending, 14)[-1], 50)
        self.assertLess(adx(ranging, 14)[-1], 25)

    def test_session_vwap(self):
        bars = [Bar(0, 10, 10, 10, 10, 1), Bar(1, 20, 20, 20, 20, 3)]
        vwap, sigma = session_vwap(bars)
        self.assertEqual(vwap, [10, 17.5])
        self.assertAlmostEqual(sigma[1], (0.25 * 7.5 ** 2 + 0.75 * 2.5 ** 2) ** 0.5)


# ---------------------------------------------------------------------------
def env(**values):
    return mock.patch.dict(os.environ, {k: str(v) for k, v in values.items()})


class BreakoutTests(unittest.TestCase):
    def setUp(self):
        with env(BREAKOUT_TREND_EMA=0):
            self.strategy = SessionBreakout(strategy_params("session-breakout"))
        self.market = Market("GBP_USD", "GBP/USD", "GBP_USD", 1, 1)
        # Tuesday 29 Sep 2026: London is UTC+1, so the 07:00-13:00 range is 06:00-12:00 UTC.
        start = utc(2026, 9, 28, 12, 0)
        self.bars = flat_bars(start, int((utc(2026, 9, 29, 12, 0) - start) / 900), 1.3000, wiggle=0.0015)

    def with_bar(self, close):
        bar = Bar(utc(2026, 9, 29, 12, 0), 1.3010, max(close, 1.3010), min(close, 1.3010), close)
        return {"exec": self.bars + [bar]}

    def test_building_range(self):
        a = self.strategy.assess(self.market, {"exec": self.bars}, utc(2026, 9, 29, 12, 0))
        self.assertIsNone(a.signal)
        self.assertIn("building", a.note)

    def test_long_breakout(self):
        # The range is about 1.2984-1.3016, the buffer about 0.0007: 1.3025 is a clean break, not a chase.
        a = self.strategy.assess(self.market, self.with_bar(1.3025), utc(2026, 9, 29, 12, 15))
        self.assertIsNotNone(a.signal, a.note)
        self.assertEqual(a.signal.direction, "long")
        high = max(b.high for b in self.bars if b.time >= utc(2026, 9, 29, 6, 0))
        low = min(b.low for b in self.bars if b.time >= utc(2026, 9, 29, 6, 0))
        self.assertAlmostEqual(a.signal.stop, (high + low) / 2)
        self.assertEqual(a.signal.reward_risk, 1.5)

    def test_inside_and_chasing(self):
        self.assertIn("inside", self.strategy.assess(self.market, self.with_bar(1.3010), 0).note)
        self.assertIn("chasing", self.strategy.assess(self.market, self.with_bar(1.3100), 0).note)

    def test_flat_time(self):
        position = Position("GBP_USD", "long", 1, 1.3, opened_at=utc(2026, 9, 29, 12, 20))
        self.assertIsNone(self.strategy.exit_on_time(self.market, position, {}, utc(2026, 9, 29, 18, 59)))
        self.assertIsNotNone(self.strategy.exit_on_time(self.market, position, {}, utc(2026, 9, 29, 19, 0)))


class ReversionTests(unittest.TestCase):
    def setUp(self):
        self.strategy = IndexReversion(strategy_params("index-reversion"))
        self.market = Market("US500", "US 500", "US500", 0.01, 0.01)
        self.strategy.prepare({"US500": self.market}, mock.Mock())
        # The day before (session and evening), then the 29 Sep US session from 13:30 UTC, calm around 6700...
        bars = flat_bars(utc(2026, 9, 28, 13, 30), 40, 6700, wiggle=3)
        bars += flat_bars(utc(2026, 9, 29, 13, 30), 12, 6700, wiggle=3)
        # ...then a slide to well below the session's mean.
        t = utc(2026, 9, 29, 16, 30)
        for i, close in enumerate((6690, 6680, 6670, 6660, 6650, 6640)):
            bars.append(Bar(t + i * 900, close + 10, close + 11, close - 2, close, 100))
        self.bars = bars

    def test_session_guess(self):
        self.assertEqual(guess_session("SPX500_USD"), "us")
        self.assertEqual(guess_session("UK100"), "uk")
        self.assertEqual(guess_session("GER40"), "eu")
        self.assertEqual(guess_session("Japan 225"), "jp")
        self.assertIsNone(guess_session("XAUUSD"))

    def test_long_fade_targets_vwap(self):
        with mock.patch.dict(self.strategy.p, max_adx=0):
            a = self.strategy.assess(self.market, {"exec": self.bars}, utc(2026, 9, 29, 18, 0))
        self.assertIsNotNone(a.signal, a.note)
        self.assertEqual(a.signal.direction, "long")
        self.assertGreater(a.signal.take_profit, self.bars[-1].close)
        self.assertLess(a.signal.stop, self.bars[-1].close)

    def test_adx_filter(self):
        with mock.patch.dict(self.strategy.p, max_adx=1):
            a = self.strategy.assess(self.market, {"exec": self.bars}, utc(2026, 9, 29, 18, 0))
        self.assertIsNone(a.signal)
        self.assertIn("trending", a.note)

    def test_no_entries_in_first_hour_and_flat_before_close(self):
        early = [b for b in self.bars if b.time < utc(2026, 9, 29, 14, 0)]
        self.assertIn("first 60 min", self.strategy.assess(self.market, {"exec": early}, 0).note)
        position = Position("US500", "long", 1, 6650, opened_at=utc(2026, 9, 29, 17, 0))
        self.assertIsNone(self.strategy.exit_on_time(self.market, position, {}, utc(2026, 9, 29, 19, 44)))
        self.assertIsNotNone(self.strategy.exit_on_time(self.market, position, {}, utc(2026, 9, 29, 19, 45)))


class TrendTests(unittest.TestCase):
    def setUp(self):
        self.strategy = CommodityTrend(strategy_params("commodity-trend"))
        self.market = Market("XAU_USD", "Gold", "XAU_USD", 0.1, 0.1)
        # A steady 4-hour uptrend with pullbacks...
        self.htf = [Bar(i * 14400, 3000 + i * 3, 3000 + i * 3 + 8, 3000 + i * 3 - 5 - (i % 3),
                        3000 + i * 3 + (4 if i % 3 else -2), 100) for i in range(300)]
        # ...and 15-minute bars dipping, then turning up through the EMAs.
        ltf = []
        price = 3900.0
        for i in range(100):
            price += -1.0 if i < 80 else 3.0
            ltf.append(Bar(i * 900, price, price + 1, price - 1, price, 100))
        self.ltf = ltf

    def test_trend_detection(self):
        self.assertEqual(self.strategy.trend(self.htf)[0], "up")
        self.assertIsNone(self.strategy.trend(self.htf[:100])[0])  # not enough for EMA 200

    def test_crossover_signal(self):
        now = utc(2026, 9, 29, 12, 0)  # a Tuesday
        for n in range(82, 100):
            a = self.strategy.assess(self.market, {"exec": self.ltf[:n], "htf": self.htf}, now)
            if a.signal:
                break
        self.assertIsNotNone(a.signal, a.note)
        self.assertEqual(a.signal.direction, "long")
        self.assertEqual(a.signal.reward_risk, 3.0)

    def test_trailing_stop(self):
        position = Position("XAU_USD", "long", 0.1, 3800)
        notes = {"best": 3990.0, "opened_at": 0}
        bars = self.ltf[:-1] + [Bar(99 * 900, 3900, 3901, 3880, 3880, 100)]
        self.assertIn("trailing", self.strategy.exit_on_bar(self.market, {"exec": bars, "htf": self.htf},
                                                            position, notes, 0))


# ---------------------------------------------------------------------------
class FakeBroker(Broker):
    key, name = "fake", "Fake"

    def __init__(self, settings, bars, quote, positions=None, **attributes):
        super().__init__(settings, mock.Mock(), mock.Mock())
        self._bars, self._quote, self._positions = bars, quote, positions or {}
        self.opened, self.closed = [], []
        for k, v in attributes.items():
            setattr(self, k, v)

    def connect(self):
        return Account("1", "GBP", 1000, 1000, "fake account")

    def resolve(self, names):
        return {n: Market(n, n, n, 1.0, 1.0, digits=5) for n in names}

    def bars(self, market, timeframe, count):
        return self._bars[timeframe][-count:]

    def quotes(self, markets):
        return {m.symbol: self._quote for m in markets}

    def positions(self, markets):
        return dict(self._positions)

    def open(self, market, direction, size, stop, take_profit, quote):
        self.opened.append((market.symbol, direction, size, stop, take_profit))
        return True

    def close(self, market, position, reason, size=None):
        self.closed.append((market.symbol, reason) if size is None else (market.symbol, reason, size))
        if getattr(self, "refuse", None):
            self.close_problem = self.refuse
            return False
        return True

    def refs(self, position):
        return set(position.raw or ())


def settings(strategy="session-breakout", **overrides):
    values = dict(broker="fake", strategy=strategy, slug="test-bot", name="Test bot", markets=["GBP_USD"],
                  budget=100.0, max_positions=2, account_id="", dry_run=False, params=strategy_params(strategy))
    values.update(overrides)
    return BotSettings(**values)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(runner, "STATE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def bot(self, broker, s):
        return runner.StrategyBot(s, SessionBreakout(s.params), broker, mock.Mock(), mock.Mock())

    def signal(self, stop=1.2950, direction="long"):
        from engine.strategies import Signal
        return Signal(direction, stop=stop, reward_risk=1.5, why="test", score=1)

    def test_sizing_by_risk_and_leverage_cap(self):
        s = settings()
        quote = Quote(1.29995, 1.30005, True, unit_value=1.0)  # one unit worth 1 GBP
        broker = FakeBroker(s, {}, quote)
        bot = self.bot(broker, s)
        bot.markets = broker.resolve(["GBP_USD"])
        size, text = bot.size(bot.markets["GBP_USD"], quote, 0.0050)  # a 50-pip stop
        # 1% of 100 = 1 GBP at risk; per unit a 0.005 move is worth 0.005/1.3 GBP -> 260 units,
        # under the 100 x 5 / 2 = 250 GBP cap?  250 units = 250 GBP, so the cap wins.
        self.assertEqual(size, 250)
        self.assertIn("leverage cap", text)
        size, text = bot.size(bot.markets["GBP_USD"], quote, 0.0100)
        self.assertEqual(size, 130)  # 1 / (0.01 / 1.3) = 130
        self.assertIn("risk", text)

    def test_entry_rules(self):
        s = settings()
        quote = Quote(1.3000, 1.3002, True, unit_value=1.0)
        broker = FakeBroker(s, {}, quote)
        bot = self.bot(broker, s)
        bot.markets = broker.resolve(["GBP_USD"])
        now = utc(2026, 9, 29, 12, 30)
        bot.enter([(1, "GBP_USD", self.signal())], {}, now)
        self.assertEqual(len(broker.opened), 1)
        symbol, direction, size, stop, take_profit = broker.opened[0]
        self.assertAlmostEqual(take_profit, 1.3002 + 1.5 * (1.3002 - 1.2950))
        self.assertEqual(bot.state.trades_on("GBP_USD", "2026-09-29"), 1)
        # one trade a day
        bot.enter([(1, "GBP_USD", self.signal())], {}, now)
        self.assertEqual(len(broker.opened), 1)

    def test_spread_and_rollover_and_slots(self):
        s = settings()
        wide = Quote(1.2990, 1.3010, True, unit_value=1.0)  # 20-pip spread vs a 60-pip stop = 33%
        broker = FakeBroker(s, {}, wide)
        bot = self.bot(broker, s)
        bot.markets = broker.resolve(["GBP_USD"])
        bot.enter([(1, "GBP_USD", self.signal())], {}, utc(2026, 9, 29, 12, 30))
        self.assertEqual(broker.opened, [])
        broker._quote = Quote(1.3000, 1.3001, True, unit_value=1.0)
        bot.enter([(1, "GBP_USD", self.signal())], {}, utc(2026, 9, 29, 21, 5))  # just after the rollover
        self.assertEqual(broker.opened, [])
        full = {"A": Position("A", "long", 1, 1), "B": Position("B", "long", 1, 1)}
        bot.enter([(1, "GBP_USD", self.signal())], full, utc(2026, 9, 29, 12, 30))
        self.assertEqual(broker.opened, [])

    def test_short_skipped_where_impossible(self):
        s = settings()
        broker = FakeBroker(s, {}, Quote(1.3, 1.3001, True, unit_value=1.0), can_short=False)
        bot = self.bot(broker, s)
        bot.markets = broker.resolve(["GBP_USD"])
        bot.enter([(1, "GBP_USD", self.signal(stop=1.3050, direction="short"))], {}, utc(2026, 9, 29, 12, 30))
        self.assertEqual(broker.opened, [])

    def test_dry_run_sends_nothing(self):
        s = settings(dry_run=True)
        broker = FakeBroker(s, {}, Quote(1.3000, 1.3001, True, unit_value=1.0))
        bot = self.bot(broker, s)
        bot.markets = broker.resolve(["GBP_USD"])
        bot.enter([(1, "GBP_USD", self.signal())], {}, utc(2026, 9, 29, 12, 30))
        self.assertEqual(broker.opened, [])

    def test_bot_side_stop(self):
        position = Position("SPY", "long", 1, 100)
        notes = {"stop": 99.0, "take_profit": 102.0}
        stop = runner.StrategyBot.bot_side_stop
        self.assertIsNone(stop(position, notes, Quote(100, 100.01, True)))
        self.assertIn("stop-loss", stop(position, notes, Quote(98.9, 99.0, True)))
        self.assertIn("take-profit", stop(position, notes, Quote(102.1, 102.2, True)))
        self.assertIsNone(stop(position, notes, Quote(98.9, 99.0, False)))  # market shut

    def test_full_cycle_trades_a_breakout_and_flattens(self):
        """start() and cycle() end to end: no trade on the bar before startup,
        a breakout on the next bar, then the 20:00 London flat."""
        s = settings()
        start = utc(2026, 9, 28, 12, 0)
        history = flat_bars(start, int((utc(2026, 9, 29, 12, 0) - start) / 900), 1.3000, wiggle=0.0015)
        broker = FakeBroker(s, {"M15": history}, Quote(1.3025, 1.3026, True, unit_value=1.0))
        bot = self.bot(broker, s)
        with mock.patch("time.time", return_value=utc(2026, 9, 29, 12, 5)):
            bot.start()
        breakout = Bar(utc(2026, 9, 29, 12, 0), 1.3010, 1.3027, 1.3008, 1.3025, 100)
        broker._bars["M15"] = history + [breakout]
        for feed in bot.feeds["GBP_USD"].values():
            feed.next_check = 0
        with mock.patch("time.time", return_value=utc(2026, 9, 29, 12, 15, 20)):
            bot.cycle()
        self.assertEqual(len(broker.opened), 1, broker.opened)
        broker._positions = {"GBP_USD": Position("GBP_USD", "long", broker.opened[0][2], 1.3026,
                                                 opened_at=utc(2026, 9, 29, 12, 15))}
        with mock.patch("time.time", return_value=utc(2026, 9, 29, 19, 0, 5)):
            bot.cycle()
        self.assertEqual(broker.closed[0][0], "GBP_USD")
        self.assertIn("flat time", broker.closed[0][1])


    def test_slow_fill_keeps_its_notes(self):
        """A position that hasn't shown up yet (Alpaca fills a moment after
        accepting) keeps its notes for a few minutes, then they're dropped."""
        s = settings()
        broker = FakeBroker(s, {}, Quote(1.3, 1.3001, True, unit_value=1.0))
        bot = self.bot(broker, s)
        bot.markets = broker.resolve(["GBP_USD"])
        opened = utc(2026, 9, 29, 12, 30)
        bot.enter([(1, "GBP_USD", self.signal())], {}, opened)
        bot.reconcile({}, {}, opened + 30)
        self.assertIn("GBP_USD", bot.state.positions)
        bot.reconcile({}, {}, opened + runner.FILL_GRACE_SECONDS + 1)
        self.assertNotIn("GBP_USD", bot.state.positions)

    def test_leaves_other_positions_alone(self):
        """Someone else's position in the bot's market is never closed, and
        blocks the bot from trading that market while it's open."""
        s = settings()
        start = utc(2026, 9, 28, 12, 0)
        history = flat_bars(start, int((utc(2026, 9, 29, 12, 0) - start) / 900), 1.3000, wiggle=0.0015)
        other = Position("GBP_USD", "short", 19, 1.3257, opened_at=utc(2026, 9, 28, 10, 0), own=False)
        broker = FakeBroker(s, {"M15": history}, Quote(1.3025, 1.3026, True, unit_value=1.0),
                            positions={"GBP_USD": other})
        bot = self.bot(broker, s)
        with mock.patch("time.time", return_value=utc(2026, 9, 29, 12, 5)):
            bot.start()
        broker._bars["M15"] = history + [Bar(utc(2026, 9, 29, 12, 0), 1.3010, 1.3027, 1.3008, 1.3025, 100)]
        for feed in bot.feeds["GBP_USD"].values():
            feed.next_check = 0
        for moment in (utc(2026, 9, 29, 12, 15, 20), utc(2026, 9, 29, 19, 0, 5)):
            with mock.patch("time.time", return_value=moment):
                bot.cycle()
        self.assertEqual(broker.opened, [])
        self.assertEqual(broker.closed, [])


    # -- closes asked for on the dashboard ------------------------------------------
    def dashboard_bot(self, position, dry_run=False, refuse=None):
        s = settings(dry_run=dry_run)
        broker = FakeBroker(s, {}, Quote(1.3, 1.3001, True, unit_value=1.0), positions={"GBP_USD": position},
                            refuse=refuse)
        bot = self.bot(broker, s)
        bot.markets = broker.resolve(["GBP_USD"])
        bot.state.positions["GBP_USD"] = {"direction": position.direction, "opened_at": 1.0}
        return bot, broker

    def test_dashboard_closes_the_bots_own_position(self):
        bot, broker = self.dashboard_bot(Position("GBP_USD", "long", 250, 1.3, raw=["T1"], own=True))
        answer = bot.close_from_dashboard("GBP_USD", "T1", "long", None)
        self.assertEqual(broker.closed, [("GBP_USD", "closed from the dashboard")])
        self.assertIn("Closed the GBP_USD long position (250)", answer)
        self.assertNotIn("GBP_USD", bot.state.positions)

    def test_dashboard_closes_part_rounded_down_to_the_step(self):
        bot, broker = self.dashboard_bot(Position("GBP_USD", "long", 250, 1.3, own=True))
        answer = bot.close_from_dashboard("GBP_USD", None, "long", 100.7)
        self.assertEqual(broker.closed, [("GBP_USD", "closed from the dashboard", 100.0)])
        self.assertIn("Closed 100 of the GBP_USD long position (250)", answer)
        self.assertIn("GBP_USD", bot.state.positions, "still open, so the notes stay")
        broker.closed.clear()
        bot.close_from_dashboard("GBP_USD", None, "long", 250)
        self.assertEqual(broker.closed, [("GBP_USD", "closed from the dashboard")], "all of it is a plain close")

    def test_dashboard_close_is_refused_when_the_position_isnt_the_one_shown(self):
        from dashboard_reporter import CommandError
        cases = [
            (Position("GBP_USD", "short", 19, 1.3257, own=False), "GBP_USD", None, "short", "no open GBP_USD"),
            (Position("GBP_USD", "short", 250, 1.3, own=True), "GBP_USD", None, "long", "is short now"),
            (Position("GBP_USD", "long", 250, 1.3, raw=["T2"], own=True), "GBP_USD", "T1", "long", "different trade"),
            (Position("GBP_USD", "long", 250, 1.3, own=True), "EUR_USD", None, "long", "isn't one of this bot's"),
            (Position("GBP_USD", "long", 250, 1.3, own=True), "GBP_USD", None, "long", "smallest step"),
        ]
        for position, symbol, ref, direction, why in cases:
            with self.subTest(why):
                bot, broker = self.dashboard_bot(position)
                size = 0.4 if why == "smallest step" else None
                with self.assertRaisesRegex(CommandError, why):
                    bot.close_from_dashboard(symbol, ref, direction, size)
                self.assertEqual(broker.closed, [])

    def test_dashboard_close_on_a_dry_run_or_refused_by_the_broker(self):
        from dashboard_reporter import CommandError
        bot, broker = self.dashboard_bot(Position("GBP_USD", "long", 250, 1.3, own=True), dry_run=True)
        with self.assertRaisesRegex(CommandError, "dry run"):
            bot.close_from_dashboard("GBP_USD", None, "long", None)
        self.assertEqual(broker.closed, [])

        bot, broker = self.dashboard_bot(Position("GBP_USD", "long", 250, 1.3, own=True), refuse="MARKET_HALTED")
        with self.assertRaisesRegex(CommandError, "Fake didn't close GBP_USD: MARKET_HALTED"):
            bot.close_from_dashboard("GBP_USD", None, "long", None)
        self.assertIn("GBP_USD", bot.state.positions)

    def test_dashboard_names_ig_markets_by_name(self):
        bot, broker = self.dashboard_bot(Position("CS.D.GBPUSD.TODAY.IP", "long", 1, 1.3, own=True))
        bot.markets = {"CS.D.GBPUSD.TODAY.IP": Market("CS.D.GBPUSD.TODAY.IP", "GBP/USD", "GBP/USD", 1.0, 1.0)}
        broker._positions = {"CS.D.GBPUSD.TODAY.IP": broker._positions["GBP_USD"]}
        bot.close_from_dashboard("GBP/USD", None, "long", None)
        self.assertEqual(broker.closed, [("CS.D.GBPUSD.TODAY.IP", "closed from the dashboard")])


class FeedTests(unittest.TestCase):
    def test_incremental_fetches(self):
        calls = []

        class Source:
            metered_history = False

            def bars(self, market, timeframe, count):
                calls.append(count)
                return flat_bars(0, 200, 1.0)[-count:]

        feed = runner.Feed(Source(), None, "M15", 150)
        feed.refresh(200 * 900 + 30)
        self.assertEqual(calls, [150])
        self.assertEqual(len(feed.bars), 150)
        feed.refresh(200 * 900 + 60)       # the next bar isn't due yet
        self.assertEqual(calls, [150])
        feed.refresh(201 * 900 + 30)       # due: asks for just the newest few
        self.assertLessEqual(calls[-1], 5)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
