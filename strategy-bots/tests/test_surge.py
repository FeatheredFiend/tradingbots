"""
Tests for the opening surge scanner and its followers (engine/surge.py,
engine/surge_scanner.py): the surge rule on made-up polls, the signals file
between them, the scanner against fake Alpaca data and the followers
against a fake broker. No broker or data service is contacted.

    python -m unittest discover -s strategy-bots/tests
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

os.environ["DASHBOARD_URL"] = ""  # never report test runs to the real dashboard
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from engine import runner, surge  # noqa: E402
from engine.brokers.base import Account, Broker, Market, Position, Quote  # noqa: E402
from engine.settings import (BotSettings, SettingsError, bot_settings, strategy_params,  # noqa: E402
                             surge_scanner_params)
from engine.strategies import make_strategy  # noqa: E402
from engine.surge import Detector, SignalReader, write_signal  # noqa: E402
from engine.surge_scanner import SurgeScanner, utc_seconds  # noqa: E402

# Tuesday 29 Sep 2026, 09:30 New York (13:30 UTC): the open.
OPEN = datetime(2026, 9, 29, 13, 30, tzinfo=timezone.utc).timestamp()


def env(**values):
    return mock.patch.dict(os.environ, {k: str(v) for k, v in values.items()})


class TempState(unittest.TestCase):
    """Points the bots' state folder (and so the signals file) at a temporary one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(runner, "STATE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)


# ---------------------------------------------------------------------------
class DetectorTests(unittest.TestCase):
    def polls(self, prices, confirm=3, jump=0.2, start=OPEN + 1, every=5.0, fresh=True, ticker="NVDA"):
        """One poll every `every` seconds at each price, each a new trade half a
        second before it (or all the same old trade); returns the surges."""
        detector = Detector(confirm, jump, 5)
        found = []
        for i, price in enumerate(prices):
            now = start + i * every
            surge_found = detector.add(ticker, now, now - 0.5 if fresh else start - 0.5, price, not_before=OPEN)
            if surge_found:
                found.append(surge_found)
        return found

    def test_three_jumps_up_is_a_long_surge(self):
        found = self.polls([100, 100.25, 100.5, 100.8])
        self.assertEqual(len(found), 1)
        s = found[0]
        self.assertEqual((s.direction, s.start_price, s.price), ("long", 100, 100.8))
        self.assertAlmostEqual(s.move_percent, 0.8)
        self.assertEqual(len(s.steps), 3)
        self.assertAlmostEqual(s.at - s.started, 15)

    def test_three_jumps_down_is_a_short_surge(self):
        found = self.polls([100, 99.7, 99.4, 99.1])
        self.assertEqual([s.direction for s in found], ["short"])

    def test_what_isnt_a_surge(self):
        self.assertEqual(self.polls([100, 100.25, 100.35, 100.6]), [])     # the middle step is only 0.1%
        self.assertEqual(self.polls([100, 100.3, 100.0, 100.3]), [])       # up, down, up
        self.assertEqual(self.polls([100, 100.25, 100.5]), [])             # only two polls of jumps
        self.assertEqual(self.polls([100, 100.25, 100.5, 100.8], fresh=False), [])  # no new trades

    def test_two_polls_when_set_to_two(self):
        self.assertEqual(len(self.polls([100, 100.25, 100.5], confirm=2)), 1)

    def test_only_trades_after_the_open_count(self):
        # jumps from 15s before the open: the confirming trades are pre-open
        self.assertEqual(self.polls([100, 100.25, 100.5, 100.8], start=OPEN - 16), [])
        # the price just before the open is the base of a surge right after it
        self.assertEqual(len(self.polls([100, 100.25, 100.5, 100.8], start=OPEN - 4)), 1)

    def test_a_gap_starts_again(self):
        detector = Detector(3, 0.2, 5)
        for i, price in enumerate([100, 100.25, 100.5]):
            detector.add("NVDA", OPEN + 1 + 5 * i, OPEN + 0.5 + 5 * i, price, OPEN)
        self.assertIsNone(detector.add("NVDA", OPEN + 60, OPEN + 59, 100.8, OPEN))   # a missed 40s

    def test_one_surge_then_fresh_polls(self):
        found = self.polls([100, 100.25, 100.5, 100.8, 101.1])
        self.assertEqual(len(found), 1)   # the next surge needs three new jumps of its own


