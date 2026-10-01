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
- Overnight : the positions it opened are closed 15 minutes before the
  daily rollover (22:00 UK), so no overnight fee is paid, and nothing new
  opens from an hour before it to 45 minutes after (SCANNER_FLAT_MINUTES /
  SCANNER_LAST_ENTRY_MINUTES, 0 = off; see shared/rollover.py). It knows
  its own positions by the deal IDs saved in own_trades.json beside it.
- Stagnancy : one of its own positions that has gone nowhere for a while is
  logged (shadow, the default) or closed with the reason TIMEOUT_STAGNANT,
  freeing its slot (SCANNER_STAGNANT_* settings; see shared/stagnancy.py).
- Broadcasts: with DASHBOARD_BROADCAST=1 it takes broadcast trades from the
  dashboard (shared/broadcast.py) - the admin's market and side, its own
  slice size (or less), stop-loss, take-profit and limits. A market outside
  the pool is managed until its trade closes (brackets, the pre-rollover
  close, the stagnancy timeout) but never traded on a streak.

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
import broadcast  # noqa: E402 - needs the path above
import rollover  # noqa: E402 - needs the path above
import stagnancy  # noqa: E402 - needs the path above
import symbols  # noqa: E402 - needs the path above

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("CAPITAL_API_KEY", "")
EMAIL = os.environ.get("CAPITAL_EMAIL", "")
PASSWORD = os.environ.get("CAPITAL_EMAIL_PASSWORD", "")
ACCOUNT_ID = os.environ.get("CAPITAL_ACCOUNT_ID", "")
BASE_URL = "https://demo-api-capital.backend-capital.com/api/v1"  # Demo only — do not change.
ACCOUNT_MODE = "demo" if "demo-api" in BASE_URL else "live"  # for the dashboard's broadcast previews

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
OWN_TRADES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "own_trades.json")
BROADCASTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "broadcasts.json")

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
own_trades = rollover.OwnTrades(OWN_TRADES_FILE)  # the positions this bot opened - only these close before the rollover
broadcasts = broadcast.Book(path=BROADCASTS_FILE, log=log)  # the broadcast trades it acted on (shared/broadcast.py)


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


_open_problem = ""  # why the last open_position() wasn't accepted, for a broadcast's answer


def open_position(epic: str, market: dict, unit_value: float, direction: str, currency: str,
                  size: float = None) -> bool:
    """Market order with the stop-loss and take-profit attached: one budget
    slice, or `size` (a broadcast's). True if accepted. (Capital.com's API
    takes no label for an order, so a broadcast's is known by its deal ID.)"""
    global _open_problem
    _open_problem = ""
    if rollover.entries_paused():
        log.info(f"{epic}: {direction} signal, but it's too near the daily rollover to open a trade; skipping.")
        _open_problem = "too near the daily rollover"
        return False
    size = size_for_slice(market, unit_value) if size is None else size
    if size == 0:
        log.info(f"{epic}: {direction} signal, but its smallest trade is now worth more than a slice; skipping.")
        _open_problem = "its smallest trade is worth more than a slice"
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
        confirmation = confirm(capital("POST", "/positions", json=order)["dealReference"])
    except (CapitalError, requests.RequestException) as e:
        log.error(f"Error submitting {direction} for {epic}: {e}")
        _open_problem = str(e)
        return False
    accepted, outcome = deal_outcome(confirmation)
    if accepted:
        # The position's deal ID is one of these (as the strategy bots' Capital.com adapter records them).
        own_trades.add(confirmation.get("dealId"),
                       *(d.get("dealId") for d in confirmation.get("affectedDeals") or ()))
    else:
        _open_problem = outcome
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


