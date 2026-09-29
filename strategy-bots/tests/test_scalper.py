"""
Tests for the tick scalper (engine/scalper.py): what it makes of a stream
of made-up bid/ask reads, and the runner reading, trading and closing on
them against a fake broker. No broker is contacted.

    python -m unittest discover -s strategy-bots/tests
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

os.environ["DASHBOARD_URL"] = ""  # never report test runs to the real dashboard
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from engine import runner  # noqa: E402
from engine.brokers.base import Account, Broker, Market, Position, Quote  # noqa: E402
from engine.scalper import Scalper  # noqa: E402
from engine.settings import BotSettings, SettingsError, strategy_params  # noqa: E402
from engine.strategies import make_strategy  # noqa: E402

SPREAD = 0.0001
# Tuesday 29 Sep 2026, 13:00 London (UTC+1): inside the default 07:00-21:00 session.
NOON = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc).timestamp()


def env(**values):
    return mock.patch.dict(os.environ, {k: str(v) for k, v in values.items()})


def scalper(**settings) -> Scalper:
    with env(**{f"SCALPER_{k.upper()}": v for k, v in settings.items()}):
        return Scalper(strategy_params("scalper"))


class ScalperTests(unittest.TestCase):
    def setUp(self):
        self.market = Market("EUR_USD", "EUR/USD", "EUR_USD", 1, 1, digits=5)

    def feed(self, strategy, mids, start=NOON, every=2.0, spread=SPREAD, last_spread=None):
        """One read every `every` seconds at each of `mids`; returns the last read's time."""
        now = start
        for i, mid in enumerate(mids):
            now = start + i * every
            s = last_spread if last_spread is not None and i == len(mids) - 1 else spread
            strategy.on_price(self.market, Quote(mid - s / 2, mid + s / 2, True), now)
        return now

    def burst(self, *steps):
        """60s flat at 1.1000, then these mids."""
        return [1.1000] * 31 + [1.1000 + s * SPREAD for s in steps]

    def test_momentum_goes_with_a_burst(self):
        strategy = scalper()
        now = self.feed(strategy, self.burst(1, 2, 3, 4, 5, 6))
        a = strategy.assess_price(self.market, now)
        self.assertIsNotNone(a.signal, a.note)
        self.assertEqual(a.signal.direction, "long")
        ask = 1.1006 + SPREAD / 2
        self.assertAlmostEqual(a.signal.stop, ask - 3 * SPREAD)       # 3 usual spreads from the fill
        self.assertAlmostEqual(a.signal.reward_risk, 1.0)             # take-profit 3 spreads too
        self.assertIn("+6.0-spread", a.signal.why)

    def test_reversion_fades_it(self):
        strategy = scalper(mode="reversion")
        now = self.feed(strategy, self.burst(1, 2, 3, 4, 5, 6))
        a = strategy.assess_price(self.market, now)
        self.assertEqual(a.signal.direction, "short")
        bid = 1.1006 - SPREAD / 2
        self.assertAlmostEqual(a.signal.stop, bid + 3 * SPREAD)

    def test_small_or_fading_moves_are_left(self):
        strategy = scalper()
        now = self.feed(strategy, self.burst(1, 2, 3))                  # 3 spreads < the 4 trigger
        self.assertIsNone(strategy.assess_price(self.market, now).signal)
        strategy = scalper()
        now = self.feed(strategy, self.burst(2, 4, 6, 5))              # up 5, but off the high already
        a = strategy.assess_price(self.market, now)
        self.assertIsNone(a.signal)
        self.assertIn("fading", a.note)

    def test_wide_spread_waits(self):
        strategy = scalper()
        now = self.feed(strategy, self.burst(1, 2, 3, 4, 5, 6), last_spread=2 * SPREAD)
        a = strategy.assess_price(self.market, now)
        self.assertIsNone(a.signal)
        self.assertIn("spread over 1.5x", a.note)

    def test_needs_a_full_window_without_gaps(self):
        strategy = scalper()
        now = self.feed(strategy, [1.1000] * 10)                        # 18s of reads
        self.assertIn("collecting", strategy.assess_price(self.market, now).note)
        now = self.feed(strategy, self.burst(1, 2, 3, 4, 5, 6), start=now + 2)
        self.assertIsNotNone(strategy.assess_price(self.market, now).signal)
        # a 100s gap (the PC slept, the broker was slow) starts the window again
        strategy.on_price(self.market, Quote(1.1010, 1.1011, True), now + 100)
        self.assertIn("collecting", strategy.assess_price(self.market, now + 100).note)
        # and so does the market shutting
        strategy.on_price(self.market, Quote(1.1010, 1.1011, False), now + 102)
        self.assertEqual(strategy.assess_price(self.market, now + 102).note, "no price yet")

    def test_session(self):
        strategy = scalper()
        late = datetime(2026, 9, 29, 20, 30, tzinfo=timezone.utc).timestamp()   # 21:30 London
        now = self.feed(strategy, self.burst(1, 2, 3, 4, 5, 6), start=late)
        a = strategy.assess_price(self.market, now)
        self.assertIsNone(a.signal)
        self.assertIn("outside", a.note)

    def test_exits(self):
        strategy = scalper()
        position = Position("EUR_USD", "long", 1000, 1.1)
        notes = {"opened_at": NOON}
        self.assertIsNone(strategy.exit_on_time(self.market, position, notes, NOON + 299))
        self.assertIn("time stop", strategy.exit_on_time(self.market, position, notes, NOON + 300))
        end = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc).timestamp()     # 21:00 London
        self.assertIn("session over", strategy.exit_on_time(self.market, position, {"opened_at": end - 10}, end))

    def test_settings(self):
        with env(SCALPER_MODE="sideways"), self.assertRaises(SettingsError):
            strategy_params("scalper")
        with env(SCALPER_SESSION_START="21:00", SCALPER_SESSION_END="07:00"), self.assertRaises(SettingsError):
            strategy_params("scalper")
        self.assertEqual(strategy_params("scalper")["max_spread_percent"], 60)


