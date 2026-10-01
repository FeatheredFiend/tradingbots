"""
IG demo account through the trading-ig library - the same calls as
ig-momentum-scanner-bot/. DEMO only.

Differences from the other brokers, all IG's:
- Every trade is the market's minimum deal size, as with the other IG
  bots - IG's smallest trade is thousands of pounds of exposure, so the
  budget and risk-per-trade settings don't apply here.
- Bars come from IG's /prices, which allows 10,000 data points a week.
  The runner fetches a market's history once and then only the newest
  couple of bars, which keeps these bots to roughly 1,000-2,500 points a
  week each. Shares aren't available at all (IG doesn't license their
  prices over the API) - these strategies don't use any.
- IG allows ~30 non-trading requests a minute for the whole account;
  every call here is paced to IG_REQUESTS_PER_MINUTE (default 28). Split
  it between IG bots running at once.
- The bot manages only the positions it opened (by deal ID), and doesn't
  trade a market while anyone else's position is open in it.

Untested against a real IG account: IG hasn't granted this account API
access. It uses only calls the IG scanner already makes, plus /prices.
"""

import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

from trading_ig import IGService

from .base import Account, Broker, BrokerError, Market, Position, Quote
from ..indicators import Bar

# trading-ig logs every request at INFO; that would bury the bot's own log
# (and fill the dashboard). The history allowance is reported below instead.
logging.getLogger("trading_ig").setLevel(logging.WARNING)
ALLOWANCE_WARNING = 1500   # warn when fewer history points than this are left this week

