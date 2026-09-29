#!/usr/bin/env python3
"""
Capital.com Demo Momentum Streak Scanner — sized for a small account
=====================================================================

The IG momentum scanner's strategy on a Capital.com demo account: the same
signal, longs AND shorts, and the stop-loss / take-profit attached to the
order. Capital.com sells small fractions of its markets (a hundredth of
the US 500, a tenth of an ounce of gold), so each trade is sized from a
small budget instead of being IG's minimum contract worth thousands, and
its API gives free price history for everything it offers.

Strategy — momentum streak (no smoothing, reacts fast, whipsaws more)
-----------------------------------------------------------------------
- Timeframe : 15-minute bars by default (SCANNER_TIMEFRAME env var: M1, M5,
  M15 or M30, shared with the other scanners); Capital.com's own, mid of
  bid and ask, closed bars only
- Buy       : STREAK_LENGTH consecutive HIGHER closes -> open LONG
- Sell      : STREAK_LENGTH consecutive LOWER closes  -> open SHORT
- A reversal streak closes an opposing position; the same-direction streak
  while already positioned is a no-op (no pyramiding).
- Risk mgmt : 2% stop-loss / 5% take-profit by default (STOP_LOSS_PERCENT /
  TAKE_PROFIT_PERCENT env vars, shared with the other scanners), attached
  to the order, so Capital.com enforces them even while the bot isn't running.

Sizing
------
CAPITAL_BUDGET (default 600, in the account's currency) is split into
CAPITAL_MAX_POSITIONS (default 5) slices, and each trade is worth one
slice — £120 by default. The budget is exposure (what the positions are
worth), not margin: at Capital.com's 3-10% margin on indices, currencies
and commodities, a £120 trade ties up £4-£12. The default is about the
smallest that takes in the whole pool: the biggest minimum trades are the
UK 100 (0.01 contracts, about £107) and 100 units of a currency pair (£55
to £100). A smaller budget skips those at startup, with the reason logged.
Once CAPITAL_MAX_POSITIONS positions are open, further signals are
skipped; when several markets signal at once, the biggest move across its
streak gets the slot first.

Differences from the IG scanner
-------------------------------
- Real price history: Capital.com's candles are free, so there are no
  locally built bars, no bar file and no 45-minute warm-up.
- Closed bars only, acted on as they close: each market is traded at most
  once per bar, and never on a bar that closed before the bot started — so
  after a start, nothing happens until the next bar closes (up to one bar).
- No tight request budget: Capital.com allows 10 requests a second, and
  this makes about one.

Sharing an account
------------------
It treats every position in its markets as its own. The EMA bot
(capital_ema_bot.py) trades shares by default, so the two can share an
account as long as their market lists don't overlap; the dashboard then
shows the same balance for both. To keep them apart, add a second demo
account on Capital.com and set CAPITAL_ACCOUNT_ID per bot.

Setup
-----
1. pip install -r requirements.txt   (just `requests` — ig-bot-env has it)
2. On Capital.com, turn on two-factor authentication, then Settings > API
   integrations > Generate new key, with a password of its own.
3. CAPITAL_API_KEY = the key, CAPITAL_EMAIL = your login email,
   CAPITAL_EMAIL_PASSWORD = the key's password.
4. CAPITAL_ACCOUNT_ID: optional — the login's preferred account otherwise.
5. Run:
       python capital_momentum_scanner_bot.py

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

# The IG scanner's pool as Capital.com epics. Anything missing, or too big
# for one budget slice, is skipped at startup, not fatal.
DEFAULT_POOL = (
    "UK100,US500,US30,US100,DE40,J225,"          # indices
    "GOLD,SILVER,OIL_BRENT,OIL_CRUDE,"           # commodities
    "EURUSD,GBPUSD,USDJPY,EURGBP,AUDUSD"         # FX majors
)
POOL = [
    s.strip().upper()
    for s in os.environ.get("CAPITAL_POOL", DEFAULT_POOL).split(",")
    if s.strip()
]

STREAK_LENGTH = int(os.environ.get("STREAK_LENGTH", "3"))  # consecutive up/down bars to trigger

BUDGET = float(os.environ.get("CAPITAL_BUDGET", "600"))  # total exposure, in the account's currency
MAX_OPEN_POSITIONS = int(os.environ.get("CAPITAL_MAX_POSITIONS", "5"))
TRADE_EXPOSURE = BUDGET / MAX_OPEN_POSITIONS

# Percent of the entry price, e.g. STOP_LOSS_PERCENT=0.5 for 0.5%.
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PERCENT", "2")) / 100
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PERCENT", "5")) / 100

RESOLUTIONS = {"M1": "MINUTE", "M5": "MINUTE_5", "M15": "MINUTE_15", "M30": "MINUTE_30"}
BAR_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30}
TIMEFRAME = os.environ.get("SCANNER_TIMEFRAME", "").strip().upper() or "M15"  # bar length
assert TIMEFRAME in BAR_MINUTES, "SCANNER_TIMEFRAME must be M1, M5, M15 or M30"
RESOLUTION = RESOLUTIONS[TIMEFRAME]
BAR_SECONDS = BAR_MINUTES[TIMEFRAME] * 60
LOOP_INTERVAL_SECONDS = min(30, BAR_SECONDS // 4)  # how often to look for newly closed bars
REQUEST_TIMEOUT_SECONDS = 20
MIN_REQUEST_INTERVAL = 0.15    # Capital.com allows 10 requests a second
ERROR_BACKOFF_SECONDS = 60
MAX_CONSECUTIVE_ERRORS = 10

assert STREAK_LENGTH >= 2, "STREAK_LENGTH must be at least 2 to mean anything"
assert MAX_OPEN_POSITIONS >= 1, "CAPITAL_MAX_POSITIONS must be at least 1"
assert BUDGET > 0, "CAPITAL_BUDGET must be positive"
assert STOP_LOSS_PCT > 0 and TAKE_PROFIT_PCT > 0, "STOP_LOSS_PERCENT and TAKE_PROFIT_PERCENT must be positive"
assert POOL, "CAPITAL_POOL resolved to an empty pool"

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("capital_momentum_bot")

# Off unless DASHBOARD_URL and DASHBOARD_TOKEN are set (see shared/dashboard_reporter.py).
dashboard = DashboardReporter("capital-momentum-scanner", "Capital.com momentum scanner", broker="Capital.com",
                              strategy="Momentum streak")


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
        return ""
    names = [f"{m['epic']} ({m['instrumentName']})" for m in found[:5]]
    return f" — similar: {', '.join(names)}" if names else ""


def resolve_pool(home: str) -> tuple:
    """({epic: market details}, conversion pairs) for the usable part of
    the pool. Like the IG scanner, this SKIPS (logging why) anything it
    can't trade instead of exiting — the pool is about breadth."""
    offered = fetch_markets(POOL)
    pairs = conversion_pairs({m["instrument"]["currency"] for m in offered.values()}, home)
    quotes = Quotes(offered, pairs, home)

    usable = {}
    for epic in POOL:
        market = offered.get(epic)
        if market is None:
            log.warning(f"Skipping {epic}: Capital.com has no market with that epic{similar_markets(epic)}.")
            continue
        value = quotes.unit_value(epic)
        if value is None:
            log.warning(f"Skipping {epic}: no {market['instrument']['currency']}/{home} market to value it in {home}.")
            continue
        if size_for_slice(market, value) == 0:
            minimum = market["dealingRules"]["minDealSize"]["value"]
            log.warning(
                f"Skipping {epic}: its smallest trade (size {minimum:g}) is worth about "
                f"{float(minimum) * value:,.0f} {home}, more than a {TRADE_EXPOSURE:,.2f} {home} slice — "
                f"raise CAPITAL_BUDGET to include it."
            )
            continue
        usable[epic] = market
        log.info(f"Using {epic} ({market['instrument']['name']})")

    if not usable:
        log.critical("Nothing in the pool is tradable within the budget. Exiting.")
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


