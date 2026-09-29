#!/usr/bin/env python3
"""
Alpaca Paper Trading Bot — EMA(9/21) Crossover Trend Follower
================================================================

Strategy
--------
- Timeframe : 15-minute bars by default (EMA_TIMEFRAME env var: M1, M5, M15
  or M30, shared with the other EMA bots)
- Entry     : 9-period EMA crosses ABOVE the 21-period EMA  -> BUY
- Exit      : 9-period EMA crosses BELOW the 21-period EMA  -> SELL (close position)
- Risk mgmt : 2% hard stop-loss / 5% take-profit, measured against the
              position's average entry price and checked every loop.

The bot scans a WATCHLIST of symbols every cycle and applies the same EMA
crossover logic independently to each one — a bullish cross on any symbol
with no open position in it triggers its own $2 buy; a bearish cross, a
stop-loss, or a take-profit on any symbol closes just that symbol's position.

Why risk management is a separate polling check instead of a bracket order:
Alpaca does not allow fractional / notional orders to carry attached
stop-loss or take-profit legs (OCO/OTO). Since this bot trades a fixed
$2.00 notional (fractional shares) per symbol, the stop-loss and take-profit
are enforced manually every cycle using each position's live unrealized
P/L%, and executed as an immediate market close via `close_position()`.

Setup
-----
1. pip install alpaca-trade-api pandas
2. Create a free Alpaca PAPER account: https://alpaca.markets
3. Set your paper API credentials as environment variables (never hardcode
   secrets in the script). PowerShell (Windows):

       $env:APCA_API_KEY_ID = "your_paper_key_id"
       $env:APCA_API_SECRET_KEY = "your_paper_secret_key"

   macOS/Linux:

       export APCA_API_KEY_ID="your_paper_key_id"
       export APCA_API_SECRET_KEY="your_paper_secret_key"

4. (Optional) set BOT_SYMBOLS to a comma-separated watchlist, mixing
   equities and crypto freely (a "/" in a symbol means crypto), e.g.:
       $env:BOT_SYMBOLS = "AAPL,MSFT,TSLA,BTC/USD"
   Every symbol MUST be fractionable on Alpaca or $2 notional orders will
   be rejected. Defaults to a big-tech watchlist if unset.

5. Run:
       python alpaca_ema_bot.py

This script only ever talks to the Alpaca PAPER endpoint
(https://paper-api.alpaca.markets). It never places live-money orders.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests

import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import APIError, TimeFrame, TimeFrameUnit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))
from dashboard_reporter import DashboardReporter  # noqa: E402 - needs the path above

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("APCA_API_KEY_ID", "")
API_SECRET = os.environ.get("APCA_API_SECRET_KEY", "")
BASE_URL = "https://paper-api.alpaca.markets"  # Paper trading only — do not change.

DEFAULT_WATCHLIST = "AAPL,MSFT,AMZN,GOOGL,TSLA"
WATCHLIST = [
    s.strip().upper()
    for s in os.environ.get("BOT_SYMBOLS", DEFAULT_WATCHLIST).split(",")
    if s.strip()
]

TRADE_NOTIONAL_USD = 2.00     # Fixed dollar size per trade, per symbol.
EMA_SHORT_PERIOD = 9
EMA_LONG_PERIOD = 21
BAR_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30}
TIMEFRAME = os.environ.get("EMA_TIMEFRAME", "").strip().upper() or "M15"  # bar length
assert TIMEFRAME in BAR_MINUTES, "EMA_TIMEFRAME must be M1, M5, M15 or M30"
BAR_TIMEFRAME = TimeFrame(BAR_MINUTES[TIMEFRAME], TimeFrameUnit.Minute)
BARS_LOOKBACK = 200           # plenty for a 21-EMA warm-up.
DATA_FEED = "iex"             # Free-tier Alpaca data plans only permit the IEX feed (equities only).

STOP_LOSS_PCT = 0.02          # 2% hard stop-loss on average entry price.
TAKE_PROFIT_PCT = 0.05        # 5% take-profit target on average entry price.

LOOP_INTERVAL_SECONDS = min(60, BAR_MINUTES[TIMEFRAME] * 15)  # Poll cadence while the market is open.
CLOSED_MARKET_SLEEP_SECONDS = 300   # Sleep cadence while all-equity watchlists are closed.
MAX_CONSECUTIVE_ERRORS = 10         # Safety cutoff to avoid an unattended error loop.

assert TRADE_NOTIONAL_USD >= 1.00, "Alpaca requires a minimum notional order of $1.00"
assert WATCHLIST, "BOT_SYMBOLS resolved to an empty watchlist"


def is_crypto_symbol(symbol: str) -> bool:
    """Alpaca's unified symbology always puts a "/" in crypto pairs (e.g.
    BTC/USD) and never in equity tickers (e.g. AAPL) — use that to switch
    code paths, since crypto trades 24/7 and uses separate endpoints."""
    return "/" in symbol


def order_time_in_force(symbol: str) -> str:
    # Crypto has no trading-day boundary, so "day" doesn't cleanly apply.
    return "gtc" if is_crypto_symbol(symbol) else "day"


HAS_EQUITY_SYMBOLS = any(not is_crypto_symbol(s) for s in WATCHLIST)
HAS_CRYPTO_SYMBOLS = any(is_crypto_symbol(s) for s in WATCHLIST)

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ema_bot")

# Off unless DASHBOARD_URL and DASHBOARD_TOKEN are set (see shared/dashboard_reporter.py).
dashboard = DashboardReporter("alpaca-ema-bot", "Alpaca EMA crossover", broker="Alpaca", strategy="EMA 9/21 crossover")


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


def verify_asset_is_tradable(api: tradeapi.REST, symbol: str) -> None:
    try:
        asset = api.get_asset(symbol)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        log.error(f"Network error verifying asset {symbol}: {e}")
        sys.exit(1)
    except APIError as e:
        log.error(f"Alpaca API error verifying asset {symbol}: {e}")
        sys.exit(1)

    if not asset.tradable:
        log.critical(f"{symbol} is not tradable on Alpaca. Exiting.")
        sys.exit(1)
    if not asset.fractionable:
        log.critical(
            f"{symbol} does not support fractional trading, which is required "
            f"for ${TRADE_NOTIONAL_USD:.2f} notional orders. Remove it from "
            f"BOT_SYMBOLS or choose a fractionable symbol. Exiting."
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# MARKET DATA
# ---------------------------------------------------------------------------
def fetch_bars(api: tradeapi.REST, symbol: str) -> Optional[pd.DataFrame]:
    """Fetch recent bars and drop any still-forming (incomplete) bar."""
    try:
        if is_crypto_symbol(symbol):
            df = api.get_crypto_bars(symbol, BAR_TIMEFRAME, limit=BARS_LOOKBACK).df
        else:
            df = api.get_bars(
                symbol, BAR_TIMEFRAME, limit=BARS_LOOKBACK, feed=DATA_FEED
            ).df
    except requests.exceptions.Timeout:
        log.error(f"Timeout fetching bars for {symbol}; will retry next loop.")
        return None
    except requests.exceptions.ConnectionError:
        log.error(f"Connection error fetching bars for {symbol}; will retry next loop.")
        return None
    except APIError as e:
        log.error(f"Alpaca API error fetching bars for {symbol}: {e}")
        return None
    except Exception as e:
        log.error(f"Unexpected error fetching bars for {symbol}: {e}")
        return None

    if df is None or df.empty:
        return None

    df = df.copy()

    # The API can include a partially-formed final bar; exclude it so the
    # crossover check only ever acts on fully closed candles.
    now_utc = pd.Timestamp.now(tz="UTC")
    last_bar_close = df.index[-1] + pd.Timedelta(minutes=BAR_MINUTES[TIMEFRAME])
    if last_bar_close > now_utc:
        df = df.iloc[:-1]

    return df


def compute_emas(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_short"] = df["close"].ewm(span=EMA_SHORT_PERIOD, adjust=False).mean()
    df["ema_long"] = df["close"].ewm(span=EMA_LONG_PERIOD, adjust=False).mean()
    return df


def detect_crossover(df: pd.DataFrame) -> Optional[str]:
    """Return 'bullish', 'bearish', or None based on the last two closed bars."""
    if len(df) < 2:
        return None
    prev, curr = df.iloc[-2], df.iloc[-1]
    if prev["ema_short"] <= prev["ema_long"] and curr["ema_short"] > curr["ema_long"]:
        return "bullish"
    if prev["ema_short"] >= prev["ema_long"] and curr["ema_short"] < curr["ema_long"]:
        return "bearish"
    return None


# ---------------------------------------------------------------------------
# ACCOUNT / POSITIONS
# ---------------------------------------------------------------------------
def get_open_position(api: tradeapi.REST, symbol: str):
    """Return the open position for `symbol`, or None if there isn't one."""
    try:
        return api.get_position(symbol)
    except requests.exceptions.HTTPError as e:
        # This SDK only wraps errors into APIError when Alpaca's response body
        # includes a "message" field; a bare 404 (no open position) falls back
        # to a raw HTTPError instead, so it must be handled separately here.
        if e.response is not None and e.response.status_code == 404:
            return None
        log.error(f"HTTP error fetching position for {symbol}: {e}")
        raise
    except APIError as e:
        message = str(e).lower()
        if "position does not exist" in message or "404" in message:
            return None
        log.error(f"Alpaca API error fetching position for {symbol}: {e}")
        raise
    except requests.exceptions.Timeout:
        log.error(f"Timeout fetching position for {symbol}.")
        raise
    except requests.exceptions.ConnectionError:
        log.error(f"Connection error fetching position for {symbol}.")
        raise