# ---------------------------------------------------------------------------
class SignalFileTests(TempState):
    def signal(self, ticker="NVDA", at=OPEN + 16, **extra):
        return {"time": at, "ticker": ticker, "direction": "long", "move_percent": 0.8, "polls": 3, "seconds": 15,
                **extra}

    def test_reads_whats_new_once(self):
        reader = SignalReader(mock.Mock())
        self.assertEqual(reader.read(OPEN), [])                       # no file yet
        write_signal(self.signal("NVDA"))
        write_signal(self.signal("AMD"))
        self.assertEqual([s["ticker"] for s in reader.read(OPEN + 17)], ["NVDA", "AMD"])
        self.assertEqual(reader.read(OPEN + 18), [])
        write_signal(self.signal("TSLA"))
        self.assertEqual([s["ticker"] for s in reader.read(OPEN + 19)], ["TSLA"])

    def test_waits_for_a_whole_line_and_skips_bad_ones(self):
        log = mock.Mock()
        reader = SignalReader(log)
        path = surge.signals_path("2026-09-29")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json\n" + json.dumps(self.signal("NVDA"))[:20])
        self.assertEqual(reader.read(OPEN + 17), [])
        log.warning.assert_called_once()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(self.signal("NVDA"))[20:] + "\n")
        self.assertEqual([s["ticker"] for s in reader.read(OPEN + 18)], ["NVDA"])

    def test_a_new_day_is_a_new_file(self):
        reader = SignalReader(mock.Mock())
        write_signal(self.signal("NVDA"))
        self.assertEqual(len(reader.read(OPEN + 17)), 1)
        tomorrow = OPEN + 86400
        write_signal(self.signal("AMD", at=tomorrow + 16))
        self.assertEqual([s["ticker"] for s in reader.read(tomorrow + 17)], ["AMD"])


# ---------------------------------------------------------------------------
class SettingsTests(unittest.TestCase):
    def test_follower_settings(self):
        s = bot_settings("capital", "surge-follower")
        self.assertEqual((s.markets, s.symbol_format, s.budget, s.max_positions), ([], "{}", 200, 3))
        self.assertEqual(s.name, "Capital.com opening surge follower")
        self.assertEqual(s.slug, "capital-surge-follower")
        self.assertEqual(bot_settings("pepperstone", "surge-follower").symbol_format, "{}.US")
        with env(PEPPERSTONE_SURGE_SYMBOL="{}.NAS"):
            self.assertEqual(bot_settings("pepperstone", "surge-follower").symbol_format, "{}.NAS")
        p = strategy_params("surge-follower")
        self.assertEqual((p["reward_risk"], p["max_hold_minutes"], p["max_spread_percent"]), (2.0, 15, 25))

    def test_no_follower_without_shares(self):
        for broker in ("ig", "oanda"):
            with self.assertRaises(SettingsError):
                bot_settings(broker, "surge-follower")
        with env(CAPITAL_SURGE_SYMBOL="AAPL"), self.assertRaises(SettingsError):
            bot_settings("capital", "surge-follower")

    def test_scanner_settings(self):
        p = surge_scanner_params()
        self.assertEqual((p["poll_seconds"], p["confirm_polls"], p["jump_percent"], p["feed"]), (5, 3, 0.2, "iex"))
        with env(SURGE_FEED="bloomberg"), self.assertRaises(SettingsError):
            surge_scanner_params()
        with env(SURGE_CONFIRM_POLLS="2.5"), self.assertRaises(SettingsError):
            surge_scanner_params()

    def test_strategy(self):
        strategy = make_strategy("surge-follower", strategy_params("surge-follower"))
        self.assertTrue(strategy.uses_signals and strategy.checks_own_levels)
        self.assertFalse(strategy.uses_prices)


