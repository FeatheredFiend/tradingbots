"""
Tests for the stagnancy timeout in the original EMA bots and momentum
scanners (each bot's stagnancy_pass()), against fake brokers: a trade going
nowhere is closed with TIMEOUT_STAGNANT once (enforce) or only logged
(shadow, the default), only the bot's own trades are touched, and the
close's fields reach the bot's dashboard records. MetaTrader 5 is faked; the
Alpaca bots need alpaca-trade-api (alpaca-bot-env), else they're skipped.
No broker or dashboard is called.

    python -m unittest discover -s strategy-bots/tests
"""

import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

os.environ["DASHBOARD_URL"] = ""  # never report test runs to the real dashboard
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..")
sys.path.insert(0, os.path.join(ROOT, "shared"))
for folder in ("oanda-momentum-scanner-bot", "oanda-ema-bot", "capital-momentum-scanner-bot", "ig-momentum-scanner-bot",
               "pepperstone-momentum-scanner-bot", "alpaca-momentum-scanner-bot"):
    sys.path.insert(0, os.path.join(ROOT, folder))

import pandas as pd  # noqa: E402

import rollover  # noqa: E402
import stagnancy  # noqa: E402


class _FakeMT5(types.ModuleType):
    """Just enough of MetaTrader5 to import the Pepperstone bots: every
    constant is a distinct number; the calls are set by each test."""

    def __init__(self):
        super().__init__("MetaTrader5")
        self._numbers = {}

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self._numbers.setdefault(name, len(self._numbers) + 1000)


if "MetaTrader5" not in sys.modules:
    sys.modules["MetaTrader5"] = _FakeMT5()

def optional(name):
    """The bot's module, or None in a venv without its broker's package (each
    bot's tests are skipped there - run them from that bot's venv)."""
    try:
        return __import__(name)
    except ImportError:
        return None


capital_scanner = optional("capital_momentum_scanner_bot")
ig_scanner = optional("ig_momentum_scanner_bot")
oanda_ema = optional("oanda_ema_bot")
oanda_scanner = optional("oanda_momentum_scanner_bot")
pepperstone_scanner = optional("pepperstone_momentum_scanner_bot")
alpaca_scanner = optional("alpaca_momentum_scanner_bot")


def needs(module, venv):
    return unittest.skipIf(module is None, f"needs its broker's package (run from {venv})")

NOWHERE = os.path.join(tempfile.gettempdir(), "no-such-stagnancy-file.json")
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc).timestamp()   # Tuesday, markets open
# Short enough to test quickly: older than a minute, flat for two.
QUICK = {"SCANNER_STAGNANT_MIN_AGE": "1m", "SCANNER_STAGNANT_WINDOW": "2m", "EMA_STAGNANT_MIN_AGE": "1m",
         "EMA_STAGNANT_WINDOW": "2m"}


def make_watch(bot_type, broker, mode="enforce"):
    book = stagnancy.RuleBook(bot_type, broker, bar_seconds=900, environ={**QUICK, "STAGNANT_MODE": mode},
                              path=NOWHERE)
    return stagnancy.Watch(book, f"test-{broker}-{bot_type}", broker, mock.Mock(), currency="GBP", gap=60)


def passes(run, start=T0, end=T0 + 150, every=30):
    """Call run() once a pass, every `every` seconds, with the clock set."""
    now = start
    while now <= end:
        with mock.patch("time.time", return_value=now):
            run()
        now += every


class BotTestCase(unittest.TestCase):
    bot = None
    bot_type, broker = "scanner", ""

    def setUp(self):
        self.watch = make_watch(self.bot_type, self.broker, getattr(self, "mode", "enforce"))
        for name, value in (("watch", self.watch), ("own_trades", self.own_trades())):
            if value is not None:
                patcher = mock.patch.object(self.bot, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)

    def own_trades(self):
        if not hasattr(self.bot, "own_trades"):
            return None
        own = rollover.OwnTrades(os.path.join(tempfile.mkdtemp(), "own_trades.json"))
        own.add(*self.own_ids)
        return own

    own_ids = ()

    def logged(self, text) -> int:
        log = self.watch.log
        return sum(text in str(c.args[0]) for c in log.info.call_args_list + log.warning.call_args_list
                   + log.error.call_args_list)


# ---------------------------------------------------------------------------
# OANDA: its own trades by ID, closed one by one
# ---------------------------------------------------------------------------
def oanda_trade(trade_id, units="25", price="1.1000"):
    return {"id": trade_id, "instrument": "EUR_USD", "currentUnits": units, "price": price,
            "openTime": str(T0 - 3600), "unrealizedPL": "-0.01", "financing": "0",
            "stopLossOrder": {"price": "1.0945"}}


