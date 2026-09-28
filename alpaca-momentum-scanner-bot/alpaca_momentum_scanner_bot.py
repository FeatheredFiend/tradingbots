#!/usr/bin/env python3
"""
Alpaca Paper Momentum Streak Scanner — built for a $100 account
=================================================================

The same momentum-streak signal as ig_momentum_scanner_bot.py, moved to
Alpaca so it can trade small amounts. IG's smallest trade is thousands of
pounds of exposure; Alpaca sells fractions of a US share from $1, so a $100
account can hold several real positions at once.

Strategy — momentum streak, buys only
-------------------------------------
- Timeframe : 15-minute bars, regular US market hours (9:30-16:00 ET)
- Buy       : STREAK_LENGTH consecutive HIGHER closes -> buy one slice
- Sell      : STREAK_LENGTH consecutive LOWER closes  -> close the position
- Risk mgmt : 2% stop-loss / 5% take-profit on the position's average entry
  price, checked by the bot every loop. Alpaca can't attach stop/limit legs
  to fractional orders, so these only work WHILE THE BOT IS RUNNING, and
  only during market hours — an overnight gap can blow straight past them.
- Buys only : fractional shares can't be sold short on Alpaca, and short
  selling needs a $2,000+ margin account, so a falling streak only ever
  closes a position — it never opens a short.

Sizing
------
BUDGET_USD (default $100) is split into MAX_OPEN_POSITIONS (default 5)
equal slices, so each buy is $20 and a stop-loss costs about $0.40. Once
MAX_OPEN_POSITIONS pool positions are open, further buy signals are skipped;
when several symbols signal at once, the strongest rising streak (biggest %
gain across it) gets the slot first. Sizes come from the budget, not the
account's equity, so the bot trades the same on a $100,000 paper account as
on a real $100 one — but on a big paper account, losses don't shrink the
budget the way they would with real money.

Differences from the IG scanner
-------------------------------
- Real price history: Alpaca's free IEX feed gives 15-minute bars for the
  whole pool in one request, so shares work and there's no warm-up.
- Closed bars only: the signal is judged on completed bars, so it can't
  flicker on and off within a bar. Each bar allows at most one buy per
  symbol, so a stop-loss mid-bar doesn't immediately re-buy on the same,
  unchanged streak.
- IEX pre/after-market bars are ignored; a streak can span the overnight
  gap (yesterday's last bars, then today's first), as IG's bars do.

Setup
-----
1. Reuse the alpaca-bot-env venv — same deps as alpaca_ema_bot.py.
2. Same APCA_API_KEY_ID / APCA_API_SECRET_KEY env vars as alpaca_ema_bot.py.
3. Run:
       python alpaca_momentum_scanner_bot.py

Don't run it alongside alpaca_ema_bot.py on the same paper account: both
trade AAPL, MSFT and friends, and each would close the other's positions.

This script only ever talks to the Alpaca PAPER endpoint
(https://paper-api.alpaca.markets). It never places live-money orders.
"""

import logging
import os
import sys
import time

import pandas as pd
import requests

import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import APIError, TimeFrame, TimeFrameUnit

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("APCA_API_KEY_ID", "")
API_SECRET = os.environ.get("APCA_API_SECRET_KEY", "")
BASE_URL = "https://paper-api.alpaca.markets"  # Paper trading only — do not change.

# ~30 liquid US large caps across sectors. Anything that turns out not to be
# tradable or fractionable is skipped at startup with a warning, not fatal.
DEFAULT_POOL = (
    "AAPL,MSFT,AMZN,GOOGL,META,NVDA,TSLA,NFLX,AMD,AVGO,"
    "ORCL,CRM,ADBE,INTC,JPM,BAC,V,MA,WMT,COST,"
    "KO,PEP,MCD,NKE,DIS,XOM,CVX,UNH,JNJ,PFE"
)
POOL = [
    s.strip().upper()
    for s in os.environ.get("BOT_POOL", DEFAULT_POOL).split(",")
    if s.strip()
]

STREAK_LENGTH = int(os.environ.get("STREAK_LENGTH", "3"))  # consecutive up/down bars to trigger

