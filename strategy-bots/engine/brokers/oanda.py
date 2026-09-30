"""
OANDA fxTrade Practice through the v20 REST API - the same calls as
oanda-momentum-scanner-bot/. Practice endpoint only.

The bot manages only the trades it opened (by OANDA trade ID). OANDA nets
buys and sells of a market together, though, so it won't open a trade in
a market where anyone else's position is open; for bots that trade the
same markets side by side, give each an OANDA sub-account of its own
(<BROKER>_<STRATEGY>_ACCOUNT_ID).
"""

import os
import re

import requests

from .base import Account, Broker, BrokerError, Market, Position, Quote
from ..indicators import Bar

BASE_URL = "https://api-fxpractice.oanda.com"  # Practice (demo) only - do not change.
REQUEST_TIMEOUT_SECONDS = 20
GRANULARITY = {"M5": "M5", "M15": "M15", "M30": "M30", "H1": "H1", "H4": "H4", "D": "D"}  # D: 17:00-17:00 New York


class OandaBroker(Broker):
    key = "oanda"
    name = "OANDA"

    def __init__(self, settings, dashboard, log):
        super().__init__(settings, dashboard, log)
        self.token = os.environ.get("OANDA_API_TOKEN", "")
        self.account_id = settings.account_id
        self.currency = None
        self._http = requests.Session()
        self._http.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept-Datetime-Format": "UNIX",  # times as epoch seconds
        })

    def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self._http.request(method, BASE_URL + path, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
        except requests.RequestException as e:
            raise BrokerError(f"OANDA unreachable: {e}") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            reject = next((v for k, v in body.items() if k.endswith("RejectTransaction")), {})
            reason = reject.get("rejectReason") or body.get("errorCode")
            message = body.get("errorMessage") or response.text[:200] or response.reason
            raise BrokerError(f"HTTP {response.status_code}: {message}" + (f" ({reason})" if reason else ""))
        return body

    # -- account / markets ----------------------------------------------------
    def connect(self) -> Account:
        if not self.token:
            raise BrokerError("Set OANDA_API_TOKEN (OANDA hub > Tools > API > Generate, while logged into the "
                              "practice account).")
        if not self.account_id:
            accounts = self._call("GET", "/v3/accounts")["accounts"]
            if len(accounts) != 1:
                ids = ", ".join(a["id"] for a in accounts) or "none"
                raise BrokerError(f"this token can see {len(accounts)} accounts ({ids}); set "
                                  f"{self.settings.env_prefix}ACCOUNT_ID or OANDA_ACCOUNT_ID to pick one")
            self.account_id = accounts[0]["id"]
        summary = self._call("GET", f"/v3/accounts/{self.account_id}/summary")["account"]
        self.currency = summary["currency"]
        return Account(
            id=self.account_id, currency=self.currency, balance=float(summary["balance"]), equity=float(summary["NAV"]),
            description=f"OANDA practice account {self.account_id} | balance {float(summary['balance']):,.2f} "
                        f"{self.currency} | NAV {float(summary['NAV']):,.2f} | margin available "
                        f"{float(summary['marginAvailable']):,.2f}",
        )

    def resolve(self, names: list) -> dict:
        offered = {i["name"]: i for i in self._call("GET", f"/v3/accounts/{self.account_id}/instruments")["instruments"]}
        markets = {}
        for requested in names:
            symbol = requested.upper()
            instrument = offered.get(symbol)
            if instrument is None:
                stem = re.sub(r"\d+$", "", symbol.split("_")[0])
                similar = sorted(n for n in offered if n.startswith(stem))
                self.log.warning(f"Skipping {requested}: this account doesn't offer it"
                                 + (f" - similar: {', '.join(similar)}" if similar else "") + ".")
                continue
            markets[symbol] = Market(
                symbol=symbol, name=instrument.get("displayName", symbol), requested=requested,
                min_size=float(instrument["minimumTradeSize"]), size_step=10.0 ** -int(instrument["tradeUnitsPrecision"]),
                digits=int(instrument["displayPrecision"]), raw=instrument,
            )
        return markets

    # -- prices -----------------------------------------------------------------
    def bars(self, market: Market, timeframe: str, count: int) -> list:
        body = self._call("GET", f"/v3/instruments/{market.symbol}/candles",
                          params={"price": "M", "granularity": GRANULARITY[timeframe], "count": min(count + 1, 5000)})
        return [
            Bar(float(c["time"]), float(c["mid"]["o"]), float(c["mid"]["h"]), float(c["mid"]["l"]),
                float(c["mid"]["c"]), float(c.get("volume") or 0))
            for c in body.get("candles", []) if c.get("complete")
        ][-count:]

    def quotes(self, markets: list) -> dict:
        body = self._call("GET", f"/v3/accounts/{self.account_id}/pricing",
                          params={"instruments": ",".join(m.symbol for m in markets), "includeHomeConversions": "true"})
        to_home = {c["currency"]: float(c["positionValue"]) for c in body.get("homeConversions", [])}
        quotes = {}
        for p in body.get("prices", []):
            bid = float(p["bids"][0]["price"]) if p.get("bids") else float(p["closeoutBid"])
            ask = float(p["asks"][0]["price"]) if p.get("asks") else float(p["closeoutAsk"])
            quote_currency = p["instrument"].rsplit("_", 1)[1]  # EUR_USD is priced in USD, UK100_GBP in GBP
            rate = 1.0 if quote_currency == self.currency else (
                to_home.get(quote_currency) or float(p["quoteHomeConversionFactors"]["positiveUnits"]))
            tradeable = p.get("tradeable", p.get("status") == "tradeable")
            quotes[p["instrument"]] = Quote(bid, ask, tradeable, unit_value=(bid + ask) / 2 * rate,
                                            why_not="" if tradeable else "OANDA says it isn't tradeable right now")
        return quotes

    def swap_percent_per_night(self, market: Market, direction: str):
        financing = (market.raw or {}).get("financing") or {}
        rate = financing.get("longRate" if direction == "long" else "shortRate")
        return float(rate) * 100 / 365 if rate is not None else None  # OANDA's rates are yearly

    # -- positions / orders -------------------------------------------------------
    def positions(self, markets: dict) -> dict:
        own, others = {}, {}
        for t in self._call("GET", f"/v3/accounts/{self.account_id}/openTrades").get("trades", []):
            symbol = t["instrument"]
            if symbol not in markets:
                continue
            is_own = t["id"] in self.own_ids
            book = own if is_own else others
            units = float(t["currentUnits"])
            held = book.get(symbol)
            if held is None:
                held = book[symbol] = Position(
                    symbol, "long" if units > 0 else "short", 0.0, 0.0, opened_at=float(t["openTime"]), pnl=0.0,
                    stop=float(t["stopLossOrder"]["price"]) if t.get("stopLossOrder") else None,
                    take_profit=float(t["takeProfitOrder"]["price"]) if t.get("takeProfitOrder") else None,
                    raw=[], own=is_own)
            held.entry = (held.entry * held.size + float(t["price"]) * abs(units)) / (held.size + abs(units))
            held.size += abs(units)
            held.pnl += float(t.get("unrealizedPL") or 0)
            held.opened_at = min(held.opened_at, float(t["openTime"]))
            held.raw.append((t["id"], abs(units)))
        return {**others, **own}

    def refs(self, position: Position) -> set:
        return {trade_id for trade_id, _ in position.raw or ()}

    def open(self, market: Market, direction: str, size: float, stop: float, take_profit, quote: Quote) -> bool:
        sign = 1 if direction == "long" else -1
        precision = int(market.raw["tradeUnitsPrecision"])
        order = {
            "type": "MARKET",
            "instrument": market.symbol,
            "units": f"{sign * size:.{precision}f}",
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": f"{stop:.{market.digits}f}"},
        }
        if take_profit is not None:
            order["takeProfitOnFill"] = {"price": f"{take_profit:.{market.digits}f}"}
        try:
            body = self._call("POST", f"/v3/accounts/{self.account_id}/orders", json={"order": order})
        except BrokerError as e:
            self.log.error(f"{market.symbol}: {direction} order refused: {e}")
            return False
        filled, outcome = _fill_outcome(body, "order")
        opened = (body.get("orderFillTransaction") or {}).get("tradeOpened") or {}
        if opened.get("tradeID"):
            self.own_ids.add(opened["tradeID"])
        self.log.info(f"{direction.upper()} {market.symbol} units={order['units']} stop={order['stopLossOnFill']['price']} "
                      f"take-profit={order.get('takeProfitOnFill', {}).get('price', 'none')} -> {outcome}")
        return filled

    def close(self, market: Market, position: Position, reason: str, size: float = None) -> bool:
        """Trade by trade - closing the whole position would close any other
        bot's trades in the same direction too. A partial close takes whole
        trades first and the rest out of the next one."""
        all_closed = True
        left = size
        precision = int(market.raw["tradeUnitsPrecision"])
        for trade_id, units in position.raw or ():
            if left is not None and left <= 0:
                break
            body = {}
            if left is not None and left < units:
                body = {"units": f"{left:.{precision}f}"}
            try:
                reply = self._call("PUT", f"/v3/accounts/{self.account_id}/trades/{trade_id}/close",
                                   **({"json": body} if body else {}))
            except BrokerError as e:
                self.log.error(f"{market.symbol}: close of trade {trade_id} refused ({reason}): {e}")
                self.close_problem = str(e)
                all_closed = False
                continue
            filled, outcome = _fill_outcome(reply, "order")
            self.log.info(f"CLOSE {market.symbol} {position.direction} trade {trade_id} "
                          f"{body.get('units', 'all')} ({reason}) -> {outcome}")
            if filled and not body:
                self.close_reasons[trade_id] = reason
            if not filled:
                self.close_problem = outcome
            if left is not None:
                left -= min(left, units)
            all_closed = all_closed and filled
        return all_closed

    # -- dashboard ------------------------------------------------------------------
    def report(self, markets: dict, notes: dict) -> None:
        try:
            summary = self._call("GET", f"/v3/accounts/{self.account_id}/summary")["account"]
            open_trades = self._call("GET", f"/v3/accounts/{self.account_id}/openTrades").get("trades", [])
            closed = self._call("GET", f"/v3/accounts/{self.account_id}/trades",
                                params={"state": "CLOSED", "count": 50}).get("trades", [])
        except BrokerError:
            return
        self.dashboard.update(
            account={"balance": float(summary["balance"]), "equity": float(summary["NAV"]),
                     "unrealizedPl": float(summary["unrealizedPL"])},
            positions=[self._dashboard_trade(t) for t in open_trades if t["id"] in self.own_ids],
            trades=[self._dashboard_trade(t) for t in closed if t["id"] in self.own_ids],
        )

    def _dashboard_trade(self, trade: dict) -> dict:
        units = float(trade.get("initialUnits") or trade.get("currentUnits") or 0)
        row = {"ref": trade["id"], "symbol": trade["instrument"], "direction": "long" if units > 0 else "short",
               "size": abs(units), "entryPrice": float(trade["price"]), "openedAt": trade.get("openTime")}
        if trade.get("state") == "CLOSED":
            if (trade.get("stopLossOrder") or {}).get("state") == "FILLED":
                reason = "stop-loss"
            elif (trade.get("takeProfitOrder") or {}).get("state") == "FILLED":
                reason = "take-profit"
            else:
                reason = self.close_reasons.get(trade["id"], "closed by the bot or by hand")
            row.update(exitPrice=float(trade["averageClosePrice"]) if trade.get("averageClosePrice") else None,
                       closedAt=trade.get("closeTime"),
                       pnl=float(trade.get("realizedPL") or 0) + float(trade.get("financing") or 0), closeReason=reason)
        else:
            row.update(size=abs(float(trade.get("currentUnits") or units)), pnl=float(trade.get("unrealizedPL") or 0),
                       stopLoss=float(trade["stopLossOrder"]["price"]) if trade.get("stopLossOrder") else None,
                       takeProfit=float(trade["takeProfitOrder"]["price"]) if trade.get("takeProfitOrder") else None)
        return row


def _fill_outcome(body: dict, prefix: str) -> tuple:
    """(filled?, e.g. 'FILLED 23 @ 1.08512' or 'CANCELLED (INSUFFICIENT_MARGIN)')."""
    fill = body.get(f"{prefix}FillTransaction")
    if fill:
        return True, f"FILLED {fill.get('units')} @ {fill.get('fullVWAP') or fill.get('price')}"
    cancel = body.get(f"{prefix}CancelTransaction")
    if cancel:
        return False, f"CANCELLED ({cancel.get('reason', '?')})"
    return False, "no fill reported"
