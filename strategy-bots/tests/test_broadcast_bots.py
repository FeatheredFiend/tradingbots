"""
Tests for broadcast trades in the momentum scanners (each bot's
broadcast_plan / _preview / _open), against fake brokers: the preview sends
nothing and uses the bot's own slice, stop-loss and take-profit; the open
labels the order where the broker takes one, is noted so it never opens
twice, and a market outside the pool is managed afterwards; the bot's
limits still apply. MetaTrader 5 is faked; the Alpaca scanner needs
alpaca-trade-api (alpaca-bot-env), else it's skipped. No broker or
dashboard is called.

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
for folder in ("oanda-momentum-scanner-bot", "capital-momentum-scanner-bot", "ig-momentum-scanner-bot",
               "pepperstone-momentum-scanner-bot", "alpaca-momentum-scanner-bot"):
    sys.path.insert(0, os.path.join(ROOT, folder))

import broadcast  # noqa: E402
import rollover  # noqa: E402


class _FakeMT5(types.ModuleType):
    """Just enough of MetaTrader5 to import the Pepperstone scanner: every
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
    """The bot's module, or None in a venv without its broker's package."""
    try:
        return __import__(name)
    except ImportError:
        return None


oanda_scanner = optional("oanda_momentum_scanner_bot")
capital_scanner = optional("capital_momentum_scanner_bot")
ig_scanner = optional("ig_momentum_scanner_bot")
pepperstone_scanner = optional("pepperstone_momentum_scanner_bot")
alpaca_scanner = optional("alpaca_momentum_scanner_bot")


def needs(module, venv):
    return unittest.skipIf(module is None, f"needs its broker's package (run from {venv})")


T0 = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc).timestamp()   # Tuesday, markets open, far from the rollover


def command(action="broadcast-preview", broadcast_id=11, symbol="EURUSD", side="buy", quantity=None, preview=None):
    payload = {"broadcastId": broadcast_id, "target": 3, "symbol": symbol, "side": side, "quantity": quantity}
    if preview is not None:
        payload["preview"] = preview
    return {"id": 1, "action": action, "symbol": symbol, "ref": None, "direction": "long" if side == "buy" else "short",
            "size": (preview or {}).get("size"), "payload": payload}