class FakeOanda:
    def __init__(self, trades):
        self.trades, self.closed = trades, []

    def __call__(self, method, path, **kwargs):
        if path.endswith("/openTrades"):
            return {"trades": [t for t in self.trades if t["id"] not in self.closed]}
        if path.endswith("/pricing"):
            return {"prices": [{"instrument": "EUR_USD", "bids": [{"price": "1.1000"}], "asks": [{"price": "1.1001"}],
                                "tradeable": True}], "homeConversions": [{"currency": "USD", "positionValue": "0.75"}]}
        trade_id = path.split("/trades/")[1].split("/")[0]
        self.closed.append(trade_id)
        return {"orderFillTransaction": {"units": "-25", "price": "1.09995", "fullVWAP": "1.09995",
                                         "tradesClosed": [{"tradeID": trade_id, "realizedPL": "-0.02",
                                                           "financing": "0"}]}}


@needs(oanda_scanner, "ig-bot-env")
class OandaScannerTests(BotTestCase):
    bot, broker = oanda_scanner, "oanda"
    own_ids = ("101",)

    def run_passes(self, fake):
        with mock.patch.object(oanda_scanner, "oanda", fake):
            passes(lambda: oanda_scanner.stagnancy_pass("acct", {"EUR_USD": {}}, "GBP"))

    def test_closes_its_own_flat_trade_once(self):
        fake = FakeOanda([oanda_trade("101"), oanda_trade("202")])   # 202 is another bot's
        self.run_passes(fake)
        self.assertEqual(fake.closed, ["101"])
        self.addCleanup(oanda_scanner.CLOSE_REASONS.pop, "101", None)
        self.assertEqual(oanda_scanner.CLOSE_REASONS["101"], "TIMEOUT_STAGNANT")
        record = oanda_scanner.dashboard_trade({**oanda_trade("101"), "state": "CLOSED", "initialUnits": "25",
                                                "averageClosePrice": "1.09995", "closeTime": str(T0 + 150),
                                                "realizedPL": "-0.02", "stopLossOrder": {"state": "CANCELLED"}})
        self.assertEqual(record["closeReason"], "TIMEOUT_STAGNANT")
        self.assertEqual(record["stagnancy"]["mode"], "enforce")
        self.assertAlmostEqual(record["slippage"], -0.0001)          # sold at 1.09995 against a 1.10005 mid
        self.assertEqual(self.logged("TIMEOUT_STAGNANT | trade 101"), 1)

    def test_shadow_only_logs(self):
        self.watch.book = make_watch("scanner", "oanda", mode="shadow").book
        fake = FakeOanda([oanda_trade("101")])
        self.run_passes(fake)
        self.assertEqual(fake.closed, [])
        self.assertEqual(self.logged("TIMEOUT_STAGNANT_SHADOW"), 1)


@needs(oanda_ema, "ig-bot-env")
class OandaEmaTests(BotTestCase):
    bot, bot_type, broker = oanda_ema, "ema", "oanda"

    def test_remembers_its_trades_and_times_out_only_those(self):
        instrument = {"tradeUnitsPrecision": 0, "minimumTradeSize": "1", "displayPrecision": 5}
        price = {"bid": 1.1000, "ask": 1.1001, "unit_value": 0.75}
        filled = {"orderFillTransaction": {"units": "26", "price": "1.1001", "tradeOpened": {"tradeID": "303"}}}
        with mock.patch.object(oanda_ema, "oanda", return_value=filled):
            self.assertTrue(oanda_ema.submit_buy("acct", "EUR_USD", instrument, price, "GBP"))
        self.assertEqual(oanda_ema.own_trades.ids, {"303"})
        fake = FakeOanda([oanda_trade("303"), oanda_trade("404")])  # 404: the scanner's, on the same account
        with mock.patch.object(oanda_ema, "oanda", fake):
            passes(lambda: oanda_ema.stagnancy_pass("acct", {"EUR_USD": {}}, "GBP"))
        self.addCleanup(oanda_ema.CLOSE_REASONS.pop, "303", None)
        self.assertEqual(fake.closed, ["303"])


