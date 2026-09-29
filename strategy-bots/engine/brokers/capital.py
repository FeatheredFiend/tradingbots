"""
Capital.com demo account through its REST API - the same calls as
capital-momentum-scanner-bot/. Demo endpoint only.

The bot manages only the positions it opened (by deal ID), and won't open
one in a market where anyone else's position is open - unless the account
is in hedging mode, Capital.com nets the two. For bots that trade the same
markets side by side, give each its own demo account
(<BROKER>_<STRATEGY>_ACCOUNT_ID, falling back to CAPITAL_ACCOUNT_ID).
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from .base import Account, Broker, BrokerError, Market, Position, Quote
from ..indicators import Bar

BASE_URL = "https://demo-api-capital.backend-capital.com/api/v1"  # Demo only - do not change.
REQUEST_TIMEOUT_SECONDS = 20
MIN_REQUEST_INTERVAL = 0.15    # Capital.com allows 10 requests a second
RESOLUTION = {"M5": "MINUTE_5", "M15": "MINUTE_15", "M30": "MINUTE_30", "H1": "HOUR", "H4": "HOUR_4"}
TIMEFRAME_SECONDS = {"M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400}
CLOSE_SOURCES = {"SL": "stop-loss", "TP": "take-profit", "CLOSE_OUT": "margin close-out",
                 "DEALER": "closed by Capital.com", "SYSTEM": "closed by Capital.com"}
# The first dashboard report after a start looks back this far, so trades that
# closed while the bot was stopped still reach the dashboard; later ones a day.
HISTORY_BACKFILL_DAYS = 7


def utc_seconds(text: str) -> float:
    """Capital.com's UTC times ('2026-09-28T19:15:00', sometimes with milliseconds) as Unix seconds."""
    return datetime.fromisoformat(text.rstrip("Z")).replace(tzinfo=timezone.utc).timestamp()


def _mid(market: dict) -> float:
    snapshot = market["snapshot"]
    return (float(snapshot["bid"]) + float(snapshot["offer"])) / 2


def _mid_price(price: dict) -> float:
    bid = price.get("bid")
    ask = price.get("ask")
    if bid is None:
        return float(ask)
    return (float(bid) + float(ask if ask is not None else bid)) / 2