def close_before_rollover() -> None:
    """Close every position this bot opened, so none is charged an
    overnight fee. Other bots' and hand-made positions are left alone."""
    items = capital("GET", "/positions").get("positions", [])
    own_trades.keep_open(i["position"]["dealId"] for i in items)
    for item in items:
        p, epic = item["position"], item["market"]["epic"]
        if p["dealId"] not in own_trades:
            continue
        side = "long" if p["direction"] == "BUY" else "short"
        try:
            closed, outcome = deal_outcome(confirm(capital("DELETE", f"/positions/{p['dealId']}")["dealReference"]))
        except (CapitalError, requests.RequestException) as e:
            log.error(f"Error closing {epic} ({rollover.REASON}): {e}")
            continue
        log.info(f"CLOSE submitted ({rollover.REASON}) -> {epic} {side} {float(p['size']):g} result={outcome}")
        if closed:
            CLOSE_REASONS[p["dealId"]] = rollover.REASON


# ---------------------------------------------------------------------------
# STAGNANCY TIMEOUT (shared/stagnancy.py)
# ---------------------------------------------------------------------------
watch = None  # the timeout, set up in run_bot()


def stagnancy_pass(pool: dict, pairs: dict, currency: str) -> None:
    """The stagnancy timeout over the positions this bot opened: one that has
    gone nowhere for a while is logged (shadow) or closed (enforce). Its
    slot is free once Capital.com no longer lists it - the next bar's count
    of open positions doesn't include it."""
    now = time.time()
    items = [i for i in capital("GET", "/positions").get("positions", [])
             if i["market"]["epic"] in pool and i["position"]["dealId"] in own_trades]
    quotes = Quotes({i["market"]["epic"] for i in items}, pairs, currency) if items else None
    held = []
    for item in items:
        p, epic = item["position"], item["market"]["epic"]
        market = quotes.markets.get(epic)
        snapshot = (market or {}).get("snapshot") or {}
        tradeable = snapshot.get("marketStatus") == "TRADEABLE" and snapshot.get("bid") is not None
        if tradeable:
            watch.observe(epic, now, float(snapshot["bid"]), float(snapshot["offer"]))
        else:
            watch.observe(epic, now, None, None, False)
        entry, size = float(p["level"]), float(p["size"])
        # What it stands to lose at its stop-loss, in the account's currency.
        distance = abs(entry - float(p["stopLevel"])) if p.get("stopLevel") is not None else entry * STOP_LOSS_PCT
        value = quotes.unit_value(epic)
        risk = size * distance * value / mid(market) if value and tradeable else None
        held.append((stagnancy.Held(
            key=p["dealId"], symbol=epic, direction="long" if p["direction"] == "BUY" else "short", size=size,
            entry=entry, opened_at=utc_seconds(p["createdDateUTC"]), risk=risk, refs=(p["dealId"],),
            # Capital.com values a position at the price it would close at, so net of the spread;
            # overnight fees come off the balance instead, so aren't in it.
            pnl=float(p.get("upl") or 0),
        ), tradeable))
    watch.run(held, now, close_deal_now)


def close_deal_now(held) -> dict:
    """Close one of the bot's positions at market for the stagnancy timeout -
    by deal ID, so anyone else's in the market is left alone. Capital.com
    closes whole positions only."""
    try:
        confirmation = confirm(capital("DELETE", f"/positions/{held.key}")["dealReference"])
    except (CapitalError, requests.RequestException) as e:
        # A position its stop-loss or take-profit closed a moment before is
        # refused here (not found): the next pass finds it gone.
        log.error(f"Error closing {held.symbol} ({stagnancy.TIMEOUT_REASON}): {e}")
        return {"done": False, "problem": str(e)}
    closed, outcome = deal_outcome(confirmation)
    log.info(f"CLOSE submitted ({stagnancy.TIMEOUT_REASON}) -> {held.symbol} {held.direction} {held.size:g} "
             f"result={outcome}")
    if not closed:
        return {"done": False, "problem": outcome}
    CLOSE_REASONS[held.key] = stagnancy.TIMEOUT_REASON
    level, profit = confirmation.get("level"), confirmation.get("profit")  # profit: if the confirmation has it
    return {"done": True, "price": float(level) if level is not None else None,
            "pnl": float(profit) if profit is not None else None}


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
# BROADCAST TRADES FROM THE DASHBOARD (shared/broadcast.py)
# ---------------------------------------------------------------------------
guests = {}  # epic -> Capital.com's market details: a broadcast trade's market outside the pool, managed but never traded