# ---------------------------------------------------------------------------
# Capital.com: its own positions by deal ID, whole positions only
# ---------------------------------------------------------------------------
@needs(capital_scanner, "ig-bot-env")
class CapitalScannerTests(BotTestCase):
    bot, broker = capital_scanner, "capital"
    own_ids = ("DEAL1",)

    def test_closes_its_own_flat_position(self):
        deleted = []
        positions = [
            {"market": {"epic": "US500"}, "position": {"dealId": "DEAL1", "direction": "BUY", "size": 0.02,
                                                       "level": 6500.0, "stopLevel": 6467.5, "upl": -0.01,
                                                       "createdDateUTC": "2026-09-29T11:00:00"}},
            {"market": {"epic": "US500"}, "position": {"dealId": "HAND1", "direction": "SELL", "size": 0.05,
                                                       "level": 6510.0, "upl": 0.2,
                                                       "createdDateUTC": "2026-09-29T10:00:00"}},
        ]

        def fake_capital(method, path, **kwargs):
            if method == "GET":
                return {"positions": [p for p in positions if p["position"]["dealId"] not in
                                      [d.rsplit("/", 1)[1] for d in deleted]]}
            deleted.append(path)
            return {"dealReference": "ref"}

        markets = {"US500": {"instrument": {"epic": "US500", "currency": "GBP", "lotSize": 1},
                             "snapshot": {"bid": 6500.0, "offer": 6500.4, "marketStatus": "TRADEABLE"}}}
        with mock.patch.object(capital_scanner, "capital", fake_capital), \
                mock.patch.object(capital_scanner, "fetch_markets", return_value=markets), \
                mock.patch.object(capital_scanner, "confirm", return_value={"dealStatus": "ACCEPTED", "size": 0.02,
                                                                            "level": 6499.9}):
            passes(lambda: capital_scanner.stagnancy_pass({"US500": {}}, {}, "GBP"))
        self.addCleanup(capital_scanner.CLOSE_REASONS.pop, "DEAL1", None)
        self.assertEqual(deleted, ["/positions/DEAL1"])
        self.assertEqual(self.watch.fields_for("DEAL1")["closeReason"], "TIMEOUT_STAGNANT")


# ---------------------------------------------------------------------------
# IG: prices from its positions read; the closed one leaves this pass's positions
# ---------------------------------------------------------------------------
@needs(ig_scanner, "ig-bot-env")
class IGScannerTests(BotTestCase):
    bot, broker = ig_scanner, "ig"
    own_ids = ("DIOWN",)
    POOL = [{"epic": "IX.D.DAX.IFD.IP", "name": "Germany 40"}]

    def test_closes_its_own_flat_position_and_frees_the_slot(self):
        positions = pd.DataFrame([
            {"epic": "IX.D.DAX.IFD.IP", "dealId": "DIOWN", "direction": "BUY", "size": 0.5, "level": 25400.0,
             "stopLevel": 25350.0, "contractSize": 1.0, "currency": "GBP", "bid": 25401.0, "offer": 25402.0,
             "marketStatus": "TRADEABLE", "createdDateUTC": "2026-09-29T11:00:00"},
            {"epic": "IX.D.DAX.IFD.IP", "dealId": "DIHAND", "direction": "SELL", "size": 1.0, "level": 25300.0,
             "stopLevel": None, "contractSize": 1.0, "currency": "GBP", "bid": 25401.0, "offer": 25402.0,
             "marketStatus": "TRADEABLE", "createdDateUTC": "2026-09-29T09:00:00"},
        ])
        service = mock.Mock()
        service.close_open_position.return_value = {"dealStatus": "ACCEPTED", "level": 25401.0, "profit": 0.5,
                                                    "profitCurrency": "GBP", "dealId": "DIAAAAAAAA12345678"}
        limits = ig_scanner._LossLimits()
        limits.open_positions = 2
        seen = [positions]  # each pass gets what the last one left: IG no longer lists a closed position

        def one_pass():
            seen.append(ig_scanner.stagnancy_pass(service, seen[-1], self.POOL))

        with mock.patch.object(ig_scanner, "loss_limits", limits):
            passes(one_pass)
        service.close_open_position.assert_called_once()
        self.assertEqual(service.close_open_position.call_args.kwargs["deal_id"], "DIOWN")
        self.assertEqual(list(seen[-1]["dealId"]), ["DIHAND"])        # out of the pass's positions at once
        self.assertEqual(limits.open_positions, 1)                    # one slot freed, once
        self.assertEqual(ig_scanner.stagnancy_fields("12345678", "IX.D.DAX.IFD.IP", None)["closeReason"],
                         "TIMEOUT_STAGNANT")