BUDGET_USD = float(os.environ.get("BOT_BUDGET_USD", "100"))
MAX_OPEN_POSITIONS = int(os.environ.get("BOT_MAX_POSITIONS", "5"))
TRADE_NOTIONAL_USD = round(BUDGET_USD / MAX_OPEN_POSITIONS, 2)

BAR_TIMEFRAME = TimeFrame(15, TimeFrameUnit.Minute)
BAR_LENGTH = pd.Timedelta(minutes=15)
# Calendar days of bars per fetch — enough to find STREAK_LENGTH + 1
# regular-hours bars even first thing on the Tuesday after a long weekend.
BAR_HISTORY_DAYS = 5
# How long after a bar closes to wait before fetching it, so it's complete.
BAR_SETTLE = pd.Timedelta(seconds=15)
DATA_FEED = "iex"             # Free-tier Alpaca data plans only permit the IEX feed.
MARKET_TZ = "America/New_York"

STOP_LOSS_PCT = 0.02          # 2% hard stop-loss on average entry price.
TAKE_PROFIT_PCT = 0.05        # 5% take-profit target on average entry price.

LOOP_INTERVAL_SECONDS = 60          # Risk checks run this often; bars refresh once per bar.
CLOSED_MARKET_SLEEP_SECONDS = 300
MAX_CONSECUTIVE_ERRORS = 10         # Safety cutoff to avoid an unattended error loop.

assert STREAK_LENGTH >= 2, "STREAK_LENGTH must be at least 2 to mean anything"
assert MAX_OPEN_POSITIONS >= 1, "BOT_MAX_POSITIONS must be at least 1"
assert TRADE_NOTIONAL_USD >= 1.00, (
    "Alpaca requires a minimum notional order of $1.00 — raise BOT_BUDGET_USD "
    "or lower BOT_MAX_POSITIONS"
)
assert POOL, "BOT_POOL resolved to an empty pool"

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("alpaca_momentum_bot")


# ---------------------------------------------------------------------------
# CLIENT / STARTUP CHECKS
# ---------------------------------------------------------------------------
def get_api_client() -> tradeapi.REST:
    if not API_KEY or not API_SECRET:
        log.error(
            "Missing credentials. Set APCA_API_KEY_ID and APCA_API_SECRET_KEY "
            "environment variables before running this bot."
        )
        sys.exit(1)
    return tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version="v2")


def resolve_pool(api: tradeapi.REST) -> list:
    """Skip (with a reason) anything that can't take a fractional buy instead
    of exiting — like the IG scanner, the pool is about breadth, so one bad
    symbol shouldn't stop the rest."""
    usable = []
    for symbol in POOL:
        try:
            asset = api.get_asset(symbol)
        except (APIError, requests.exceptions.RequestException) as e:
            log.warning(f"Skipping {symbol}: couldn't look it up ({e}).")
            continue

        if getattr(asset, "class", None) != "us_equity":
            reason = "not a US share"
        elif not asset.tradable:
            reason = "not tradable on Alpaca"
        elif not asset.fractionable:
            reason = f"no fractional trading, which ${TRADE_NOTIONAL_USD:.2f} buys need"
        else:
            usable.append(symbol)
            continue
        log.warning(f"Skipping {symbol}: {reason}.")

    if not usable:
        log.critical("Nothing in the pool is tradable. Exiting.")
        sys.exit(1)
    return usable


# ---------------------------------------------------------------------------
# MARKET DATA / SIGNAL
# ---------------------------------------------------------------------------
def latest_settled_bar_close(now: pd.Timestamp) -> pd.Timestamp:
    """End time of the most recent bar that closed at least BAR_SETTLE ago —
    bars only need refetching when this changes, once per 15 minutes."""
    return (now - BAR_SETTLE).floor(BAR_LENGTH)