# ---------------------------------------------------------------------------
class ShareBroker(Broker):
    """A broker offering a few US shares, whose prices the test sets."""
    key, name = "fake", "Fake"

    def __init__(self, settings, offered=("NVDA.US", "AMD.US"), **attributes):
        super().__init__(settings, mock.Mock(), mock.Mock())
        self.offered = set(offered)
        self.prices = {"NVDA.US": 180.0, "AMD.US": 150.0}
        self.spreads = {}            # symbol -> spread (default 0.02)
        self.no_price = set()
        self.stale = set()           # yesterday's last price, as MT5 gives before Pepperstone's shares open
        self.positions_open, self.opened, self.closed, self.resolved = {}, [], [], []
        for k, v in attributes.items():
            setattr(self, k, v)

    def connect(self):
        return Account("1", "USD", 10000, 10000, "fake account")

    def resolve(self, names):
        self.resolved.append(list(names))
        return {n: Market(n, n, n, 1.0, 1.0, digits=2) for n in names if n in self.offered}

    def quotes(self, markets):
        quotes = {}
        for m in markets:
            if m.symbol in self.no_price:
                continue
            price, half = self.prices[m.symbol], self.spreads.get(m.symbol, 0.02) / 2
            stale = m.symbol in self.stale
            quotes[m.symbol] = Quote(price - half, price + half, not stale, unit_value=price,
                                     why_not="no price for 1052 min - market closed?" if stale else "")
        return quotes

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


SIGNAL_AT = OPEN + 16