# ---------------------------------------------------------------------------
# ORDER EXECUTION
# ---------------------------------------------------------------------------
def submit_buy(api: tradeapi.REST, symbol: str) -> None:
    try:
        order = api.submit_order(
            symbol=symbol,
            notional=TRADE_NOTIONAL_USD,
            side="buy",
            type="market",
            time_in_force=order_time_in_force(symbol),
        )
        log.info(
            f"BUY submitted -> {symbol} notional=${TRADE_NOTIONAL_USD:.2f} "
            f"order_id={order.id}"
        )
    except requests.exceptions.Timeout:
        log.error(f"Timeout submitting BUY order for {symbol}; will retry next loop.")
    except requests.exceptions.ConnectionError:
        log.error(f"Connection error submitting BUY order for {symbol}; will retry next loop.")
    except APIError as e:
        log.error(f"Alpaca API error submitting BUY order for {symbol}: {e}")
    except Exception as e:
        log.error(f"Unexpected error submitting BUY order for {symbol}: {e}")


def close_open_position(api: tradeapi.REST, symbol: str, reason: str, position=None) -> None:
    try:
        order = api.close_position(symbol)
        log.info(f"CLOSE position submitted ({reason}) -> {symbol} order_id={order.id}")
        report_closed_trade(order, position, reason)
    except requests.exceptions.Timeout:
        log.error(f"Timeout closing position for {symbol} ({reason}); will retry next loop.")
    except requests.exceptions.ConnectionError:
        log.error(f"Connection error closing position for {symbol} ({reason}); will retry next loop.")
    except APIError as e:
        log.error(f"Alpaca API error closing position for {symbol} ({reason}): {e}")
    except Exception as e:
        log.error(f"Unexpected error closing position for {symbol} ({reason}): {e}")


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
def report_to_dashboard(api: tradeapi.REST, symbols) -> None:
    """Account and this bot's open positions, about once a minute (two
    requests); a failure just skips it. Closed trades are reported by
    close_open_position() as they happen. The stop/limit shown are the
    levels this bot enforces itself."""
    try:
        account = api.get_account()
        positions = api.list_positions()
    except Exception:
        return
    unrealized = sum(float(p.unrealized_pl) for p in positions)
    dashboard.update(
        account={"balance": float(account.equity) - unrealized, "equity": float(account.equity), "unrealizedPl": unrealized},
        positions=[{
            "symbol": p.symbol,
            "direction": "long",
            "size": float(p.qty),
            "entryPrice": float(p.avg_entry_price),
            "currentPrice": float(p.current_price),
            "pnl": float(p.unrealized_pl),
            "stopLoss": round(float(p.avg_entry_price) * (1 - STOP_LOSS_PCT), 4),
            "takeProfit": round(float(p.avg_entry_price) * (1 + TAKE_PROFIT_PCT), 4),
        } for p in positions if p.symbol in symbols],
    )


