#!/usr/bin/env python3
"""
OANDA Practice Momentum Streak Scanner — sized for a small account
====================================================================

The IG momentum scanner (ig_momentum_scanner_bot.py) moved to OANDA's
fxTrade Practice (demo) account. Same signal, same longs AND shorts, same
stop-loss / take-profit attached to the order — but OANDA trades in single
units (one unit of EUR/USD is one euro), so each trade is sized from a small
budget instead of being IG's minimum contract worth thousands. And unlike
IG, a practice login can generate its own API token: no live account needed.

Strategy — momentum streak (no smoothing, reacts fast, whipsaws more)
-----------------------------------------------------------------------
- Timeframe : 15-minute bars by default (SCANNER_TIMEFRAME env var: M1, M5,
  M15 or M30, shared with the other scanners); OANDA's own mid-price
  candles, closed bars only
- Buy       : STREAK_LENGTH consecutive HIGHER closes -> open LONG
- Sell      : STREAK_LENGTH consecutive LOWER closes  -> open SHORT
- A reversal streak closes an opposing position; the same-direction streak
  while already positioned is a no-op (no pyramiding).
- Risk mgmt : 2% stop-loss / 5% take-profit by default (STOP_LOSS_PERCENT /
  TAKE_PROFIT_PERCENT env vars, shared with the other scanners), attached
  to the order, so OANDA enforces them even while the bot isn't running.

Sizing
------
OANDA_BUDGET (default 100, in the account's currency) is split into
OANDA_MAX_POSITIONS (default 5) slices, and each trade is worth one slice —
£20 of EUR/USD by default, so a 2% stop-loss costs about £0.40. The budget
is exposure (what the positions are worth), not margin, so the default uses
no leverage; set it above the balance to use some. A market whose smallest
trade is worth more than a slice is skipped at startup with the reason
logged. OANDA sells fractions of a unit of indices and gold, but their
smallest trades still range from about £60 (US 500) to £1,100 (UK 100),
so at the default budget only the currency pairs trade; a budget of 5,500
takes in the whole pool. Once OANDA_MAX_POSITIONS positions are open,
further signals are skipped; when several markets signal at once, the
biggest move across its streak gets the slot first.
Sizes come from the budget, not the balance, so the bot trades the same on
OANDA's 100,000 practice balance as on a real £100 one.

Differences from the IG scanner
-------------------------------
- Real price history: OANDA's candles are free and unmetered, so there are
  no locally built bars, no bar file and no 45-minute warm-up.
- Closed bars only, acted on as they close: each market is traded at most
  once per bar, and never on a bar that closed before the bot started — so
  after a start, nothing happens until the next bar closes (up to one bar).
- No request pacing: this makes ~30 requests a minute (~60 on 1-minute
  bars), far under OANDA's limit.

One bot per account
-------------------
It treats every position in its markets as its own, and OANDA nets buys and
sells of one market into a single position. Running it alongside
oanda_ema_bot.py on the same account and markets would have each closing
the other's trades — give each bot its own sub-account (the OANDA hub can
add sub-accounts to a practice login) and set OANDA_ACCOUNT_ID per window.

Setup
-----
1. pip install -r requirements.txt   (just `requests`)
2. OANDA_API_TOKEN: log into the practice account in the OANDA hub, then
   Tools > API > Generate.
3. OANDA_ACCOUNT_ID: optional when the token can only see one account.
4. Run:
       python oanda_momentum_scanner_bot.py

This script only ever talks to OANDA's practice endpoint
(https://api-fxpractice.oanda.com). It never places live-money orders.
"""

import logging
import math
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))
from dashboard_reporter import CommandError, DashboardReporter  # noqa: E402 - needs the path above

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
API_TOKEN = os.environ.get("OANDA_API_TOKEN", "")
ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "")
BASE_URL = "https://api-fxpractice.oanda.com"  # Practice (demo) only — do not change.

# The IG scanner's pool in OANDA's instrument names (OANDA only quotes the
# pound against the euro as EUR/GBP). Anything this account doesn't offer,
# or can't trade within one budget slice, is skipped at startup, not fatal.
DEFAULT_POOL = (
    "UK100_GBP,SPX500_USD,US30_USD,NAS100_USD,DE30_EUR,JP225_USD,"  # indices
    "XAU_USD,XAG_USD,BCO_USD,WTICO_USD,"                            # commodities
    "EUR_USD,GBP_USD,USD_JPY,EUR_GBP,AUD_USD"                       # FX majors
)
POOL = [
    s.strip().upper()
    for s in os.environ.get("OANDA_POOL", DEFAULT_POOL).split(",")
    if s.strip()
]