class FollowerTests(TempState):
    def settings(self):
        return BotSettings(broker="fake", strategy="surge-follower", slug="test-surge", name="Test surge follower",
                           markets=[], budget=1000.0, max_positions=3, account_id="", dry_run=False,
                           params=strategy_params("surge-follower"), symbol_format="{}.US")

    def bot(self, broker=None, started=OPEN - 600):
        s = self.settings()
        broker = broker or ShareBroker(s)
        bot = surge.FollowerBot(s, make_strategy("surge-follower", s.params), broker, mock.Mock(), mock.Mock())
        with mock.patch("time.time", return_value=started):
            bot.start()
        return bot, broker

    @staticmethod
    def signal(ticker="NVDA", direction="long", move=0.8, at=SIGNAL_AT):
        write_signal({"time": at, "ticker": ticker, "direction": direction, "move_percent": move, "polls": 3,
                      "seconds": 15})

    @staticmethod
    def cycle(bot, now):
        with mock.patch("time.time", return_value=now):
            bot.cycle()

    def logged(self, bot, text):
        return any(text in str(call) for call in bot.log.info.call_args_list)

    def test_trades_a_fresh_signal(self):
        bot, broker = self.bot()
        self.signal()
        self.cycle(bot, SIGNAL_AT + 1)
        self.assertEqual(broker.resolved, [["NVDA.US"]])
        self.assertEqual(len(broker.opened), 1, bot.log.info.call_args_list)
        symbol, direction, size, stop, take_profit = broker.opened[0]
        ask = 180.01
        self.assertEqual((symbol, direction), ("NVDA.US", "long"))
        self.assertAlmostEqual(stop, ask * (1 - 0.008))                  # back where the surge started
        self.assertAlmostEqual(take_profit, ask + 2 * (ask - stop))      # 2R
        self.assertEqual(size, 3)       # 0.5% of 1,000 at risk over a 1.44 stop, in whole shares
        self.assertEqual(bot.state.positions["NVDA.US"]["requested"], "NVDA.US")

    def test_shorts_a_fall(self):
        bot, broker = self.bot()
        self.signal(direction="short")
        self.cycle(bot, SIGNAL_AT + 1)
        _, direction, _, stop, _ = broker.opened[0]
        self.assertEqual(direction, "short")
        self.assertAlmostEqual(stop, 179.99 * 1.008)

    def test_what_it_skips(self):
        bot, broker = self.bot(ShareBroker(self.settings(), can_short=False))
        self.signal("NVDA", "short")                        # buys only
        self.signal("MSFT")                                 # not offered
        self.signal("AMD", at=SIGNAL_AT - 30)               # 31s old when read
        self.cycle(bot, SIGNAL_AT + 1)
        self.assertEqual(broker.opened, [])
        self.assertTrue(self.logged(bot, "can't sell short"))
        self.assertTrue(self.logged(bot, "doesn't offer it as MSFT.US"))
        self.assertTrue(self.logged(bot, "old when read"))
        self.signal("MSFT", at=SIGNAL_AT + 2)
        self.cycle(bot, SIGNAL_AT + 3)
        self.assertEqual(broker.resolved, [["MSFT.US"]])    # a share it doesn't have is looked up once a day

    def test_signals_from_before_it_started_are_ignored_quietly(self):
        self.signal(at=OPEN - 100)
        bot, broker = self.bot(started=OPEN - 50)
        self.cycle(bot, OPEN + 1)
        self.assertEqual(broker.opened, [])
        self.assertFalse(self.logged(bot, "old when read"))

    def test_waits_for_a_first_price(self):
        broker = ShareBroker(self.settings())
        broker.no_price.add("NVDA.US")                      # e.g. MT5 has no tick for it yet
        bot, broker = self.bot(broker)
        self.signal()
        self.cycle(bot, SIGNAL_AT + 1)
        self.assertEqual(broker.opened, [])
        broker.no_price.clear()
        self.cycle(bot, SIGNAL_AT + 3)
        self.assertEqual(len(broker.opened), 1)
        broker.no_price.add("AMD.US")
        late = OPEN + 200                                   # past the opening wait for a first price
        self.signal("AMD", at=late)
        for t in range(1, 25):
            self.cycle(bot, late + t)
        self.assertEqual(len(broker.opened), 1)             # no price within 20s: dropped
        self.assertTrue(self.logged(bot, "dropped - no price within 20s"))

    def test_waits_for_the_brokers_first_price_after_the_open(self):
        broker = ShareBroker(self.settings())
        broker.stale.add("NVDA.US")                         # Pepperstone: no quotes until 09:31 New York
        bot, broker = self.bot(broker)
        self.signal(at=OPEN + 10)
        for t in range(11, 60):
            self.cycle(bot, OPEN + t)
        self.assertEqual(broker.opened, [])
        self.assertEqual(len([c for c in bot.log.info.call_args_list if "waits for its first price" in str(c)]), 1)
        broker.stale.clear()
        broker.spreads["NVDA.US"] = 0.5                     # 35% of the 1.44 stop at first...
        for t in range(60, 62):
            self.cycle(bot, OPEN + t)
        self.assertTrue(self.logged(bot, "first price 179.75 / 180.25"))
        self.assertEqual(broker.opened, [])
        broker.spreads["NVDA.US"] = 0.1                     # ...then it narrows, within 20s of the first price
        for t in range(62, 80):
            self.cycle(bot, OPEN + t)
        self.assertEqual(len(broker.opened), 1)
        self.assertAlmostEqual(broker.opened[0][3], 180.05 * (1 - 0.008))   # the stop, from its own entry

    def test_gives_up_on_a_first_price_after_the_wait(self):
        broker = ShareBroker(self.settings())
        broker.stale.add("NVDA.US")
        bot, broker = self.bot(broker)
        self.signal(at=OPEN + 10)
        for t in range(11, 95):
            self.cycle(bot, OPEN + t)
        self.assertTrue(self.logged(bot, "still no price 90s after the open"))
        broker.stale.clear()
        self.cycle(bot, OPEN + 96)
        self.assertEqual(broker.opened, [])

        with env(SURGE_FIRST_PRICE_WAIT="0"):               # switched off: 20s, as for any signal
            s = self.settings()
            s.params = strategy_params("surge-follower")
        broker = ShareBroker(s)
        broker.stale.add("NVDA.US")
        bot = surge.FollowerBot(s, make_strategy("surge-follower", s.params), broker, mock.Mock(), mock.Mock())
        with mock.patch("time.time", return_value=OPEN - 600):
            bot.start()                                     # reads the same signal from the file
        for t in range(11, 35):
            self.cycle(bot, OPEN + t)
        self.assertTrue(self.logged(bot, "dropped - no price for 1052 min - market closed? within 20s"))

    def test_tries_a_wide_spread_again_for_20s(self):
        broker = ShareBroker(self.settings())
        broker.spreads["NVDA.US"] = 0.5                     # 35% of the 1.44 stop - max 25%
        bot, broker = self.bot(broker)
        self.signal()
        for t in range(1, 25):
            self.cycle(bot, SIGNAL_AT + t)
        self.assertEqual(broker.opened, [])
        info = [str(c) for c in bot.log.info.call_args_list]
        self.assertEqual(len([c for c in info if "trying again" in c]), 1)
        self.assertEqual(len([c for c in info if "still 20s on" in c]), 1)

        broker = ShareBroker(self.settings())
        broker.spreads["NVDA.US"] = 0.5
        bot, broker = self.bot(broker)
        self.signal("NVDA", at=SIGNAL_AT + 100)
        self.cycle(bot, SIGNAL_AT + 101)
        broker.spreads["NVDA.US"] = 0.2                     # 14%: fine
        self.cycle(bot, SIGNAL_AT + 103)
        self.assertEqual(len(broker.opened), 1)

    def test_leaves_other_positions_alone(self):
        broker = ShareBroker(self.settings())
        broker.positions_open["NVDA.US"] = Position("NVDA.US", "long", 5, 170.0, own=False)
        bot, broker = self.bot(broker)
        self.signal()
        self.cycle(bot, SIGNAL_AT + 1)
        self.assertEqual(broker.opened, [])
        self.assertTrue(self.logged(bot, "someone else's position"))

    def test_exits(self):
        bot, broker = self.bot()
        self.signal()
        self.cycle(bot, SIGNAL_AT + 1)
        stop = broker.opened[0][3]
        broker.prices["NVDA.US"] = stop - 0.5               # through the stop: the bot closes it itself
        self.cycle(bot, SIGNAL_AT + 7)
        self.assertEqual(len(broker.closed), 1)
        self.assertIn("stop-loss", broker.closed[0][1])

        bot, broker = self.bot()
        self.signal("AMD", at=SIGNAL_AT + 100)
        self.cycle(bot, SIGNAL_AT + 101)
        self.cycle(bot, SIGNAL_AT + 101 + 14 * 60)
        self.assertEqual(broker.closed, [])
        self.cycle(bot, SIGNAL_AT + 101 + 15 * 60)
        self.assertIn("time stop", broker.closed[0][1])

    def test_one_trade_per_share_a_day(self):
        bot, broker = self.bot()
        self.signal()
        self.cycle(bot, SIGNAL_AT + 1)
        broker.positions_open.clear()                       # closed at the broker
        self.signal(at=SIGNAL_AT + 200)
        self.cycle(bot, SIGNAL_AT + 201)
        self.assertEqual(len(broker.opened), 1)
        self.assertTrue(any("already traded" in str(c) for c in bot.log.info.call_args_list))

    def test_picks_up_its_positions_after_a_restart(self):
        bot, broker = self.bot()
        self.signal()
        self.cycle(bot, SIGNAL_AT + 1)
        restarted, _ = self.bot(broker, started=SIGNAL_AT + 60)
        self.assertIn("NVDA.US", restarted.markets)
        self.cycle(restarted, SIGNAL_AT + 1 + 15 * 60)
        self.assertIn("time stop", broker.closed[0][1])

    def test_never_held_overnight(self):
        with env(SURGE_MAX_HOLD_MINUTES="360"):
            strategy = make_strategy("surge-follower", strategy_params("surge-follower"))
        position = Position("NVDA.US", "long", 1, 180.0)
        market = Market("NVDA.US", "NVDA", "NVDA.US", 1, 1)
        opened = {"opened_at": OPEN + 6 * 3600}             # 15:30 New York
        self.assertIsNone(strategy.exit_on_time(market, position, opened, OPEN + 6 * 3600 + 10 * 60))
        self.assertIn("US close", strategy.exit_on_time(market, position, opened, OPEN + 6 * 3600 + 15 * 60))