# ---------------------------------------------------------------------------
# Pepperstone: its own positions by magic number, prices from MT5's ticks
# ---------------------------------------------------------------------------
@needs(pepperstone_scanner, "ig-bot-env")
class PepperstoneScannerTests(BotTestCase):
    bot, broker = pepperstone_scanner, "pepperstone"

    def test_closes_its_own_flat_position(self):
        mt5 = pepperstone_scanner.mt5
        own = SimpleNamespace(ticket=555, symbol="EURUSD", magic=pepperstone_scanner.MAGIC, type=mt5.POSITION_TYPE_BUY,
                              volume=0.1, price_open=1.1000, sl=1.0945, profit=-0.5, swap=0.0,
                              time=T0 - 3600 + 3 * 3600)              # server time, UTC+3
        other = SimpleNamespace(**{**vars(own), "ticket": 777, "magic": 0})
        open_positions = [own, other]
        tick = lambda now: SimpleNamespace(bid=1.1000, ask=1.1001, time=now + 3 * 3600)  # noqa: E731
        sent = []

        def order_send(request):
            sent.append(request["position"])
            open_positions.remove(own)
            return SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE, volume=0.1, price=1.09995, comment="done")

        exit_deal = SimpleNamespace(entry=mt5.DEAL_ENTRY_OUT, volume=0.1, price=1.09995, profit=-0.55, swap=0.0,
                                    commission=0.0, fee=0.0)
        fakes = {
            "positions_get": lambda **kw: [p for p in open_positions if p.ticket == kw["ticket"]] if "ticket" in kw
            else list(open_positions),
            "history_deals_get": lambda **kw: [exit_deal] if own not in open_positions else [],
            "order_send": order_send,
            "symbol_info": lambda symbol: SimpleNamespace(filling_mode=1),
            "order_calc_profit": lambda *a: 85.0,                     # one lot worth 8,500 in the account's currency
        }
        with mock.patch.multiple(mt5, create=True, **fakes):
            now = [T0]
            with mock.patch.object(mt5, "symbol_info_tick", lambda symbol: tick(now[0]), create=True):
                def one_pass():
                    now[0] = pepperstone_scanner.time.time()
                    pepperstone_scanner.stagnancy_pass({"EURUSD": SimpleNamespace(filling_mode=1)})
                passes(one_pass)
        self.addCleanup(pepperstone_scanner.CLOSE_REASONS.pop, 555, None)
        self.assertEqual(sent, [555])                                 # the other bot's 777 never
        fields = self.watch.fields_for(555)
        self.assertEqual(fields["closeReason"], "TIMEOUT_STAGNANT")
        self.assertEqual(self.logged("P/L -0.55 GBP net"), 1)


# ---------------------------------------------------------------------------
# Alpaca: the sell is filled after it's accepted - done once the position has gone
# ---------------------------------------------------------------------------
@needs(alpaca_scanner, "alpaca-bot-env")
class AlpacaScannerTests(BotTestCase):
    bot, broker = alpaca_scanner, "alpaca"

    def test_sells_then_records_the_fill_once_the_position_has_gone(self):
        position = SimpleNamespace(symbol="AAPL", qty="0.1", avg_entry_price="200", unrealized_pl="-0.01",
                                   current_price="200", unrealized_plpc="0")
        positions = {"AAPL": position}
        api = mock.Mock()
        api.get_latest_quotes.return_value = {"AAPL": SimpleNamespace(bp=199.99, ap=200.01)}
        api.get_latest_trades.return_value = {}
        api.list_orders.return_value = [SimpleNamespace(side="buy", filled_at="2026-09-29T11:00:00Z")]
        api.close_position.return_value = SimpleNamespace(id="ORD1")
        api.get_order.return_value = SimpleNamespace(filled_avg_price="199.98", filled_qty="0.1",
                                                     filled_at="2026-09-29T12:02:01Z")
        acted_on_bar, sent = {}, []
        with mock.patch.object(alpaca_scanner.dashboard, "trade", sent.append):
            passes(lambda: alpaca_scanner.stagnancy_pass(api, ["AAPL"], positions, {}, {}, acted_on_bar))
            api.close_position.assert_called_once_with("AAPL")
            self.assertTrue(self.watch.closing("AAPL"))              # sold, not filled yet: still holds its slot
            positions.clear()                                         # filled
            passes(lambda: alpaca_scanner.stagnancy_pass(api, ["AAPL"], positions, {}, {}, acted_on_bar),
                   start=T0 + 180, end=T0 + 180)
        self.assertFalse(self.watch.closing("AAPL"))
        corrected = sent[-1]
        self.assertEqual((corrected["ref"], corrected["exitPrice"], corrected["closeReason"]),
                         ("ORD1", 199.98, "TIMEOUT_STAGNANT"))
        self.assertEqual(corrected["stagnancy"]["mode"], "enforce")
        self.assertIn("AAPL", acted_on_bar)                           # not bought straight back on that bar


if __name__ == "__main__":
    unittest.main()