# ---------------------------------------------------------------------------
class PriceBroker(Broker):
    """A broker whose price the test moves by hand."""
    key, name = "fake", "Fake"

    def __init__(self, settings, mid=1.1000, **attributes):
        super().__init__(settings, mock.Mock(), mock.Mock())
        self.mid, self.positions_open, self.opened, self.closed = mid, {}, [], []
        for k, v in attributes.items():
            setattr(self, k, v)

    def connect(self):
        return Account("1", "GBP", 1000, 1000, "fake account")

    def resolve(self, names):
        return {n: Market(n, n, n, 1.0, 1.0, digits=5) for n in names}

    def quotes(self, markets):
        return {m.symbol: Quote(self.mid - SPREAD / 2, self.mid + SPREAD / 2, True, unit_value=1.0) for m in markets}

    def positions(self, markets):
        return {s: p for s, p in self.positions_open.items() if s in markets}

    def open(self, market, direction, size, stop, take_profit, quote):
        self.opened.append((market.symbol, direction, size, stop, take_profit))
        entry = quote.ask if direction == "long" else quote.bid
        self.positions_open[market.symbol] = Position(market.symbol, direction, size, entry, own=True)
        return True

    def close(self, market, position, reason, size=None):
        self.closed.append((market.symbol, reason))
        self.positions_open.pop(market.symbol, None)
        return True


# 60s flat, then a 6-spread jump in one read - the runner acts on the first read past the trigger
JUMP = [1.1000] * 31 + [1.1006]


class ScalperRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(runner, "STATE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def bot(self, broker_attributes=None, **settings):
        with env(**{f"SCALPER_{k.upper()}": v for k, v in settings.items()}):
            s = BotSettings(broker="fake", strategy="scalper", slug="test-scalper", name="Test scalper",
                            markets=["EUR_USD"], budget=1000.0, max_positions=2, account_id="", dry_run=False,
                            params=strategy_params("scalper"))
        broker = PriceBroker(s, **(broker_attributes or {}))
        bot = runner.StrategyBot(s, make_strategy("scalper", s.params), broker, mock.Mock(), mock.Mock())
        with mock.patch("time.time", return_value=NOON):
            bot.start()
        return bot, broker

    def run_reads(self, bot, broker, mids, start):
        now = start
        for i, mid in enumerate(mids):
            now = start + 2 * i
            broker.mid = mid
            with mock.patch("time.time", return_value=now):
                bot.cycle()
        return now

    def test_reads_trades_and_takes_profit(self):
        bot, broker = self.bot()
        self.assertEqual(bot.feeds, {})
        self.assertEqual(bot.loop_seconds, 2)
        now = self.run_reads(bot, broker, JUMP, NOON)
        self.assertEqual(len(broker.opened), 1, broker.opened)
        symbol, direction, size, stop, take_profit = broker.opened[0]
        self.assertEqual(direction, "long")
        ask = 1.1006 + SPREAD / 2
        self.assertAlmostEqual(stop, ask - 3 * SPREAD)            # no broker minimum: its levels are the bot's
        self.assertAlmostEqual(take_profit, ask + 3 * SPREAD)
        # the bid reaching the take-profit closes it on the next read, whatever the broker does
        self.run_reads(bot, broker, [ask + 4 * SPREAD], now + 2)
        self.assertEqual(len(broker.closed), 1)
        self.assertIn("take-profit", broker.closed[0][1])

    def test_time_stop(self):
        bot, broker = self.bot()
        now = self.run_reads(bot, broker, JUMP, NOON)
        self.run_reads(bot, broker, [1.1006] * 2, now + 290)
        self.assertEqual(broker.closed, [])
        self.run_reads(bot, broker, [1.1006], now + 301)
        self.assertIn("time stop", broker.closed[0][1])

    def test_broker_minimum_stop_distance(self):
        bot, broker = self.bot({"min_stop_distance": lambda market, quote: 0.0005})
        self.run_reads(bot, broker, JUMP, NOON)
        _, _, _, stop, take_profit = broker.opened[0]
        ask = 1.1006 + SPREAD / 2
        self.assertAlmostEqual(stop, ask - 0.00055)               # pushed out to the minimum (+10%)
        self.assertAlmostEqual(take_profit, ask + 0.00055)
        notes = bot.state.positions["EUR_USD"]
        self.assertAlmostEqual(notes["stop"], ask - 3 * SPREAD)   # the bot still closes at its own levels
        self.assertAlmostEqual(notes["take_profit"], ask + 3 * SPREAD)

    def test_cooldown_and_buys_only(self):
        bot, broker = self.bot(cooldown_seconds=600)
        now = self.run_reads(bot, broker, JUMP, NOON)
        broker.positions_open.clear()                           # closed at the broker (say its stop)
        now = self.run_reads(bot, broker, [1.1006 + s * SPREAD for s in (1, 2, 3, 4, 5)], now + 2)
        self.assertEqual(len(broker.opened), 1)                 # still cooling down
        bot, broker = self.bot({"can_short": False})
        self.run_reads(bot, broker, [1.1000] * 31 + [1.1000 - 6 * SPREAD], NOON)
        self.assertEqual(broker.opened, [])                     # a fall is a short signal: not on a buys-only broker

    def test_read_pace_respects_the_broker(self):
        bot, _ = self.bot({"min_poll_seconds": 10})
        self.assertEqual(bot.loop_seconds, 10)


if __name__ == "__main__":
    unittest.main()