ACCOUNT_TYPE = "DEMO"  # Hardcoded - never a live account.
RESOLUTION = {"M5": "5Min", "M15": "15Min", "M30": "30Min", "H1": "1h", "H4": "4h"}
TIMEFRAME_SECONDS = {"M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400}
EXPECTED_TYPE = {"session-breakout": "CURRENCIES", "index-reversion": "INDICES", "commodity-trend": "COMMODITIES"}
NOT_THE_MARKET_MARKERS = ["Leverage", "GraniteShares", "IncomeShares", "ETP", "ETF",
                          "Rights Issue", "NOT IN USE", "Weekend"]
CONTRACT_SIZE_PATTERN = r"\((?:£|\$|€|E|GBP|USD|EUR)?([\d.]+)\s*(?:oz|Contract)?\)"
TRADES_EVERY_SECONDS = 300


def _normalized_name(name: str) -> str:
    name = re.sub(r"\([^)]*\)", " ", name.lower())
    name = re.sub(r"\b(?:cash|mini)\b", " ", name)
    return " ".join(name.split())


def _contract_size(name: str) -> float:
    match = re.search(CONTRACT_SIZE_PATTERN, name)
    return float(match.group(1)) if match else float("inf")


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _utc_seconds(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.rstrip("Z").replace("/", "-")).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _history_market_name(name) -> str:
    """A transaction in a market dealt in another currency names it e.g.
    'GBP/EUR Mini converted at 0.864586683820944'."""
    return re.sub(r"\s+converted at [\d.]+$", "", str(name or ""))


def _mid(price: dict):
    bid, ask = _number(price.get("bid")), _number(price.get("ask"))
    if bid is None or ask is None:
        return bid if ask is None else ask
    return (bid + ask) / 2


class IGBroker(Broker):
    key = "ig"
    name = "IG"
    account_mode = "demo" if ACCOUNT_TYPE == "DEMO" else "live"
    size_unit = "contracts"
    fixed_min_size = True
    metered_history = True
    dashboard_every = 60        # each snapshot costs two of IG's ~30 requests a minute
    min_poll_seconds = 10       # the scalper's read (prices + positions) costs two of them too

    def __init__(self, settings, dashboard, log):
        super().__init__(settings, dashboard, log)
        self.username = os.environ.get("IG_USERNAME", "")
        self.password = os.environ.get("IG_PASSWORD", "")
        self.api_key = os.environ.get("IG_API_KEY", "")
        self.currency_code = os.environ.get("IG_CURRENCY_CODE", "GBP")
        self._interval = 60.0 / max(1, int(os.environ.get("IG_REQUESTS_PER_MINUTE", "28") or 28))
        self._last_call = 0.0
        self._service = None
        self.account_id = None
        self._last_trades_fetch = 0.0
        self._allowance_warned = None

    # -- client -------------------------------------------------------------------
    def _login(self) -> None:
        self._service = IGService(self.username, self.password, self.api_key, ACCOUNT_TYPE)
        try:
            session = self._service.create_session()
        except Exception as e:
            raise BrokerError(f"IG login failed: {e}") from None
        self.account_id = (session or {}).get("currentAccountId") or self.account_id

    def _call(self, what: str, fn, *args, paced: bool = True, **kwargs):
        """One trading-ig call: paced to IG's limit, logged in again once if
        the session has lapsed (IG's v2 tokens expire), errors as BrokerError."""
        for attempt in (1, 2):
            if paced:
                wait = self._interval - (time.monotonic() - self._last_call)
                if wait > 0:
                    time.sleep(wait)
                self._last_call = time.monotonic()
            try:
                return fn(self._service, *args, **kwargs)
            except Exception as e:
                text = str(e) or "empty response - likely rate limited"
                if attempt == 1 and re.search(r"token|security|401|unauthori[sz]ed.*session", text, re.I):
                    self.log.info(f"IG session lapsed ({text}); logging in again.")
                    self._login()
                    continue
                raise BrokerError(f"{what}: {text}") from None

    # -- account / markets ----------------------------------------------------
    def connect(self) -> Account:
        if not (self.username and self.password and self.api_key):
            raise BrokerError("Set IG_USERNAME, IG_PASSWORD and IG_API_KEY.")
        self._login()
        account = self._fetch_account() or {"balance": 0.0, "equity": 0.0}
        return Account(
            id=str(self.account_id), currency=self.currency_code, balance=account["balance"], equity=account["equity"],
            description=f"IG demo account {self.account_id} | balance {account['balance']:,.2f} | every trade is the "
                        f"market's minimum deal size",
        )

    def resolve(self, names: list) -> dict:
        expected = EXPECTED_TYPE.get(self.settings.strategy)
        markets = {}
        for requested in names:
            if ":" in requested:
                term, epic = (s.strip() for s in requested.split(":", 1))
            else:
                term, epic = requested, self._search(requested, expected)
                if epic is None:
                    continue
            try:
                details = self._call(f"market details for {epic}", IGService.fetch_market_by_epic, epic)
            except BrokerError as e:
                self.log.warning(f"Skipping '{term}': {e}")
                continue
            instrument, rules, snapshot = details["instrument"], details["dealingRules"], details["snapshot"]
            minimum = float(rules["minDealSize"]["value"])
            markets[epic] = Market(symbol=epic, name=instrument.get("name") or term, requested=requested,
                                   min_size=minimum, size_step=minimum, digits=snapshot.get("decimalPlacesFactor"),
                                   raw=details)
            self.log.info(f"Resolved '{term}' -> {epic} ({instrument.get('name')}), minimum deal {minimum:g}")
        return markets

    def _search(self, term: str, expected_type):
        """The IG scanner's soft resolution: the plainest, undated, smallest-contract match, or None."""
        try:
            found = self._call(f"search for '{term}'", IGService.search_markets, term)
        except BrokerError as e:
            self.log.warning(f"Skipping '{term}': {e}")
            return None
        if found is None or len(found) == 0:
            self.log.warning(f"Skipping '{term}': no markets found.")
            return None

        def narrow(frame, mask):
            narrowed = frame[mask]
            return narrowed if len(narrowed) > 0 else frame

        candidates = found
        if expected_type and "instrumentType" in candidates:
            candidates = narrow(candidates, candidates["instrumentType"] == expected_type)
        if "instrumentName" in candidates:
            noise = "|".join(re.escape(m) for m in NOT_THE_MARKET_MARKERS)
            candidates = narrow(candidates, ~candidates["instrumentName"].str.contains(noise, case=False, regex=True))
            candidates = narrow(candidates, candidates["instrumentName"].map(_normalized_name) == _normalized_name(term))
        if "expiry" in candidates:
            candidates = narrow(candidates, candidates["expiry"].isin(["-", "DFB"]))
        if len(candidates) > 1 and "instrumentName" in candidates:
            order = candidates["instrumentName"].map(
                lambda n: (len(_normalized_name(n)), _contract_size(n), 0 if "mini" in n.lower() else 1))
            candidates = candidates.loc[order.sort_values(kind="stable").index]
        row = candidates.iloc[0]
        if len(candidates) > 1:
            listed = "; ".join(f"{r['epic']} = {r.get('instrumentName', '?')}" for _, r in candidates.iterrows())
            self.log.info(f"'{term}' had {len(candidates)} matches - picked {row['epic']}. If wrong, write it as "
                          f"'{term}:EPIC'. Candidates: {listed}")
        return row["epic"]

    # -- prices -----------------------------------------------------------------
    def bars(self, market: Market, timeframe: str, count: int) -> list:
        data = self._call(f"{timeframe} prices for {market.symbol}", IGService.fetch_historical_prices_by_epic,
                          market.symbol, resolution=RESOLUTION[timeframe], numpoints=count + 1, pagesize=0,
                          format=lambda prices, version: prices, wait=0)
        allowance = (data.get("metadata") or {}).get("allowance") or {}
        remaining = allowance.get("remainingAllowance")
        if remaining is not None and remaining < ALLOWANCE_WARNING and (
                self._allowance_warned is None or remaining <= self._allowance_warned - 250):
            self._allowance_warned = remaining
            resets = allowance.get("allowanceExpiry")
            self.log.warning(f"IG price history: only {remaining} of its {allowance.get('totalAllowance', '?')} data "
                             f"points a week left" + (f", resetting in {resets / 3600:.0f}h" if resets else "")
                             + " - shared by every IG bot and the backtest.")
        now, length = time.time(), TIMEFRAME_SECONDS[timeframe]
        bars = []
        for p in data.get("prices", []):
            start = _utc_seconds(p.get("snapshotTimeUTC"))
            values = [_mid(p.get(k) or {}) for k in ("openPrice", "highPrice", "lowPrice", "closePrice")]
            if start is None or start + length > now or None in values:
                continue
            bars.append(Bar(start, *values, float(p.get("lastTradedVolume") or 0)))
        return bars[-count:]

    def quotes(self, markets: list) -> dict:
        """Every market's snapshot in one request (up to 50), matched by epic -
        IG doesn't return them in the order asked."""
        quotes = {}
        for i in range(0, len(markets), 50):
            batch = markets[i:i + 50]
            epics = ",".join(m.symbol for m in batch)
            try:
                found = self._call(f"prices for {epics}", IGService.fetch_markets_by_epics, epics, detailed=False)
            except BrokerError as e:
                self.log.warning(str(e))
                continue
            snapshots = {(item.get("instrument") or {}).get("epic"): item.get("snapshot") or {} for item in found or ()}
            for market in batch:
                snapshot = snapshots.get(market.symbol)
                if snapshot is None:
                    continue
                bid, offer = _number(snapshot.get("bid")), _number(snapshot.get("offer"))
                if bid is None or offer is None:
                    continue
                status = snapshot.get("marketStatus")
                market.raw["snapshot"] = snapshot  # the rest of market.raw (dealing rules, currencies) stays as resolved
                quotes[market.symbol] = Quote(bid, offer, status == "TRADEABLE",
                                              why_not="" if status == "TRADEABLE" else f"IG says it's {status}")
        return quotes

    def min_stop_distance(self, market: Market, quote: Quote) -> float:
        rule = (market.raw.get("dealingRules") or {}).get("minNormalStopOrLimitDistance") or {}
        value = _number(rule.get("value")) or 0.0
        if rule.get("unit") == "PERCENTAGE":
            return value / 100 * quote.mid
        # IG's points: 1 pip on currencies (scaling factor 10,000), 1.0 on indices.
        return value / (_number(market.raw["snapshot"].get("scalingFactor")) or 1.0)

    # -- positions / orders -------------------------------------------------------
    def _open_positions(self):
        return self._call("open positions", IGService.fetch_open_positions)

    def _pnl(self, row, market: Market):
        """An open deal's profit in the account's currency - IG's REST API
        doesn't give it. The move x size x contract size, in the deal's
        currency, converted at the rate IG gave when the market was resolved
        (its market details list each currency's "baseExchangeRate")."""
        is_long = row["direction"] == "BUY"
        close, level = _number(row["bid"] if is_long else row["offer"]), _number(row["level"])
        size, contract_size = _number(row["size"]), _number(row.get("contractSize"))
        currency = row.get("currency")
        rate = 1.0 if currency == self.currency_code else next(
            (_number(c.get("baseExchangeRate")) for c in (market.raw.get("instrument") or {}).get("currencies") or ()
             if c.get("code") == currency), None)
        if None in (close, level, size, contract_size) or not rate:
            return None
        return round((close - level) * (1 if is_long else -1) * size * contract_size / rate, 2)

    def risk_money(self, market: Market, quote, size: float, distance: float):
        """As _pnl() values a move: distance x size x contract size, in the
        deal's currency, at IG's rate for it (IG's quotes carry no unit value)."""
        instrument = (market.raw or {}).get("instrument") or {}
        contract_size = _number(instrument.get("contractSize"))
        currency = self._deal_currency(instrument)
        rate = 1.0 if currency == self.currency_code else next(
            (_number(c.get("baseExchangeRate")) for c in instrument.get("currencies") or () if c.get("code") == currency),
            None)
        if not contract_size or not rate:
            return None
        return distance * size * contract_size / rate

    def positions(self, markets: dict) -> dict:
        frame = self._open_positions()
        own, others = {}, {}
        if frame is None or len(frame) == 0:
            return {}
        for _, row in frame.iterrows():
            epic = row["epic"]
            if epic not in markets:
                continue
            is_own = row["dealId"] in self.own_ids
            book = own if is_own else others
            size, level = float(row["size"]), float(row["level"])
            held = book.get(epic)
            if held is None:
                held = book[epic] = Position(
                    epic, "long" if row["direction"] == "BUY" else "short", 0.0, 0.0,
                    opened_at=_utc_seconds(row.get("createdDateUTC")), pnl=0.0,
                    stop=_number(row.get("stopLevel")), take_profit=_number(row.get("limitLevel")), raw=[],
                    own=is_own)
            held.entry = (held.entry * held.size + level * size) / (held.size + size)
            held.size += size
            pnl = self._pnl(row, markets[epic])
            held.pnl = None if pnl is None or held.pnl is None else held.pnl + pnl
            held.raw.append((row["dealId"], row["direction"], size))
        return {**others, **own}

    def _deal_currency(self, instrument: dict) -> str:
        currencies = instrument.get("currencies") or []
        codes = [c.get("code") for c in currencies]
        if not codes or self.currency_code in codes:
            return self.currency_code
        return next((c["code"] for c in currencies if c.get("isDefault")), codes[0])

    @staticmethod
    def _outcome(result) -> tuple:
        if not isinstance(result, dict):
            return False, str(result)
        status, reason = result.get("dealStatus", "?"), result.get("reason")
        return status == "ACCEPTED", (status if status == "ACCEPTED" or not reason else f"{status} ({reason})")

    def open(self, market: Market, direction: str, size: float, stop: float, take_profit, quote: Quote) -> bool:
        instrument = market.raw["instrument"]
        try:
            result = self._call(
                f"{direction} {market.symbol}", IGService.create_open_position, paced=False,
                currency_code=self._deal_currency(instrument), direction="BUY" if direction == "long" else "SELL",
                epic=market.symbol, expiry=instrument.get("expiry", "-"), force_open=True, guaranteed_stop=False,
                level=None, limit_distance=None,
                limit_level=self.round_price(market, take_profit) if take_profit is not None else None,
                order_type="MARKET", quote_id=None, size=size, stop_distance=None,
                stop_level=self.round_price(market, stop), trailing_stop=False, trailing_stop_increment=None,
            )
        except BrokerError as e:
            self.log.error(f"{market.symbol}: {direction} order failed: {e}")
            self.open_problem = str(e)
            return False
        accepted, outcome = self._outcome(result)
        if accepted:
            self.own_ids.update(d["dealId"] for d in result.get("affectedDeals") or () if d.get("dealId"))
            if result.get("dealId"):
                self.own_ids.add(result["dealId"])
        else:
            self.open_problem = outcome
        self.log.info(f"{direction.upper()} {market.symbol} size={size:g} stop={self.round_price(market, stop)} "
                      f"take-profit={self.round_price(market, take_profit) if take_profit is not None else 'none'} "
                      f"-> {outcome}")
        return accepted

    def close(self, market: Market, position: Position, reason: str, size: float = None) -> bool:
        all_closed = True
        left = size
        size_filled = value_filled = 0.0
        pnl, refs = 0.0, []
        for deal_id, direction, deal_size in position.raw or ():
            if left is not None and left <= 0:
                break
            amount = deal_size if left is None else min(left, deal_size)
            try:
                # By deal ID alone: IG rejects a close that also names the epic and expiry.
                # A deal its stop or limit closed a moment before comes back REJECTED:
                # the next read shows it gone.
                result = self._call(f"close {market.symbol}", IGService.close_open_position, paced=False,
                                    deal_id=deal_id, direction="SELL" if direction == "BUY" else "BUY", epic=None,
                                    expiry=None, level=None, order_type="MARKET", quote_id=None, size=amount)
            except BrokerError as e:
                self.log.error(f"{market.symbol}: close failed ({reason}): {e}")
                self.close_problem = str(e)
                all_closed = False
                continue
            closed, outcome = self._outcome(result)
            self.log.info(f"CLOSE {market.symbol} {position.direction} {amount:g} ({reason}) -> {outcome}")
            if closed:
                # IG's confirmation says where it closed and what it made (in the
                # deal's currency); its trade history names the trade by the end
                # of the closing deal's ID.
                size_filled += amount
                value_filled += amount * (_number(result.get("level")) or 0.0)
                profit = _number(result.get("profit"))
                in_account_currency = result.get("profitCurrency") in (None, self.currency_code)
                pnl = None if pnl is None or profit is None or not in_account_currency else pnl + profit
                if result.get("dealId"):
                    refs.append(str(result["dealId"])[-8:])
            if not closed:
                self.close_problem = outcome
            if left is not None:
                left -= amount
            all_closed = all_closed and closed
        if size_filled and value_filled:
            self.closing_fill = {"price": value_filled / size_filled, "pnl": None if pnl is None else round(pnl, 2),
                                 "refs": tuple(refs)}
        return all_closed

    def refs(self, position: Position) -> set:
        return {deal_id for deal_id, _, _ in position.raw or ()}

    # -- dashboard ------------------------------------------------------------------
    def _fetch_account(self):
        accounts = self._call("accounts", IGService.fetch_accounts)
        if accounts is None or len(accounts) == 0:
            return None
        current = accounts[accounts["accountId"] == self.account_id]
        row = (current if len(current) else accounts).iloc[0]
        balance, unrealized = _number(row["balance"]), _number(row["profitLoss"])
        if balance is None:
            return None
        return {"balance": balance, "equity": balance + (unrealized or 0.0), "unrealizedPl": unrealized}

    def report(self, markets: dict, notes: dict) -> None:
        try:
            frame = self._open_positions()
            report = {"account": self._fetch_account(), "positions": []}
            for _, p in (frame.iterrows() if frame is not None else ()):
                if p["epic"] not in markets or p["dealId"] not in self.own_ids:
                    continue
                is_long = p["direction"] == "BUY"
                report["positions"].append({
                    "ref": p["dealId"], "symbol": markets[p["epic"]].name, "direction": "long" if is_long else "short",
                    "size": _number(p["size"]), "entryPrice": _number(p["level"]),
                    "currentPrice": _number(p["bid"] if is_long else p["offer"]), "pnl": self._pnl(p, markets[p["epic"]]),
                    "stopLoss": _number(p.get("stopLevel")), "takeProfit": _number(p.get("limitLevel")),
                    "openedAt": _utc_seconds(p.get("createdDateUTC")),
                })
            if time.monotonic() - self._last_trades_fetch >= TRADES_EVERY_SECONDS:
                self._last_trades_fetch = time.monotonic()
                report["trades"] = self._closed_trades(markets)
            self.dashboard.update(**report)
        except Exception as e:
            self.log.warning(f"Couldn't gather the dashboard report: {e}")

    def _closed_trades(self, markets: dict) -> list:
        epics = {m.name: m.symbol for m in markets.values()}
        since = datetime.now(timezone.utc) - timedelta(days=7)
        history = self._call("transaction history", IGService.fetch_transaction_history, trans_type="ALL_DEAL",
                             from_date=since.strftime("%Y-%m-%dT%H:%M:%S"), page_size=200)
        trades = []
        for _, t in history.iterrows():
            size = _number(str(t.get("size", "")).replace("+", ""))
            closed_at = _utc_seconds(t.get("dateUtc"))
            name = _history_market_name(t.get("instrumentName"))
            if name not in epics or not t.get("reference") or not size or closed_at is None:
                continue
            profit = t.get("profitAndLoss")
            opened_at = _utc_seconds(t.get("openDateUtc"))
            # The stagnancy timeout's fields: by the closing deal, or - for a trade
            # it only watched - by market and opening time, as IG's history doesn't
            # name the deal that opened it.
            extra = self.exit_fields(t["reference"]) or (
                self.stagnancy.fields_matching(epics[name], opened_at) if self.stagnancy is not None else {})
            trades.append({
                "ref": str(t["reference"]), "symbol": name, "direction": "short" if size < 0 else "long",
                "size": abs(size), "entryPrice": _number(t.get("openLevel")), "exitPrice": _number(t.get("closeLevel")),
                "openedAt": opened_at, "closedAt": closed_at,
                "pnl": _number(re.sub(r"[^\d.\-]", "", profit)) if isinstance(profit, str) else _number(profit),
                **extra,
            })
        return trades
