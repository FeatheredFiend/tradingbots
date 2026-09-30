"""
Tests for the Alpaca momentum scanner's stop orders at Alpaca and its
closing time, against a fake Alpaca: every position gets one stop order
for all its shares, the bot cancels it before selling, a stop fill is
reported once, and nothing is bought near the close or held past it. No
broker or dashboard is called.

    python -m unittest discover -s strategy-bots/tests   # needs alpaca-trade-api (alpaca-bot-env), else skipped
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ["DASHBOARD_URL"] = ""  # never report test runs to the real dashboard
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "shared"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "alpaca-momentum-scanner-bot"))

import pandas as pd  # noqa: E402

try:
    import alpaca_momentum_scanner_bot as scanner  # noqa: E402
    from alpaca_trade_api.rest import APIError  # noqa: E402
except ImportError:  # run from a venv without alpaca-trade-api
    scanner = None

POOL = ["AAPL", "MSFT", "NVDA"]
NOW = pd.Timestamp("2026-09-30T15:00:00Z")


def http_error(status: int):
    """An APIError as the SDK raises it for an HTTP `status` answer."""
    return APIError({"code": status, "message": "refused"}, SimpleNamespace(response=SimpleNamespace(status_code=status)))


def position(symbol, qty="0.5", entry="100", plpc="0"):
    return SimpleNamespace(symbol=symbol, qty=qty, avg_entry_price=entry, unrealized_plpc=plpc,
                           unrealized_pl="0", current_price=entry)


class FakeAlpaca:
    """Just enough of alpaca_trade_api.REST: orders and positions in memory.
    `fill_on_cancel` names stop orders that fill instead of cancelling."""

    def __init__(self, positions=(), orders=()):
        self.positions = {p.symbol: p for p in positions}
        self.orders = {o.id: o for o in orders}
        self.fill_on_cancel = set()
        self.calls = []
        self.next_id = 0

    def stop(self, symbol, qty, status="new", own=True, filled_at=None, price="99"):
        self.next_id += 1
        order = SimpleNamespace(id=f"o{self.next_id}", symbol=symbol, qty=qty, status=status,
                                client_order_id=(scanner.STOP_ORDER_PREFIX if own else "hand-") + symbol,
                                filled_at=filled_at, filled_avg_price=price, filled_qty=qty)
        self.orders[order.id] = order
        return order

    def list_orders(self, status=None, limit=None, after=None, symbols=None):
        wanted = {"open": ("new", "accepted"), "closed": ("filled", "canceled")}[status]
        return [o for o in self.orders.values() if o.status in wanted and (not symbols or o.symbol in symbols)]

    def submit_order(self, **kwargs):
        self.calls.append(("submit", kwargs["symbol"], kwargs.get("type"), kwargs.get("qty"), kwargs.get("stop_price")))
        if kwargs.get("type") == "stop":
            order = self.stop(kwargs["symbol"], kwargs["qty"])
            order.client_order_id = kwargs["client_order_id"]
            return order
        return SimpleNamespace(id="buy")

    def cancel_order(self, order_id):
        self.calls.append(("cancel", self.orders[order_id].symbol))
        order = self.orders[order_id]
        if order.id in self.fill_on_cancel:
            order.status = "filled"
            self.positions.pop(order.symbol, None)
        else:
            order.status = "canceled"

    def get_order(self, order_id):
        return self.orders[order_id]

    def close_position(self, symbol, qty=None):
        held = [o for o in self.orders.values() if o.symbol == symbol and o.status == "new"]
        if held:
            raise APIError({"message": "insufficient qty available for order"})
        self.calls.append(("close", symbol))
        self.positions.pop(symbol, None)
        return SimpleNamespace(id=f"close-{symbol}")

    def get_position(self, symbol):
        return self.positions[symbol]

    def get_clock(self):
        return SimpleNamespace(is_open=True)


@unittest.skipIf(scanner is None, "needs alpaca-trade-api (run from alpaca-bot-env)")
class StopOrderTests(unittest.TestCase):
    def test_every_pool_position_gets_one_stop_for_all_its_shares(self):
        api = FakeAlpaca([position("AAPL", "0.25", "200"), position("KOD", "10")])
        stops = scanner.protect(api, POOL, api.positions, scanner.own_stops(api))
        self.assertEqual(list(stops), ["AAPL"])  # KOD isn't the bot's
        _, symbol, kind, qty, price = api.calls[0]
        self.assertEqual((symbol, kind, qty), ("AAPL", "stop", "0.25"))
        self.assertAlmostEqual(float(price), 200 * (1 - scanner.STOP_LOSS_PCT))
        # A second loop changes nothing.
        api.calls.clear()
        scanner.protect(api, POOL, api.positions, scanner.own_stops(api))
        self.assertEqual(api.calls, [])

    def test_replaces_a_stop_that_no_longer_covers_the_position(self):
        api = FakeAlpaca([position("AAPL", "0.1")])
        old = api.stop("AAPL", "0.25")  # before a part-close from the dashboard
        stops = scanner.protect(api, POOL, api.positions, scanner.own_stops(api))
        self.assertEqual(old.status, "canceled")
        self.assertEqual([o.qty for o in stops["AAPL"]], ["0.1"])

    def test_cancels_a_stop_whose_position_is_gone_but_never_anyone_elses(self):
        api = FakeAlpaca()
        mine, theirs = api.stop("MSFT", "0.2"), api.stop("MSFT", "3", own=False)
        self.assertEqual(scanner.protect(api, POOL, api.positions, scanner.own_stops(api)), {})
        self.assertEqual((mine.status, theirs.status), ("canceled", "new"))

    def test_a_refused_stop_order_leaves_the_bot_watching_the_stop(self):
        api = FakeAlpaca([position("AAPL")])
        api.submit_order = mock.Mock(side_effect=http_error(422))
        with mock.patch.object(scanner, "REFUSED_STOPS", set()):
            self.assertEqual(scanner.protect(api, POOL, api.positions, {}), {})
            self.assertEqual(scanner.protect(api, POOL, api.positions, {}), {})
        self.assertEqual(api.submit_order.call_count, 1)  # not asked again every loop
        loss = position("AAPL", plpc=str(-scanner.STOP_LOSS_PCT - 0.001))
        self.assertTrue(scanner.exit_reason(loss, None, watch_stop=True).startswith("stop-loss"))
        self.assertEqual(scanner.exit_reason(loss, None, watch_stop=False), "")

    def test_a_rate_limit_is_tried_again_next_loop(self):
        api = FakeAlpaca([position("AAPL")])
        api.submit_order = mock.Mock(side_effect=http_error(429))
        with mock.patch.object(scanner, "REFUSED_STOPS", set()):
            scanner.protect(api, POOL, api.positions, {})
            scanner.protect(api, POOL, api.positions, {})
        self.assertEqual(api.submit_order.call_count, 2)

    def test_cancels_the_stop_before_selling(self):
        api = FakeAlpaca([position("AAPL")])
        stop = api.stop("AAPL", "0.5")
        self.assertTrue(scanner.close_open_position(api, "AAPL", "take-profit", api.positions["AAPL"], [stop]))
        self.assertEqual(api.calls, [("cancel", "AAPL"), ("close", "AAPL")])

    def test_a_stop_that_fills_first_leaves_nothing_to_sell(self):
        api = FakeAlpaca([position("AAPL")])
        stop = api.stop("AAPL", "0.5")
        api.fill_on_cancel.add(stop.id)
        self.assertTrue(scanner.close_open_position(api, "AAPL", "take-profit", api.positions["AAPL"], [stop]))
        self.assertNotIn(("close", "AAPL"), api.calls)

    def test_dashboard_close_cancels_the_stop_first(self):
        api = FakeAlpaca([position("AAPL")])
        api.stop("AAPL", "0.5")
        answer = scanner.close_from_dashboard(api, POOL, "AAPL", None, "long", None)
        self.assertIn("close-AAPL", answer)
        self.assertEqual(api.calls, [("cancel", "AAPL"), ("close", "AAPL")])

    def test_reports_a_stop_fill_once_and_only_its_own(self):
        api = FakeAlpaca()
        api.stop("AAPL", "0.5", status="filled", filled_at="2026-09-30T15:00:30Z", price="98")
        api.stop("MSFT", "0.2", status="filled", filled_at="2026-09-30T15:00:30Z", own=False)
        api.stop("NVDA", "0.3", status="filled", filled_at="2026-09-30T13:40:00Z")  # an earlier trade's
        gone = {s: position(s, entry="100") for s in ("AAPL", "MSFT", "NVDA")}
        with mock.patch.object(scanner.dashboard, "trade") as trade:
            self.assertEqual(scanner.report_stop_fills(api, gone, NOW), ["AAPL"])
        report = trade.call_args.args[0]
        self.assertEqual((report["symbol"], report["exitPrice"], report["closeReason"]), ("AAPL", 98.0, "stop-loss"))
        self.assertAlmostEqual(report["pnl"], -1.0)

    def test_with_a_stop_order_the_bot_leaves_the_stop_to_alpaca(self):
        loss = str(-scanner.STOP_LOSS_PCT - 0.001)
        api = FakeAlpaca([position("AAPL", plpc=loss)])
        stops = {"AAPL": [api.stop("AAPL", "0.5")]}
        scanner.scan_pool(api, POOL, api.positions, stops, {}, {})
        self.assertEqual(api.calls, [])
        api = FakeAlpaca([position("AAPL", plpc=loss)])  # no stop order there
        scanner.scan_pool(api, POOL, api.positions, {}, {}, {})
        self.assertEqual(api.calls, [("close", "AAPL")])


@unittest.skipIf(scanner is None, "needs alpaca-trade-api (run from alpaca-bot-env)")
class ClosingTimeTests(unittest.TestCase):
    @staticmethod
    def clock(minutes_left):
        return SimpleNamespace(timestamp=NOW, next_close=NOW + pd.Timedelta(minutes=minutes_left))

    def test_sells_in_the_last_ten_minutes_and_buys_nothing_in_the_last_thirty(self):
        with mock.patch.multiple(scanner, FLAT_MINUTES=10, LAST_ENTRY_MINUTES=30):
            self.assertFalse(scanner.closing_time(self.clock(11)))
            self.assertTrue(scanner.closing_time(self.clock(10)))
            self.assertFalse(scanner.buys_paused(self.clock(31)))
            self.assertTrue(scanner.buys_paused(self.clock(30)))

    def test_zero_switches_each_part_off(self):
        with mock.patch.multiple(scanner, FLAT_MINUTES=0, LAST_ENTRY_MINUTES=0):
            self.assertFalse(scanner.closing_time(self.clock(1)))
            self.assertFalse(scanner.buys_paused(self.clock(1)))
        with mock.patch.multiple(scanner, FLAT_MINUTES=10, LAST_ENTRY_MINUTES=0):
            self.assertTrue(scanner.buys_paused(self.clock(10)))  # never buys what it would sell at once

    def test_no_buys_when_paused(self):
        api = FakeAlpaca()
        bars = {"AAPL": (NOW, [1.0, 2.0, 3.0, 4.0])}
        with mock.patch.object(scanner, "submit_buy", return_value=True) as buy:
            self.assertFalse(scanner.scan_pool(api, POOL, {}, {}, bars, {}, buys_allowed=False))
            buy.assert_not_called()
            with mock.patch.object(scanner, "has_cash_for_a_slice", return_value=True):
                self.assertTrue(scanner.scan_pool(api, POOL, {}, {}, bars, {}))
            buy.assert_called_once_with(api, "AAPL")


if __name__ == "__main__":
    unittest.main()