def managed(pool: dict) -> dict:
    """The pool, plus the markets outside it holding a broadcast trade."""
    return {**guests, **pool}


def reported(pool: dict) -> dict:
    """What the dashboard reports cover: the managed markets, and those of
    broadcast trades that have closed lately (their closed trades)."""
    return {**{s: None for s in broadcasts.symbols()}, **managed(pool)}


def restore_guests(pool: dict, pairs: dict, home: str) -> None:
    """After a restart: look up the markets outside the pool its open broadcast trades are in."""
    wanted = sorted(broadcasts.open_symbols() - set(pool))
    found = fetch_markets(wanted) if wanted else {}
    for epic in wanted:
        if epic not in found:
            log.warning(f"{epic}: couldn't look up its broadcast trade's market; Capital.com still holds its stop-loss "
                        f"and take-profit.")
            continue
        guests[epic] = found[epic]
        pairs.update(conversion_pairs({found[epic]["instrument"]["currency"]} - set(pairs), home))
        log.info(f"{epic}: managing its broadcast trade (not in the pool).")


def broadcast_plan(pool: dict, pairs: dict, currency: str, request, now: float, most: float = None) -> dict:
    """The trade this bot would make for a broadcast, every limit applied -
    or broadcast.Declined saying why not."""
    found = symbols.candidates("capital", request.symbol)
    epics = [c.code.upper() for c in found]
    known = fetch_markets([e for e in epics if e not in pool and e not in guests]) if epics else {}
    epic = candidate = None
    for c in found:
        if c.code.upper() in pool or c.code.upper() in guests or c.code.upper() in known:
            epic, candidate = c.code.upper(), c
            break
    if epic is None:
        raise broadcast.Declined(broadcast.UNAVAILABLE, f"{request.symbol} isn't available on Capital.com"
                                 + (f" (looked for {', '.join(epics)})." if epics else "."))
    details = pool.get(epic) or guests.get(epic) or known[epic]
    instrument_currency = details["instrument"]["currency"]
    if instrument_currency != currency and instrument_currency not in pairs:
        pairs.update(conversion_pairs({instrument_currency}, currency))
    if rollover.entries_paused(now):
        raise broadcast.Declined(broadcast.CLOSED, f"{epic}: too near the daily rollover to open a trade.")
    positions = fetch_positions()
    if epic in positions:
        raise broadcast.Declined(broadcast.NO_SLOT, f"An {epic} position is already open on the account.")
    held = [s for s in positions if s in pool or s in guests]
    if len(held) >= MAX_OPEN_POSITIONS:
        raise broadcast.Declined(broadcast.NO_SLOT, f"{len(held)} positions are already open (max {MAX_OPEN_POSITIONS}).")
    if watch is not None and watch.cooling(epic, now):
        raise broadcast.Declined(broadcast.RISK, f"{epic}: the stagnancy timeout closed it lately (cooldown).")
    quotes = Quotes([epic], pairs, currency)
    market, value = quotes.markets.get(epic), quotes.unit_value(epic)
    if market is None or market["snapshot"].get("marketStatus") != "TRADEABLE":
        raise broadcast.Declined(broadcast.CLOSED, f"{epic}: Capital.com says it isn't tradeable right now.")
    if value is None:
        raise broadcast.Declined(broadcast.UNAVAILABLE, f"{epic}: no {instrument_currency}/{currency} market to value it "
                                                        f"in {currency}.")

    exposure, capped = broadcast.exposure_for(request, TRADE_EXPOSURE, currency, "budget slice")
    rules = market["dealingRules"]
    step, minimum = float(rules["minSizeIncrement"]["value"]), float(rules["minDealSize"]["value"])
    size = math.floor(exposure / value / step + 1e-9) * step
    if most is not None:
        size = min(size, math.floor(most / step + 1e-9) * step)
    size = round(size, 8)
    if size < minimum or size <= 0:
        raise broadcast.Declined(broadcast.RISK, f"{epic}: its smallest trade (size {minimum:g}) is worth about "
                                                 f"{minimum * value:,.2f} {currency}, more than the {exposure:,.2f} it "
                                                 f"may trade.")
    sign = 1 if request.side == "buy" else -1
    snapshot = market["snapshot"]
    entry = float(snapshot["offer"] if sign > 0 else snapshot["bid"])
    digits = int(snapshot.get("decimalPlacesFactor", 5))
    rule = watch.book.rule(epic) if watch is not None else None
    exits = (f"stop-loss {STOP_LOSS_PCT * 100:g}% / take-profit {TAKE_PROFIT_PCT * 100:g}% at Capital.com; "
             + (f"closed {rollover.FLAT_MINUTES} min before the rollover" if rollover.FLAT_MINUTES else "held overnight")
             + ("; streak reversal" if epic in pool else "; managed until it closes (not in the pool)")
             + (f"; stagnancy timeout ({rule.mode})" if rule and rule.mode != "off" else ""))
    return {"epic": epic, "market": market, "value": value, "size": size, "direction": "BUY" if sign > 0 else "SELL",
            "capped": capped,
            "figures": broadcast.figures(
                symbol=epic, name=market["instrument"].get("name"), size=size, sizeUnit="units",
                exposure=round(size * value, 2), risk=round(size * value * STOP_LOSS_PCT, 2), currency=currency,
                entry=entry, stopLoss=round(entry * (1 - sign * STOP_LOSS_PCT), digits),
                takeProfit=round(entry * (1 + sign * TAKE_PROFIT_PCT), digits), accountMode=ACCOUNT_MODE, exits=exits,
                capped=capped or None, standIn=candidate.canonical if candidate.stand_in else None, previewedAt=now)}