def fetch_closes(api: tradeapi.REST, symbols: list) -> dict:
    """Closed, regular-hours 15-minute bars for the whole pool in one request:
    {symbol: (start time of its latest closed bar, [last STREAK_LENGTH + 1
    closes, oldest first])}. Raises on failure, so the caller keeps its
    previous bars and retries next loop."""
    now = pd.Timestamp.now(tz="UTC")
    start = (now - pd.Timedelta(days=BAR_HISTORY_DAYS)).isoformat()
    # Always a list, even for one symbol, so the SDK tags rows with "symbol".
    df = api.get_bars(list(symbols), BAR_TIMEFRAME, start=start, feed=DATA_FEED).df
    if df is None or df.empty:
        return {}

    df = df[df.index + BAR_LENGTH <= now]  # drop the still-forming bar
    # Bars are indexed by start time: 09:30 is the first regular-hours bar,
    # 15:45 the last. IEX pre/after-market bars fall outside that.
    df = df.tz_convert(MARKET_TZ).between_time("09:30", "15:45")

    return {
        symbol: (bars.index[-1], bars["close"].tolist()[-(STREAK_LENGTH + 1):])
        for symbol, bars in df.groupby("symbol")
    }


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


def streak_gain(closes: list) -> float:
    """% move across the streak — ranks simultaneous buy signals."""
    recent = closes[-(STREAK_LENGTH + 1):]
    return recent[-1] / recent[0] - 1


# ---------------------------------------------------------------------------
# ORDER EXECUTION
# ---------------------------------------------------------------------------
def submit_buy(api: tradeapi.REST, symbol: str) -> bool:
    try:
        order = api.submit_order(
            symbol=symbol,
            notional=TRADE_NOTIONAL_USD,
            side="buy",
            type="market",
            time_in_force="day",
        )
        log.info(f"BUY submitted -> {symbol} notional=${TRADE_NOTIONAL_USD:.2f} order_id={order.id}")
        return True
    except (APIError, requests.exceptions.RequestException) as e:
        log.error(f"Error submitting BUY for {symbol}; will retry next loop: {e}")
        return False


def close_open_position(api: tradeapi.REST, symbol: str, reason: str) -> bool:
    try:
        order = api.close_position(symbol)
        log.info(f"CLOSE submitted ({reason}) -> {symbol} order_id={order.id}")
        return True
    except (APIError, requests.exceptions.RequestException) as e:
        log.error(f"Error closing {symbol} ({reason}); will retry next loop: {e}")
        return False


def has_cash_for_a_slice(api: tradeapi.REST) -> bool:
    """Fractional buys can't use margin, so check settled-cash buying power."""
    account = api.get_account()
    return float(account.non_marginable_buying_power) >= TRADE_NOTIONAL_USD


# ---------------------------------------------------------------------------
# RISK MANAGEMENT
# ---------------------------------------------------------------------------
def exit_reason(position, closes) -> str:
    """Why this position should be closed now, or "" to keep it."""
    try:
        plpc = float(position.unrealized_plpc)
    except (TypeError, ValueError):
        plpc = 0.0
    if plpc <= -STOP_LOSS_PCT:
        return f"stop-loss, P/L {plpc:+.2%}"
    if plpc >= TAKE_PROFIT_PCT:
        return f"take-profit, P/L {plpc:+.2%}"
    if closes is not None and detect_streak(closes) == "bearish":
        return f"falling streak, P/L {plpc:+.2%}"
    return ""