class CapitalBroker(Broker):
    key = "capital"
    name = "Capital.com"

    def __init__(self, settings, dashboard, log):
        super().__init__(settings, dashboard, log)
        self.api_key = os.environ.get("CAPITAL_API_KEY", "")
        self.email = os.environ.get("CAPITAL_EMAIL", "")
        self.password = os.environ.get("CAPITAL_EMAIL_PASSWORD", "")
        self.wanted_account = settings.account_id
        self.account_id = None
        self.currency = None
        self.pairs = {}              # currency -> (FX epic, inverted?) valuing it in the account's currency
        self._http = requests.Session()
        self._last_request = 0.0
        self._seen_open = {}         # deal ID -> the position as last seen open, for trades opened over a day ago
        self._backfilled = False     # whether a report has looked back HISTORY_BACKFILL_DAYS yet
        self._last_report_problem = None

    # -- client -------------------------------------------------------------------
    def _login(self) -> None:
        response = self._send("POST", "/session", headers={"X-CAP-API-KEY": self.api_key},
                              json={"identifier": self.email, "password": self.password, "encryptedPassword": False})
        body = self._body(response)
        self._http.headers.update({"CST": response.headers["CST"], "X-SECURITY-TOKEN": response.headers["X-SECURITY-TOKEN"]})
        self.account_id = body["currentAccountId"]
        if self.wanted_account and self.wanted_account != self.account_id:
            self._body(self._send("PUT", "/session", json={"accountId": self.wanted_account}))
            self.account_id = self.wanted_account

    def _call(self, method: str, path: str, **kwargs) -> dict:
        if self.account_id is None:
            self._login()
        response = self._send(method, path, **kwargs)
        if response.status_code == 401:  # session lapsed
            self._login()
            response = self._send(method, path, **kwargs)
        return self._body(response)

    def _send(self, method: str, path: str, **kwargs) -> requests.Response:
        wait = MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        try:
            return self._http.request(method, BASE_URL + path, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
        except requests.RequestException as e:
            raise BrokerError(f"Capital.com unreachable: {e}") from None

    @staticmethod
    def _body(response: requests.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            raise BrokerError(f"HTTP {response.status_code}: {body.get('errorCode') or response.text[:200] or response.reason}")
        return body

    def _fetch_markets(self, epics: list) -> dict:
        markets = {}
        for i in range(0, len(epics), 50):
            body = self._call("GET", "/markets", params={"epics": ",".join(epics[i:i + 50])})
            markets.update({m["instrument"]["epic"]: m for m in body.get("marketDetails", [])})
        return markets

    def _account(self) -> dict:
        accounts = self._call("GET", "/accounts").get("accounts", [])
        account = next((a for a in accounts if a["accountId"] == self.account_id), None)
        if account is None:
            raise BrokerError(f"account {self.account_id} isn't one of this login's accounts")
        return account

    # -- account / markets ----------------------------------------------------
    def connect(self) -> Account:
        if not (self.api_key and self.email and self.password):
            raise BrokerError("Set CAPITAL_API_KEY, CAPITAL_EMAIL and CAPITAL_EMAIL_PASSWORD "
                              "(Capital.com > Settings > API integrations).")
        account = self._account()
        money = account["balance"]
        self.currency = account["currency"]
        warnings = []
        if float(money["available"]) <= 0:
            warnings.append("Nothing is available to trade with: every order will be refused until the demo account "
                            "is topped up on Capital.com.")
        return Account(
            id=self.account_id, currency=self.currency, balance=float(money["deposit"]), equity=float(money["balance"]),
            description=f"Capital.com demo account {self.account_id} | cash {float(money['deposit']):,.2f} "
                        f"{self.currency} | equity {float(money['balance']):,.2f} | available "
                        f"{float(money['available']):,.2f}",
            warnings=warnings,
        )

    def resolve(self, names: list) -> dict:
        wanted = {name.strip().upper(): name for name in names}
        offered = self._fetch_markets(list(wanted))
        markets = {}
        for epic, requested in wanted.items():
            details = offered.get(epic)
            if details is None:
                self.log.warning(f"Skipping {requested}: Capital.com has no market with that epic{self._similar(epic)}.")
                continue
            rules = details["dealingRules"]
            markets[epic] = Market(
                symbol=epic, name=details["instrument"]["name"], requested=requested,
                min_size=float(rules["minDealSize"]["value"]), size_step=float(rules["minSizeIncrement"]["value"]),
                digits=int(details["snapshot"].get("decimalPlacesFactor", 5)), raw=details,
            )
        self.pairs = self._conversion_pairs({m.raw["instrument"]["currency"] for m in markets.values()})
        for epic, market in list(markets.items()):
            currency = market.raw["instrument"]["currency"]
            if currency != self.currency and currency not in self.pairs:
                self.log.warning(f"Skipping {epic}: no {currency}/{self.currency} market to value it in {self.currency}.")
                del markets[epic]
        return markets

    def _similar(self, term: str) -> str:
        try:
            found = self._call("GET", "/markets", params={"searchTerm": term}).get("markets", [])
        except BrokerError:
            return ""
        names = [f"{m['epic']} ({m['instrumentName']})" for m in found[:5]]
        return f" - similar: {', '.join(names)}" if names else ""

    def _conversion_pairs(self, currencies) -> dict:
        wanted = sorted(set(currencies) - {self.currency})
        candidates = [e for c in wanted for e in (f"{c}{self.currency}", f"{self.currency}{c}")]
        offered = self._fetch_markets(candidates) if candidates else {}
        pairs = {}
        for c in wanted:
            if f"{c}{self.currency}" in offered:
                pairs[c] = (f"{c}{self.currency}", False)
            elif f"{self.currency}{c}" in offered:
                pairs[c] = (f"{self.currency}{c}", True)
        return pairs

    # -- prices -----------------------------------------------------------------
    def bars(self, market: Market, timeframe: str, count: int) -> list:
        body = self._call("GET", f"/prices/{market.symbol}",
                          params={"resolution": RESOLUTION[timeframe], "max": min(count + 1, 1000)})
        now, length = time.time(), TIMEFRAME_SECONDS[timeframe]
        bars = []
        for p in body.get("prices", []):
            start = utc_seconds(p["snapshotTimeUTC"])
            if start + length > now or p["closePrice"].get("bid") is None:
                continue  # the bar still forming
            bars.append(Bar(start, _mid_price(p["openPrice"]), _mid_price(p["highPrice"]), _mid_price(p["lowPrice"]),
                            _mid_price(p["closePrice"]), float(p.get("lastTradedVolume") or 0)))
        return bars[-count:]

    def quotes(self, markets: list) -> dict:
        details = self._fetch_markets([m.symbol for m in markets] + [epic for epic, _ in self.pairs.values()])
        rates = {self.currency: 1.0}
        for currency, (epic, inverted) in self.pairs.items():
            if epic in details:
                rate = _mid(details[epic])
                rates[currency] = 1 / rate if inverted else rate
        quotes = {}
        for market in markets:
            d = details.get(market.symbol)
            if d is None or d["snapshot"].get("bid") is None:
                continue
            snapshot = d["snapshot"]
            rate = rates.get(d["instrument"]["currency"])
            status = snapshot.get("marketStatus")
            quotes[market.symbol] = Quote(
                float(snapshot["bid"]), float(snapshot["offer"]), status == "TRADEABLE",
                unit_value=_mid(d) * float(d["instrument"].get("lotSize") or 1) * rate if rate else None,
                why_not="" if status == "TRADEABLE" else f"Capital.com says it's {status}",
            )
            market.raw = d  # keeps the overnight fee current
        return quotes

    def min_stop_distance(self, market: Market, quote: Quote) -> float:
        rule = ((market.raw or {}).get("dealingRules") or {}).get("minStopOrProfitDistance") or {}
        value = float(rule.get("value") or 0)
        # A percentage of the price (0.01% on the FX majors and the US 500), or
        # "points", which on Capital.com are price units (EURUSD's step is 0.00001 points).
        return value / 100 * quote.mid if rule.get("unit") == "PERCENTAGE" else value

    def swap_percent_per_night(self, market: Market, direction: str):
        fee = (market.raw or {}).get("instrument", {}).get("overnightFee") or {}
        rate = fee.get("longRate" if direction == "long" else "shortRate")
        return float(rate) if rate is not None else None  # already a percent per day

    # -- positions / orders -------------------------------------------------------
    def positions(self, markets: dict) -> dict:
        own, others = {}, {}
        for item in self._call("GET", "/positions").get("positions", []):
            epic, p = item["market"]["epic"], item["position"]
            if epic not in markets:
                continue
            is_own = p["dealId"] in self.own_ids
            book = own if is_own else others
            size, opened = float(p["size"]), utc_seconds(p["createdDateUTC"])
            held = book.get(epic)
            if held is None:
                held = book[epic] = Position(
                    epic, "long" if p["direction"] == "BUY" else "short", 0.0, 0.0, opened_at=opened, pnl=0.0,
                    stop=p.get("stopLevel"), take_profit=p.get("profitLevel"), raw=[], own=is_own)
            held.entry = (held.entry * held.size + float(p["level"]) * size) / (held.size + size)
            held.size += size
            held.pnl += float(p.get("upl") or 0)
            held.opened_at = min(held.opened_at, opened)
            held.raw.append(p["dealId"])
        return {**others, **own}

    def _confirm(self, deal_reference: str) -> dict:
        for attempt in range(5):
            try:
                return self._call("GET", f"/confirms/{deal_reference}")
            except BrokerError as e:
                if "not-found" not in str(e) or attempt == 4:
                    raise
                time.sleep(0.5)

    @staticmethod
    def _outcome(confirmation: dict) -> tuple:
        status = confirmation.get("dealStatus", "?")
        if status == "ACCEPTED":
            return True, f"ACCEPTED {confirmation.get('size')} @ {confirmation.get('level')}"
        return False, f"{status} ({confirmation.get('reason', '?')})"

    def open(self, market: Market, direction: str, size: float, stop: float, take_profit, quote: Quote) -> bool:
        order = {"epic": market.symbol, "direction": "BUY" if direction == "long" else "SELL", "size": size,
                 "guaranteedStop": False, "stopLevel": self.round_price(market, stop)}
        if take_profit is not None:
            order["profitLevel"] = self.round_price(market, take_profit)
        try:
            confirmation = self._confirm(self._call("POST", "/positions", json=order)["dealReference"])
        except BrokerError as e:
            self.log.error(f"{market.symbol}: {direction} order refused: {e}")
            return False
        accepted, outcome = self._outcome(confirmation)
        if accepted:
            self.own_ids.update(d["dealId"] for d in confirmation.get("affectedDeals") or () if d.get("dealId"))
            if confirmation.get("dealId"):
                self.own_ids.add(confirmation["dealId"])
        self.log.info(f"{direction.upper()} {market.symbol} size={size:g} stop={order['stopLevel']} "
                      f"take-profit={order.get('profitLevel', 'none')} -> {outcome}")
        return accepted

    def close(self, market: Market, position: Position, reason: str, size: float = None) -> bool:
        if size is not None:
            self.close_problem = "Capital.com's API only closes whole positions"
            return False
        all_closed = True
        for deal_id in position.raw or ():
            try:
                closed, outcome = self._outcome(self._confirm(self._call("DELETE", f"/positions/{deal_id}")["dealReference"]))
            except BrokerError as e:
                self.log.error(f"{market.symbol}: close refused ({reason}): {e}")
                self.close_problem = str(e)
                all_closed = False
                continue
            self.log.info(f"CLOSE {market.symbol} {position.direction} ({reason}) -> {outcome}")
            if closed:
                self.close_reasons[deal_id] = reason
            else:
                self.close_problem = outcome
            all_closed = all_closed and closed
        return all_closed

    def refs(self, position: Position) -> set:
        return set(position.raw or ())

    # -- dashboard ------------------------------------------------------------------
    def report(self, markets: dict, notes: dict) -> None:
        try:
            account = self._account()
            items = [i for i in self._call("GET", "/positions").get("positions", [])
                     if i["market"]["epic"] in markets and i["position"]["dealId"] in self.own_ids]
            for item in items:
                p = item["position"]
                self._seen_open[p["dealId"]] = {"direction": p["direction"], "level": p["level"], "size": p["size"],
                                                "openedAt": utc_seconds(p["createdDateUTC"])}
            trades = self._closed_trades(markets, {i["position"]["dealId"] for i in items})
        except (BrokerError, KeyError, TypeError, ValueError) as e:
            problem = f"{type(e).__name__}: {e}"
            if problem != self._last_report_problem:
                self.log.warning(f"Couldn't gather the dashboard report ({problem}); trading carries on.")
            self._last_report_problem = problem
            return
        self._last_report_problem = None
        money = account["balance"]
        self.dashboard.update(
            account={"balance": float(money["deposit"]), "equity": float(money["balance"]),
                     "unrealizedPl": float(money["profitLoss"])},
            positions=[{
                "ref": i["position"]["dealId"],
                "symbol": i["market"]["epic"],
                "direction": "long" if i["position"]["direction"] == "BUY" else "short",
                "size": float(i["position"]["size"]),
                "entryPrice": float(i["position"]["level"]),
                "currentPrice": i["market"].get("bid") if i["position"]["direction"] == "BUY" else i["market"].get("offer"),
                "pnl": i["position"].get("upl"),
                "stopLoss": i["position"].get("stopLevel"),
                "takeProfit": i["position"].get("profitLevel"),
                "openedAt": utc_seconds(i["position"]["createdDateUTC"]),
            } for i in items],
            trades=trades,
        )

    def _history(self, path: str, key: str, **params) -> list:
        """Capital.com's activity or transaction history for the last day - or,
        on the first report, the last HISTORY_BACKFILL_DAYS days, a day per
        request (the most the activity history takes in one)."""
        if self._backfilled:
            return self._call("GET", path, params={**params, "lastPeriod": 86400}).get(key, [])
        items, seen = [], set()
        end = datetime.now(timezone.utc)
        for day in range(HISTORY_BACKFILL_DAYS):
            window = {"from": (end - timedelta(days=day + 1)).strftime("%Y-%m-%dT%H:%M:%S"),
                      "to": (end - timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%S")}
            for item in self._call("GET", path, params={**params, **window}).get(key, []):
                identity = json.dumps(item, sort_keys=True)  # one on a day boundary comes twice
                if identity not in seen:
                    seen.add(identity)
                    items.append(item)
        return items

    def _closed_trades(self, markets, open_ids: set) -> list:
        """Recent closed trades in the bot's markets, pieced together as
        capital-momentum-scanner-bot/ does: opening and closing deals from the
        activity history, realised profit from the transaction history (by
        deal ID; by market and time for a transaction without one)."""
        activities = self._history("/history/activity", "activities", detailed="true", filter="type==POSITION")
        transactions = self._history("/history/transactions", "transactions", type="TRADE")
        self._backfilled = True
        deals = {}
        for a in activities:
            if (a.get("epic") in markets and a.get("dealId") in self.own_ids and a.get("status") != "REJECTED"
                    and a.get("details")):
                deals.setdefault(a["dealId"], []).append(a)
        by_deal = {}
        for t in transactions:
            if t.get("dealId"):
                by_deal[t["dealId"]] = by_deal.get(t["dealId"], 0.0) + float(t["size"])
        profits = [{"at": utc_seconds(t["dateUtc"]), "market": str(t.get("instrumentName", "")).upper(),
                    "pnl": float(t["size"])} for t in transactions if t.get("dateUtc") and not t.get("dealId")]
        trades = []
        for deal_id, history in deals.items():
            if deal_id in open_ids:
                continue
            history.sort(key=lambda a: a["dateUTC"])
            close = history[-1]
            opening = history[0]["details"] if len(history) > 1 else self._seen_open.get(deal_id)
            if opening is None or opening["direction"] == close["details"]["direction"]:
                continue
            epic, closed_at = close["epic"], utc_seconds(close["dateUTC"])
            entry, exit_price, size = float(opening["level"]), float(close["details"]["level"]), float(opening["size"])
            pnl = by_deal.get(deal_id)
            if pnl is None:
                names = {epic.upper(), str(close["details"].get("marketName", "")).upper()}
                match = min((p for p in profits if p["market"] in names and abs(p["at"] - closed_at) <= 120),
                            key=lambda p: abs(p["at"] - closed_at), default=None)
                if match is not None:
                    profits.remove(match)
                    pnl = match["pnl"]
            source = close.get("source")
            reason = self.close_reasons.get(deal_id, "closed by the bot or by hand") if source == "USER" \
                else CLOSE_SOURCES.get(source, source)
            trades.append({
                "ref": deal_id, "symbol": epic, "direction": "long" if opening["direction"] == "BUY" else "short",
                "size": size, "entryPrice": entry, "exitPrice": exit_price,
                "openedAt": utc_seconds(history[0]["dateUTC"]) if len(history) > 1 else opening.get("openedAt"),
                "closedAt": closed_at, "pnl": None if pnl is None else round(pnl, 2), "closeReason": reason,
            })
        return trades