def broadcast_preview(pool: dict, pairs: dict, currency: str, command: dict) -> tuple:
    """What this bot would do with a broadcast trade: (a line, {figures})."""
    request = broadcast.Request.parse(command)
    plan = broadcast_plan(pool, pairs, currency, request, time.time())
    f = plan["figures"]
    line = (f"Would {request.side} {f['size']:g} of {f['symbol']} at ~{f['entry']:g}: stop-loss {f['stopLoss']:g}, "
            f"take-profit {f['takeProfit']:g}" + (f" ({plan['capped']})" if plan["capped"] else ""))
    log.info(f"Broadcast #{request.id}: {line}")
    return line + ".", f


def broadcast_open(pool: dict, pairs: dict, currency: str, command: dict) -> tuple:
    """Open a broadcast trade the admin confirmed - every limit checked again
    on fresh prices, never bigger than previewed. (a line, {figures})."""
    now = time.time()
    request = broadcast.Request.parse(command)
    broadcasts.check_new(request)
    request.check_age(now)
    plan = broadcast_plan(pool, pairs, currency, request, now, most=request.size)
    epic, market = plan["epic"], plan["market"]
    broadcasts.opening(request, epic, (market["instrument"].get("name"),), now)
    before = set(own_trades.ids)
    if not open_position(epic, market, plan["value"], plan["direction"], currency, size=plan["size"]):
        broadcasts.failed(request)
        raise CommandError(f"Capital.com didn't accept it: {_open_problem or 'see the bot log'}.")
    refs = sorted(own_trades.ids - before)
    broadcasts.opened(request, refs, now)
    if epic not in pool:
        guests[epic] = market
        log.info(f"{epic}: not in the pool - managing its broadcast trade until it closes, never trading it on a streak.")
    entry = plan["figures"]["entry"]
    verb = "Bought" if request.side == "buy" else "Sold"
    return (f"{verb} {plan['size']:g} of {epic} at ~{entry:g}" + (f" (deal {refs[0]})" if refs else "") + "."), \
        {"symbol": epic, "size": plan["size"], "price": entry, **({"ref": refs[0]} if refs else {})}


