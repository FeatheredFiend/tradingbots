#!/usr/bin/env python3
"""
Capital.com Demo EMA(9/21) Crossover Bot — sized for a small account
=====================================================================

The same strategy as ig_cfd_ema_bot.py, on a Capital.com demo account —
and, unlike IG, Capital.com's API gives price history for shares, so the
watchlist can be the shares the IG bot was meant to trade. Capital.com
sells fractions of a share (a tenth of Apple, a hundredth of Microsoft),
so each trade is sized from a small budget.

Strategy
--------
- Timeframe : 15-minute bars by default (EMA_TIMEFRAME env var: M1, M5, M15
  or M30, shared with the other EMA bots); Capital.com's own, mid of bid and
  ask, closed bars only
- Entry     : 9-period EMA crosses ABOVE the 21-period EMA -> BUY (open long)
- Exit      : 9-period EMA crosses BELOW the 21-period EMA -> close the long
- Risk mgmt : 2% stop-loss / 5% take-profit, attached to the order, so
              Capital.com enforces them even while the bot isn't running.

Sizing
------
The same as capital_momentum_scanner_bot.py: CAPITAL_BUDGET (default 600,
in the account's currency) split into CAPITAL_MAX_POSITIONS (default 5)
slices, and each buy is worth one slice. The budget is exposure, not
margin; shares need 20% margin, so a £120 slice ties up about £24. A
watchlist market whose smallest trade is worth more than a slice is
skipped at startup with the reason logged.

Watchlist
---------
CAPITAL_WATCHLIST is a fixed list of Capital.com epics (for US shares, the
ticker), not a screener. An epic Capital.com doesn't have stops the bot at
startup (like the IG bot, a hand-picked list should be exactly right),
listing similar markets it does have. US shares trade 14:30-21:00 UK time;
outside that there are no new bars, so nothing happens.

Bars are acted on as they close, so after a start nothing happens until the
next bar closes. The bot treats every position in its markets as
its own — fine alongside capital_momentum_scanner_bot.py on one account
while their market lists don't overlap (see that bot's docstring).

Setup
-----
1. pip install -r requirements.txt   (just `requests` — ig-bot-env has it)
2. The same CAPITAL_API_KEY / CAPITAL_EMAIL / CAPITAL_EMAIL_PASSWORD as the
   scanner (Capital.com > Settings > API integrations).
3. (Optional) CAPITAL_WATCHLIST="AAPL,MSFT,NVDA", CAPITAL_ACCOUNT_ID
4. Run:
       python capital_ema_bot.py

This script only ever talks to Capital.com's demo endpoint
(https://demo-api-capital.backend-capital.com). It never places live-money orders.
"""

import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))
from dashboard_reporter import CommandError, DashboardReporter  # noqa: E402 - needs the path above

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("CAPITAL_API_KEY", "")
EMAIL = os.environ.get("CAPITAL_EMAIL", "")
PASSWORD = os.environ.get("CAPITAL_EMAIL_PASSWORD", "")
ACCOUNT_ID = os.environ.get("CAPITAL_ACCOUNT_ID", "")
BASE_URL = "https://demo-api-capital.backend-capital.com/api/v1"  # Demo only — do not change.

DEFAULT_WATCHLIST = "AAPL,MSFT,AMZN,GOOGL,TSLA,NVDA,META,NFLX"
WATCHLIST = [
    s.strip().upper()
    for s in os.environ.get("CAPITAL_WATCHLIST", DEFAULT_WATCHLIST).split(",")
    if s.strip()
]

EMA_SHORT_PERIOD = 9
EMA_LONG_PERIOD = 21
BARS_LOOKBACK = 200           # plenty for a 21-EMA warm-up.

BUDGET = float(os.environ.get("CAPITAL_BUDGET", "600"))  # total exposure, in the account's currency
MAX_OPEN_POSITIONS = int(os.environ.get("CAPITAL_MAX_POSITIONS", "5"))
TRADE_EXPOSURE = BUDGET / MAX_OPEN_POSITIONS