class ScannerTestCase(unittest.TestCase):
    """Each test gets the bot's own fresh broadcast book, guests, own-trades
    file and no stagnancy timeout, at T0 - and the scanners' default
    settings, whatever this PC's launcher has set."""
    bot = None
    guests = dict
    settings = {}

    def setUp(self):
        folder = tempfile.mkdtemp()
        self.book = broadcast.Book(path=os.path.join(folder, "broadcasts.json"))
        patches = {"broadcasts": self.book, "guests": self.guests(), "watch": None, "STOP_LOSS_PCT": 0.02,
                   "TAKE_PROFIT_PCT": 0.05, **self.settings}
        if hasattr(self.bot, "own_trades"):
            patches["own_trades"] = rollover.OwnTrades(os.path.join(folder, "own_trades.json"))
        for name, value in patches.items():
            patcher = mock.patch.object(self.bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (mock.patch("time.time", return_value=T0),
                        mock.patch.object(rollover, "entries_paused", return_value=False)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def declined(self, kind, call):
        with self.assertRaises(broadcast.Declined) as caught:
            call()
        self.assertEqual(caught.exception.kind, kind, str(caught.exception))
        return str(caught.exception)


# ---------------------------------------------------------------------------
# OANDA: units sized from the budget slice; the order and its trade labelled
# ---------------------------------------------------------------------------
def oanda_instrument(name):
    return {"name": name, "displayName": name.replace("_", "/"), "tradeUnitsPrecision": 0, "minimumTradeSize": "1",
            "displayPrecision": 5}


class FakeOanda:
    def __init__(self, positions=(), offered=("EUR_USD", "GBP_USD", "XAU_USD")):
        self.positions, self.offered, self.orders = list(positions), offered, []

    def __call__(self, method, path, **kwargs):
        params = kwargs.get("params") or {}
        if path.endswith("/instruments"):
            name = params["instruments"]
            return {"instruments": [oanda_instrument(name)] if name in self.offered else []}
        if path.endswith("/openPositions"):
            return {"positions": [{"instrument": s, "long": {"units": "10", "unrealizedPL": "0"},
                                   "short": {"units": "0"}} for s in self.positions]}
        if path.endswith("/pricing"):
            return {"prices": [{"instrument": n, "bids": [{"price": "1.1000"}], "asks": [{"price": "1.1002"}],
                                "tradeable": True} for n in params["instruments"].split(",")],
                    "homeConversions": [{"currency": "USD", "positionValue": "0.75"}]}
        if method == "POST" and path.endswith("/orders"):
            order = kwargs["json"]["order"]
            self.orders.append(order)
            return {"orderFillTransaction": {"units": order["units"], "price": "1.1002", "tradeOpened": {"tradeID": "777"}}}
        raise AssertionError(f"unexpected call {method} {path}")


@needs(oanda_scanner, "ig-bot-env")
class OandaScannerTests(ScannerTestCase):
    bot = oanda_scanner
    settings = {"TRADE_EXPOSURE": 20.0, "MAX_OPEN_POSITIONS": 5}
    POOL = {"EUR_USD": oanda_instrument("EUR_USD")}

    def preview(self, fake, **kwargs):
        with mock.patch.object(oanda_scanner, "oanda", fake):
            return oanda_scanner.broadcast_preview("acct", self.POOL, "GBP", command(**kwargs))

    def open(self, fake, figures, **kwargs):
        with mock.patch.object(oanda_scanner, "oanda", fake):
            return oanda_scanner.broadcast_open("acct", self.POOL, "GBP", command("broadcast-open", preview=figures,
                                                                                  **kwargs))

    def test_preview_then_open_once(self):
        fake = FakeOanda()
        line, figures = self.preview(fake)
        self.assertEqual(fake.orders, [])
        # One 20 GBP slice of EUR/USD: each unit worth 1.1001 USD x 0.75 = 0.825 GBP -> 24 units.
        self.assertEqual((figures["symbol"], figures["size"], figures["accountMode"]), ("EUR_USD", 24, "demo"))
        self.assertAlmostEqual(figures["stopLoss"], round(1.1002 * 0.98, 5))
        self.assertAlmostEqual(figures["takeProfit"], round(1.1002 * 1.05, 5))
        self.assertIn("streak reversal", figures["exits"])
        self.assertIn("Would buy 24 units of EUR_USD", line)

        line, result = self.open(fake, figures)
        (order,) = fake.orders
        self.assertEqual((order["instrument"], order["units"]), ("EUR_USD", "24"))
        self.assertEqual(order["clientExtensions"], {"tag": "broadcast", "comment": "broadcast-11"})
        self.assertEqual(result, {"symbol": "EUR_USD", "size": 24, "price": 1.1002, "ref": "777"})
        self.assertIn("777", oanda_scanner.own_trades)
        self.assertEqual(self.book.tags({"ref": "777", "symbol": "EUR_USD"})["broadcastId"], 11)
        self.declined(broadcast.OTHER, lambda: self.open(fake, figures))
        self.assertEqual(len(fake.orders), 1, "never twice")

    def test_a_market_outside_the_pool_is_managed_afterwards(self):
        fake = FakeOanda()
        _, figures = self.preview(fake, symbol="gold")
        self.assertEqual(figures["symbol"], "XAU_USD")
        self.assertIn("not in the pool", figures["exits"])
        self.open(fake, figures, symbol="gold")
        self.assertIn("XAU_USD", oanda_scanner.managed(self.POOL))
        self.assertIn("XAU_USD", oanda_scanner.reported(self.POOL))

    def test_its_limits_still_apply(self):
        self.declined(broadcast.NO_SLOT, lambda: self.preview(FakeOanda(positions=["EUR_USD"])))
        with mock.patch.object(oanda_scanner, "MAX_OPEN_POSITIONS", 1), \
                mock.patch.object(oanda_scanner, "guests", {"GBP_USD": oanda_instrument("GBP_USD")}):
            self.declined(broadcast.NO_SLOT, lambda: self.preview(FakeOanda(positions=["GBP_USD"])))
        self.assertIn("smallest trade", self.declined(broadcast.RISK, lambda: self.preview(FakeOanda(), quantity=0.5)))
        self.declined(broadcast.UNAVAILABLE, lambda: self.preview(FakeOanda(), symbol="TSLA"))
        rollover.entries_paused.return_value = True
        self.declined(broadcast.CLOSED, lambda: self.preview(FakeOanda()))


# ---------------------------------------------------------------------------
# Capital.com: sized from the budget slice; no label for an order
# ---------------------------------------------------------------------------
def capital_market(epic, bid=6500.0, offer=6500.4):
    return {"instrument": {"epic": epic, "name": epic.title(), "currency": "GBP", "lotSize": 1},
            "dealingRules": {"minSizeIncrement": {"value": 0.01}, "minDealSize": {"value": 0.01}},
            "snapshot": {"bid": bid, "offer": offer, "marketStatus": "TRADEABLE", "decimalPlacesFactor": 1}}


@needs(capital_scanner, "ig-bot-env")
class CapitalScannerTests(ScannerTestCase):
    bot = capital_scanner
    settings = {"TRADE_EXPOSURE": 120.0, "MAX_OPEN_POSITIONS": 5}
    POOL = {"US500": capital_market("US500")}

    def run_bot(self, call, positions=()):
        posted = []

        def fake_capital(method, path, **kwargs):
            if method == "GET" and path == "/positions":
                return {"positions": [{"market": {"epic": e}, "position": {"dealId": "X", "direction": "BUY", "size": 1,
                                                                          "upl": 0}} for e in positions]}
            posted.append(kwargs.get("json"))
            return {"dealReference": "ref"}

        markets = {"US500": capital_market("US500"), "GOLD": capital_market("GOLD", 2650.0, 2650.3)}
        with mock.patch.object(capital_scanner, "capital", fake_capital), \
                mock.patch.object(capital_scanner, "fetch_markets",
                                  lambda epics: {e: markets[e] for e in epics if e in markets}), \
                mock.patch.object(capital_scanner, "confirm", return_value={
                    "dealStatus": "ACCEPTED", "dealId": "DEAL9", "affectedDeals": [{"dealId": "DEAL9"}],
                    "size": 0.04, "level": 2650.3}):
            return call(), posted

    def test_preview_then_open_a_market_outside_the_pool(self):
        (line, figures), posted = self.run_bot(
            lambda: capital_scanner.broadcast_preview(self.POOL, {}, "GBP", command(symbol="XAUUSD")))
        self.assertEqual(posted, [])
        # A 120 GBP slice of gold at 2650.15: 0.04 (in steps of 0.01).
        self.assertEqual((figures["symbol"], figures["size"]), ("GOLD", 0.04))
        self.assertAlmostEqual(figures["stopLoss"], round(2650.3 * 0.98, 1))
        (line, result), posted = self.run_bot(lambda: capital_scanner.broadcast_open(
            self.POOL, {}, "GBP", command("broadcast-open", symbol="XAUUSD", preview=figures)))
        self.assertEqual(posted[0]["epic"], "GOLD")
        self.assertEqual(posted[0]["size"], 0.04)
        self.assertEqual(result["ref"], "DEAL9")
        self.assertIn("GOLD", capital_scanner.managed(self.POOL))

    def test_its_limits_still_apply(self):
        preview = lambda: capital_scanner.broadcast_preview(self.POOL, {}, "GBP", command(symbol="US500"))  # noqa: E731
        self.declined(broadcast.NO_SLOT, lambda: self.run_bot(preview, positions=["US500"]))
        self.declined(broadcast.UNAVAILABLE, lambda: self.run_bot(
            lambda: capital_scanner.broadcast_preview(self.POOL, {}, "GBP", command(symbol="JPN225X"))))


# ---------------------------------------------------------------------------
# Pepperstone: lots from MT5's valuation; the order's comment names the broadcast
# ---------------------------------------------------------------------------
@needs(pepperstone_scanner, "ig-bot-env")
class PepperstoneScannerTests(ScannerTestCase):
    bot = pepperstone_scanner
    settings = {"TRADE_EXPOSURE": 2000.0, "MAX_OPEN_POSITIONS": 5}

    def run_bot(self, call, own=()):
        mt5 = pepperstone_scanner.mt5
        info = SimpleNamespace(volume_step=0.01, volume_min=0.01, volume_max=100.0, trade_mode=mt5.SYMBOL_TRADE_MODE_FULL,
                               description="Euro vs US Dollar", filling_mode=1, trade_tick_size=0.00001, point=0.00001,
                               digits=5)
        sent = []

        def order_send(request):
            sent.append(request)
            return SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE, volume=request["volume"], price=1.1001, comment="done")

        fakes = {
            "symbols_get": lambda: [SimpleNamespace(name="EURUSD"), SimpleNamespace(name="GBPUSD")],
            "symbol_select": lambda symbol, on: True,
            "symbol_info": lambda symbol: info,
            "symbol_info_tick": lambda symbol: SimpleNamespace(bid=1.1000, ask=1.1001, time=T0 + 3 * 3600),
            "order_calc_profit": lambda *a: 85.0,             # one lot worth 8,500 in the account's currency
            "positions_get": lambda **kw: [SimpleNamespace(symbol=s, magic=pepperstone_scanner.MAGIC) for s in own],
            "order_send": order_send,
        }
        with mock.patch.multiple(mt5, create=True, **fakes):
            return call(), sent

    def test_preview_then_open_labelled(self):
        (line, figures), sent = self.run_bot(lambda: pepperstone_scanner.broadcast_preview({}, "GBP", command()))
        self.assertEqual(sent, [])
        self.assertEqual((figures["symbol"], figures["size"], figures["sizeUnit"]), ("EURUSD", 0.23, "lots"))
        (line, result), sent = self.run_bot(lambda: pepperstone_scanner.broadcast_open(
            {}, "GBP", command("broadcast-open", preview=figures)))
        self.assertEqual((sent[0]["volume"], sent[0]["comment"]), (0.23, "broadcast-11"))
        self.assertEqual(result, {"symbol": "EURUSD", "size": 0.23, "price": 1.1001})
        self.assertIn("EURUSD", pepperstone_scanner.managed({}))

    def test_already_holding_it(self):
        self.declined(broadcast.NO_SLOT, lambda: self.run_bot(
            lambda: pepperstone_scanner.broadcast_preview({}, "GBP", command()), own=["EURUSD"]))


# ---------------------------------------------------------------------------
# IG: always the minimum size; the stop pulled in to IG_MAX_TRADE_LOSS
# ---------------------------------------------------------------------------
@needs(ig_scanner, "ig-bot-env")
class IGScannerTests(ScannerTestCase):
    bot = ig_scanner
    guests = list
    settings = {"MAX_TRADE_LOSS": 25.0, "MAX_POSITIONS": 5}
    POOL = [{"term": "US 500", "epic": "IX.D.SPTRD.IFS.IP", "name": "US 500"}]

    def setUp(self):
        super().setUp()
        self.limits = ig_scanner._LossLimits()
        for patcher in (mock.patch.object(ig_scanner, "loss_limits", self.limits),
                        mock.patch.object(ig_scanner._rate_limiter, "wait", lambda: None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        import pandas as pd
        self.service = mock.Mock()
        self.service.fetch_market_by_epic.return_value = {
            "instrument": {"expiry": "-", "currencies": [{"code": "GBP", "isDefault": True}], "contractSize": 1,
                           "name": "US 500"},
            "dealingRules": {"minDealSize": {"value": 0.5}, "minNormalStopOrLimitDistance": {"value": 1, "unit": "POINTS"}},
            "snapshot": {"scalingFactor": 1, "marketStatus": "TRADEABLE", "bid": 6500.0, "offer": 6500.4}}
        self.service.fetch_open_positions.return_value = pd.DataFrame()
        self.service.create_open_position.return_value = {"dealStatus": "ACCEPTED", "dealId": "DIX1",
                                                          "affectedDeals": [{"dealId": "DIX1"}]}

    def test_preview_then_open(self):
        line, figures = ig_scanner.broadcast_preview(self.service, self.POOL, command(symbol="US500", quantity=5000))
        self.service.create_open_position.assert_not_called()
        self.assertEqual((figures["symbol"], figures["size"], figures["sizeUnit"]), ("IX.D.SPTRD.IFS.IP", 0.5, "contracts"))
        # A 2% stop (130 points at 0.50 GBP a point = 65) pulled in to lose at most IG_MAX_TRADE_LOSS=25: 50 points.
        self.assertEqual(figures["risk"], 25.0)
        self.assertAlmostEqual(figures["stopLoss"], 6500.4 - 50)
        self.assertEqual(figures["capped"], "IG always trades the market's minimum size")
        line, result = ig_scanner.broadcast_open(self.service, self.POOL, command(
            "broadcast-open", symbol="US500", preview=figures))
        kwargs = self.service.create_open_position.call_args.kwargs
        self.assertEqual((kwargs["epic"], kwargs["size"], kwargs["stop_distance"]), ("IX.D.SPTRD.IFS.IP", 0.5, 50.0))
        self.assertEqual(result["ref"], "DIX1")
        self.assertEqual(self.limits.open_positions, 1)

    def test_its_loss_limits_still_apply(self):
        self.assertIn("more than the 100.00 asked for", self.declined(broadcast.RISK, lambda: ig_scanner.broadcast_preview(
            self.service, self.POOL, command(symbol="US500", quantity=100))))
        self.limits.stopped = True
        self.declined(broadcast.PAUSED, lambda: ig_scanner.broadcast_preview(self.service, self.POOL,
                                                                             command(symbol="US500")))


# ---------------------------------------------------------------------------
# Alpaca: buys only, dollars from the slice; the client order ID names the broadcast
# ---------------------------------------------------------------------------
@needs(alpaca_scanner, "alpaca-bot-env")
class AlpacaScannerTests(ScannerTestCase):
    bot = alpaca_scanner
    guests = set
    settings = {"TRADE_NOTIONAL_USD": 20.0, "MAX_OPEN_POSITIONS": 5, "FLAT_MINUTES": 10, "LAST_ENTRY_MINUTES": 30}

    def api(self, is_open=True, minutes_left=120, held=()):
        import pandas as pd
        api = mock.Mock()
        api.get_asset.return_value = SimpleNamespace(**{"class": "us_equity", "tradable": True, "fractionable": True})
        now = pd.Timestamp(T0, unit="s", tz="UTC")
        api.get_clock.return_value = SimpleNamespace(is_open=is_open, timestamp=now, next_open="tomorrow",
                                                     next_close=now + pd.Timedelta(minutes=minutes_left))
        api.list_positions.return_value = [SimpleNamespace(symbol=s) for s in held]
        api.get_latest_quotes.return_value = {"TSLA": SimpleNamespace(bp=199.99, ap=200.01)}
        api.get_account.return_value = SimpleNamespace(non_marginable_buying_power="1000")
        api.submit_order.return_value = SimpleNamespace(id="ORD9")
        return api

    def test_preview_then_buy_a_share_outside_the_pool(self):
        api = self.api()
        line, figures = alpaca_scanner.broadcast_preview(api, ["AAPL"], command(symbol="TSLA"))
        api.submit_order.assert_not_called()
        self.assertEqual((figures["symbol"], figures["exposure"]), ("TSLA", 20.0))  # one $20 slice
        line, result = alpaca_scanner.broadcast_open(api, ["AAPL"], command("broadcast-open", symbol="TSLA",
                                                                            preview=figures))
        kwargs = api.submit_order.call_args.kwargs
        self.assertEqual((kwargs["symbol"], kwargs["notional"], kwargs["side"]), ("TSLA", 20.0, "buy"))
        self.assertTrue(kwargs["client_order_id"].startswith("broadcast-11-"))
        self.assertIn("TSLA", alpaca_scanner.managed(["AAPL"]))
        self.assertEqual(self.book.tags({"symbol": "TSLA", "size": 0.1})["broadcastId"], 11)

    def test_never_sells_and_keeps_its_hours(self):
        self.declined(broadcast.UNAVAILABLE, lambda: alpaca_scanner.broadcast_preview(
            self.api(), [], command(symbol="TSLA", side="sell")))
        self.declined(broadcast.CLOSED, lambda: alpaca_scanner.broadcast_preview(
            self.api(is_open=False), [], command(symbol="TSLA")))
        self.declined(broadcast.CLOSED, lambda: alpaca_scanner.broadcast_preview(
            self.api(minutes_left=20), [], command(symbol="TSLA")))
        self.declined(broadcast.NO_SLOT, lambda: alpaca_scanner.broadcast_preview(
            self.api(held=["TSLA"]), [], command(symbol="TSLA")))


if __name__ == "__main__":
    unittest.main()