def broadcast_symbols(pool: dict) -> list:
    """The instrument names the dashboard can offer for this bot."""
    return sorted(set(symbols.names("capital")) | {symbols.canonical(s) or s for s in pool})


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
    for deal_id, steps in deals.items():
        if deal_id in open_ids:
            continue
        steps.sort(key=lambda a: a["dateUTC"])
        close = steps[-1]
        opening = steps[0]["details"] if len(steps) > 1 else _seen_open.get(deal_id)
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
            "openedAt": utc_seconds(steps[0]["dateUTC"]) if len(steps) > 1 else opening.get("openedAt"),
            "closedAt": closed_at,
            "pnl": pnl,
            "closeReason": reason,
            **(watch.fields_for(deal_id) if watch is not None else {}),  # the stagnancy timeout's, if any
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
    # Broadcast trades no longer open have closed; their markets outside the pool stop being managed.
    now = time.time()
    broadcasts.sync({i["market"]["epic"] for i in items if i["position"]["dealId"] in own_trades}, now)
    broadcasts.prune(now)
    for epic in set(guests) - broadcasts.open_symbols():
        del guests[epic]
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
    held = [s for s in positions if s in pool or s in guests]  # a broadcast trade outside the pool holds a slot too

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
            if watch is not None and watch.closing_in(epic):
                log.info(f"{epic}: {signal} reversal, but its {stagnancy.TIMEOUT_REASON} close is under way.")
            elif close_position(epic, position, f"{signal} reversal"):
                held.remove(epic)

    # Biggest streak first, while slots last.
    for move, epic, direction in sorted(candidates, reverse=True):
        market, value = quotes.markets.get(epic), quotes.unit_value(epic)
        if len(held) >= MAX_OPEN_POSITIONS:
            log.info(f"{epic}: {direction} streak ({move:.2%}), but {MAX_OPEN_POSITIONS} positions "
                     f"are already open; skipping this bar.")
        elif watch is not None and watch.cooling(epic, time.time()):  # only with SCANNER_STAGNANT_COOLDOWN set
            log.info(f"{epic}: {direction} streak, but the stagnancy timeout closed it lately (cooldown); "
                     f"skipping this bar.")
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
    log.info(rollover.describe())
    global watch
    watch = stagnancy.start_watch("scanner", "capital", "capital-momentum-scanner", "Capital.com", log,
                                  bar_seconds=BAR_SECONDS, loop_seconds=LOOP_INTERVAL_SECONDS, currency=currency)
    log.info("=" * 78)
    dashboard.describe(account=account["id"], currency=currency, account_mode=ACCOUNT_MODE, config={
        "markets": list(pool), "budget": BUDGET, "maxPositions": MAX_OPEN_POSITIONS,
        "timeframe": TIMEFRAME, "streakLength": STREAK_LENGTH, "stopLossPercent": STOP_LOSS_PCT * 100,
        "takeProfitPercent": TAKE_PROFIT_PCT * 100, **watch.book.config(),
    })
    dashboard.accept_closes(lambda *command: close_from_dashboard(managed(pool), *command))
    restore_guests(pool, pairs, currency)
    dashboard.tag_rows(broadcasts.tags)
    dashboard.accept_broadcasts(lambda command: broadcast_preview(pool, pairs, currency, command),
                                lambda command: broadcast_open(pool, pairs, currency, command),
                                broadcast_symbols(pool))

    seen_bar = None  # epic -> start time of the latest closed bar already dealt with
    consecutive_errors = 0

    while True:
        try:
            if dashboard.due():
                report_to_dashboard(reported(pool), rates)
            if rollover.flat_due():
                close_before_rollover()
            if watch.book.active:
                stagnancy_pass(managed(pool), pairs, currency)

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
        dashboard.sleep(LOOP_INTERVAL_SECONDS, lambda: report_to_dashboard(reported(pool), rates))  # reports fall due while it waits


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        log.info("Bot stopped manually (KeyboardInterrupt). Goodbye.")
        sys.exit(0)