def report_closed_trade(order, position, reason: str) -> None:
    """The trade this close ends, priced as Alpaca valued the position just
    before the sell - the fill can differ by a cent or two."""
    if position is None:
        return
    dashboard.trade({
        "ref": str(order.id),
        "symbol": position.symbol,
        "direction": "long",
        "size": float(position.qty),
        "entryPrice": float(position.avg_entry_price),
        "exitPrice": float(position.current_price),
        "closedAt": datetime.now(timezone.utc).isoformat(),
        "pnl": float(position.unrealized_pl),
        "closeReason": reason.split(",")[0],
    })


# ---------------------------------------------------------------------------
# RISK MANAGEMENT
# ---------------------------------------------------------------------------
def check_risk_management(api: tradeapi.REST, symbol: str, position) -> bool:
    """Check the open position's unrealized P/L against SL/TP thresholds.

    Returns True if the position was closed as a result.
    """
    if position is None:
        return False

    try:
        unrealized_plpc = float(position.unrealized_plpc)
    except (TypeError, ValueError):
        return False

    if unrealized_plpc <= -STOP_LOSS_PCT:
        log.warning(
            f"STOP-LOSS triggered on {symbol}: unrealized P/L "
            f"{unrealized_plpc:+.2%} <= -{STOP_LOSS_PCT:.0%}"
        )
        close_open_position(api, symbol, "stop-loss", position)
        return True

    if unrealized_plpc >= TAKE_PROFIT_PCT:
        log.warning(
            f"TAKE-PROFIT triggered on {symbol}: unrealized P/L "
            f"{unrealized_plpc:+.2%} >= {TAKE_PROFIT_PCT:.0%}"
        )
        close_open_position(api, symbol, "take-profit", position)
        return True

    return False