def detect_streak(closes: list):
    """STREAK_LENGTH consecutive higher (or lower) closes in a row."""
    if len(closes) < STREAK_LENGTH + 1:
        return None
    recent = closes[-(STREAK_LENGTH + 1):]
    diffs = [b - a for a, b in zip(recent, recent[1:])]
    if all(d > 0 for d in diffs):
        return "bullish"
    if all(d < 0 for d in diffs):
        return "bearish"
    return None


def streak_move(closes: list) -> float:
    """% move across the streak — ranks simultaneous signals."""
    recent = closes[-(STREAK_LENGTH + 1):]
    return recent[-1] / recent[0] - 1


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
    """(accepted?, e.g. 'ACCEPTED 0.38 @ 7684.3' or 'REJECTED (INSUFFICIENT_FUNDS)')."""
    status = confirmation.get("dealStatus", "?")
    if status == "ACCEPTED":
        return True, f"ACCEPTED {confirmation.get('size')} @ {confirmation.get('level')}"
    return False, f"{status} ({confirmation.get('reason', '?')})"


def open_position(epic: str, market: dict, unit_value: float, direction: str, currency: str) -> bool:
    """Market order with the stop-loss and take-profit attached. True if accepted."""
    size = size_for_slice(market, unit_value)
    if size == 0:
        log.info(f"{epic}: {direction} signal, but its smallest trade is now worth more than a slice; skipping.")
        return False
    sign = 1 if direction == "BUY" else -1
    snapshot = market["snapshot"]
    entry = float(snapshot["offer"] if sign > 0 else snapshot["bid"])
    digits = int(snapshot.get("decimalPlacesFactor", 5))  # the market's price precision
    order = {
        "epic": epic,
        "direction": direction,
        "size": size,
        "guaranteedStop": False,
        "stopLevel": round(entry * (1 - sign * STOP_LOSS_PCT), digits),
        "profitLevel": round(entry * (1 + sign * TAKE_PROFIT_PCT), digits),
    }
    try:
        accepted, outcome = deal_outcome(confirm(capital("POST", "/positions", json=order)["dealReference"]))
    except (CapitalError, requests.RequestException) as e:
        log.error(f"Error submitting {direction} for {epic}: {e}")
        return False
    log.info(
        f"{direction} submitted -> {epic} size={size:g} (~{size * unit_value:,.2f} {currency}) "
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
def trade_new_bars(pool: dict, pairs: dict, new_bars: dict, positions: dict, currency: str) -> dict:
    """Act on the markets whose bar just closed. `new_bars` maps epic ->
    (bar time, closes); `positions` is every open position on the account.
    Returns the exchange rates it fetched, for the dashboard."""
    quotes = Quotes(new_bars, pairs, currency)
    held = [s for s in positions if s in pool]

    # Reversals first, so any slot they free is available to this bar's entries.
    candidates = []
    for epic, (_, closes) in new_bars.items():
        signal = detect_streak(closes)
        position = positions.get(epic)
        if signal is None:
            continue
        if position is None:
            candidates.append((abs(streak_move(closes)), epic, "BUY" if signal == "bullish" else "SELL"))
        elif (position["side"] == "short") == (signal == "bullish"):
            if close_position(epic, position, f"{signal} reversal"):
                held.remove(epic)

    # Biggest streak first, while slots last.
    for move, epic, direction in sorted(candidates, reverse=True):
        market, value = quotes.markets.get(epic), quotes.unit_value(epic)
        if len(held) >= MAX_OPEN_POSITIONS:
            log.info(f"{epic}: {direction} streak ({move:.2%}), but {MAX_OPEN_POSITIONS} positions "
                     f"are already open; skipping this bar.")
        elif market is None or value is None or market["snapshot"].get("marketStatus") != "TRADEABLE":
            log.info(f"{epic}: {direction} streak, but Capital.com says it isn't tradeable right now; skipping this bar.")
        elif open_position(epic, market, value, direction, currency):
            held.append(epic)
    return quotes.rates


def log_bar_summary(new_bars: dict, positions: dict, pool: dict) -> None:
    """One line per batch of newly closed bars: what's streaking and what's held."""
    rising = [s for s, (_, c) in new_bars.items() if detect_streak(c) == "bullish"]
    falling = [s for s, (_, c) in new_bars.items() if detect_streak(c) == "bearish"]
    held = [f"{s} {p['side']} {p['pl']:+.2f}" for s, p in positions.items() if s in pool]
    closed_at = time.strftime("%a %H:%M UTC", time.gmtime(max(t for t, _ in new_bars.values()) + BAR_SECONDS))
    log.info(
        f"Bar closed {closed_at} ({len(new_bars)} market(s)) | rising: {', '.join(rising) or 'none'} | "
        f"falling: {', '.join(falling) or 'none'} | "
        f"held {len(held)}/{MAX_OPEN_POSITIONS}: {', '.join(held) or 'none'}"
    )


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
    backoff = min(ERROR_BACKOFF_SECONDS * consecutive_errors, 300)
    log.info(f"Retrying in {backoff}s...")
    time.sleep(backoff)


def run_bot() -> None:
    account = connect()
    currency = account["currency"]
    pool, pairs = resolve_pool(currency)
    rates = Quotes([], pairs, currency).rates

    log.info("=" * 78)
    log.info("Capital.com Momentum Streak Scanner starting — DEMO ACCOUNT ONLY — HIGH RISK / EXPERIMENTAL")
    log.info(f"Account {account['id']} | cash={account['cash']:,.2f} {currency} | equity={account['equity']:,.2f} | "
             f"available={account['available']:,.2f}")
    if account["available"] <= 0:
        log.warning("Nothing is available to trade with: every order will be refused until the demo account "
                    "is topped up on Capital.com.")
    log.info(f"Pool: {len(pool)}/{len(POOL)} markets usable")
    log.info(
        f"Budget={BUDGET:,.2f} {currency} in {MAX_OPEN_POSITIONS} slices of {TRADE_EXPOSURE:,.2f} | "
        f"Streak length={STREAK_LENGTH} bars | Stop-loss={STOP_LOSS_PCT * 100:g}% | "
        f"Take-profit={TAKE_PROFIT_PCT * 100:g}% | Timeframe={TIMEFRAME}"
    )
    log.info("=" * 78)
    dashboard.describe(account=account["id"], currency=currency, config={
        "markets": list(pool), "budget": BUDGET, "maxPositions": MAX_OPEN_POSITIONS,
        "timeframe": TIMEFRAME, "streakLength": STREAK_LENGTH, "stopLossPercent": STOP_LOSS_PCT * 100,
        "takeProfitPercent": TAKE_PROFIT_PCT * 100,
    })
    dashboard.accept_closes(lambda *command: close_from_dashboard(pool, *command))

    seen_bar = None  # epic -> start time of the latest closed bar already dealt with
    consecutive_errors = 0

    while True:
        try:
            if dashboard.due():
                report_to_dashboard(pool, rates)

            latest = {}
            for epic in pool:
                bars = fetch_closes(epic, STREAK_LENGTH + 1)
                if bars is not None:
                    latest[epic] = bars

            if seen_bar is None:
                # Bars that closed before startup are never traded on.
                seen_bar = {s: t for s, (t, _) in latest.items()}
                log.info(f"Waiting for the next {BAR_MINUTES[TIMEFRAME]}-minute bar to close before trading.")
            else:
                new_bars = {s: bars for s, bars in latest.items() if bars[0] != seen_bar.get(s)}
                if new_bars:
                    positions = fetch_positions()
                    log_bar_summary(new_bars, positions, pool)
                    rates = trade_new_bars(pool, pairs, new_bars, positions, currency)
                    seen_bar.update({s: t for s, (t, _) in new_bars.items()})
        except Exception as e:  # anything unexpected counts toward the safety cutoff, not a crash
            consecutive_errors += 1
            log.error(f"[{consecutive_errors}] Error this loop: {e}")
            sleep_after_error(consecutive_errors)
            continue

        consecutive_errors = 0
        dashboard.sleep(LOOP_INTERVAL_SECONDS, lambda: report_to_dashboard(pool, rates))  # reports fall due while it waits


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        log.info("Bot stopped manually (KeyboardInterrupt). Goodbye.")
        sys.exit(0)