STOP_LOSS_PCT = 0.02           # 2% hard stop-loss, attached to the order itself.
TAKE_PROFIT_PCT = 0.05         # 5% take-profit, attached to the order itself.

RESOLUTIONS = {"M1": "MINUTE", "M5": "MINUTE_5", "M15": "MINUTE_15", "M30": "MINUTE_30"}
BAR_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30}
TIMEFRAME = os.environ.get("EMA_TIMEFRAME", "").strip().upper() or "M15"  # bar length
assert TIMEFRAME in BAR_MINUTES, "EMA_TIMEFRAME must be M1, M5, M15 or M30"
RESOLUTION = RESOLUTIONS[TIMEFRAME]
BAR_SECONDS = BAR_MINUTES[TIMEFRAME] * 60
LOOP_INTERVAL_SECONDS = min(60, BAR_SECONDS // 4)  # how often to look for newly closed bars
REQUEST_TIMEOUT_SECONDS = 20
MIN_REQUEST_INTERVAL = 0.15    # Capital.com allows 10 requests a second
MAX_CONSECUTIVE_ERRORS = 10

assert MAX_OPEN_POSITIONS >= 1, "CAPITAL_MAX_POSITIONS must be at least 1"
assert BUDGET > 0, "CAPITAL_BUDGET must be positive"
assert WATCHLIST, "CAPITAL_WATCHLIST resolved to an empty list"

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("capital_ema_bot")

# Off unless DASHBOARD_URL and DASHBOARD_TOKEN are set (see shared/dashboard_reporter.py).
dashboard = DashboardReporter("capital-ema-bot", "Capital.com EMA crossover", broker="Capital.com",
                              strategy="EMA 9/21 crossover")


# ---------------------------------------------------------------------------
# CLIENT
# ---------------------------------------------------------------------------
class CapitalError(Exception):
    """Capital.com answered, but refused the request — the message says why."""


class CapitalClient:
    """Capital.com's REST API with its session looked after: it logs in on
    first use and again whenever the session lapses (10 minutes without a
    request, or a token Capital.com has dropped), and spaces requests
    MIN_REQUEST_INTERVAL apart."""

    def __init__(self):
        self._http = requests.Session()
        self._last_request = 0.0
        self.account_id = None

    def login(self) -> None:
        response = self._send("POST", "/session", headers={"X-CAP-API-KEY": API_KEY},
                              json={"identifier": EMAIL, "password": PASSWORD, "encryptedPassword": False})
        body = self._body(response)
        self._http.headers.update({"CST": response.headers["CST"],
                                   "X-SECURITY-TOKEN": response.headers["X-SECURITY-TOKEN"]})
        self.account_id = body["currentAccountId"]
        if ACCOUNT_ID and ACCOUNT_ID != self.account_id:
            self._body(self._send("PUT", "/session", json={"accountId": ACCOUNT_ID}))
            self.account_id = ACCOUNT_ID

    def __call__(self, method: str, path: str, **kwargs) -> dict:
        """One API call. Raises CapitalError when Capital.com refuses it, and
        requests.RequestException when it can't be reached."""
        if self.account_id is None:
            self.login()
        response = self._send(method, path, **kwargs)
        if response.status_code == 401:  # session expired
            self.login()
            response = self._send(method, path, **kwargs)
        return self._body(response)

    def _send(self, method: str, path: str, **kwargs) -> requests.Response:
        wait = MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        return self._http.request(method, BASE_URL + path, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)

    @staticmethod
    def _body(response: requests.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            reason = body.get("errorCode") or response.text[:200] or response.reason
            raise CapitalError(f"HTTP {response.status_code}: {reason}")
        return body


capital = CapitalClient()


def fetch_account() -> dict:
    """The traded account's money. Capital.com's "balance" already includes
    open positions' profit; the cash is its "deposit"."""
    accounts = capital("GET", "/accounts").get("accounts", [])
    account = next((a for a in accounts if a["accountId"] == capital.account_id), None)
    if account is None:
        raise CapitalError(f"account {capital.account_id} isn't one of this login's accounts")
    money = account["balance"]
    return {
        "id": account["accountId"],
        "currency": account["currency"],
        "cash": float(money["deposit"]),
        "equity": float(money["balance"]),
        "unrealizedPl": float(money["profitLoss"]),
        "available": float(money["available"]),
    }


def connect() -> dict:
    """The account the bot trades on. Exits if it can't log in."""
    if not (API_KEY and EMAIL and PASSWORD):
        log.error("Missing credentials. Set CAPITAL_API_KEY, CAPITAL_EMAIL and CAPITAL_EMAIL_PASSWORD "
                  "(Capital.com > Settings > API integrations).")
        sys.exit(1)
    try:
        return fetch_account()
    except (CapitalError, requests.RequestException) as e:
        log.error(f"Couldn't open the Capital.com demo account: {e}")
        sys.exit(1)


def utc_seconds(text: str) -> float:
    """Capital.com's UTC times ('2026-09-28T19:15:00', sometimes with
    milliseconds) as Unix seconds."""
    return datetime.fromisoformat(text.rstrip("Z")).replace(tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# MARKETS / SIZING
# ---------------------------------------------------------------------------
def fetch_markets(epics: list) -> dict:
    """{epic: Capital.com's market details (instrument, dealingRules,
    snapshot)}. One request per 50 epics; unknown epics are just absent."""
    markets = {}
    for i in range(0, len(epics), 50):
        body = capital("GET", "/markets", params={"epics": ",".join(epics[i:i + 50])})
        markets.update({m["instrument"]["epic"]: m for m in body.get("marketDetails", [])})
    return markets


def mid(market: dict) -> float:
    snapshot = market["snapshot"]
    return (float(snapshot["bid"]) + float(snapshot["offer"])) / 2


def conversion_pairs(currencies, home: str) -> dict:
    """{currency: (epic, inverted?)} — the Capital.com FX market that values
    it in the account's currency: EURGBP for EUR on a GBP account, GBPUSD
    (inverted) for USD. A currency with neither is left out."""
    wanted = sorted(set(currencies) - {home})
    candidates = [e for c in wanted for e in (f"{c}{home}", f"{home}{c}")]
    offered = fetch_markets(candidates) if candidates else {}
    pairs = {}
    for c in wanted:
        if f"{c}{home}" in offered:
            pairs[c] = (f"{c}{home}", False)
        elif f"{home}{c}" in offered:
            pairs[c] = (f"{home}{c}", True)
    return pairs


class Quotes:
    """Current market details for some epics, and what one unit of each is
    worth in the account's currency — fetched together in one request."""

    def __init__(self, epics, pairs: dict, home: str):
        self.markets = fetch_markets(list(epics) + [epic for epic, _ in pairs.values()])
        self.rates = {home: 1.0}
        for currency, (epic, inverted) in pairs.items():
            if epic in self.markets:
                rate = mid(self.markets[epic])
                self.rates[currency] = 1 / rate if inverted else rate

    def unit_value(self, epic: str):
        """None if the epic or its currency's rate is missing."""
        market = self.markets.get(epic)
        rate = market and self.rates.get(market["instrument"]["currency"])
        if not rate:
            return None
        return mid(market) * float(market["instrument"].get("lotSize") or 1) * rate


def size_for_slice(market: dict, unit_value: float) -> float:
    """A size worth up to one budget slice, rounded down to one Capital.com
    accepts; 0 if even its smallest trade is worth more than a slice."""
    rules = market["dealingRules"]
    step = float(rules["minSizeIncrement"]["value"])
    size = round(math.floor(TRADE_EXPOSURE / unit_value / step + 1e-9) * step, 8)
    return size if size >= float(rules["minDealSize"]["value"]) else 0.0


def similar_markets(term: str) -> str:
    try:
        found = capital("GET", "/markets", params={"searchTerm": term}).get("markets", [])
    except (CapitalError, requests.RequestException):
        return "none found"
    return ", ".join(f"{m['epic']} ({m['instrumentName']})" for m in found[:5]) or "none found"


def resolve_watchlist(home: str) -> tuple:
    """({epic: market details}, conversion pairs). Exits on an epic
    Capital.com doesn't have; skips (logging why) one too big for a slice."""
    offered = fetch_markets(WATCHLIST)
    missing = [s for s in WATCHLIST if s not in offered]
    if missing:
        for epic in missing:
            log.critical(f"Capital.com has no market with the epic {epic}. Similar: {similar_markets(epic)}")
        log.critical("Fix CAPITAL_WATCHLIST (Capital.com epics, e.g. AAPL). Exiting.")
        sys.exit(1)

    pairs = conversion_pairs({m["instrument"]["currency"] for m in offered.values()}, home)
    quotes = Quotes(WATCHLIST, pairs, home)
    usable = {}
    for epic in WATCHLIST:
        market, value = offered[epic], quotes.unit_value(epic)
        if value is None:
            log.warning(f"Skipping {epic}: no {market['instrument']['currency']}/{home} market to value it in {home}.")
        elif size_for_slice(market, value) == 0:
            minimum = market["dealingRules"]["minDealSize"]["value"]
            log.warning(
                f"Skipping {epic}: its smallest trade (size {minimum:g}) is worth about "
                f"{float(minimum) * value:,.0f} {home}, more than a {TRADE_EXPOSURE:,.2f} {home} slice — "
                f"raise CAPITAL_BUDGET to include it."
            )
        else:
            usable[epic] = market

    if not usable:
        log.critical("Nothing on the watchlist is tradable within the budget. Exiting.")
        sys.exit(1)
    return usable, pairs


# ---------------------------------------------------------------------------
# MARKET DATA / SIGNAL
# ---------------------------------------------------------------------------
def fetch_closes(epic: str, count: int):
    """(start time of the latest closed bar, its last `count` closes, oldest
    first), or None if there are no closed bars. Capital.com's list ends
    with the bar still forming, which is dropped."""
    body = capital("GET", f"/prices/{epic}", params={"resolution": RESOLUTION, "max": count + 1})
    now = time.time()
    bars = []
    for p in body.get("prices", []):
        start = utc_seconds(p["snapshotTimeUTC"])
        close = p["closePrice"]
        if start + BAR_SECONDS <= now and close.get("bid") is not None:
            bars.append((start, (float(close["bid"]) + float(close.get("ask") or close["bid"])) / 2))
    if not bars:
        return None
    return bars[-1][0], [c for _, c in bars[-count:]]


def ema(values: list, span: int) -> list:
    """Same as pandas' ewm(span=span, adjust=False).mean(), as the IG bot uses."""
    alpha = 2 / (span + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def detect_crossover(ema_short: list, ema_long: list):
    """'bullish', 'bearish', or None, from the last two closed bars."""
    if len(ema_short) < 2:
        return None
    if ema_short[-2] <= ema_long[-2] and ema_short[-1] > ema_long[-1]:
        return "bullish"
    if ema_short[-2] >= ema_long[-2] and ema_short[-1] < ema_long[-1]:
        return "bearish"
    return None


# ---------------------------------------------------------------------------
# POSITIONS / ORDERS
# ---------------------------------------------------------------------------
def fetch_positions() -> dict:
    """{epic: {"side": "long"/"short", "size", "pl", "deal_ids"}} for every
    open position; several positions in one market are added together."""
    positions = {}
    for item in capital("GET", "/positions").get("positions", []):
        p = item["position"]
        held = positions.setdefault(item["market"]["epic"], {
            "side": "long" if p["direction"] == "BUY" else "short", "size": 0.0, "pl": 0.0, "deal_ids": [],
        })
        held["size"] += float(p["size"])
        held["pl"] += float(p.get("upl") or 0)
        held["deal_ids"].append(p["dealId"])
    return positions


def confirm(deal_reference: str) -> dict:
    """Capital.com's verdict on a deal, which can take a moment to appear."""
    for attempt in range(5):
        try:
            return capital("GET", f"/confirms/{deal_reference}")
        except CapitalError as e:
            if "not-found" not in str(e) or attempt == 4:
                raise
            time.sleep(0.5)


def deal_outcome(confirmation: dict) -> tuple:
    """(accepted?, e.g. 'ACCEPTED 0.35 @ 338.5' or 'REJECTED (INSUFFICIENT_FUNDS)')."""
    status = confirmation.get("dealStatus", "?")
    if status == "ACCEPTED":
        return True, f"ACCEPTED {confirmation.get('size')} @ {confirmation.get('level')}"
    return False, f"{status} ({confirmation.get('reason', '?')})"


def submit_buy(epic: str, market: dict, unit_value: float, currency: str) -> bool:
    """Market buy with the stop-loss and take-profit attached. True if accepted."""
    size = size_for_slice(market, unit_value)
    if size == 0:
        log.info(f"{epic}: bullish crossover, but its smallest trade is now worth more than a slice; skipping.")
        return False
    entry = float(market["snapshot"]["offer"])
    digits = int(market["snapshot"].get("decimalPlacesFactor", 2))  # the market's price precision
    order = {
        "epic": epic,
        "direction": "BUY",
        "size": size,
        "guaranteedStop": False,
        "stopLevel": round(entry * (1 - STOP_LOSS_PCT), digits),
        "profitLevel": round(entry * (1 + TAKE_PROFIT_PCT), digits),
    }
    try:
        accepted, outcome = deal_outcome(confirm(capital("POST", "/positions", json=order)["dealReference"]))
    except (CapitalError, requests.RequestException) as e:
        log.error(f"Error submitting BUY for {epic}: {e}")
        return False
    log.info(
        f"BUY submitted -> {epic} size={size:g} (~{size * unit_value:,.2f} {currency}) "
        f"stop={order['stopLevel']} limit={order['profitLevel']} result={outcome}"
    )
    return accepted


def close_position(epic: str, position: dict, reason: str) -> bool:
    """Close every position the bot holds in the market. True if all closed."""
    all_closed = True
    for deal_id in position["deal_ids"]:
        try:
            closed, outcome = deal_outcome(confirm(capital("DELETE", f"/positions/{deal_id}")["dealReference"]))
        except (CapitalError, requests.RequestException) as e:
            log.error(f"Error closing {epic} ({reason}): {e}")
            all_closed = False
            continue
        log.info(f"CLOSE submitted ({reason}) -> {epic} {position['side']} {position['size']:g} result={outcome}")
        if closed:
            CLOSE_REASONS[deal_id] = reason
        all_closed = all_closed and closed
    return all_closed


def close_from_dashboard(markets: dict, symbol: str, ref, direction: str, size) -> str:
    """A close asked for on the dashboard (DASHBOARD_COMMANDS=1): the
    position it showed as `ref`, if it's still open on the same side.
    Capital.com's API only closes whole positions, so there's no `size`.
    Answers what happened, or raises CommandError."""
    if symbol not in markets:
        raise CommandError(f"{symbol} isn't one of this bot's markets.")
    if size is not None:
        raise CommandError("Capital.com's API only closes whole positions - close all of it instead.")
    items = [i for i in capital("GET", "/positions").get("positions", [])
             if i["market"]["epic"] == symbol and (not ref or i["position"]["dealId"] == ref)]
    if not items:
        raise CommandError(f"There's no open {symbol} position now - it may have closed already.")
    p = items[0]["position"]
    side = "long" if p["direction"] == "BUY" else "short"
    if side != direction:
        raise CommandError(f"The {symbol} position is {side} now, not {direction}, so it was left alone.")
    try:
        closed, outcome = deal_outcome(confirm(capital("DELETE", f"/positions/{p['dealId']}")["dealReference"]))
    except CapitalError as e:
        raise CommandError(f"Capital.com refused: {e}") from None
    log.info(f"CLOSE submitted (from the dashboard) -> {symbol} {side} {float(p['size']):g} result={outcome}")
    if not closed:
        raise CommandError(f"Capital.com didn't close it: {outcome}")
    CLOSE_REASONS[p["dealId"]] = "closed from the dashboard"
    return f"Closed the {symbol} {side} position ({float(p['size']):g}): {outcome}."


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
CLOSE_REASONS = {}  # deal ID -> why this bot closed it; stop-loss/take-profit come from Capital.com
CLOSE_SOURCES = {"SL": "stop-loss", "TP": "take-profit", "CLOSE_OUT": "margin close-out",
                 "DEALER": "closed by Capital.com", "SYSTEM": "closed by Capital.com"}
_seen_open = {}  # deal ID -> the position as last seen open, for trades opened over a day ago
_last_report_problem = None
# The first dashboard report after a start looks back this far, so trades that
# closed while the bot was stopped still reach the dashboard; later ones a day.
HISTORY_BACKFILL_DAYS = 7
_backfilled = False


def dashboard_position(item: dict) -> dict:
    """A Capital.com position in the dashboard's shape."""
    p, market = item["position"], item["market"]
    is_long = p["direction"] == "BUY"
    return {
        "ref": p["dealId"],
        "symbol": market["epic"],
        "direction": "long" if is_long else "short",
        "size": float(p["size"]),
        "entryPrice": float(p["level"]),
        "currentPrice": market.get("bid") if is_long else market.get("offer"),
        "pnl": p.get("upl"),
        "stopLoss": p.get("stopLevel"),
        "takeProfit": p.get("profitLevel"),
        "openedAt": utc_seconds(p["createdDateUTC"]),
    }


def history(path: str, key: str, **params) -> list:
    """Capital.com's activity or transaction history for the last day - or,
    on the first report, the last HISTORY_BACKFILL_DAYS days, a day per
    request (the most the activity history takes in one)."""
    if _backfilled:
        return capital("GET", path, params={**params, "lastPeriod": 86400}).get(key, [])
    items, seen = [], set()
    end = datetime.now(timezone.utc)
    for day in range(HISTORY_BACKFILL_DAYS):
        window = {"from": (end - timedelta(days=day + 1)).strftime("%Y-%m-%dT%H:%M:%S"),
                  "to": (end - timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%S")}
        for item in capital("GET", path, params={**params, **window}).get(key, []):
            identity = json.dumps(item, sort_keys=True)  # one on a day boundary comes twice
            if identity not in seen:
                seen.add(identity)
                items.append(item)
    return items


def closed_trades(markets, open_ids: set, quotes_rates: dict) -> list:
    """Recent closed trades in this bot's markets. Capital.com has no list of
    closed trades, so they're pieced together: the opening and closing deal
    from the activity history (the source says whether a stop-loss or
    take-profit did it), and the realised profit from the transaction
    history - by deal ID, or for a transaction without one, the one in the
    same market closed at the same moment."""
    global _backfilled
    activities = history("/history/activity", "activities", detailed="true", filter="type==POSITION")
    transactions = history("/history/transactions", "transactions", type="TRADE")
    _backfilled = True

    deals = {}
    for a in activities:
        if a.get("epic") in markets and a.get("status") != "REJECTED" and a.get("details"):
            deals.setdefault(a["dealId"], []).append(a)
    by_deal = {}
    for t in transactions:
        if t.get("dealId"):
            by_deal[t["dealId"]] = by_deal.get(t["dealId"], 0.0) + float(t["size"])
    profits = [
        {"at": utc_seconds(t["dateUtc"]), "market": str(t.get("instrumentName", "")).upper(), "pnl": float(t["size"])}
        for t in transactions if t.get("dateUtc") and not t.get("dealId")
    ]

    trades = []
    for deal_id, history in deals.items():
        if deal_id in open_ids:
            continue
        history.sort(key=lambda a: a["dateUTC"])
        close = history[-1]
        opening = history[0]["details"] if len(history) > 1 else _seen_open.get(deal_id)
        if opening is None or opening["direction"] == close["details"]["direction"]:
            continue  # only its opening is in the last day's history
        epic, closed_at = close["epic"], utc_seconds(close["dateUTC"])
        entry, exit_price, size = float(opening["level"]), float(close["details"]["level"]), float(opening["size"])
        sign = 1 if opening["direction"] == "BUY" else -1

        names = {epic.upper(), str(close["details"].get("marketName", "")).upper()}
        match = None if deal_id in by_deal else min(
            (p for p in profits if p["market"] in names and abs(p["at"] - closed_at) <= 120),
            key=lambda p: abs(p["at"] - closed_at), default=None)
        if deal_id in by_deal:
            pnl = round(by_deal[deal_id], 2)
        elif match is not None:
            profits.remove(match)
            pnl = match["pnl"]
        else:  # estimated at today's exchange rate, without costs
            rate = quotes_rates.get(close["details"].get("currency"))
            pnl = (exit_price - entry) * size * sign * rate if rate else None

        source = close.get("source")
        reason = CLOSE_REASONS.get(deal_id, "closed by the bot or by hand") if source == "USER" \
            else CLOSE_SOURCES.get(source, source)
        trades.append({
            "ref": deal_id,
            "symbol": epic,
            "direction": "long" if sign > 0 else "short",
            "size": size,
            "entryPrice": entry,
            "exitPrice": exit_price,
            "openedAt": utc_seconds(history[0]["dateUTC"]) if len(history) > 1 else opening.get("openedAt"),
            "closedAt": closed_at,
            "pnl": pnl,
            "closeReason": reason,
        })
    return trades


def report_to_dashboard(markets, rates: dict) -> None:
    """Account, open positions and the last day's closed trades in this
    bot's markets. Four requests, every 15 seconds or so; a failure only
    skips it (and is logged once, not every time)."""
    global _last_report_problem
    try:
        account = fetch_account()
        items = [i for i in capital("GET", "/positions").get("positions", []) if i["market"]["epic"] in markets]
        for item in items:
            p = item["position"]
            _seen_open[p["dealId"]] = {"direction": p["direction"], "level": p["level"], "size": p["size"],
                                       "openedAt": utc_seconds(p["createdDateUTC"])}
        trades = closed_trades(markets, {i["position"]["dealId"] for i in items}, rates)
    except (CapitalError, requests.RequestException, KeyError, TypeError, ValueError) as e:
        problem = f"{type(e).__name__}: {e}"
        if problem != _last_report_problem:
            log.warning(f"Couldn't gather the dashboard report ({problem}); trading carries on.")
        _last_report_problem = problem
        return
    _last_report_problem = None
    dashboard.update(
        account={"balance": account["cash"], "equity": account["equity"], "unrealizedPl": account["unrealizedPl"]},
        positions=[dashboard_position(i) for i in items],
        trades=trades,
    )


# ---------------------------------------------------------------------------
# TRADING CYCLE
# ---------------------------------------------------------------------------
def trade_new_bars(watchlist: dict, pairs: dict, new_bars: dict, currency: str) -> dict:
    """Act on the markets whose bar just closed. `new_bars` maps epic ->
    (bar time, closes). Returns the exchange rates it fetched, for the dashboard."""
    positions = fetch_positions()
    quotes = Quotes(new_bars, pairs, currency)
    held = [s for s in positions if s in watchlist]

    for epic, (_, closes) in new_bars.items():
        ema_short, ema_long = ema(closes, EMA_SHORT_PERIOD), ema(closes, EMA_LONG_PERIOD)
        signal = detect_crossover(ema_short, ema_long)
        position = positions.get(epic)
        position_desc = f"{position['side']} {position['size']:g} P/L={position['pl']:+.2f}" if position else "FLAT"
        log.info(
            f"{epic} | price={closes[-1]:g} | EMA9={ema_short[-1]:.5f} | EMA21={ema_long[-1]:.5f} | "
            f"signal={signal or 'none'} | position={position_desc}"
        )

        if signal == "bullish":
            market, value = quotes.markets.get(epic), quotes.unit_value(epic)
            if position is not None:
                log.info(f"{epic}: bullish crossover, but already holding a position; skipping buy.")
            elif len(held) >= MAX_OPEN_POSITIONS:
                log.info(f"{epic}: bullish crossover, but {MAX_OPEN_POSITIONS} positions are already open; "
                         f"skipping buy.")
            elif market is None or value is None or market["snapshot"].get("marketStatus") != "TRADEABLE":
                log.info(f"{epic}: bullish crossover, but Capital.com says it isn't tradeable right now; skipping buy.")
            elif submit_buy(epic, market, value, currency):
                held.append(epic)
        elif signal == "bearish" and position is not None and position["side"] == "long":
            if close_position(epic, position, "EMA bearish crossover"):
                held.remove(epic)
    return quotes.rates


# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------
def sleep_after_error(consecutive_errors: int) -> None:
    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
        log.critical(
            f"Reached {MAX_CONSECUTIVE_ERRORS} consecutive errors. "
            f"Exiting for safety — check connectivity/credentials before restarting."
        )
        sys.exit(1)
    backoff = min(LOOP_INTERVAL_SECONDS * consecutive_errors, 300)
    log.info(f"Retrying in {backoff}s...")
    time.sleep(backoff)


def run_bot() -> None:
    account = connect()
    currency = account["currency"]
    watchlist, pairs = resolve_watchlist(currency)
    rates = Quotes([], pairs, currency).rates

    log.info("=" * 78)
    log.info("Capital.com EMA(9/21) Crossover Bot starting — DEMO ACCOUNT ONLY")
    log.info(f"Account {account['id']} | cash={account['cash']:,.2f} {currency} | equity={account['equity']:,.2f} | "
             f"available={account['available']:,.2f}")
    if account["available"] <= 0:
        log.warning("Nothing is available to trade with: every order will be refused until the demo account "
                    "is topped up on Capital.com.")
    log.info(f"Watchlist: {', '.join(watchlist)}")
    log.info(
        f"Budget={BUDGET:,.2f} {currency} in {MAX_OPEN_POSITIONS} slices of {TRADE_EXPOSURE:,.2f} | "
        f"Stop-loss={STOP_LOSS_PCT:.0%} | Take-profit={TAKE_PROFIT_PCT:.0%} | "
        f"Timeframe={TIMEFRAME} | EMA periods={EMA_SHORT_PERIOD}/{EMA_LONG_PERIOD}"
    )
    log.info("=" * 78)
    dashboard.describe(account=account["id"], currency=currency, config={
        "watchlist": list(watchlist), "budget": BUDGET, "maxPositions": MAX_OPEN_POSITIONS,
        "timeframe": TIMEFRAME, "emaPeriods": f"{EMA_SHORT_PERIOD}/{EMA_LONG_PERIOD}",
        "stopLossPercent": STOP_LOSS_PCT * 100, "takeProfitPercent": TAKE_PROFIT_PCT * 100,
    })
    dashboard.accept_closes(lambda *command: close_from_dashboard(watchlist, *command))

    seen_bar = None  # epic -> start time of the latest closed bar already dealt with
    consecutive_errors = 0

    while True:
        try:
            if dashboard.due():
                report_to_dashboard(watchlist, rates)

            latest = {}
            for epic in watchlist:
                bars = fetch_closes(epic, BARS_LOOKBACK)
                if bars is not None and len(bars[1]) > EMA_LONG_PERIOD:
                    latest[epic] = bars

            if seen_bar is None:
                # Bars that closed before startup are never traded on.
                seen_bar = {s: t for s, (t, _) in latest.items()}
                log.info(f"Waiting for the next {BAR_MINUTES[TIMEFRAME]}-minute bar to close before trading.")
            else:
                new_bars = {s: bars for s, bars in latest.items() if bars[0] != seen_bar.get(s)}
                if new_bars:
                    rates = trade_new_bars(watchlist, pairs, new_bars, currency)
                    seen_bar.update({s: t for s, (t, _) in new_bars.items()})
        except Exception as e:  # anything unexpected counts toward the safety cutoff, not a crash
            consecutive_errors += 1
            log.error(f"[{consecutive_errors}] Error this loop: {e}")
            sleep_after_error(consecutive_errors)
            continue

        consecutive_errors = 0
        dashboard.sleep(LOOP_INTERVAL_SECONDS, lambda: report_to_dashboard(watchlist, rates))  # reports fall due while it waits


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        log.info("Bot stopped manually (KeyboardInterrupt). Goodbye.")
        sys.exit(0)