STREAK_LENGTH = int(os.environ.get("STREAK_LENGTH", "3"))  # consecutive up/down bars to trigger

BUDGET = float(os.environ.get("OANDA_BUDGET", "100"))  # total exposure, in the account's currency
MAX_OPEN_POSITIONS = int(os.environ.get("OANDA_MAX_POSITIONS", "5"))
TRADE_EXPOSURE = BUDGET / MAX_OPEN_POSITIONS

# Percent of the entry price, e.g. STOP_LOSS_PERCENT=0.5 for 0.5%.
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PERCENT", "2")) / 100
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PERCENT", "5")) / 100

BAR_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30}
TIMEFRAME = os.environ.get("SCANNER_TIMEFRAME", "").strip().upper() or "M15"  # bar length
assert TIMEFRAME in BAR_MINUTES, "SCANNER_TIMEFRAME must be M1, M5, M15 or M30"
GRANULARITY = TIMEFRAME        # OANDA's own names for these
BAR_SECONDS = BAR_MINUTES[TIMEFRAME] * 60
LOOP_INTERVAL_SECONDS = min(30, BAR_SECONDS // 4)  # how often to look for newly closed bars
REQUEST_TIMEOUT_SECONDS = 20
ERROR_BACKOFF_SECONDS = 60
MAX_CONSECUTIVE_ERRORS = 10

assert STREAK_LENGTH >= 2, "STREAK_LENGTH must be at least 2 to mean anything"
assert MAX_OPEN_POSITIONS >= 1, "OANDA_MAX_POSITIONS must be at least 1"
assert BUDGET > 0, "OANDA_BUDGET must be positive"
assert STOP_LOSS_PCT > 0 and TAKE_PROFIT_PCT > 0, "STOP_LOSS_PERCENT and TAKE_PROFIT_PERCENT must be positive"
assert POOL, "OANDA_POOL resolved to an empty pool"

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("oanda_momentum_bot")

# Off unless DASHBOARD_URL and DASHBOARD_TOKEN are set (see shared/dashboard_reporter.py).
dashboard = DashboardReporter("oanda-momentum-scanner", "OANDA momentum scanner", broker="OANDA", strategy="Momentum streak")


# ---------------------------------------------------------------------------
# CLIENT
# ---------------------------------------------------------------------------
class OandaError(Exception):
    """OANDA answered, but refused the request — the message says why."""


_session = requests.Session()
_session.headers.update({
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json",
    "Accept-Datetime-Format": "UNIX",  # candle times as epoch seconds, not RFC 3339
})


def oanda(method: str, path: str, **kwargs) -> dict:
    """One API call. Raises OandaError when OANDA refuses it, and
    requests.RequestException when OANDA can't be reached."""
    response = _session.request(method, BASE_URL + path, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400:
        reject = next((v for k, v in body.items() if k.endswith("RejectTransaction")), {})
        reason = reject.get("rejectReason") or body.get("errorCode")
        message = body.get("errorMessage") or response.text[:200] or response.reason
        raise OandaError(f"HTTP {response.status_code}: {message}" + (f" ({reason})" if reason else ""))
    return body


def connect() -> tuple:
    """(account ID, account summary). Exits if there's no usable account."""
    if not API_TOKEN:
        log.error("Missing credentials. Set OANDA_API_TOKEN (OANDA hub > Tools > API > Generate, "
                  "while logged into the practice account).")
        sys.exit(1)
    try:
        account_id = ACCOUNT_ID
        if not account_id:
            accounts = oanda("GET", "/v3/accounts")["accounts"]
            if len(accounts) != 1:
                ids = ", ".join(a["id"] for a in accounts) or "none"
                log.error(f"This token can see {len(accounts)} accounts ({ids}); set OANDA_ACCOUNT_ID to pick one.")
                sys.exit(1)
            account_id = accounts[0]["id"]
        summary = oanda("GET", f"/v3/accounts/{account_id}/summary")["account"]
    except (OandaError, requests.RequestException) as e:
        # A live-account token is refused here too: it only works on the live endpoint.
        log.error(f"Couldn't open the OANDA practice account: {e}")
        sys.exit(1)
    return account_id, summary


# ---------------------------------------------------------------------------
# MARKETS / SIZING
# ---------------------------------------------------------------------------
def fetch_prices(account_id: str, instruments: list, currency: str) -> dict:
    """{instrument: {"bid", "ask", "tradeable", "unit_value"}}, where
    unit_value is what one unit is worth in the account's currency."""
    body = oanda("GET", f"/v3/accounts/{account_id}/pricing",
                 params={"instruments": ",".join(instruments), "includeHomeConversions": "true"})
    to_home = {c["currency"]: float(c["positionValue"]) for c in body.get("homeConversions", [])}
    prices = {}
    for p in body.get("prices", []):
        bid = float(p["bids"][0]["price"]) if p.get("bids") else float(p["closeoutBid"])
        ask = float(p["asks"][0]["price"]) if p.get("asks") else float(p["closeoutAsk"])
        quote_currency = p["instrument"].rsplit("_", 1)[1]  # EUR_USD is priced in USD, UK100_GBP in GBP
        if quote_currency == currency:
            rate = 1.0
        else:
            rate = to_home.get(quote_currency) or float(p["quoteHomeConversionFactors"]["positiveUnits"])
        prices[p["instrument"]] = {
            "bid": bid,
            "ask": ask,
            "tradeable": p.get("tradeable", p.get("status") == "tradeable"),
            "unit_value": (bid + ask) / 2 * rate,
        }
    return prices


def units_for_slice(instrument: dict, unit_value: float) -> float:
    """Units worth up to one budget slice, rounded down to a size OANDA
    accepts; 0 if even its smallest trade is worth more than a slice."""
    step = 10.0 ** -int(instrument["tradeUnitsPrecision"])
    units = math.floor(TRADE_EXPOSURE / unit_value / step + 1e-9) * step
    return units if units >= float(instrument["minimumTradeSize"]) else 0.0


def resolve_pool(account_id: str, currency: str) -> dict:
    """{instrument name: OANDA's instrument details} for the usable part of
    the pool. Like the IG scanner, this SKIPS (logging why) anything it
    can't trade instead of exiting — the pool is about breadth."""
    offered = {i["name"]: i for i in oanda("GET", f"/v3/accounts/{account_id}/instruments")["instruments"]}
    prices = fetch_prices(account_id, [s for s in POOL if s in offered], currency)

    usable = {}
    for symbol in POOL:
        instrument = offered.get(symbol)
        if instrument is None:
            stem = re.sub(r"\d+$", "", symbol.split("_")[0])  # DE30_EUR -> DE, to spot a rename
            similar = sorted(n for n in offered if n.startswith(stem))
            hint = f" — similar: {', '.join(similar)}" if similar else ""
            log.warning(f"Skipping {symbol}: this account doesn't offer it{hint}.")
            continue
        price = prices.get(symbol)
        if price is None:
            log.warning(f"Skipping {symbol}: OANDA gave no price for it.")
            continue
        if units_for_slice(instrument, price["unit_value"]) == 0:
            smallest = float(instrument["minimumTradeSize"]) * price["unit_value"]
            log.warning(
                f"Skipping {symbol}: its smallest trade (minimum size {instrument['minimumTradeSize']}) is worth "
                f"about {smallest:,.0f} {currency}, more than a {TRADE_EXPOSURE:,.2f} {currency} slice — "
                f"raise OANDA_BUDGET to include it."
            )
            continue
        usable[symbol] = instrument
        log.info(f"Using {symbol} ({instrument.get('displayName', symbol)})")

    if not usable:
        log.critical("Nothing in the pool is tradable within the budget. Exiting.")
        sys.exit(1)
    return usable


# ---------------------------------------------------------------------------
# MARKET DATA / SIGNAL
# ---------------------------------------------------------------------------
def fetch_closes(symbol: str, count: int):
    """(start time of the latest closed bar, its last `count` closes, oldest
    first), or None if OANDA has no closed bars for it."""
    body = oanda("GET", f"/v3/instruments/{symbol}/candles",
                 params={"price": "M", "granularity": GRANULARITY, "count": count + 1})
    candles = [c for c in body.get("candles", []) if c.get("complete")]
    if not candles:
        return None
    return float(candles[-1]["time"]), [float(c["mid"]["c"]) for c in candles[-count:]]


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
def fetch_positions(account_id: str) -> dict:
    """{instrument: {"side": "long"/"short", "units", "pl"}} for every open position."""
    positions = {}
    for p in oanda("GET", f"/v3/accounts/{account_id}/openPositions").get("positions", []):
        for side in ("long", "short"):
            units = float(p[side]["units"])  # a short's units are negative
            if units != 0:
                positions[p["instrument"]] = {"side": side, "units": abs(units),
                                              "pl": float(p[side].get("unrealizedPL", 0))}
    return positions


def fill_outcome(body: dict, prefix: str) -> tuple:
    """(filled?, e.g. 'FILLED 23 @ 1.08512' or 'CANCELLED (INSUFFICIENT_MARGIN)')."""
    fill = body.get(f"{prefix}FillTransaction")
    if fill:
        return True, f"FILLED {fill.get('units')} @ {fill.get('fullVWAP') or fill.get('price')}"
    cancel = body.get(f"{prefix}CancelTransaction")
    if cancel:
        return False, f"CANCELLED ({cancel.get('reason', '?')})"
    return False, "no fill reported"


def open_position(account_id: str, symbol: str, instrument: dict, price: dict, direction: str,
                  currency: str) -> bool:
    """Market order with the stop-loss and take-profit attached. True if it filled."""
    units = units_for_slice(instrument, price["unit_value"])
    if units == 0:
        log.info(f"{symbol}: {direction} signal, but its smallest trade is now worth more than a slice; skipping.")
        return False
    sign = 1 if direction == "BUY" else -1
    entry = price["ask"] if sign > 0 else price["bid"]
    digits = int(instrument["displayPrecision"])  # OANDA rejects prices with more decimals
    order = {
        "type": "MARKET",
        "instrument": symbol,
        "units": f"{sign * units:.{int(instrument['tradeUnitsPrecision'])}f}",
        "timeInForce": "FOK",
        "positionFill": "DEFAULT",
        "stopLossOnFill": {"price": f"{entry * (1 - sign * STOP_LOSS_PCT):.{digits}f}"},
        "takeProfitOnFill": {"price": f"{entry * (1 + sign * TAKE_PROFIT_PCT):.{digits}f}"},
    }
    try:
        body = oanda("POST", f"/v3/accounts/{account_id}/orders", json={"order": order})
    except (OandaError, requests.RequestException) as e:
        log.error(f"Error submitting {direction} for {symbol}: {e}")
        return False
    filled, outcome = fill_outcome(body, "order")
    log.info(
        f"{direction} submitted -> {symbol} units={order['units']} "
        f"(~{units * price['unit_value']:,.2f} {currency}) stop={order['stopLossOnFill']['price']} "
        f"limit={order['takeProfitOnFill']['price']} result={outcome}"
    )
    return filled


def close_position(account_id: str, symbol: str, position: dict, reason: str) -> bool:
    """Close the whole position at market. True if it filled."""
    side = position["side"]
    try:
        body = oanda("PUT", f"/v3/accounts/{account_id}/positions/{symbol}/close", json={f"{side}Units": "ALL"})
    except (OandaError, requests.RequestException) as e:
        log.error(f"Error closing {symbol} ({reason}): {e}")
        return False
    filled, outcome = fill_outcome(body, f"{side}Order")
    log.info(f"CLOSE submitted ({reason}) -> {symbol} {side} {position['units']:g} units result={outcome}")
    for closed in (body.get(f"{side}OrderFillTransaction") or {}).get("tradesClosed", []):
        CLOSE_REASONS[closed["tradeID"]] = reason
    return filled


def close_from_dashboard(account_id: str, markets: dict, symbol: str, ref, direction: str, size) -> str:
    """A close asked for on the dashboard (DASHBOARD_COMMANDS=1): the trade
    it showed as `ref` - all of it, or `size` units - if it's still open on
    the same side. Answers what happened, or raises CommandError."""
    if symbol not in markets:
        raise CommandError(f"{symbol} isn't one of this bot's markets.")
    if not ref:
        raise CommandError("No OANDA trade ID came with it, so nothing was closed.")
    try:
        trade = oanda("GET", f"/v3/accounts/{account_id}/trades/{ref}")["trade"]
    except OandaError as e:
        raise CommandError(f"OANDA can't find trade {ref}: {e}") from None
    units = float(trade.get("currentUnits") or 0)
    if trade.get("state") != "OPEN" or trade.get("instrument") != symbol or units == 0:
        raise CommandError(f"Trade {ref} isn't open any more - it may have closed already.")
    side = "long" if units > 0 else "short"
    if side != direction:
        raise CommandError(f"Trade {ref} is {side}, not {direction}, so it was left alone.")

    body = {}
    if size is not None and size < abs(units):
        precision = int(markets[symbol]["tradeUnitsPrecision"])
        amount = math.floor(size * 10 ** precision + 1e-9) / 10 ** precision
        if amount <= 0:
            raise CommandError(f"{size:g} is less than the smallest amount OANDA trades in {symbol}.")
        body = {"units": f"{amount:.{precision}f}"}
    try:
        reply = oanda("PUT", f"/v3/accounts/{account_id}/trades/{ref}/close", **({"json": body} if body else {}))
    except OandaError as e:
        raise CommandError(f"OANDA refused: {e}") from None
    filled, outcome = fill_outcome(reply, "order")
    log.info(f"CLOSE submitted (from the dashboard) -> {symbol} trade {ref} {side} "
             f"{body.get('units', 'all')} units result={outcome}")
    if not filled:
        raise CommandError(f"OANDA didn't close it: {outcome}")
    if not body:
        CLOSE_REASONS[ref] = "closed from the dashboard"
    return f"Closed {body.get('units', 'all')} of trade {ref} ({symbol} {side}): {outcome}."


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
CLOSE_REASONS = {}  # trade ID -> why this bot closed it; stop-loss/take-profit come from OANDA


def dashboard_trade(trade: dict) -> dict:
    """An OANDA trade (open or closed) in the dashboard's shape."""
    units = float(trade.get("initialUnits") or trade.get("currentUnits") or 0)
    row = {
        "ref": trade["id"],
        "symbol": trade["instrument"],
        "direction": "long" if units > 0 else "short",
        "size": abs(units),
        "entryPrice": float(trade["price"]),
        "openedAt": trade.get("openTime"),
    }
    if trade.get("state") == "CLOSED":
        if (trade.get("stopLossOrder") or {}).get("state") == "FILLED":
            reason = "stop-loss"
        elif (trade.get("takeProfitOrder") or {}).get("state") == "FILLED":
            reason = "take-profit"
        else:
            reason = CLOSE_REASONS.get(trade["id"], "closed by the bot or by hand")
        row.update(
            exitPrice=float(trade["averageClosePrice"]) if trade.get("averageClosePrice") else None,
            closedAt=trade.get("closeTime"),
            pnl=float(trade.get("realizedPL") or 0) + float(trade.get("financing") or 0),
            closeReason=reason,
        )
    else:
        row.update(
            size=abs(float(trade.get("currentUnits") or units)),
            pnl=float(trade.get("unrealizedPL") or 0),
            stopLoss=float(trade["stopLossOrder"]["price"]) if trade.get("stopLossOrder") else None,
            takeProfit=float(trade["takeProfitOrder"]["price"]) if trade.get("takeProfitOrder") else None,
        )
    return row


def report_to_dashboard(account_id: str, markets) -> None:
    """Account, open trades and the last 50 closed trades in this bot's
    markets. Three requests, every 15 seconds or so; a failure just skips it."""
    try:
        summary = oanda("GET", f"/v3/accounts/{account_id}/summary")["account"]
        open_trades = oanda("GET", f"/v3/accounts/{account_id}/openTrades").get("trades", [])
        closed_trades = oanda("GET", f"/v3/accounts/{account_id}/trades",
                              params={"state": "CLOSED", "count": 50}).get("trades", [])
    except (OandaError, requests.RequestException):
        return
    dashboard.update(
        account={"balance": float(summary["balance"]), "equity": float(summary["NAV"]),
                 "unrealizedPl": float(summary["unrealizedPL"])},
        positions=[dashboard_trade(t) for t in open_trades if t["instrument"] in markets],
        trades=[dashboard_trade(t) for t in closed_trades if t["instrument"] in markets],
    )


# ---------------------------------------------------------------------------
# TRADING CYCLE
# ---------------------------------------------------------------------------
def trade_new_bars(account_id: str, pool: dict, new_bars: dict, positions: dict, currency: str) -> None:
    """Act on the markets whose bar just closed. `new_bars` maps symbol ->
    (bar time, closes); `positions` is every open position on the account."""
    prices = fetch_prices(account_id, list(new_bars), currency)
    held = [s for s in positions if s in pool]

    # Reversals first, so any slot they free is available to this bar's entries.
    candidates = []
    for symbol, (_, closes) in new_bars.items():
        signal = detect_streak(closes)
        position = positions.get(symbol)
        if signal is None:
            continue
        if position is None:
            candidates.append((abs(streak_move(closes)), symbol, "BUY" if signal == "bullish" else "SELL"))
        elif (position["side"] == "short") == (signal == "bullish"):
            if close_position(account_id, symbol, position, f"{signal} reversal"):
                held.remove(symbol)

    # Biggest streak first, while slots last.
    for move, symbol, direction in sorted(candidates, reverse=True):
        price = prices.get(symbol)
        if len(held) >= MAX_OPEN_POSITIONS:
            log.info(f"{symbol}: {direction} streak ({move:.2%}), but {MAX_OPEN_POSITIONS} positions "
                     f"are already open; skipping this bar.")
        elif price is None or not price["tradeable"]:
            log.info(f"{symbol}: {direction} streak, but OANDA says it isn't tradeable right now; skipping this bar.")
        elif open_position(account_id, symbol, pool[symbol], price, direction, currency):
            held.append(symbol)


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
    account_id, summary = connect()
    currency = summary["currency"]
    pool = resolve_pool(account_id, currency)

    log.info("=" * 78)
    log.info("OANDA Momentum Streak Scanner starting — PRACTICE ACCOUNT ONLY — HIGH RISK / EXPERIMENTAL")
    log.info(f"Account {account_id} | balance={summary['balance']} {currency} | NAV={summary['NAV']} | "
             f"margin available={summary['marginAvailable']}")
    log.info(f"Pool: {len(pool)}/{len(POOL)} markets usable")
    log.info(
        f"Budget={BUDGET:,.2f} {currency} in {MAX_OPEN_POSITIONS} slices of {TRADE_EXPOSURE:,.2f} | "
        f"Streak length={STREAK_LENGTH} bars | Stop-loss={STOP_LOSS_PCT * 100:g}% | "
        f"Take-profit={TAKE_PROFIT_PCT * 100:g}% | Timeframe={TIMEFRAME}"
    )
    log.info("=" * 78)
    dashboard.describe(account=account_id, currency=currency, config={
        "markets": list(pool), "budget": BUDGET, "maxPositions": MAX_OPEN_POSITIONS,
        "timeframe": TIMEFRAME, "streakLength": STREAK_LENGTH, "stopLossPercent": STOP_LOSS_PCT * 100,
        "takeProfitPercent": TAKE_PROFIT_PCT * 100,
    })
    dashboard.accept_closes(lambda *command: close_from_dashboard(account_id, pool, *command))

    seen_bar = None  # symbol -> start time of the latest closed bar already dealt with
    consecutive_errors = 0

    while True:
        try:
            if dashboard.due():
                report_to_dashboard(account_id, pool)

            latest = {}
            for symbol in pool:
                bars = fetch_closes(symbol, STREAK_LENGTH + 1)
                if bars is not None:
                    latest[symbol] = bars

            if seen_bar is None:
                # Bars that closed before startup are never traded on.
                seen_bar = {s: t for s, (t, _) in latest.items()}
                log.info(f"Waiting for the next {BAR_MINUTES[TIMEFRAME]}-minute bar to close before trading.")
            else:
                new_bars = {s: bars for s, bars in latest.items() if bars[0] != seen_bar.get(s)}
                if new_bars:
                    positions = fetch_positions(account_id)
                    log_bar_summary(new_bars, positions, pool)
                    trade_new_bars(account_id, pool, new_bars, positions, currency)
                    seen_bar.update({s: t for s, (t, _) in new_bars.items()})
        except Exception as e:  # anything unexpected counts toward the safety cutoff, not a crash
            consecutive_errors += 1
            log.error(f"[{consecutive_errors}] Error this loop: {e}")
            sleep_after_error(consecutive_errors)
            continue

        consecutive_errors = 0
        dashboard.sleep(LOOP_INTERVAL_SECONDS, lambda: report_to_dashboard(account_id, pool))  # reports fall due while it waits


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        log.info("Bot stopped manually (KeyboardInterrupt). Goodbye.")
        sys.exit(0)