# ---------------------------------------------------------------------------
# TRADING CYCLE
# ---------------------------------------------------------------------------
def trading_cycle(api: tradeapi.REST, symbol: str) -> None:
    position = get_open_position(api, symbol)

    # Risk management takes priority over signal generation every loop.
    if check_risk_management(api, symbol, position):
        position = None

    df = fetch_bars(api, symbol)
    if df is None or len(df) < EMA_LONG_PERIOD + 1:
        log.warning(f"Not enough closed {TIMEFRAME} bars for {symbol} yet; skipping this cycle.")
        return

    df = compute_emas(df)
    signal = detect_crossover(df)

    last = df.iloc[-1]
    price = float(last["close"])
    ema_short = float(last["ema_short"])
    ema_long = float(last["ema_long"])

    if position is not None:
        qty = float(position.qty)
        avg_entry = float(position.avg_entry_price)
        plpc = float(position.unrealized_plpc)
        position_desc = f"LONG qty={qty:.6f} avg_entry=${avg_entry:.2f} P/L={plpc:+.2%}"
    else:
        position_desc = "FLAT"

    log.info(
        f"{symbol} | price=${price:.2f} | EMA9=${ema_short:.4f} | "
        f"EMA21=${ema_long:.4f} | signal={signal or 'none'} | position={position_desc}"
    )

    if signal == "bullish":
        if position is None:
            submit_buy(api, symbol)
        else:
            log.info(f"{symbol}: bullish crossover detected but already holding a position; skipping buy.")
    elif signal == "bearish":
        if position is not None:
            close_open_position(api, symbol, "EMA bearish crossover", position)
        else:
            log.info(f"{symbol}: bearish crossover detected but no open position; nothing to sell.")


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
    for symbol in WATCHLIST:
        verify_asset_is_tradable(api, symbol)

    log.info("=" * 78)
    log.info("Alpaca EMA(9/21) Crossover Bot starting — PAPER TRADING ONLY")
    watchlist_desc = ", ".join(
        f"{s} ({'crypto' if is_crypto_symbol(s) else 'equity'})" for s in WATCHLIST
    )
    log.info(f"Watchlist: {watchlist_desc}")
    log.info(
        f"Trade size=${TRADE_NOTIONAL_USD:.2f}/symbol | "
        f"Stop-loss={STOP_LOSS_PCT:.0%} | Take-profit={TAKE_PROFIT_PCT:.0%} | "
        f"Timeframe={TIMEFRAME} | EMA periods={EMA_SHORT_PERIOD}/{EMA_LONG_PERIOD}"
    )
    log.info("=" * 78)

    try:
        account = api.get_account()
        pdt_flag = getattr(account, "pattern_day_trader", "N/A")
        log.info(
            f"Account status={account.status} | Equity=${float(account.equity):.2f} | "
            f"Buying power=${float(account.buying_power):.2f} | "
            f"Pattern day trader={pdt_flag}"
        )
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        log.error(f"Network error fetching account info at startup: {e}")
    except APIError as e:
        log.error(f"Alpaca API error fetching account info at startup: {e}")

    dashboard.describe(currency="USD", config={
        "watchlist": WATCHLIST, "tradeUsd": TRADE_NOTIONAL_USD,
        "timeframe": TIMEFRAME, "emaPeriods": f"{EMA_SHORT_PERIOD}/{EMA_LONG_PERIOD}",
        "stopLossPercent": STOP_LOSS_PCT * 100, "takeProfitPercent": TAKE_PROFIT_PCT * 100,
    })

    consecutive_errors = 0

    while True:
        if dashboard.due():
            report_to_dashboard(api, WATCHLIST)

        market_open = True
        if HAS_EQUITY_SYMBOLS:
            try:
                clock = api.get_clock()
            except requests.exceptions.Timeout:
                consecutive_errors += 1
                log.error(f"[{consecutive_errors}] Timeout fetching market clock.")
                sleep_after_error(consecutive_errors)
                continue
            except requests.exceptions.ConnectionError:
                consecutive_errors += 1
                log.error(f"[{consecutive_errors}] Connection error fetching market clock.")
                sleep_after_error(consecutive_errors)
                continue
            except APIError as e:
                consecutive_errors += 1
                log.error(f"[{consecutive_errors}] Alpaca API error fetching market clock: {e}")
                sleep_after_error(consecutive_errors)
                continue

            market_open = clock.is_open
            if not market_open:
                if HAS_CRYPTO_SYMBOLS:
                    log.info(
                        f"Equity market is closed (next open {clock.next_open}); "
                        f"continuing to trade crypto symbols only."
                    )
                else:
                    log.info(
                        f"Market is closed. Next open at {clock.next_open}. "
                        f"Sleeping {CLOSED_MARKET_SLEEP_SECONDS}s."
                    )
                    time.sleep(CLOSED_MARKET_SLEEP_SECONDS)
                    continue

        cycle_had_error = False
        for symbol in WATCHLIST:
            if not market_open and not is_crypto_symbol(symbol):
                continue
            try:
                trading_cycle(api, symbol)
            except Exception as e:
                cycle_had_error = True
                log.error(f"Error processing {symbol} this cycle: {e}")

        if cycle_had_error:
            consecutive_errors += 1
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
