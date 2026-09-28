#!/usr/bin/env python3
"""
OANDA Practice EMA(9/21) Crossover Bot — sized for a small account
====================================================================

The same strategy as ig_cfd_ema_bot.py, on OANDA's fxTrade Practice (demo)
account. OANDA's API needs no live account for a key (a practice login can
generate its own token), gives price history for everything it offers, and
trades in single units, so each trade is sized from a small budget instead
of being IG's minimum contract worth thousands.

Strategy
--------
- Timeframe : 15-minute bars (OANDA's own mid-price candles, closed bars only)
- Entry     : 9-period EMA crosses ABOVE the 21-period EMA -> BUY (open long)
- Exit      : 9-period EMA crosses BELOW the 21-period EMA -> close the long
- Risk mgmt : 2% stop-loss / 5% take-profit, attached to the order, so OANDA
              enforces them even while the bot isn't running.

Sizing
------
The same as oanda_momentum_scanner_bot.py: OANDA_BUDGET (default 100, in the
account's currency) split into OANDA_MAX_POSITIONS (default 5) slices, and
each buy is worth one slice. The budget is exposure, not margin, so the
default uses no leverage. A watchlist market whose smallest trade is worth
more than a slice (one unit of an index or of gold is worth thousands) is
skipped at startup with the reason logged — raise OANDA_BUDGET to include it.

Watchlist
---------
OANDA_WATCHLIST is a fixed list of OANDA instrument names, not a screener.
OANDA has no single-company shares, so unlike the IG bot's default list this
one is currency pairs. A name the account doesn't offer
stops the bot at startup (like the IG bot, a hand-picked list should be
exactly right), listing similar names it does offer.

Bars are acted on as they close, so after a start nothing happens until the
next 15-minute bar closes. The bot treats every position in its markets as
its own — run it on its own sub-account, not alongside
oanda_momentum_scanner_bot.py on the same one (see that bot's docstring).

Setup
-----
1. pip install -r requirements.txt   (just `requests`)
2. OANDA_API_TOKEN: log into the practice account in the OANDA hub, then
   Tools > API > Generate.
3. OANDA_ACCOUNT_ID: optional when the token can only see one account.
4. (Optional) OANDA_WATCHLIST="EUR_USD,GBP_USD,XAU_USD"
5. Run:
       python oanda_ema_bot.py

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

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
API_TOKEN = os.environ.get("OANDA_API_TOKEN", "")
ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "")
BASE_URL = "https://api-fxpractice.oanda.com"  # Practice (demo) only — do not change.

# Currency pairs only: one unit of those is worth about £1, so they fit any
# budget, while one unit of silver or oil is already worth more than a £20 slice.
DEFAULT_WATCHLIST = "EUR_USD,GBP_USD,USD_JPY,AUD_USD,EUR_GBP,USD_CAD,USD_CHF,NZD_USD"
WATCHLIST = [
    s.strip().upper()
    for s in os.environ.get("OANDA_WATCHLIST", DEFAULT_WATCHLIST).split(",")
    if s.strip()
]

EMA_SHORT_PERIOD = 9
EMA_LONG_PERIOD = 21
BARS_LOOKBACK = 200           # ~50 hours of 15-min bars — plenty for a 21-EMA warm-up.

BUDGET = float(os.environ.get("OANDA_BUDGET", "100"))  # total exposure, in the account's currency
MAX_OPEN_POSITIONS = int(os.environ.get("OANDA_MAX_POSITIONS", "5"))
TRADE_EXPOSURE = BUDGET / MAX_OPEN_POSITIONS

STOP_LOSS_PCT = 0.02           # 2% hard stop-loss, attached to the order itself.
TAKE_PROFIT_PCT = 0.05         # 5% take-profit, attached to the order itself.

GRANULARITY = "M15"
BAR_SECONDS = 15 * 60
LOOP_INTERVAL_SECONDS = 60     # how often to look for newly closed bars
REQUEST_TIMEOUT_SECONDS = 20
MAX_CONSECUTIVE_ERRORS = 10

assert MAX_OPEN_POSITIONS >= 1, "OANDA_MAX_POSITIONS must be at least 1"
assert BUDGET > 0, "OANDA_BUDGET must be positive"
assert WATCHLIST, "OANDA_WATCHLIST resolved to an empty list"

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("oanda_ema_bot")


# ---------------------------------------------------------------------------
# CLIENT / STARTUP CHECKS
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


def resolve_watchlist(account_id: str, currency: str) -> dict:
    """{instrument name: OANDA's instrument details}. Exits on a name the
    account doesn't offer; skips (logging why) one too big for a slice."""
    offered = {i["name"]: i for i in oanda("GET", f"/v3/accounts/{account_id}/instruments")["instruments"]}
    missing = [s for s in WATCHLIST if s not in offered]
    if missing:
        for symbol in missing:
            stem = re.sub(r"\d+$", "", symbol.split("_")[0])  # DE30_EUR -> DE, to spot a rename
            similar = sorted(n for n in offered if n.startswith(stem))
            log.critical(f"{symbol} isn't offered on this account. Similar: {', '.join(similar) or 'none'}")
        log.critical("Fix OANDA_WATCHLIST (OANDA instrument names, e.g. EUR_USD). Exiting.")
        sys.exit(1)

    prices = fetch_prices(account_id, WATCHLIST, currency)
    usable = {}
    for symbol in WATCHLIST:
        instrument, price = offered[symbol], prices.get(symbol)
        if price is None:
            log.warning(f"Skipping {symbol}: OANDA gave no price for it.")
        elif units_for_slice(instrument, price["unit_value"]) == 0:
            smallest = float(instrument["minimumTradeSize"]) * price["unit_value"]
            log.warning(
                f"Skipping {symbol}: its smallest trade (minimum size {instrument['minimumTradeSize']}) is worth "
                f"about {smallest:,.0f} {currency}, more than a {TRADE_EXPOSURE:,.2f} {currency} slice — "
                f"raise OANDA_BUDGET to include it."
            )
        else:
            usable[symbol] = instrument

    if not usable:
        log.critical("Nothing on the watchlist is tradable within the budget. Exiting.")
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


def submit_buy(account_id: str, symbol: str, instrument: dict, price: dict, currency: str) -> bool:
    """Market buy with the stop-loss and take-profit attached. True if it filled."""
    units = units_for_slice(instrument, price["unit_value"])
    if units == 0:
        log.info(f"{symbol}: bullish crossover, but its smallest trade is now worth more than a slice; skipping.")
        return False
    digits = int(instrument["displayPrecision"])  # OANDA rejects prices with more decimals
    order = {
        "type": "MARKET",
        "instrument": symbol,
        "units": f"{units:.{int(instrument['tradeUnitsPrecision'])}f}",
        "timeInForce": "FOK",
        "positionFill": "DEFAULT",
        "stopLossOnFill": {"price": f"{price['ask'] * (1 - STOP_LOSS_PCT):.{digits}f}"},
        "takeProfitOnFill": {"price": f"{price['ask'] * (1 + TAKE_PROFIT_PCT):.{digits}f}"},
    }
    try:
        body = oanda("POST", f"/v3/accounts/{account_id}/orders", json={"order": order})
    except (OandaError, requests.RequestException) as e:
        log.error(f"Error submitting BUY for {symbol}: {e}")
        return False
    filled, outcome = fill_outcome(body, "order")
    log.info(
        f"BUY submitted -> {symbol} units={order['units']} (~{units * price['unit_value']:,.2f} {currency}) "
        f"stop={order['stopLossOnFill']['price']} limit={order['takeProfitOnFill']['price']} result={outcome}"
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
    return filled


# ---------------------------------------------------------------------------
# TRADING CYCLE
# ---------------------------------------------------------------------------
def trade_new_bars(account_id: str, watchlist: dict, new_bars: dict, currency: str) -> None:
    """Act on the markets whose bar just closed. `new_bars` maps symbol ->
    (bar time, closes)."""
    positions = fetch_positions(account_id)
    prices = fetch_prices(account_id, list(new_bars), currency)
    held = [s for s in positions if s in watchlist]

    for symbol, (_, closes) in new_bars.items():
        ema_short, ema_long = ema(closes, EMA_SHORT_PERIOD), ema(closes, EMA_LONG_PERIOD)
        signal = detect_crossover(ema_short, ema_long)
        position = positions.get(symbol)
        position_desc = f"{position['side']} {position['units']:g} P/L={position['pl']:+.2f}" if position else "FLAT"
        log.info(
            f"{symbol} | price={closes[-1]} | EMA9={ema_short[-1]:.5f} | EMA21={ema_long[-1]:.5f} | "
            f"signal={signal or 'none'} | position={position_desc}"
        )

        if signal == "bullish":
            price = prices.get(symbol)
            if position is not None:
                log.info(f"{symbol}: bullish crossover, but already holding a position; skipping buy.")
            elif len(held) >= MAX_OPEN_POSITIONS:
                log.info(f"{symbol}: bullish crossover, but {MAX_OPEN_POSITIONS} positions are already open; "
                         f"skipping buy.")
            elif price is None or not price["tradeable"]:
                log.info(f"{symbol}: bullish crossover, but OANDA says it isn't tradeable right now; skipping buy.")
            elif submit_buy(account_id, symbol, watchlist[symbol], price, currency):
                held.append(symbol)
        elif signal == "bearish" and position is not None and position["side"] == "long":
            if close_position(account_id, symbol, position, "EMA bearish crossover"):
                held.remove(symbol)


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
    account_id, summary = connect()
    currency = summary["currency"]
    watchlist = resolve_watchlist(account_id, currency)

    log.info("=" * 78)
    log.info("OANDA EMA(9/21) Crossover Bot starting — PRACTICE ACCOUNT ONLY")
    log.info(f"Account {account_id} | balance={summary['balance']} {currency} | NAV={summary['NAV']} | "
             f"margin available={summary['marginAvailable']}")
    log.info(f"Watchlist: {', '.join(watchlist)}")
    log.info(
        f"Budget={BUDGET:,.2f} {currency} in {MAX_OPEN_POSITIONS} slices of {TRADE_EXPOSURE:,.2f} | "
        f"Stop-loss={STOP_LOSS_PCT:.0%} | Take-profit={TAKE_PROFIT_PCT:.0%} | "
        f"Timeframe=15Min | EMA periods={EMA_SHORT_PERIOD}/{EMA_LONG_PERIOD}"
    )
    log.info("=" * 78)

    seen_bar = None  # symbol -> start time of the latest closed bar already dealt with
    consecutive_errors = 0

    while True:
        try:
            latest = {}
            for symbol in watchlist:
                bars = fetch_closes(symbol, BARS_LOOKBACK)
                if bars is not None and len(bars[1]) > EMA_LONG_PERIOD:
                    latest[symbol] = bars

            if seen_bar is None:
                # Bars that closed before startup are never traded on.
                seen_bar = {s: t for s, (t, _) in latest.items()}
                log.info("Waiting for the next 15-minute bar to close before trading.")
            else:
                new_bars = {s: bars for s, bars in latest.items() if bars[0] != seen_bar.get(s)}
                if new_bars:
                    trade_new_bars(account_id, watchlist, new_bars, currency)
                    seen_bar.update({s: t for s, (t, _) in new_bars.items()})
        except Exception as e:  # anything unexpected counts toward the safety cutoff, not a crash
            consecutive_errors += 1
            log.error(f"[{consecutive_errors}] Error this loop: {e}")
            sleep_after_error(consecutive_errors)
            continue

        consecutive_errors = 0
        time.sleep(LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        log.info("Bot stopped manually (KeyboardInterrupt). Goodbye.")
        sys.exit(0)
