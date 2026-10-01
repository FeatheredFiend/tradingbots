"""
Tests for the stagnancy timeout (shared/stagnancy.py): the rule on made-up
price series (flat, slow drift, spike then return, stale and closed
markets, bars), its settings, what it does about a stagnant trade (shadow
logging, closing, retries, part fills, a stop-loss getting there first)
and the runner using it against a fake broker - including that one close
frees exactly one position slot. No broker is contacted.

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
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from engine import runner  # noqa: E402
from engine.brokers.base import Account, Broker, Market, Position, Quote  # noqa: E402
from engine.indicators import Bar  # noqa: E402
from engine.settings import BotSettings, SettingsError, strategy_params  # noqa: E402
from engine.strategies import SessionBreakout, Signal, make_strategy  # noqa: E402

import stagnancy  # noqa: E402 - on the path runner put shared/ on
from stagnancy import Held, RuleBook, Watch, Window, check  # noqa: E402

NOWHERE = os.path.join(tempfile.gettempdir(), "no-such-stagnancy-file.json")
# Tuesday 29 Sep 2026, 13:00 London: inside the scalper's session, before the breakout's 20:00 flat.
NOON = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc).timestamp()
SPREAD = 0.0001


def clean_env(**values):
    """The process environment without anyone's STAGNANT settings, plus `values`."""
    env = {k: v for k, v in os.environ.items() if "STAGNANT" not in k}
    env.update({"STAGNANT_FILE": NOWHERE, **{k: str(v) for k, v in values.items()}})
    return mock.patch.dict(os.environ, env, clear=True)


def book(bot_type="scanner", broker="oanda", environ=None, path=NOWHERE, **kwargs):
    kwargs.setdefault("bar_seconds", 900)
    return RuleBook(bot_type, broker, environ=environ or {}, path=path, **kwargs)


def window(mids, start, every=30.0, gap=120.0, keep=4000.0) -> Window:
    w = Window(keep, gap)
    for i, mid in enumerate(mids):
        w.add(start + i * every, mid)
    return w


def logged(log, text) -> int:
    """How many lines the (mock) log got containing `text`, at any level."""
    calls = log.info.call_args_list + log.warning.call_args_list + log.error.call_args_list
    return sum(text in str(c.args[0]) for c in calls)