# ---------------------------------------------------------------------------
class FakeData:
    """Alpaca's calendar, share list, daily bars and latest trades, made up."""

    def __init__(self, sessions=(("2026-09-29", "09:30", "16:00"), ("2026-09-30", "09:30", "16:00"))):
        self.sessions = sessions
        self.trades = {}
        self.bar_calls = 0

    def calendar(self, first, last):
        return [{"date": d, "open": o, "close": c} for d, o, c in self.sessions]

    def shares(self):
        return ["AMD", "NVDA", "PENNY", "THIN"]

    def daily_bars(self, symbols, first, end, feed):
        self.bar_calls += 1
        self.bars_end = end
        return {"NVDA": [(180.0, 2e8)] * 5, "AMD": [(150.0, 5e7)] * 5,
                "PENNY": [(2.0, 1e9)] * 5, "THIN": [(50.0, 1e5)] * 5}   # under $5; $5M a day

    def latest_trades(self, symbols):
        return {s: self.trades[s] for s in symbols if s in self.trades}


class ScannerTests(TempState):
    def scanner(self, data=None, **settings):
        with env(**{f"SURGE_{k.upper()}": v for k, v in settings.items()}):
            params = surge_scanner_params()
        scanner = SurgeScanner(params, data or FakeData(), mock.Mock(), mock.Mock())
        with mock.patch("time.time", return_value=OPEN - 3600):
            scanner.start()
        return scanner

    def test_timestamps(self):
        self.assertAlmostEqual(utc_seconds("2026-09-29T13:30:01.123456789Z"), OPEN + 1.123456789, places=6)
        self.assertEqual(utc_seconds("2026-09-29T09:30:00-04:00"), OPEN)
        self.assertIsNone(utc_seconds("yesterday"))

    def test_picks_liquid_shares_most_traded_first(self):
        data = FakeData()
        scanner = self.scanner(data)
        with mock.patch("time.time", return_value=OPEN - 10 * 3600):                  # 23:30 New York the night before
            self.assertEqual(scanner.pick_shares(date(2026, 9, 29)), ["NVDA", "AMD"])
        self.assertEqual(data.bars_end, OPEN - 10 * 3600 - 20 * 60)   # SIP history must be 15+ minutes old
        self.assertEqual(scanner.pick_shares(date(2026, 9, 29)), ["NVDA", "AMD"])   # from the day's cache
        self.assertEqual(data.bar_calls, 1)
        self.assertEqual(self.scanner(FakeData(), max_shares=1).pick_shares(date(2026, 9, 29)), ["NVDA"])

    def test_watches_the_open_and_sends_surges(self):
        data = FakeData()
        scanner = self.scanner(data)
        self.assertEqual(scanner.session[0], date(2026, 9, 29))
        wait = scanner.cycle(OPEN - 3000)
        self.assertEqual(wait, 60)                          # idle until the open's nearer
        self.assertIsNone(scanner.shares)
        scanner.cycle(OPEN - 600)                           # picks the day's shares ahead of the open
        self.assertEqual(scanner.shares, ["NVDA", "AMD"])

        prices = [180.0, 180.5, 181.0, 181.6, 182.2]
        for i, price in enumerate(prices):
            now = OPEN - 4 + 5 * i
            data.trades = {"NVDA": (now - 0.5, price), "AMD": (now - 0.5, 150.0)}
            scanner.cycle(now)
        with open(surge.signals_path("2026-09-29"), encoding="utf-8") as f:
            sent = [json.loads(line) for line in f]
        self.assertEqual(len(sent), 1)                      # once a day per share, however long it runs
        self.assertEqual((sent[0]["ticker"], sent[0]["direction"], sent[0]["polls"]), ("NVDA", "long", 3))
        self.assertAlmostEqual(sent[0]["move_percent"], (181.6 / 180 - 1) * 100, places=3)
        with open(surge.heartbeat_path(), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["watching"])

        scanner.cycle(OPEN + 15 * 60)                       # done: on to the next session
        self.assertEqual(scanner.session[0], date(2026, 9, 30))
        self.assertTrue(any("1 signal(s) sent" in str(c) for c in scanner.log.info.call_args_list))

    def test_an_early_close_ends_the_watch(self):
        data = FakeData(sessions=(("2026-11-27", "09:30", "13:00"), ("2026-11-30", "09:30", "16:00")))
        scanner = SurgeScanner(surge_scanner_params(), data, mock.Mock(), mock.Mock())
        with env(SURGE_WATCH_MINUTES="390"):
            scanner.p = surge_scanner_params()
        early_close = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc).timestamp()   # 13:00 New York
        scanner.next_session(early_close)
        self.assertEqual(scanner.session[0], date(2026, 11, 30))


if __name__ == "__main__":
    unittest.main()
