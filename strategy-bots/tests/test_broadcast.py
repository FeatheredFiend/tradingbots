"""
Tests for broadcast trades from the dashboard: the symbol map
(shared/symbols.py), the shared part (shared/broadcast.py - requests, the
book of broadcasts acted on, tagging the dashboard's rows), the reporter's
side (shared/dashboard_reporter.py) and the strategy bots' previews and
opens against a fake broker - every limit applied, nothing opened twice,
guest markets watched until their trade closes. No broker or dashboard is
called.

    python -m unittest discover -s strategy-bots/tests
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ["DASHBOARD_URL"] = ""  # never report test runs to the real dashboard
for _name in [n for n in os.environ if "STAGNANT" in n]:  # the stagnancy timeout at its defaults
    del os.environ[_name]
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["STAGNANT_FILE"] = os.path.join(HERE, "no-stagnancy.json")
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "shared"))

import broadcast  # noqa: E402
import dashboard_reporter  # noqa: E402
import symbols  # noqa: E402
from dashboard_reporter import CommandError, DashboardReporter  # noqa: E402
from engine import runner  # noqa: E402
from engine.brokers.base import Market, Position, Quote  # noqa: E402
from engine.strategies import SessionBreakout  # noqa: E402
from test_engine import FakeBroker, flat_bars, settings, utc  # noqa: E402

NOW = utc(2026, 9, 29, 12, 30)   # a Tuesday, 13:30 London: today's breakout range is complete


# ---------------------------------------------------------------------------
class SymbolTests(unittest.TestCase):
    def codes(self, broker, text):
        return [c.code for c in symbols.candidates(broker, text)]

    def test_one_name_reaches_every_broker_in_its_own_spelling(self):
        self.assertEqual(self.codes("oanda", "US500"), ["SPX500_USD"])
        self.assertEqual(self.codes("pepperstone", "us500"), ["US500"])
        self.assertEqual(self.codes("capital", "S&P 500"), ["US500"])
        self.assertEqual(self.codes("ig", "SPX500_USD"), ["US 500:IX.D.SPTRD.IFS.IP"])
        self.assertEqual(self.codes("capital", "gold"), ["GOLD"])
        self.assertEqual(self.codes("oanda", "Spot Gold"), ["XAU_USD"])
        self.assertEqual(symbols.canonical("Oil - Brent Crude"), "BRENT")

    def test_alpaca_has_funds_standing_in(self):
        (candidate,) = symbols.candidates("alpaca", "US500")
        self.assertEqual((candidate.code, candidate.canonical, candidate.stand_in), ("SPY", "US500", True))
        self.assertFalse(symbols.candidates("capital", "US500")[0].stand_in)

    def test_an_instrument_a_broker_hasnt_got(self):
        self.assertEqual(self.codes("ig", "EURGBP"), [], "IG quotes GBP/EUR, the inverse")
        self.assertEqual(self.codes("alpaca", "EURUSD"), [])
        self.assertNotIn("EURGBP", symbols.names("ig"))
        self.assertIn("US500", symbols.names("ig"))

    def test_names_outside_the_map_are_tried_in_the_brokers_spelling(self):
        self.assertEqual(self.codes("oanda", "NZD/USD"), ["NZD_USD", "NZD/USD"])
        self.assertEqual(self.codes("ig", "nzdusd"), ["NZD/USD", "nzdusd"])
        self.assertEqual(self.codes("pepperstone", "TSLA"), ["TSLA.US", "TSLA"])
        self.assertEqual(self.codes("alpaca", "tsla"), ["TSLA"], "the same code once, whatever its case")
        self.assertEqual(self.codes("oanda", "TSLA"), ["TSLA"], "no shares on OANDA: only as typed")
        self.assertEqual(self.codes("oanda", "  "), [])


# ---------------------------------------------------------------------------
def command(action="broadcast-preview", broadcast_id=11, symbol="GBPUSD", side="buy", quantity=None, preview=None,
            size=None, command_id=1):
    payload = {"broadcastId": broadcast_id, "target": 5, "symbol": symbol, "side": side, "quantity": quantity}
    if preview is not None:
        payload["preview"] = preview
    return {"id": command_id, "action": action, "symbol": symbol, "ref": None,
            "direction": "long" if side == "buy" else "short", "size": size, "payload": payload}


class RequestTests(unittest.TestCase):
    def test_parse(self):
        request = broadcast.Request.parse(command(side="sell", quantity=250, preview={"size": 0.5}))
        self.assertEqual((request.id, request.target, request.symbol, request.direction), (11, 5, "GBPUSD", "short"))
        self.assertEqual(request.quantity, 250)
        self.assertEqual(request.size, 0.5, "the most it may open: the previewed size")
        self.assertEqual(request.most(2.0), 0.5)

    def test_malformed_commands_are_declined(self):
        for bad in ({"id": 1, "action": "broadcast-preview"}, command(side="hold"), command(quantity=-3),
                    command(symbol="")):
            with self.subTest(bad=bad), self.assertRaises(broadcast.Declined) as caught:
                broadcast.Request.parse(bad)
            self.assertEqual(caught.exception.data, {"reason": "other"})

    def test_an_open_needs_a_fresh_preview(self):
        broadcast.Request.parse(command(preview={"previewedAt": NOW - 100})).check_age(NOW)
        for preview in ({"previewedAt": NOW - broadcast.OPEN_WITHIN_SECONDS - 1}, {}):
            with self.subTest(preview=preview), self.assertRaises(broadcast.Declined):
                broadcast.Request.parse(command(preview=preview)).check_age(NOW)

    def test_a_quantity_only_ever_makes_it_smaller(self):
        request = broadcast.Request.parse(command(quantity=50))
        self.assertEqual(broadcast.exposure_for(request, 120, "GBP"), (50, ""))
        request.quantity = 500
        exposure, note = broadcast.exposure_for(request, 120, "GBP", "budget slice")
        self.assertEqual(exposure, 120)
        self.assertIn("capped at 120.00 GBP (the bot's budget slice)", note)


class BookTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "broadcasts.json")
        self.book = broadcast.Book(path=self.path)
        self.request = broadcast.Request.parse(command())

    def opened(self, symbol="EUR_USD", aliases=("EUR/USD",), refs=("T1",), at=NOW):
        self.book.opening(self.request, symbol, aliases, at)
        self.book.opened(self.request, refs, at)

    def test_never_twice_even_after_a_restart(self):
        self.book.opening(self.request, "EUR_USD", (), NOW)  # noted before the order goes
        with self.assertRaisesRegex(broadcast.Declined, "already acted on broadcast #11"):
            broadcast.Book(path=self.path).check_new(self.request)

    def test_rows_are_tagged_by_ref_market_and_time(self):
        self.opened()
        tag = {"entrySource": "MANUAL_BROADCAST", "broadcastId": 11}
        self.assertEqual(self.book.tags({"ref": "T1", "symbol": "anything"}), tag)
        # IG's history names it by another ref, and the market by its name: matched by opening time.
        self.assertEqual(self.book.tags({"ref": "ZZ9", "symbol": "EUR/USD", "openedAt": NOW + 3}), tag)
        self.assertEqual(self.book.tags({"symbol": "EUR_USD", "openedAt": "2026-09-29T12:30:05Z"}), tag)
        self.assertEqual(self.book.tags({"ref": "T7", "symbol": "EUR_USD", "openedAt": NOW - 3600}), {})
        self.assertEqual(self.book.tags({"ref": "T8", "symbol": "GBP_USD", "openedAt": NOW}), {})

    def test_alpaca_rows_with_no_opening_time(self):
        self.opened(symbol="AAPL", aliases=(), refs=())
        tag = {"entrySource": "MANUAL_BROADCAST", "broadcastId": 11}
        self.assertEqual(self.book.tags({"symbol": "AAPL", "size": 0.1}), tag, "an open position, no times at all")
        self.assertEqual(self.book.tags({"ref": "SELL1", "symbol": "AAPL", "closedAt": NOW + 600}), tag)
        self.book.sync(set(), NOW + 600)  # sold
        self.assertEqual(self.book.tags({"symbol": "AAPL", "size": 0.1}, NOW + 900), {}, "a later position isn't it")
        self.assertEqual(self.book.tags({"ref": "SELL2", "symbol": "AAPL", "closedAt": NOW + 9000}, NOW + 9000), {})

    def test_sync_waits_for_a_slow_fill_and_prune_forgets(self):
        self.opened(symbol="AAPL", aliases=(), refs=())
        self.book.sync(set(), NOW + 60)
        self.assertEqual(self.book.open_symbols(), {"AAPL"}, "may not have shown up yet")
        self.book.sync(set(), NOW + broadcast.FILL_GRACE_SECONDS + 1)
        self.assertEqual(self.book.open_symbols(), set())
        self.assertEqual(self.book.symbols(), {"AAPL"}, "its closed trade is still reported")
        self.book.prune(NOW + broadcast.KEEP_SECONDS + 1000)
        self.assertEqual(self.book.records, {})


# ---------------------------------------------------------------------------
class ReporterTests(unittest.TestCase):
    def reporter(self, broadcast_on=True, replies=()):
        with mock.patch.object(dashboard_reporter, "URL", ""):
            reporter = DashboardReporter("test-bot", "Test bot", broker="Fake", strategy="Test")
        reporter.enabled = True  # as if DASHBOARD_URL were set, but with no thread and no real posts
        self.sent, replies = [], list(replies)

        def post(report):
            self.sent.append(report)
            return replies.pop(0) if replies else {"ok": True, "commands": []}
        reporter._post = post
        for name, value in (("BROADCAST_ON", broadcast_on), ("COMMANDS_ON", True)):
            patcher = mock.patch.object(dashboard_reporter, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return reporter

    def test_takes_broadcasts_and_answers_with_figures(self):
        def preview(cmd):
            return "Would buy 250 units of GBP_USD.", {"symbol": "GBP_USD", "size": 250}

        def open_trade(cmd):
            raise broadcast.Declined(broadcast.NO_SLOT, "2 positions already open.")
        reporter = self.reporter(replies=[{"commands": [command(command_id=1), command("broadcast-open", command_id=2)]}])
        reporter.accept_closes(lambda *args: "Closed.")
        reporter.accept_broadcasts(preview, open_trade, ["EURUSD", "GBPUSD"])
        reporter.describe(account_mode="demo")
        reporter._send_once()
        bot = self.sent[0]["bot"]
        self.assertEqual(bot["acceptsCommands"], ["close", "broadcast"])
        self.assertEqual(bot["broadcast"], {"symbols": ["EURUSD", "GBPUSD"]})
        self.assertEqual(bot["accountMode"], "demo")
        self.assertEqual(reporter.run_commands(), 2)
        reporter._send_once()
        self.assertEqual(self.sent[1]["commandResults"], [
            {"id": 1, "ok": True, "message": "Would buy 250 units of GBP_USD.", "data": {"symbol": "GBP_USD", "size": 250}},
            {"id": 2, "ok": False, "message": "2 positions already open.", "data": {"reason": "no-slot"}},
        ])

    def test_off_unless_dashboard_broadcast_is_set(self):
        reporter = self.reporter(broadcast_on=False, replies=[{"commands": [command()]}])
        reporter.accept_broadcasts(lambda c: ("", {}), lambda c: ("", {}), ["EURUSD"])
        reporter._send_once()
        self.assertEqual(self.sent[0]["bot"]["acceptsCommands"], [])
        self.assertIn("DASHBOARD_BROADCAST", self.sent[0]["bot"]["broadcast"]["off"])
        reporter.run_commands()
        self.assertEqual(reporter._answers[0]["data"], {"reason": "paused"})

    def test_rows_are_tagged_on_the_way_out(self):
        reporter = self.reporter()
        reporter.tag_rows(lambda row: {"broadcastId": 4} if row.get("symbol") == "EUR_USD" else {})
        reporter.update(positions=[{"symbol": "EUR_USD"}, {"symbol": "GBP_USD"}],
                        trades=[{"ref": "9", "symbol": "EUR_USD", "pnl": 1.0}])
        reporter._send_once()
        self.assertEqual(self.sent[0]["positions"], [{"symbol": "EUR_USD", "broadcastId": 4}, {"symbol": "GBP_USD"}])
        self.assertEqual(self.sent[0]["trades"], [{"ref": "9", "symbol": "EUR_USD", "pnl": 1.0, "broadcastId": 4}])

    def test_command_error_keeps_its_data(self):
        self.assertEqual(CommandError("no", data={"reason": "risk"}).data, {"reason": "risk"})
        self.assertIsNone(CommandError("no").data)


# ---------------------------------------------------------------------------
class TaggedBroker(FakeBroker):
    """A fake broker that knows only some markets, gives each trade an ID,
    and remembers the order label each open carried."""
    account_mode = "demo"

    def __init__(self, *args, known=("GBP_USD", "EUR_USD"), **kwargs):
        super().__init__(*args, **kwargs)
        self.known, self.tags, self.next_id = set(known), [], 100

    def resolve(self, names):
        return {n: Market(n, n.replace("_", "/"), n, 1.0, 1.0, digits=5) for n in names if n in self.known}

    def open(self, market, direction, size, stop, take_profit, quote):
        self.tags.append(self.order_tag)
        self.next_id += 1
        self.own_ids.add(str(self.next_id))
        return super().open(market, direction, size, stop, take_profit, quote)


class EngineBroadcastTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(runner, "STATE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        start = utc(2026, 9, 28, 12, 0)
        # The 07:00-13:00 London range: highs 1.30163, lows 1.29837 - 0.00326 wide.
        self.history = flat_bars(start, int((utc(2026, 9, 29, 12, 0) - start) / 900), 1.3000, wiggle=0.0015)

    def bot(self, positions=None, long_only=False, **overrides):
        s = settings(broker="oanda", **overrides)  # the symbol map's OANDA spellings
        self.broker = TaggedBroker(s, {"M15": self.history}, Quote(1.3000, 1.3001, True, unit_value=1.0),
                                   positions=positions, **({"can_short": False} if long_only else {}))
        bot = runner.StrategyBot(s, SessionBreakout(s.params), self.broker, mock.Mock(), mock.Mock())
        with mock.patch("time.time", return_value=NOW - 1500):
            bot.start()
        return bot

    def preview(self, bot, at=NOW, **kwargs):
        with mock.patch("time.time", return_value=at):
            return bot.broadcast_preview(command(**kwargs))

    def open(self, bot, figures, at=NOW + 30, **kwargs):
        with mock.patch("time.time", return_value=at):
            return bot.broadcast_open(command("broadcast-open", preview=figures, size=figures.get("size"), **kwargs))

    def test_registers_for_broadcasts_with_its_names_and_account(self):
        bot = self.bot()
        bot.dashboard.accept_broadcasts.assert_called_once()
        names = bot.dashboard.accept_broadcasts.call_args.args[2]
        self.assertIn("GBPUSD", names)
        self.assertIn("XAUUSD", names, "every name the map has for its broker")
        self.assertEqual(bot.dashboard.describe.call_args.kwargs["account_mode"], "demo")
        bot.dashboard.tag_rows.assert_called_once_with(bot.broadcasts.tags)

    def test_preview_uses_the_strategys_own_stop_and_size_and_sends_nothing(self):
        bot = self.bot()
        line, figures = self.preview(bot)
        self.assertEqual(self.broker.opened, [])
        distance = 0.5 * (1.30163 - 1.29837)  # half today's range
        self.assertEqual(figures["symbol"], "GBP_USD")
        self.assertEqual(figures["size"], 250, "the per-trade leverage cap: 100 x 5 / 2 positions")
        self.assertAlmostEqual(figures["stopLoss"], round(1.3001 - distance, 5))
        self.assertAlmostEqual(figures["takeProfit"], round(1.3001 + 1.5 * distance, 5))
        self.assertEqual((figures["accountMode"], figures["currency"], figures["sizeUnit"]), ("demo", "GBP", "units"))
        self.assertEqual(figures["previewedAt"], NOW)
        self.assertIn("flat at 20:00 London", figures["exits"])
        self.assertTrue(line.startswith("Would buy 250 units of GBP_USD at ~1.3001"), line)

    def test_a_quantity_makes_it_smaller_never_bigger(self):
        bot = self.bot()
        _, smaller = self.preview(bot, quantity=100)
        self.assertEqual(smaller["size"], 100)
        self.assertNotIn("capped", smaller)
        _, capped = self.preview(bot, quantity=1000)
        self.assertEqual(capped["size"], 250)
        self.assertIn("capped at 250.00 GBP exposure", capped["capped"])

    def test_open_once_with_the_label_the_notes_and_the_count(self):
        bot = self.bot()
        _, figures = self.preview(bot)
        line, result = self.open(bot, figures)
        self.assertEqual(self.broker.opened[0][:3], ("GBP_USD", "long", 250))
        self.assertEqual(self.broker.tags, ["broadcast-11"])
        self.assertIsNone(self.broker.order_tag, "only on that order")
        self.assertEqual(result, {"symbol": "GBP_USD", "size": 250, "price": 1.3001, "ref": "101"})
        notes = bot.state.positions["GBP_USD"]
        self.assertEqual((notes["broadcast"], notes["entry_source"]), (11, "MANUAL_BROADCAST"))
        self.assertEqual(bot.state.trades_on("GBP_USD", "2026-09-29"), 1, "it counts toward the day's trades")
        self.assertEqual(bot.broadcasts.tags({"ref": "101", "symbol": "GBP_USD"})["broadcastId"], 11)
        self.assertIn("Bought 250 units of GBP_USD", line)

        again = self.bot()  # restarted, from the same saved notes
        with self.assertRaisesRegex(broadcast.Declined, "already acted on broadcast #11"):
            self.open(again, figures)

    def test_open_is_never_bigger_than_the_preview(self):
        bot = self.bot()
        _, figures = self.preview(bot, quantity=100)
        self.open(bot, figures)  # with no quantity on the open itself: the bot's own size would be 250
        self.assertEqual(self.broker.opened[0][2], 100)

    def test_a_late_open_is_refused(self):
        bot = self.bot()
        _, figures = self.preview(bot)
        with self.assertRaisesRegex(broadcast.Declined, "previewed it 200s ago"):
            self.open(bot, figures, at=NOW + 200)
        self.assertEqual(self.broker.opened, [])

    def test_limits_and_hours_still_apply(self):
        cases = [
            ({"dry_run": True}, {}, NOW, broadcast.PAUSED, "dry run"),
            ({}, {"positions": {"A": Position("A", "long", 1, 1, own=True), "B": Position("B", "long", 1, 1, own=True)}},
             NOW, broadcast.NO_SLOT, "2 position"),
            ({}, {"positions": {"GBP_USD": Position("GBP_USD", "long", 1, 1.3, own=True)}}, NOW, broadcast.NO_SLOT,
             "already holds GBP_USD"),
            ({}, {"positions": {"GBP_USD": Position("GBP_USD", "short", 1, 1.3, own=False)}}, NOW, broadcast.NO_SLOT,
             "Someone else's"),
            ({}, {}, utc(2026, 9, 29, 19, 10), broadcast.CLOSED, "flat time"),
            ({}, {}, utc(2026, 9, 29, 11, 0), broadcast.CLOSED, "range isn't complete"),
        ]
        for overrides, extra, at, kind, why in cases:
            with self.subTest(why):
                bot = self.bot(**overrides, **extra)
                with self.assertRaises(broadcast.Declined) as caught:
                    self.preview(bot, at=at)
                self.assertEqual(caught.exception.kind, kind)
                self.assertIn(why, str(caught.exception))

    def test_short_where_it_cant_and_markets_it_hasnt_got(self):
        bot = self.bot(long_only=True)
        with self.assertRaisesRegex(broadcast.Declined, "can't sell short") as caught:
            self.preview(bot, side="sell")
        self.assertEqual(caught.exception.kind, broadcast.UNAVAILABLE)
        with self.assertRaisesRegex(broadcast.Declined, "isn't available on Fake") as caught:
            self.preview(bot, symbol="XAUUSD")  # XAU_USD: not one this fake broker has
        self.assertEqual(caught.exception.kind, broadcast.UNAVAILABLE)

    def test_a_guest_market_is_watched_until_its_trade_closes(self):
        bot = self.bot()
        _, figures = self.preview(bot, symbol="EURUSD")
        self.assertNotIn("EUR_USD", bot.markets, "a preview adds nothing")
        self.assertIn("watched until it closes", figures["exits"])
        self.open(bot, figures, symbol="EURUSD")
        self.assertEqual(bot.guests, {"EUR_USD"})
        self.assertIn("EUR_USD", bot.feeds, "its bars, for its exits")
        self.assertEqual(bot.state.positions["EUR_USD"]["guest"], "EUR_USD")

        # The strategy never trades it: a breakout on its bar is ignored.
        with mock.patch.object(bot.strategy, "assess") as assess:
            bot.on_new_bars(["EUR_USD"], {}, {}, NOW + 900)
        assess.assert_not_called()

        # A restart looks it up again from the notes.
        again = self.bot()
        self.assertIn("EUR_USD", again.markets)
        self.assertEqual(again.guests, {"EUR_USD"})

        # Once its trade has gone it's dropped - but still reported a while.
        bot.reconcile({}, {}, NOW + runner.FILL_GRACE_SECONDS + 60)
        self.assertNotIn("EUR_USD", bot.markets)
        self.assertEqual(bot.guests, set())
        self.assertIn("EUR_USD", bot.left_guests)
        self.assertEqual(bot.broadcasts.open_symbols(), set())
        with mock.patch.object(bot.broker, "report") as report, mock.patch("time.time", return_value=NOW + 300):
            bot.report()
        self.assertIn("EUR_USD", report.call_args.args[0])

    def test_the_strategys_own_signals_go_through_the_same_rules(self):
        bot = self.bot()
        from engine.strategies import Signal
        bot.enter([(1, "GBP_USD", Signal("long", stop=1.2950, reward_risk=1.5, why="test"))], {}, NOW)
        self.assertEqual(len(self.broker.opened), 1)
        self.assertEqual(self.broker.tags, [None], "no label on the strategy's own orders")
        self.assertNotIn("broadcast", bot.state.positions["GBP_USD"])


if __name__ == "__main__":
    unittest.main()