# ---------------------------------------------------------------------------
# THE RULE ON MADE-UP PRICES
# ---------------------------------------------------------------------------
class DetectorTests(unittest.TestCase):
    """The scanner's defaults: older than 3 bars (45 min), the last 45 min within
    0.15% of the price, P/L within +/-0.2R."""

    def setUp(self):
        self.rule = book().base
        self.now = NOON
        self.opened = NOON - 3600

    def run_check(self, w, pnl=-0.02, risk=0.40, now=None, opened=None, **kwargs):
        return check(self.rule, now or self.now, opened or self.opened, pnl, risk=risk, samples=w.samples,
                     bar_seconds=900, **kwargs)

    def reads(self, mids):
        """Reads every 30s ending at self.now."""
        return window(mids, self.now - 30 * (len(mids) - 1))

    def test_flat_is_stagnant(self):
        v = self.run_check(self.reads([1.1000 + (0.0002 if i % 2 else 0) for i in range(101)]))
        self.assertTrue(v.stagnant, v.why or v.skipped)
        self.assertAlmostEqual(v.range, 0.0002)
        self.assertAlmostEqual(v.range_limit, 0.0015 / 100 * 1.1000 * 100)   # 0.15% of the price
        self.assertAlmostEqual(v.pnl_limit, 0.08)                             # 0.2 x 0.40

    def test_slow_drift(self):
        # 1 pip a minute: 4.5 pips over the window - 0.41%... of nothing: 0.0045 > 0.00165
        drifting = self.run_check(self.reads([1.1000 + 0.00005 * i for i in range(101)]))
        self.assertFalse(drifting.stagnant)
        self.assertIn("moving", drifting.why)
        # a fifth of that stays inside 0.15% over the window, so it's still going nowhere
        creeping = self.run_check(self.reads([1.1000 + 0.00001 * i for i in range(101)]))
        self.assertTrue(creeping.stagnant, creeping.why)

    def test_spike_then_return_counts_only_inside_the_window(self):
        mids = [1.1000] * 101
        mids[-40] = 1.1030                                   # 20 minutes ago, inside the 45-minute window
        w = self.reads(mids)
        self.assertFalse(self.run_check(w).stagnant)
        for i in range(1, 61):                               # 30 flat minutes later the spike has left the window
            w.add(self.now + 30 * i, 1.1000)
        later = self.run_check(w, now=self.now + 1800)
        self.assertTrue(later.stagnant, later.why)           # a rolling window, not since the entry

    def test_stale_reads_are_skipped(self):
        w = window([1.1000] * 101, self.now - 3300)          # the last read was 5 minutes ago
        v = check(self.rule, self.now, self.opened, -0.02, risk=0.4, samples=w.samples, bar_seconds=900,
                  stale_after=60)
        self.assertFalse(v.stagnant)
        self.assertIn("no fresh price", v.skipped)

    def test_a_gap_or_a_closed_market_starts_the_window_again(self):
        w = self.reads([1.1000] * 101)
        w.add(self.now + 600, 1.1000)                        # 10 minutes without reads
        v = self.run_check(w, now=self.now + 600)
        self.assertFalse(v.stagnant)
        self.assertIn("watching", v.why)
        w = self.reads([1.1000] * 101)
        w.add(self.now + 30, 1.1000, tradeable=False)        # the market shut
        v = self.run_check(w, now=self.now + 30)
        self.assertIn("no prices", v.skipped)

    def test_young_trades_and_real_pnl_are_left(self):
        flat = self.reads([1.1000] * 101)
        self.assertIn("old", self.run_check(flat, opened=self.now - 600).why)          # 10 minutes old
        self.assertIn("outside", self.run_check(flat, pnl=0.30).why)                    # 0.75R in profit
        self.assertTrue(self.run_check(flat, pnl=-0.05).stagnant)
        self.assertIn("doesn't know", self.run_check(flat, risk=None).skipped)          # no stop known: R unusable
        self.assertIn("no P/L", self.run_check(flat, pnl=None).skipped)

    def test_money_limit(self):
        rule = book(environ={"SCANNER_STAGNANT_PNL": "1.50"}).base
        v = check(rule, self.now, self.opened, -1.20, samples=self.reads([1.1] * 101).samples, bar_seconds=900)
        self.assertTrue(v.stagnant)
        self.assertEqual(v.pnl_limit, 1.50)

    def test_bars_window_and_atr(self):
        """The bar strategies measure a "4bars" window on their own bars, against ATR."""
        rule = book("index-reversion", has_bars=True).base   # 4 bars, 1 ATR, 0.2R
        bars = [Bar(NOON - 900 * (i + 1), 1.1, 1.1003, 1.0997, 1.1, 100) for i in reversed(range(6))]
        v = check(rule, NOON + 20, NOON - 7200, -0.01, risk=1.0, bars=bars, bar_seconds=900, atr=0.0010)
        self.assertTrue(v.stagnant, v.why or v.skipped)
        self.assertAlmostEqual(v.range, 0.0006)
        self.assertAlmostEqual(v.range_limit, 0.0010)
        moving = bars[:-1] + [Bar(NOON - 900, 1.1, 1.1012, 1.0997, 1.1010, 100)]
        self.assertFalse(check(rule, NOON + 20, NOON - 7200, -0.01, risk=1.0, bars=moving, bar_seconds=900,
                               atr=0.0010).stagnant)
        stale = check(rule, NOON + 3600, NOON - 7200, -0.01, risk=1.0, bars=bars, bar_seconds=900, atr=0.0010)
        self.assertIn("no new bar", stale.skipped)
        gappy = bars[:4] + [Bar(b.time + 86400, b.open, b.high, b.low, b.close, 0) for b in bars[4:]]  # overnight
        self.assertIn("gap", check(rule, NOON + 86420, NOON - 7200, -0.01, risk=1.0, bars=gappy, bar_seconds=900,
                                   atr=0.0010).skipped)


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------
class SettingsTests(unittest.TestCase):
    def test_defaults_per_bot_type(self):
        scalper = book("scalper", bar_seconds=None).base
        self.assertEqual((scalper.mode, scalper.min_age.seconds(), scalper.window.seconds()), ("shadow", 90, 60))
        self.assertEqual((scalper.range.unit, scalper.range.value, scalper.pnl.unit), ("%", 0.01, "R"))
        trend = book("commodity-trend", has_bars=True).base
        self.assertEqual((trend.window.text, trend.window.seconds(900), trend.range.text), ("16bars", 14400, "1.5atr"))
        self.assertEqual(book("scanner", bar_seconds=300).base.window.seconds(300), 900)  # 3 bars of M5
        self.assertIsNone(RuleBook.for_bot("slow-trend", "oanda"))                       # portfolio bots: none

    def test_launcher_settings(self):
        b = book("scalper", bar_seconds=None, environ={"STAGNANT_MODE": "enforce", "SCALPER_STAGNANT_WINDOW": "2m",
                                                       "SCALPER_STAGNANT_PNL": "0.75", "SCALPER_STAGNANT_COOLDOWN": "10m"})
        self.assertEqual((b.base.mode, b.base.window.seconds(), b.base.pnl.unit, b.base.cooldown.seconds()),
                         ("enforce", 120, "money", 600))
        b = book("scalper", bar_seconds=None, environ={"STAGNANT_MODE": "enforce", "SCALPER_STAGNANT_MODE": "off"})
        self.assertEqual(b.base.mode, "off")
        self.assertFalse(b.active)

    def test_json_exceptions_per_broker_and_market(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "stagnancy.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"_comment": "notes are allowed",
                           "scalper": {"oanda": {"range": "0.02%", "symbols": {"SPX500_USD": {"window": "90s",
                                                                                              "mode": "enforce"}}},
                                       "ig": {"mode": "off"}}}, f)
            oanda = book("scalper", bar_seconds=None, path=path, environ={"SCALPER_STAGNANT_RANGE": "0.05%"})
            self.assertEqual(oanda.base.range.value, 0.02)            # the file beats the launcher
            self.assertEqual(oanda.rule("EUR_USD").window.seconds(), 60)
            spx = oanda.rule("spx500_usd")
            self.assertEqual((spx.window.seconds(), spx.mode, spx.range.value), (90, "enforce", 0.02))
            self.assertIs(oanda.rule("CS.D.X", aliases=("SPX500_USD",)), spx)  # by another of its names
            self.assertEqual(book("scalper", "ig", bar_seconds=None, path=path).base.mode, "off")
            self.assertEqual(book("scalper", "capital", bar_seconds=None, path=path).base.range.value, 0.01)
            self.assertIn("SPX500_USD", oanda.describe())

    def test_the_example_file_works_for_every_bot(self):
        example = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "stagnancy.example.json")
        for bot_type in stagnancy.TYPES:
            for broker in stagnancy.BROKERS:
                with self.subTest(f"{bot_type} on {broker}"):
                    bars = bot_type not in ("scalper", "surge-follower")
                    b = RuleBook(bot_type, broker, bar_seconds=900 if bars else None, environ={}, path=example,
                                 has_bars=bars and bot_type not in ("ema", "scanner"))
                    self.assertIn(b.base.mode, stagnancy.MODES)
        oanda_scanner = RuleBook("scanner", "oanda", bar_seconds=900, environ={}, path=example).base
        self.assertEqual((oanda_scanner.mode, oanda_scanner.cooldown.seconds()), ("enforce", 900))

    def test_the_launcher_shows_these_defaults(self):
        import ast
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "launcher", "tradingbots_launcher.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        shown = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                     and getattr(node.targets[0], "id", "") == "STAGNANCY_DEFAULTS")
        expected = {prefix: (d["min_age"], d["window"], d["range"], d["pnl"])
                    for prefix, _, d in stagnancy.TYPES.values()}
        self.assertEqual(shown, expected)

    def test_bad_settings_are_refused_with_their_name(self):
        cases = [
            ({"SCANNER_STAGNANT_WINDOW": "45"}, {}, "SCANNER_STAGNANT_WINDOW"),
            ({"SCANNER_STAGNANT_RANGE": "0.25atr"}, {}, "ATR needs the bot's own bars"),
            ({"SCANNER_STAGNANT_PNL": "-1"}, {}, "negative"),
            ({"STAGNANT_MODE": "sometimes"}, {}, "off, shadow or enforce"),
            ({}, {"scanner": {"oanda": {"windw": "1h"}}}, "unknown setting"),
            ({}, {"scanner": {"oandaa": {}}}, "unknown broker"),
            ({}, {"scalpers": {}}, "unknown bot type"),
            ({}, {"scanner": {"oanda": {"symbols": {"EUR_USD": {"range": "lots"}}}}}, "symbols > EUR_USD > range"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            for environ, data, why in cases:
                with self.subTest(why):
                    path = os.path.join(folder, "stagnancy.json")
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(data, f)
                    with self.assertRaisesRegex(stagnancy.ConfigError, why):
                        book(environ=environ, path=path)
        with self.assertRaisesRegex(stagnancy.ConfigError, "no bars"):
            book("scalper", bar_seconds=None, environ={"SCALPER_STAGNANT_WINDOW": "4bars"})


# ---------------------------------------------------------------------------
# WHAT THE TIMEOUT DOES ABOUT A STAGNANT TRADE
# ---------------------------------------------------------------------------
class WatchTests(unittest.TestCase):
    """Scalper defaults (90s old, 60s within 0.01%, P/L within 0.25R) on reads every 2s."""

    def watch(self, mode="enforce", **kwargs):
        log = mock.Mock()
        w = Watch(book("scalper", bar_seconds=None, environ={"SCALPER_STAGNANT_MODE": mode, **kwargs}), "test-bot",
                  "Fake", log, currency="GBP", gap=30)
        return w, log

    def held(self, size=1000.0, pnl=-0.05):
        return Held(key="EUR_USD", symbol="EUR_USD", direction="long", size=size, entry=1.10005, opened_at=NOON,
                    pnl=pnl, risk=1.0, refs=("T1",))

    def flat(self, w, until, mid=1.1000, start=NOON):
        t = start
        while t <= until:
            w.observe("EUR_USD", t, mid - SPREAD / 2, mid + SPREAD / 2)
            t += 2

    def test_shadow_logs_each_stall_once_and_leaves_the_trade(self):
        w, log = self.watch("shadow")
        self.flat(w, NOON + 100)
        self.assertFalse(w.assess(self.held(), NOON + 100))
        self.assertFalse(w.assess(self.held(), NOON + 102))
        self.assertEqual(logged(log, "TIMEOUT_STAGNANT_SHADOW"), 1)
        self.assertEqual(logged(log, "left open (shadow mode)"), 1)
        moving = self.held(pnl=0.60)                          # it moves...
        self.assertFalse(w.assess(moving, NOON + 104))
        self.assertFalse(w.assess(self.held(), NOON + 106))   # ...and stalls again
        self.assertEqual(logged(log, "TIMEOUT_STAGNANT_SHADOW"), 2)
        fields = w.gone("EUR_USD", NOON + 200)
        self.assertEqual(fields["stagnancy"]["episodes"], 2)
        self.assertEqual(fields["stagnancy"]["at"], "2026-09-29T12:01:40Z")   # the first time
        self.assertNotIn("closeReason", fields)
        self.assertEqual(w.fields_for("T1")["stagnancy"]["pnlRule"], "0.25R")

    def test_enforce_closes_and_records_it(self):
        w, log = self.watch(SCALPER_STAGNANT_COOLDOWN="5m")
        self.flat(w, NOON + 100)
        self.assertFalse(w.assess(self.held(), NOON + 80))    # too young
        self.assertTrue(w.assess(self.held(), NOON + 100))
        self.assertTrue(w.closing("EUR_USD"))
        fields = w.closed(self.held(), NOON + 101, True, price=1.09994, pnl=-0.11, mid=1.1000)
        self.assertEqual(fields["closeReason"], "TIMEOUT_STAGNANT")
        self.assertAlmostEqual(fields["slippage"], -0.00006)  # sold 0.6 pips under the mid
        self.assertEqual((fields["exitMid"], fields["durationSeconds"]), (1.1, 101))
        self.assertEqual(fields["stagnancy"]["mode"], "enforce")
        self.assertFalse(w.closing("EUR_USD"))
        self.assertEqual(w.fields_for("T1")["closeReason"], "TIMEOUT_STAGNANT")
        line = next(str(c.args[0]) for c in log.info.call_args_list if str(c.args[0]).startswith("TIMEOUT_STAGNANT |"))
        for part in ("trade T1", "test-bot", "Fake", "EUR_USD long 1000", "entry 2026-09-29T12:00:00Z @ 1.10005",
                     "exit 2026-09-29T12:01:41Z @ 1.09994", "held 1m41s", "P/L -0.11 GBP net",
                     "slippage -0.00006 vs mid 1.1", "within +/-0.25 (0.25R)"):
            self.assertIn(part, line)
        self.assertEqual(w.cooling("EUR_USD", NOON + 200), NOON + 401)
        self.assertEqual(w.cooling("EUR_USD", NOON + 402), 0)

    def test_refused_closes_back_off_then_alert_once(self):
        w, log = self.watch()
        self.flat(w, NOON + 100)
        now, tries = NOON + 100, []
        while len(tries) < 7:
            if w.assess(self.held(), now):
                tries.append(now)
                w.closed(self.held(), now, False, problem="MARKET_HALTED")
            now += 1
        self.assertEqual([b - a for a, b in zip(tries, tries[1:])], [15, 30, 60, 120, 240, 300])
        self.assertEqual(len(log.error.call_args_list), 1)
        self.assertIn("ALERT", str(log.error.call_args_list[0].args[0]))

    def test_part_fill_closes_the_rest_at_once(self):
        w, log = self.watch()
        self.flat(w, NOON + 100)
        self.assertTrue(w.assess(self.held(), NOON + 100))
        w.closed(self.held(), NOON + 100, False, problem="TRADE_RETCODE_DONE_PARTIAL")
        self.assertTrue(w.assess(self.held(size=400.0), NOON + 102))   # 600 went: no waiting for the backoff
        self.assertEqual(logged(log, "partly closed"), 1)

    def test_market_closing_calls_it_off(self):
        w, log = self.watch()
        self.flat(w, NOON + 100)
        self.assertTrue(w.assess(self.held(), NOON + 100))
        w.closed(self.held(), NOON + 100, False, problem="MARKET_CLOSED")
        self.assertFalse(w.assess(self.held(), NOON + 120, tradeable=False))
        self.assertFalse(w.closing("EUR_USD"))
        self.assertEqual(logged(log, "called off"), 1)

    def test_accepted_close_counts_once_the_position_has_gone(self):
        """Alpaca: the close is a sell order, filled a moment after it's accepted."""
        w, _ = self.watch()
        self.flat(w, NOON + 100)
        w.assess(self.held(), NOON + 100)
        self.assertIsNone(w.closed(self.held(), NOON + 100, True, filled=False, mid=1.1))
        self.assertFalse(w.assess(self.held(), NOON + 130))            # not sent again while it fills
        fields = w.gone("EUR_USD", NOON + 132, price=1.09995, pnl=-0.10, refs=("order-1",))
        self.assertEqual(fields["closeReason"], "TIMEOUT_STAGNANT")
        self.assertEqual(set(w.exits), {"T1", "order-1"})

    def test_stop_loss_getting_there_first_keeps_its_reason(self):
        w, log = self.watch()
        self.flat(w, NOON + 100)
        w.assess(self.held(), NOON + 100)
        w.closed(self.held(), NOON + 100, False, problem="TRADE_DOESNT_EXIST")
        self.assertIsNone(w.gone("EUR_USD", NOON + 102))               # the broker's stop closed it
        self.assertEqual(w.exits, {})
        self.assertEqual(logged(log, "Not a timeout exit"), 1)

    def test_dry_run_only_says_what_it_would_do(self):
        w, log = self.watch()
        w.dry_run = True
        self.flat(w, NOON + 100)
        self.assertFalse(w.assess(self.held(), NOON + 100))
        self.assertEqual(logged(log, "DRY RUN: would close it"), 1)


# ---------------------------------------------------------------------------
# THE RUNNER USING IT
# ---------------------------------------------------------------------------
class SlotBroker(Broker):
    """A broker whose prices the test sets, and whose closes it controls."""
    key, name = "fake", "Fake"

    def __init__(self, settings, mids, **attributes):
        super().__init__(settings, mock.Mock(), mock.Mock())
        self.mids, self.positions_open, self.opened, self.closed, self.attached = dict(mids), {}, [], [], []
        self.refuse = None          # close() refuses with this while set
        self.fill = None            # close_fill()'s answer (fills_on_close False)
        for k, v in attributes.items():
            setattr(self, k, v)

    def connect(self):
        return Account("1", "GBP", 1000, 1000, "fake account")

    def resolve(self, names):
        return {n: Market(n, n, n, 1.0, 1.0, digits=5) for n in names}

    def quote(self, symbol):
        mid = self.mids[symbol]
        return Quote(mid - SPREAD / 2, mid + SPREAD / 2, True, unit_value=1.0)

    def quotes(self, markets):
        return {m.symbol: self.quote(m.symbol) for m in markets}

    def positions(self, markets):
        return {s: p for s, p in self.positions_open.items() if s in markets}

    def open(self, market, direction, size, stop, take_profit, quote):
        self.opened.append(market.symbol)
        return True

    def close(self, market, position, reason, size=None):
        self.closed.append((market.symbol, reason))
        if self.refuse:
            self.close_problem = self.refuse
            return False
        if self.fills_on_close:
            self.positions_open.pop(market.symbol)
            self.closing_fill = {"price": self.quote(market.symbol).bid, "pnl": position.pnl}
        return True

    def refs(self, position):
        return set(position.raw or ())

    def close_fill(self, market):
        return self.fill

    def attach_exit(self, market, fields):
        self.attached.append((market.symbol, fields))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(runner, "STATE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    # -- the tick scalper: two of its two slots held, EUR/USD going nowhere ----------
    def scalper(self, mode="enforce", broker_attributes=None, broker=None, **env):
        with clean_env(SCALPER_STAGNANT_MODE=mode, **env):
            s = BotSettings(broker="fake", strategy="scalper", slug="test-scalper", name="Test scalper",
                            markets=["EUR_USD", "GBP_USD", "AUD_USD"], budget=1000.0, max_positions=2,
                            account_id="", dry_run=False, params=strategy_params("scalper"))
            if broker is None:
                broker = SlotBroker(s, {"EUR_USD": 1.1000, "GBP_USD": 1.3000, "AUD_USD": 0.7000},
                                    **(broker_attributes or {}))
                for symbol, ref in (("EUR_USD", "T1"), ("GBP_USD", "T2")):
                    broker.positions_open[symbol] = Position(symbol, "long", 1000, broker.mids[symbol], pnl=-0.05,
                                                             opened_at=NOON, raw=[ref], own=True)
            bot = runner.StrategyBot(s, make_strategy("scalper", s.params), broker, mock.Mock(), mock.Mock())
        with mock.patch("time.time", return_value=NOON):
            bot.start()
        for symbol in ("EUR_USD", "GBP_USD"):
            bot.state.positions.setdefault(symbol, {"direction": "long", "opened_at": NOON,
                                                    "entry": broker.mids[symbol], "stop": broker.mids[symbol] - 0.001,
                                                    "risk": 1.0})
        return bot, broker

    def run_reads(self, bot, broker, start, end):
        """A read every 2s; GBP/USD swings 10 pips each time, EUR/USD sits still."""
        now = start
        while now <= end:
            broker.mids["GBP_USD"] = 1.3000 + (0.0010 if int(now) % 4 else 0)
            with mock.patch("time.time", return_value=now):
                bot.cycle()
            now += 2
        return now

    def releases(self, bot) -> int:
        return len(bot.dashboard.report_soon.call_args_list)

    def signals(self):
        return [(2, "EUR_USD", Signal("long", stop=1.0995, reward_risk=1.0, score=2, why="test")),
                (1, "AUD_USD", Signal("long", stop=0.6995, reward_risk=1.0, score=1, why="test"))]

    def test_one_close_frees_exactly_one_slot(self):
        bot, broker = self.scalper()
        now = self.run_reads(bot, broker, NOON, NOON + 120)
        self.assertEqual(broker.closed, [("EUR_USD", "TIMEOUT_STAGNANT")])
        self.assertEqual(set(bot.state.positions), {"GBP_USD"})
        self.assertEqual(self.releases(bot), 1)
        self.assertEqual(broker.attached[0][1]["closeReason"], "TIMEOUT_STAGNANT")
        self.assertEqual(broker.exit_fields("T1")["closeReason"], "TIMEOUT_STAGNANT")
        # nothing more is sent or freed, however long it runs, and a second release is a no-op
        self.run_reads(bot, broker, now, now + 60)
        self.assertEqual(len(broker.closed), 1)
        self.assertEqual(self.releases(bot), 1)
        self.assertFalse(bot.release("EUR_USD", now))
        # one slot came free: of two signals, one opens
        own, _ = bot.split_positions(bot.markets)
        bot.enter(self.signals(), own, now)
        self.assertEqual(broker.opened, ["EUR_USD"])

    def test_accepted_close_holds_the_slot_until_the_position_has_gone(self):
        """Alpaca accepts a close and fills it a moment later."""
        bot, broker = self.scalper(broker_attributes={"fills_on_close": False})
        now = self.run_reads(bot, broker, NOON, NOON + 120)
        self.assertEqual(broker.closed, [("EUR_USD", "TIMEOUT_STAGNANT")])
        self.assertIn("EUR_USD", bot.state.positions)                 # not confirmed yet: still holds its slot
        own, _ = bot.split_positions(bot.markets)
        bot.enter(self.signals(), own, now)
        self.assertEqual(broker.opened, [])
        now = self.run_reads(bot, broker, now, now + 20)              # within the 60s it's given to fill
        self.assertEqual(len(broker.closed), 1)                       # not sent twice while it fills
        broker.positions_open.pop("EUR_USD")                          # filled
        broker.fill = {"price": 1.09994, "pnl": -0.11, "refs": ("order-1",)}
        now = self.run_reads(bot, broker, now, now)
        self.assertNotIn("EUR_USD", bot.state.positions)
        self.assertEqual(self.releases(bot), 1)
        self.assertEqual(broker.attached[0][1]["closeReason"], "TIMEOUT_STAGNANT")
        own, _ = bot.split_positions(bot.markets)
        bot.enter(self.signals(), own, now)
        self.assertEqual(broker.opened, ["EUR_USD"])

    def test_stop_loss_first_frees_the_slot_once_with_the_brokers_reason(self):
        bot, broker = self.scalper(broker_attributes={"refuse": "TRADE_DOESNT_EXIST"})
        self.run_reads(bot, broker, NOON, NOON + 92)
        self.assertEqual(broker.closed, [("EUR_USD", "TIMEOUT_STAGNANT")])
        broker.positions_open.pop("EUR_USD")                          # its stop-loss had filled first
        self.run_reads(bot, broker, NOON + 94, NOON + 130)
        self.assertEqual(len(broker.closed), 1)
        self.assertNotIn("EUR_USD", bot.state.positions)
        self.assertEqual(self.releases(bot), 1)
        self.assertEqual(broker.attached, [])                         # no TIMEOUT_STAGNANT on its record
        self.assertEqual(broker.exit_fields("T1"), {})
        self.assertEqual(logged(bot.log, "Not a timeout exit"), 1)

    def test_refused_closes_retry_and_nothing_else_closes_it_meanwhile(self):
        bot, broker = self.scalper(broker_attributes={"refuse": "MARKET_HALTED"})
        self.run_reads(bot, broker, NOON, NOON + 600)                 # past the scalper's own 300s time stop
        eur_usd = [reason for symbol, reason in broker.closed if symbol == "EUR_USD"]
        self.assertEqual(set(eur_usd), {"TIMEOUT_STAGNANT"})          # its time stop never tried as well
        self.assertEqual(len(eur_usd), 6)                             # at +0, 15, 45, 105, 225, 465s
        gbp_usd = [reason for symbol, reason in broker.closed if symbol == "GBP_USD"]
        self.assertTrue(gbp_usd and all("time stop" in r for r in gbp_usd))  # GBP/USD moved: only its own exit
        self.assertEqual(logged(bot.log, "ALERT"), 1)
        self.assertIn("EUR_USD", bot.state.positions)

    def test_a_restart_carries_on_the_close_without_sending_it_twice(self):
        bot, broker = self.scalper(broker_attributes={"refuse": "MARKET_HALTED"})
        now = self.run_reads(bot, broker, NOON, NOON + 92)
        self.assertEqual(len(broker.closed), 1)
        restarted, _ = self.scalper(broker=broker)                    # reads the saved notes
        self.assertTrue(restarted.watch.closing("EUR_USD"))
        self.run_reads(restarted, broker, now, now + 10)              # before the 15s retry
        self.assertEqual(len(broker.closed), 1)
        broker.refuse = None
        self.run_reads(restarted, broker, now + 12, now + 20)
        self.assertEqual(broker.closed[-1], ("EUR_USD", "TIMEOUT_STAGNANT"))
        self.assertEqual(len(broker.closed), 2)
        self.assertNotIn("EUR_USD", restarted.state.positions)

    def test_shadow_mode_leaves_it_open_and_marks_its_record(self):
        bot, broker = self.scalper(mode="shadow")
        now = self.run_reads(bot, broker, NOON, NOON + 200)
        self.assertEqual(broker.closed, [])
        self.assertEqual(logged(bot.log, "TIMEOUT_STAGNANT_SHADOW"), 1)
        broker.positions_open.pop("EUR_USD")                          # closed by its take-profit, say
        self.run_reads(bot, broker, now + 200, now + 200)
        self.assertEqual(broker.exit_fields("T1")["stagnancy"]["mode"], "shadow")
        self.assertNotIn("closeReason", broker.exit_fields("T1"))

    def test_cooldown_keeps_it_out_of_that_market_only(self):
        bot, broker = self.scalper(SCALPER_STAGNANT_COOLDOWN="10m")
        now = self.run_reads(bot, broker, NOON, NOON + 120)
        own, _ = bot.split_positions(bot.markets)
        bot.enter(self.signals(), own, now)
        self.assertEqual(broker.opened, ["AUD_USD"])                  # EUR/USD's signal was stronger, but cooling

    def test_bad_settings_stop_the_bot_starting(self):
        with self.assertRaisesRegex(SettingsError, "SCALPER_STAGNANT_WINDOW"):
            self.scalper(SCALPER_STAGNANT_WINDOW="4bars")

    # -- a bar strategy: the window is its own bars, measured against ATR --------------
    def test_bar_strategy_uses_its_bars(self):
        with clean_env(BREAKOUT_STAGNANT_MODE="enforce"):
            s = BotSettings(broker="fake", strategy="session-breakout", slug="test-breakout", name="Test breakout",
                            markets=["GBP_USD"], budget=100.0, max_positions=2, account_id="", dry_run=False,
                            params=strategy_params("session-breakout"))
            broker = SlotBroker(s, {"GBP_USD": 1.3000})
            bot = runner.StrategyBot(s, SessionBreakout(s.params), broker, mock.Mock(), mock.Mock())
        now = NOON + 2 * 3600 + 20                                     # 15:00 London, before the 20:00 flat
        last = now - 20 - 900                                          # the latest closed bar started here
        wide = [Bar(last - 900 * i, 1.3, 1.3020, 1.2980, 1.3, 100) for i in reversed(range(8, 40))]
        quiet = [Bar(last - 900 * i, 1.3, 1.3002, 1.2999, 1.3, 100) for i in reversed(range(8))]
        bot.markets = broker.resolve(["GBP_USD"])
        bot.feeds = {"GBP_USD": {"exec": mock.Mock(bars=wide + quiet)}}
        position = Position("GBP_USD", "long", 100, 1.3, pnl=-0.01, opened_at=now - 3 * 3600, raw=["T9"], own=True)
        broker.positions_open["GBP_USD"] = position
        bot.state.positions["GBP_USD"] = {"direction": "long", "opened_at": now - 3 * 3600, "entry": 1.3,
                                          "stop": 1.29, "risk": 1.0}
        bot.time_exits({"GBP_USD": position}, now)
        self.assertEqual(broker.closed, [("GBP_USD", "TIMEOUT_STAGNANT")])
        trigger = broker.attached[0][1]["stagnancy"]
        self.assertEqual((trigger["window"], trigger["rangeRule"]), ("6bars", "1atr"))
        self.assertAlmostEqual(trigger["range"], 0.0003)


if __name__ == "__main__":
    unittest.main()