# ---------------------------------------------------------------------------
# TRADING CYCLE
# ---------------------------------------------------------------------------
def scan_pool(api: tradeapi.REST, pool: list, positions: dict, closes_by_symbol: dict,
              acted_on_bar: dict) -> None:
    """One loop over the pool. `positions` maps symbol -> open position.
    `acted_on_bar` remembers the bar each symbol was last bought, skipped or
    sold on, so a symbol is bought at most once per bar — and never straight
    back after a stop-loss while the bar's streak still looks the same."""
    held = {s: p for s, p in positions.items() if s in pool}

    # Exits first, so any slot they free is available to this loop's buys.
    for symbol, position in list(held.items()):
        bar_time, closes = closes_by_symbol.get(symbol, (None, None))
        reason = exit_reason(position, closes)
        if reason and close_open_position(api, symbol, reason):
            del held[symbol]
            acted_on_bar[symbol] = bar_time

    candidates = []
    for symbol in pool:
        if symbol in held or symbol not in closes_by_symbol:
            continue
        bar_time, closes = closes_by_symbol[symbol]
        if acted_on_bar.get(symbol) != bar_time and detect_streak(closes) == "bullish":
            candidates.append((streak_gain(closes), symbol, bar_time))

    # Strongest rising streak first, while slots last.
    for gain, symbol, bar_time in sorted(candidates, reverse=True):
        if len(held) >= MAX_OPEN_POSITIONS:
            log.info(f"{symbol}: rising streak ({gain:+.2%}), but {MAX_OPEN_POSITIONS} "
                     f"positions are already open; skipping this bar.")
        elif not has_cash_for_a_slice(api):
            log.info(f"{symbol}: rising streak ({gain:+.2%}), but not enough cash for "
                     f"a ${TRADE_NOTIONAL_USD:.2f} buy; skipping this bar.")
        elif submit_buy(api, symbol):
            held[symbol] = None
        else:
            continue  # order error — leave the bar unmarked so the next loop retries
        acted_on_bar[symbol] = bar_time


def log_bar_summary(closes_by_symbol: dict, positions: dict, pool: list) -> None:
    """One line per bar: what's streaking and what's held."""
    rising = [s for s in pool if detect_streak(closes_by_symbol.get(s, (None, []))[1]) == "bullish"]
    falling = [s for s in pool if detect_streak(closes_by_symbol.get(s, (None, []))[1]) == "bearish"]
    held = [f"{s} {float(p.unrealized_plpc):+.2%}" for s, p in positions.items() if s in pool]
    latest = max((t for t, _ in closes_by_symbol.values()), default=None)
    bar_desc = latest.strftime("%a %H:%M ET") if latest is not None else "none"
    log.info(
        f"Bars up to {bar_desc} | rising: {', '.join(rising) or 'none'} | "
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
    backoff = min(LOOP_INTERVAL_SECONDS * consecutive_errors, 300)
    log.info(f"Retrying in {backoff}s...")
    time.sleep(backoff)


def run_bot() -> None:
    api = get_api_client()
    pool = resolve_pool(api)

    log.info("=" * 78)
    log.info("Alpaca Momentum Streak Scanner starting — PAPER TRADING ONLY — buys only")
    log.info(f"Pool: {len(pool)}/{len(POOL)} symbols usable")
    log.info(
        f"Budget=${BUDGET_USD:.2f} in {MAX_OPEN_POSITIONS} slices of ${TRADE_NOTIONAL_USD:.2f} | "
        f"Streak length={STREAK_LENGTH} bars | Stop-loss={STOP_LOSS_PCT:.0%} | "
        f"Take-profit={TAKE_PROFIT_PCT:.0%} | Timeframe=15Min"
    )
    log.info("=" * 78)

    try:
        account = api.get_account()
        log.info(
            f"Account status={account.status} | Equity=${float(account.equity):.2f} | "
            f"Cash for fractional buys=${float(account.non_marginable_buying_power):.2f}"
        )
    except (APIError, requests.exceptions.RequestException) as e:
        log.error(f"Couldn't fetch account info at startup: {e}")

    closes_by_symbol = {}
    bars_as_of = None
    acted_on_bar = {}
    consecutive_errors = 0

    while True:
        try:
            clock = api.get_clock()
            if not clock.is_open:
                log.info(f"Market is closed. Next open at {clock.next_open}. "
                         f"Sleeping {CLOSED_MARKET_SLEEP_SECONDS}s.")
                time.sleep(CLOSED_MARKET_SLEEP_SECONDS)
                continue

            positions = {p.symbol: p for p in api.list_positions()}

            settled = latest_settled_bar_close(pd.Timestamp.now(tz="UTC"))
            if settled != bars_as_of:
                closes_by_symbol = fetch_closes(api, pool)
                bars_as_of = settled
                log_bar_summary(closes_by_symbol, positions, pool)

            scan_pool(api, pool, positions, closes_by_symbol, acted_on_bar)
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
