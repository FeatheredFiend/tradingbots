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
- Timeframe : 15-minute bars by default (SCANNER_TIMEFRAME env var: M1, M5,
  M15 or M30, shared with the other scanners), regular US market hours
  (9:30-16:00 ET)
- Buy       : STREAK_LENGTH consecutive HIGHER closes -> buy one slice
- Sell      : STREAK_LENGTH consecutive LOWER closes  -> close the position
- Risk mgmt : 2% stop-loss / 5% take-profit by default (STOP_LOSS_PERCENT /
  TAKE_PROFIT_PERCENT env vars) on the position's average entry price.
  The stop-loss is a stop order at Alpaca, placed as soon as a position
  shows up: Alpaca can't attach stop/limit legs to fractional orders, and
  won't keep a fractional stop past the day, but it takes a plain day stop
  order. So the stop works between the bot's checks and while the bot
  isn't running. The take-profit is checked by the bot every loop. Before
  any sell of its own, the bot cancels the stop (Alpaca won't sell shares a
  stop order is holding). If a stop order can't be placed, the bot watches
  that stop itself, as it used to.
- Closing   : sells everything ALPACA_FLAT_MINUTES (10) before the session
  ends, and buys nothing from ALPACA_LAST_ENTRY_MINUTES (30) before it
  (Alpaca's clock knows the half days), so nothing is held overnight — an
  overnight gap jumps straight past a stop. 0 switches either off.
- Buys only : fractional shares can't be sold short on Alpaca, and short
  selling needs a $2,000+ margin account, so a falling streak only ever
  closes a position — it never opens a short.
- Stagnancy : a position that has gone nowhere for a while is logged
  (shadow, the default) or sold with the reason TIMEOUT_STAGNANT, freeing
  its slot once Alpaca's sell has filled (SCANNER_STAGNANT_* settings; see
  shared/stagnancy.py).
- Broadcasts: with DASHBOARD_BROADCAST=1 it takes broadcast buys from the
  dashboard (shared/broadcast.py) - the admin's share, its own slice (or
  less), stop and take-profit, and limits; never a sell. A share outside the
  pool is managed until it's sold (its stop order, take-profit, closing time,
  the stagnancy timeout) but never bought or sold on a streak.

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
- Real price history: Alpaca's free IEX feed gives bars for the whole pool
  in one request, so shares work and there's no warm-up. IEX is a small
  slice of US trading, so on 1-minute bars some minutes have no trade and
  no bar; a streak then runs across the gap.
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
import uuid
from datetime import datetime, timezone

import pandas as pd
import requests

import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import APIError, TimeFrame, TimeFrameUnit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))
from dashboard_reporter import CommandError, DashboardReporter  # noqa: E402 - needs the path above
import broadcast  # noqa: E402 - needs the path above
import stagnancy  # noqa: E402 - needs the path above
import symbols  # noqa: E402 - needs the path above

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("APCA_API_KEY_ID", "")
API_SECRET = os.environ.get("APCA_API_SECRET_KEY", "")
BASE_URL = "https://paper-api.alpaca.markets"  # Paper trading only — do not change.
ACCOUNT_MODE = "demo" if "paper-api" in BASE_URL else "live"  # for the dashboard's broadcast previews

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

BAR_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30}
TIMEFRAME = os.environ.get("SCANNER_TIMEFRAME", "").strip().upper() or "M15"  # bar length
assert TIMEFRAME in BAR_MINUTES, "SCANNER_TIMEFRAME must be M1, M5, M15 or M30"
BAR_TIMEFRAME = TimeFrame(BAR_MINUTES[TIMEFRAME], TimeFrameUnit.Minute)
BAR_LENGTH = pd.Timedelta(minutes=BAR_MINUTES[TIMEFRAME])
LAST_BAR_START = (pd.Timestamp("16:00") - BAR_LENGTH).strftime("%H:%M")  # the regular session's last bar
# Bars are fetched for the last 60 bars' worth of time, which holds plenty
# for most of the day. Near the open, a streak reaches back past the
# overnight gap, so any symbol short of STREAK_LENGTH + 1 bars is fetched
# again over this many calendar days: enough even first thing on the
# Tuesday after a long weekend.
RECENT_HISTORY = BAR_LENGTH * 60
BAR_HISTORY_DAYS = 5
# How long after a bar closes to wait before fetching it, so it's complete.
BAR_SETTLE = pd.Timedelta(seconds=15)
DATA_FEED = "iex"             # Free-tier Alpaca data plans only permit the IEX feed.
MARKET_TZ = "America/New_York"

# Percent of the average entry price, e.g. STOP_LOSS_PERCENT=0.5 for 0.5%.
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PERCENT", "2")) / 100
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PERCENT", "5")) / 100

# Closing time: sell everything this many minutes before the session ends,
# and buy nothing from ALPACA_LAST_ENTRY_MINUTES before it. 0 = off.
FLAT_MINUTES = float(os.environ.get("ALPACA_FLAT_MINUTES", "").strip() or 10)
LAST_ENTRY_MINUTES = float(os.environ.get("ALPACA_LAST_ENTRY_MINUTES", "").strip() or 30)
# The stop orders this bot places start their client order ID with this, so
# it never touches anyone else's orders.
STOP_ORDER_PREFIX = "scanner-stop-"
STOP_CANCEL_TIMEOUT_SECONDS = 10

LOOP_INTERVAL_SECONDS = min(60, int(BAR_LENGTH.total_seconds()) // 4)  # Risk checks run this often; bars refresh once per bar.
CLOSED_MARKET_SLEEP_SECONDS = 300
MAX_CONSECUTIVE_ERRORS = 10         # Safety cutoff to avoid an unattended error loop.
BROADCASTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "broadcasts.json")

assert STREAK_LENGTH >= 2, "STREAK_LENGTH must be at least 2 to mean anything"
assert MAX_OPEN_POSITIONS >= 1, "BOT_MAX_POSITIONS must be at least 1"
assert STOP_LOSS_PCT > 0 and TAKE_PROFIT_PCT > 0, "STOP_LOSS_PERCENT and TAKE_PROFIT_PERCENT must be positive"
assert FLAT_MINUTES >= 0 and LAST_ENTRY_MINUTES >= 0, "ALPACA_FLAT_MINUTES and ALPACA_LAST_ENTRY_MINUTES can't be negative"
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

# Off unless DASHBOARD_URL and DASHBOARD_TOKEN are set (see shared/dashboard_reporter.py).
dashboard = DashboardReporter("alpaca-momentum-scanner", "Alpaca momentum scanner", broker="Alpaca", strategy="Momentum streak (buys only)")
broadcasts = broadcast.Book(path=BROADCASTS_FILE, log=log)  # the broadcast trades it acted on (shared/broadcast.py)
guests = set()  # shares outside the pool holding a broadcast trade: managed (stop, take-profit, closing time), never traded


def managed(pool: list) -> list:
    """The pool, plus the shares outside it holding a broadcast trade."""
    return [*pool, *sorted(guests - set(pool))]


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
    bars only need refetching when this changes, once per bar."""
    return (now - BAR_SETTLE).floor(BAR_LENGTH)


def fetch_closes(api: tradeapi.REST, symbols: list) -> dict:
    """Closed, regular-hours bars for the whole pool: {symbol: (start time
    of its latest closed bar, [last STREAK_LENGTH + 1 closes, oldest
    first])}. Raises on failure, so the caller keeps its previous bars and
    retries next loop."""
    now = pd.Timestamp.now(tz="UTC")
    closes = fetch_closes_since(api, symbols, now - RECENT_HISTORY, now)
    short = [s for s in symbols if len(closes.get(s, (None, []))[1]) < STREAK_LENGTH + 1]
    if short:
        closes.update(fetch_closes_since(api, short, now - pd.Timedelta(days=BAR_HISTORY_DAYS), now))
    return closes


def fetch_closes_since(api: tradeapi.REST, symbols: list, start: pd.Timestamp, now: pd.Timestamp) -> dict:
    """fetch_closes() for bars from `start` on, in one request."""
    # Always a list, even for one symbol, so the SDK tags rows with "symbol".
    df = api.get_bars(list(symbols), BAR_TIMEFRAME, start=start.isoformat(), feed=DATA_FEED).df
    if df is None or df.empty:
        return {}

    df = df[df.index + BAR_LENGTH <= now]  # drop the still-forming bar
    # Bars are indexed by start time: 09:30 is the first regular-hours bar,
    # LAST_BAR_START the last. IEX pre/after-market bars fall outside that.
    df = df.tz_convert(MARKET_TZ).between_time("09:30", LAST_BAR_START)

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
_open_problem = ""  # why the last submit_buy() was refused, for a broadcast's answer


def submit_buy(api: tradeapi.REST, symbol: str, notional: float = None, tag: str = None) -> bool:
    """A market buy of one slice, or `notional` dollars (a broadcast's) - its
    client order ID starting with `tag` if given. True if Alpaca took it."""
    global _open_problem
    _open_problem = ""
    notional = TRADE_NOTIONAL_USD if notional is None else notional
    label = {"client_order_id": f"{tag}-{uuid.uuid4().hex[:8]}"} if tag else {}
    try:
        order = api.submit_order(
            symbol=symbol,
            notional=notional,
            side="buy",
            type="market",
            time_in_force="day",
            **label,
        )
        log.info(f"BUY submitted -> {symbol} notional=${notional:.2f} order_id={order.id}")
        return True
    except (APIError, requests.exceptions.RequestException) as e:
        log.error(f"Error submitting BUY for {symbol}; will retry next loop: {e}")
        _open_problem = str(e)
        return False


def close_open_position(api: tradeapi.REST, symbol: str, reason: str, position=None, stops=()) -> bool:
    """Sell all of `symbol`, cancelling this bot's stop orders on it first.
    True once the position is gone - sold now, or by a stop that filled first
    (report_stop_fills reports that one)."""
    try:
        if stops and not cancel_stops(api, symbol, stops):
            log.info(f"{symbol}: its stop order at Alpaca filled first, so there's nothing left to close ({reason}).")
            return True
        order = api.close_position(symbol)
        log.info(f"CLOSE submitted ({reason}) -> {symbol} order_id={order.id}")
        report_closed_trade(order, position, reason)
        return True
    except (APIError, requests.exceptions.RequestException, RuntimeError) as e:
        log.error(f"Error closing {symbol} ({reason}); will retry next loop: {e}")
        return False


# ---------------------------------------------------------------------------
# STOP ORDERS AT ALPACA
# ---------------------------------------------------------------------------
def own_stops(api: tradeapi.REST) -> dict:
    """{symbol: [open stop orders this bot placed]}."""
    stops = {}
    for order in api.list_orders(status="open", limit=500):
        if (order.client_order_id or "").startswith(STOP_ORDER_PREFIX):
            stops.setdefault(order.symbol, []).append(order)
    return stops


def stop_price(position) -> float:
    price = float(position.avg_entry_price) * (1 - STOP_LOSS_PCT)
    return round(price, 2 if price >= 1 else 4)  # whole cents from $1 up, as US exchanges take them


REFUSED_STOPS = set()  # (symbol, qty) Alpaca refused a stop order for: not asked again


def place_stop(api: tradeapi.REST, position):
    """A day stop order at Alpaca for all of the position; None if it wasn't
    placed, in which case the bot watches this stop itself. A refusal isn't
    retried until the position's size changes; a network error is, next loop."""
    if (position.symbol, position.qty) in REFUSED_STOPS:
        return None
    price = stop_price(position)
    try:
        order = api.submit_order(
            symbol=position.symbol,
            qty=position.qty,
            side="sell",
            type="stop",
            stop_price=str(price),
            time_in_force="day",  # the only kind Alpaca takes for fractional shares
            client_order_id=f"{STOP_ORDER_PREFIX}{position.symbol}-{uuid.uuid4().hex[:12]}",
        )
    except (APIError, requests.exceptions.RequestException) as e:
        status = getattr(e, "status_code", None)  # the SDK raises a rate limit it gave up on as an APIError too
        if isinstance(e, APIError) and status is not None and 400 <= status < 500 and status != 429:
            REFUSED_STOPS.add((position.symbol, position.qty))
        log.warning(f"{position.symbol}: couldn't place its stop order at Alpaca ({e}); watching the stop here instead.")
        return None
    log.info(f"STOP placed at Alpaca -> {position.symbol} {float(position.qty):g} shares at ${price:g} order_id={order.id}")
    return order


def cancel_stops(api: tradeapi.REST, symbol: str, stops) -> bool:
    """Cancel these stop orders and wait until Alpaca has let go of the shares
    they hold. False if one filled first (the shares are already sold)."""
    for order in stops:
        try:
            api.cancel_order(order.id)
        except APIError:
            pass  # already filled or gone: its status below says which
    deadline = time.time() + STOP_CANCEL_TIMEOUT_SECONDS
    for order in stops:
        while True:
            status = api.get_order(order.id).status
            if status == "filled":
                return False
            if status in ("canceled", "expired", "rejected", "replaced", "done_for_day"):
                break
            if time.time() > deadline:
                raise RuntimeError(f"Alpaca hasn't cancelled {symbol}'s stop order {order.id} yet ({status})")
            time.sleep(0.25)
    return True


def protect(api: tradeapi.REST, pool: list, positions: dict, stops: dict) -> dict:
    """Give every pool position one stop order at Alpaca for all its shares:
    places missing ones, replaces any that no longer cover the whole position
    (after a part-close, or a buy that finished filling later), and cancels
    any whose position is gone. Returns {symbol: [open stop orders]} after."""
    stops = dict(stops)
    for symbol, orders in list(stops.items()):
        position = positions.get(symbol)
        if position is None or abs(sum(float(o.qty) for o in orders) - float(position.qty)) > 1e-9:
            try:
                cancel_stops(api, symbol, orders)
            except (APIError, requests.exceptions.RequestException, RuntimeError) as e:
                log.warning(f"{symbol}: couldn't cancel an out-of-date stop order ({e}); trying again next loop.")
                continue
            del stops[symbol]
    for symbol, position in positions.items():
        if symbol in pool and symbol not in stops and float(position.qty) > 0:
            order = place_stop(api, position)
            if order is not None:
                stops[symbol] = [order]
    return stops


def report_stop_fills(api: tradeapi.REST, gone: dict, since: pd.Timestamp) -> list:
    """Of the positions `gone` since the last loop ({symbol: position as last
    seen}), report the ones this bot's stop order at Alpaca sold. The bot's
    own sells were reported as it made them. Returns the symbols stopped out."""
    try:
        orders = api.list_orders(status="closed", after=(since - pd.Timedelta(days=1)).isoformat(),
                                 limit=500, symbols=list(gone))
    except (APIError, requests.exceptions.RequestException) as e:
        log.warning(f"Couldn't check whether stop orders sold {', '.join(gone)}: {e}")
        return []
    stopped = []
    for order in orders:
        position = gone.get(order.symbol)
        if (position is None or order.status != "filled" or order.symbol in stopped
                or not (order.client_order_id or "").startswith(STOP_ORDER_PREFIX)
                or pd.Timestamp(order.filled_at) < since - pd.Timedelta(seconds=5)):
            continue
        entry, exit_price, qty = float(position.avg_entry_price), float(order.filled_avg_price), float(order.filled_qty)
        log.info(f"STOPPED OUT at Alpaca -> {order.symbol} {qty:g} shares at ${exit_price:.2f} "
                 f"(entry ${entry:.2f}, {exit_price / entry - 1:+.2%}) order_id={order.id}")
        dashboard.trade({
            "ref": str(order.id),
            "symbol": order.symbol,
            "direction": "long",
            "size": qty,
            "entryPrice": entry,
            "exitPrice": exit_price,
            "closedAt": pd.Timestamp(order.filled_at).isoformat(),
            "pnl": (exit_price - entry) * qty,
            "closeReason": "stop-loss",
        })
        stopped.append(order.symbol)
    return stopped


def close_from_dashboard(api: tradeapi.REST, symbols, symbol: str, ref, direction: str, size) -> str:
    """A close asked for on the dashboard (DASHBOARD_COMMANDS=1): all of the
    position in `symbol`, or `size` shares of it. Answers what happened, or
    raises CommandError."""
    if symbol not in symbols:
        raise CommandError(f"{symbol} isn't in this bot's pool.")
    if direction != "long":
        raise CommandError("This bot only buys, so it has no short to close.")
    try:
        position = api.get_position(symbol)
    except APIError:
        raise CommandError(f"There's no open {symbol} position now - it may have closed already.") from None
    held = float(position.qty)
    qty = None if size is None or size >= held else int(size * 1e9) / 1e9  # Alpaca takes up to 9 decimals
    if qty is not None and qty <= 0:
        raise CommandError(f"{size!r} is too small to sell.")
    try:  # the stop order holds the shares; a new one for what's left is placed next loop
        released = cancel_stops(api, symbol, own_stops(api).get(symbol, []))
    except (APIError, requests.exceptions.RequestException, RuntimeError) as e:
        raise CommandError(f"Couldn't cancel {symbol}'s stop order at Alpaca first: {e}") from None
    if not released:
        raise CommandError(f"{symbol}'s stop order at Alpaca filled first - the position is already closed.")
    try:
        order = api.close_position(symbol, qty=qty)
    except (APIError, requests.exceptions.RequestException) as e:
        raise CommandError(f"Alpaca refused: {e}") from None
    log.info(f"CLOSE submitted (from the dashboard) -> {symbol} {qty if qty else 'all'} order_id={order.id}")
    report_closed_trade(order, position, "closed from the dashboard", qty)
    answer = f"Sell order {order.id} sent for {f'{qty:g} of ' if qty else 'all '}{held:g} {symbol}."
    if not api.get_clock().is_open:
        answer += " The market is shut, so Alpaca fills it when it opens."
    return answer


def has_cash_for_a_slice(api: tradeapi.REST, notional: float = None) -> bool:
    """Fractional buys can't use margin, so check settled-cash buying power."""
    account = api.get_account()
    return float(account.non_marginable_buying_power) >= (TRADE_NOTIONAL_USD if notional is None else notional)


# ---------------------------------------------------------------------------
# BROADCAST TRADES FROM THE DASHBOARD (shared/broadcast.py)
# ---------------------------------------------------------------------------
def restore_guests(pool: list) -> None:
    """After a restart: the shares outside the pool its open broadcast trades are in."""
    for symbol in sorted(broadcasts.open_symbols() - set(pool)):
        guests.add(symbol)
        log.info(f"{symbol}: managing its broadcast trade (not in the pool).")


def tradable_share(api: tradeapi.REST, symbol: str) -> bool:
    try:
        asset = api.get_asset(symbol)
    except (APIError, requests.exceptions.RequestException):
        return False
    return getattr(asset, "class", None) == "us_equity" and asset.tradable and asset.fractionable


def broadcast_plan(api: tradeapi.REST, pool: list, request, now: float, most: float = None) -> dict:
    """The buy this bot would make for a broadcast, every limit applied - or
    broadcast.Declined saying why not."""
    if request.side != "buy":
        raise broadcast.Declined(broadcast.UNAVAILABLE, "This bot only buys - fractional shares can't be sold short "
                                                        "on Alpaca.")
    found = symbols.candidates("alpaca", request.symbol)
    symbol = candidate = None
    for candidate in found:
        code = candidate.code.upper()
        if code in pool or code in guests or tradable_share(api, code):
            symbol = code
            break
    if symbol is None:
        raise broadcast.Declined(broadcast.UNAVAILABLE, f"{request.symbol} isn't a share or fund this bot can buy on "
                                                        f"Alpaca (fractional)"
                                 + (f" - looked for {', '.join(c.code for c in found)}." if found else "."))
    clock = api.get_clock()
    if not clock.is_open:
        raise broadcast.Declined(broadcast.CLOSED, f"The US market is closed (it opens {clock.next_open}).")
    if buys_paused(clock):
        raise broadcast.Declined(broadcast.CLOSED, f"No buys in the last {max(LAST_ENTRY_MINUTES, FLAT_MINUTES):g} min "
                                                   f"before the close.")
    positions = {p.symbol: p for p in api.list_positions()}
    if symbol in positions:
        raise broadcast.Declined(broadcast.NO_SLOT, f"The account already holds {symbol}.")
    held = [s for s in positions if s in pool or s in guests]
    if len(held) >= MAX_OPEN_POSITIONS:
        raise broadcast.Declined(broadcast.NO_SLOT, f"{len(held)} positions are already open (max {MAX_OPEN_POSITIONS}).")
    if watch is not None and watch.cooling(symbol, now):
        raise broadcast.Declined(broadcast.RISK, f"{symbol}: the stagnancy timeout sold it lately (cooldown).")
    bid, ask = latest_prices(api, [symbol]).get(symbol, (None, None))
    if not ask:
        raise broadcast.Declined(broadcast.CLOSED, f"{symbol}: no price right now.")
    notional, capped = broadcast.exposure_for(request, TRADE_NOTIONAL_USD, "USD", "budget slice")
    if most is not None:  # no more shares than previewed (to the cent: the preview's shares are rounded)
        notional = min(notional, round(most * ask, 2))
    notional = int(round(notional * 100, 6)) / 100  # whole cents
    if notional < 1.00:
        raise broadcast.Declined(broadcast.RISK, f"${notional:.2f} is under Alpaca's $1 minimum order.")
    if not has_cash_for_a_slice(api, notional):
        raise broadcast.Declined(broadcast.RISK, f"Not enough settled cash for a ${notional:.2f} buy.")
    shares = round(notional / ask, 6)
    rule = watch.book.rule(symbol) if watch is not None else None
    exits = (f"stop-loss {STOP_LOSS_PCT * 100:g}% (a stop order at Alpaca) / take-profit {TAKE_PROFIT_PCT * 100:g}%; "
             + (f"sold {FLAT_MINUTES:g} min before the close" if FLAT_MINUTES else "held overnight")
             + ("; falling streak" if symbol in pool else "; managed until it's sold (not in the pool)")
             + (f"; stagnancy timeout ({rule.mode})" if rule and rule.mode != "off" else ""))
    return {"symbol": symbol, "notional": notional, "shares": shares, "capped": capped,
            "figures": broadcast.figures(
                symbol=symbol, size=shares, sizeUnit="shares", exposure=notional,
                risk=round(notional * STOP_LOSS_PCT, 2), currency="USD", entry=ask,
                stopLoss=round(ask * (1 - STOP_LOSS_PCT), 2), takeProfit=round(ask * (1 + TAKE_PROFIT_PCT), 2),
                accountMode=ACCOUNT_MODE, exits=exits, capped=capped or None,
                standIn=candidate.canonical if candidate.stand_in else None, previewedAt=now)}


def broadcast_preview(api: tradeapi.REST, pool: list, command: dict) -> tuple:
    """What this bot would do with a broadcast trade: (a line, {figures})."""
    request = broadcast.Request.parse(command)
    plan = broadcast_plan(api, pool, request, time.time())
    f = plan["figures"]
    line = (f"Would buy ${plan['notional']:.2f} of {f['symbol']} (~{f['size']:g} shares at {f['entry']:g}): stop-loss "
            f"{f['stopLoss']:g}, take-profit {f['takeProfit']:g}" + (f" ({plan['capped']})" if plan["capped"] else ""))
    log.info(f"Broadcast #{request.id}: {line}")
    return line + ".", f


def broadcast_open(api: tradeapi.REST, pool: list, command: dict) -> tuple:
    """Buy for a broadcast the admin confirmed - every limit checked again on
    fresh prices, never more shares than previewed. (a line, {figures})."""
    now = time.time()
    request = broadcast.Request.parse(command)
    broadcasts.check_new(request)
    request.check_age(now)
    plan = broadcast_plan(api, pool, request, now, most=request.size)
    symbol = plan["symbol"]
    broadcasts.opening(request, symbol, (), now)
    if not submit_buy(api, symbol, plan["notional"], tag=f"broadcast-{request.id}"):
        broadcasts.failed(request)
        raise CommandError(f"Alpaca refused the buy: {_open_problem or 'see the bot log'}.")
    broadcasts.opened(request, (), now)  # its rows are matched by share and time: Alpaca's positions carry no ID
    if symbol not in pool:
        guests.add(symbol)
        log.info(f"{symbol}: not in the pool - managing its broadcast trade until it's sold, never trading it on a "
                 f"streak.")
    entry = plan["figures"]["entry"]
    return f"Bought ${plan['notional']:.2f} of {symbol} (~{plan['shares']:g} shares at {entry:g}).", \
        {"symbol": symbol, "size": plan["shares"], "price": entry}


def broadcast_symbols(pool: list) -> list:
    """The instrument names the dashboard can offer for this bot."""
    return sorted(set(symbols.names("alpaca")) | set(pool))


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
def report_to_dashboard(api: tradeapi.REST, symbols) -> None:
    """Account and this bot's open positions, every 15 seconds or so (two
    requests); a failure just skips it. Closed trades are reported by
    close_open_position() as they happen. The stop/limit shown are the
    levels this bot enforces itself."""
    try:
        account = api.get_account()
        positions = api.list_positions()
    except Exception:
        return
    # Broadcast trades no longer held have been sold; their shares outside the pool stop being managed.
    now = time.time()
    broadcasts.sync({p.symbol for p in positions}, now)
    broadcasts.prune(now)
    guests.intersection_update(broadcasts.open_symbols())
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


def report_closed_trade(order, position, reason: str, qty=None) -> None:
    """The trade this close ends (`qty` of it, if not all), priced as Alpaca
    valued the position just before the sell - the fill can differ by a cent or two."""
    if position is None:
        return
    share = 1.0 if qty is None else qty / float(position.qty)
    dashboard.trade({
        "ref": str(order.id),
        "symbol": position.symbol,
        "direction": "long",
        "size": float(position.qty) if qty is None else qty,
        "entryPrice": float(position.avg_entry_price),
        "exitPrice": float(position.current_price),
        "closedAt": datetime.now(timezone.utc).isoformat(),
        "pnl": float(position.unrealized_pl) * share,
        "closeReason": reason.split(",")[0],
        **(watch.shadow_fields(position.symbol) if watch is not None else {}),  # what the stagnancy timeout saw
    })


# ---------------------------------------------------------------------------
# STAGNANCY TIMEOUT (shared/stagnancy.py)
# ---------------------------------------------------------------------------
watch = None            # the timeout, set up in run_bot()
_opened = {}            # symbol -> (qty, when its latest buy filled): Alpaca's positions don't say when they opened
_timeout_closes = {}    # symbol -> the TIMEOUT_STAGNANT sell: {"order": id, "entry": price, "row": its fill, once known}


def latest_prices(api: tradeapi.REST, symbols: list) -> dict:
    """{symbol: (bid, ask)} - IEX often shows one side only; then the last trade stands for both."""
    quotes = api.get_latest_quotes(symbols, feed=DATA_FEED) if symbols else {}
    prices = {}
    for symbol in symbols:
        quote = quotes.get(symbol)
        bid, ask = (float(quote.bp or 0), float(quote.ap or 0)) if quote is not None else (0.0, 0.0)
        if bid > 0 and ask >= bid:
            prices[symbol] = (bid, ask)
    missing = [s for s in symbols if s not in prices]
    trades = api.get_latest_trades(missing, feed=DATA_FEED) if missing else {}
    for symbol in missing:
        if symbol in trades:
            prices[symbol] = (float(trades[symbol].p), float(trades[symbol].p))
    return prices


def opened_at(api: tradeapi.REST, position):
    """When the position's latest buy filled, looked up once per position size."""
    cached = _opened.get(position.symbol)
    if cached is not None and cached[0] == position.qty:
        return cached[1]
    orders = api.list_orders(status="closed", symbols=[position.symbol], limit=20, direction="desc")
    filled = [pd.Timestamp(o.filled_at).timestamp() for o in orders if o.side == "buy" and o.filled_at]
    _opened[position.symbol] = (position.qty, max(filled) if filled else None)
    return _opened[position.symbol][1]


def stagnancy_pass(api: tradeapi.REST, pool: list, positions: dict, stops: dict, closes_by_symbol: dict,
                   acted_on_bar: dict) -> None:
    """The stagnancy timeout over the pool's positions: one that has gone
    nowhere for a while is logged (shadow) or sold (enforce). Alpaca fills
    the sell a moment after taking it, so its slot is free once the position
    has gone; the record sent with the sell is then corrected to the fill."""
    now = time.time()
    held_positions = {s: p for s, p in positions.items() if s in pool}
    prices = latest_prices(api, sorted(held_positions))
    held = []
    for symbol, p in held_positions.items():
        bid, ask = prices.get(symbol, (None, None))
        watch.observe(symbol, now, bid, ask, symbol in prices)
        qty, entry = float(p.qty), float(p.avg_entry_price)
        held.append((stagnancy.Held(
            key=symbol, symbol=symbol, direction="long", size=qty, entry=entry, opened_at=opened_at(api, p),
            # Alpaca values a position at the last trade; selling gets the bid. No commission.
            pnl=(bid - entry) * qty if bid else float(p.unrealized_pl),
            risk=qty * entry * STOP_LOSS_PCT,
        ), symbol in prices))

    def close(h):
        result = close_for_timeout(api, h, positions, stops)
        if result.get("done"):  # as after any sell of its own: not bought straight back on the same bar
            acted_on_bar[h.symbol] = closes_by_symbol.get(h.symbol, (None, None))[0]
        return result

    for symbol, fields in watch.run(held, now, close, fill_of=lambda s: timeout_fill(api, s)):
        sold = _timeout_closes.pop(symbol, None)
        if sold is not None:
            dashboard.trade({"ref": sold["order"], **sold.get("row", {}), **fields})


def close_for_timeout(api: tradeapi.REST, held, positions: dict, stops: dict) -> dict:
    """Sell the position for the stagnancy timeout - its stop order at Alpaca
    cancelled first, as Alpaca won't sell shares a stop order holds."""
    symbol, position = held.symbol, positions.get(held.symbol)
    try:
        if stops.get(symbol) and not cancel_stops(api, symbol, stops[symbol]):
            # The stop filled first: the next pass finds the position gone, and keeps the stop's reason.
            return {"done": False, "problem": "its stop order at Alpaca filled first"}
        order = api.close_position(symbol)
    except (APIError, requests.exceptions.RequestException, RuntimeError) as e:
        log.error(f"Error closing {symbol} ({stagnancy.TIMEOUT_REASON}): {e}")
        return {"done": False, "problem": str(e)}
    log.info(f"CLOSE submitted ({stagnancy.TIMEOUT_REASON}) -> {symbol} order_id={order.id}")
    report_closed_trade(order, position, stagnancy.TIMEOUT_REASON)  # priced before the fill; corrected after
    _timeout_closes[symbol] = {"order": str(order.id), "entry": float(position.avg_entry_price)}
    return {"done": True, "filled": False, "refs": (str(order.id),)}


def timeout_fill(api: tradeapi.REST, symbol: str):
    """What the TIMEOUT_STAGNANT sell filled at, once the position has gone."""
    sold = _timeout_closes.get(symbol)
    if sold is None:
        return None
    try:
        order = api.get_order(sold["order"])
    except (APIError, requests.exceptions.RequestException) as e:
        log.warning(f"{symbol}: couldn't look up sell order {sold['order']}: {e}")
        return None
    if not order.filled_avg_price or not float(order.filled_qty or 0):
        return None
    price, qty = float(order.filled_avg_price), float(order.filled_qty)
    pnl = round((price - sold["entry"]) * qty, 2)
    sold["row"] = {"symbol": symbol, "direction": "long", "size": qty, "entryPrice": sold["entry"],
                   "exitPrice": price, "closedAt": pd.Timestamp(order.filled_at).isoformat(), "pnl": pnl,
                   "closeReason": stagnancy.TIMEOUT_REASON}
    return {"price": price, "pnl": pnl, "refs": (sold["order"],)}


# ---------------------------------------------------------------------------
# RISK MANAGEMENT
# ---------------------------------------------------------------------------
def exit_reason(position, closes, watch_stop: bool = True) -> str:
    """Why this position should be closed now, or "" to keep it. The stop is
    only watched here when there's no stop order at Alpaca doing it."""
    try:
        plpc = float(position.unrealized_plpc)
    except (TypeError, ValueError):
        plpc = 0.0
    if watch_stop and plpc <= -STOP_LOSS_PCT:
        return f"stop-loss, P/L {plpc:+.2%}"
    if plpc >= TAKE_PROFIT_PCT:
        return f"take-profit, P/L {plpc:+.2%}"
    if closes is not None and detect_streak(closes) == "bearish":
        return f"falling streak, P/L {plpc:+.2%}"
    return ""


# ---------------------------------------------------------------------------
# TRADING CYCLE
# ---------------------------------------------------------------------------
def scan_pool(api: tradeapi.REST, pool: list, positions: dict, stops: dict, closes_by_symbol: dict,
              acted_on_bar: dict, buys_allowed: bool = True) -> bool:
    """One loop over the pool. `positions` maps symbol -> open position,
    `stops` symbol -> this bot's open stop orders. `acted_on_bar` remembers
    the bar each symbol was last bought, skipped or sold on, so a symbol is
    bought at most once per bar — and never straight back after a stop-loss
    while the bar's streak still looks the same. Returns whether it bought.
    A broadcast trade's share outside the pool holds a slot and gets the
    same exits, but no falling-streak sell: it has no bars here."""
    held = {s: p for s, p in positions.items() if s in pool or s in guests}

    # Exits first, so any slot they free is available to this loop's buys.
    for symbol, position in list(held.items()):
        if watch is not None and watch.closing(symbol):
            continue  # its TIMEOUT_STAGNANT sell is under way; it holds its slot until that fills
        bar_time, closes = closes_by_symbol.get(symbol, (None, None))
        reason = exit_reason(position, closes, watch_stop=symbol not in stops)
        if reason and close_open_position(api, symbol, reason, position, stops.get(symbol, ())):
            del held[symbol]
            acted_on_bar[symbol] = bar_time

    if not buys_allowed:
        return False
    bought = False
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
        elif watch is not None and watch.cooling(symbol, time.time()):  # only with SCANNER_STAGNANT_COOLDOWN set
            log.info(f"{symbol}: rising streak ({gain:+.2%}), but the stagnancy timeout sold it lately (cooldown); "
                     f"skipping this bar.")
        elif not has_cash_for_a_slice(api):
            log.info(f"{symbol}: rising streak ({gain:+.2%}), but not enough cash for "
                     f"a ${TRADE_NOTIONAL_USD:.2f} buy; skipping this bar.")
        elif submit_buy(api, symbol):
            held[symbol] = None
            bought = True
        else:
            continue  # order error — leave the bar unmarked so the next loop retries
        acted_on_bar[symbol] = bar_time
    return bought


def minutes_to_close(clock) -> float:
    return (clock.next_close - clock.timestamp).total_seconds() / 60


def closing_time(clock) -> bool:
    """In the last FLAT_MINUTES of the session: sell everything."""
    return FLAT_MINUTES > 0 and minutes_to_close(clock) <= FLAT_MINUTES


def buys_paused(clock) -> bool:
    """Too near the close to buy: from LAST_ENTRY_MINUTES (at least
    FLAT_MINUTES) before it."""
    before = max(LAST_ENTRY_MINUTES, FLAT_MINUTES)
    return before > 0 and minutes_to_close(clock) <= before


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
        f"Streak length={STREAK_LENGTH} bars | Stop-loss={STOP_LOSS_PCT * 100:g}% (a stop order at Alpaca) | "
        f"Take-profit={TAKE_PROFIT_PCT * 100:g}% | Timeframe={TIMEFRAME}"
    )
    log.info(
        (f"Closing time: sells everything {FLAT_MINUTES:g} min before the close" if FLAT_MINUTES else
         "Closing time: OFF - positions are held overnight")
        + (f", no buys from {max(LAST_ENTRY_MINUTES, FLAT_MINUTES):g} min before it"
           if max(LAST_ENTRY_MINUTES, FLAT_MINUTES) else "")
    )
    global watch
    watch = stagnancy.start_watch("scanner", "alpaca", "alpaca-momentum-scanner", "Alpaca", log,
                                  bar_seconds=BAR_LENGTH.total_seconds(), loop_seconds=LOOP_INTERVAL_SECONDS,
                                  currency="USD")
    log.info("=" * 78)

    try:
        account = api.get_account()
        log.info(
            f"Account status={account.status} | Equity=${float(account.equity):.2f} | "
            f"Cash for fractional buys=${float(account.non_marginable_buying_power):.2f}"
        )
    except (APIError, requests.exceptions.RequestException) as e:
        log.error(f"Couldn't fetch account info at startup: {e}")

    dashboard.describe(currency="USD", account_mode=ACCOUNT_MODE, config={
        "pool": pool, "budgetUsd": BUDGET_USD, "maxPositions": MAX_OPEN_POSITIONS,
        "tradeUsd": TRADE_NOTIONAL_USD, "timeframe": TIMEFRAME, "streakLength": STREAK_LENGTH,
        "stopLossPercent": STOP_LOSS_PCT * 100, "takeProfitPercent": TAKE_PROFIT_PCT * 100,
        "flatMinutes": FLAT_MINUTES, "lastEntryMinutes": LAST_ENTRY_MINUTES, **watch.book.config(),
    })
    dashboard.accept_closes(lambda *command: close_from_dashboard(api, managed(pool), *command))
    restore_guests(pool)
    dashboard.tag_rows(broadcasts.tags)
    dashboard.accept_broadcasts(lambda command: broadcast_preview(api, pool, command),
                                lambda command: broadcast_open(api, pool, command), broadcast_symbols(pool))

    closes_by_symbol = {}
    bars_as_of = None
    acted_on_bar = {}
    last_seen, seen_at = {}, None  # pool positions at the end of the last loop, and when
    flat_logged_for = None         # the close whose closing time has been logged
    consecutive_errors = 0

    while True:
        try:
            if dashboard.due():
                report_to_dashboard(api, managed(pool))

            clock = api.get_clock()
            if not clock.is_open:
                log.info(f"Market is closed. Next open at {clock.next_open}. "
                         f"Sleeping {CLOSED_MARKET_SLEEP_SECONDS}s.")
                dashboard.sleep(CLOSED_MARKET_SLEEP_SECONDS, lambda: report_to_dashboard(api, managed(pool)))
                continue

            positions = {p.symbol: p for p in api.list_positions()}
            stops = own_stops(api)

            gone = {s: p for s, p in last_seen.items() if s not in positions}
            if gone:  # sold by a stop order at Alpaca, by hand, or from the dashboard
                for symbol in report_stop_fills(api, gone, seen_at):
                    acted_on_bar[symbol] = closes_by_symbol.get(symbol, (None, None))[0]

            if closing_time(clock):
                if flat_logged_for != clock.next_close:
                    flat_logged_for = clock.next_close
                    log.info(f"Closing time: selling everything {FLAT_MINUTES:g} minutes before the "
                             f"{clock.next_close.tz_convert(MARKET_TZ):%H:%M} ET close, and no more buys today.")
                for symbol, position in positions.items():
                    if symbol in managed(pool) and not watch.closing(symbol):  # a TIMEOUT_STAGNANT sell is already out
                        close_open_position(api, symbol, "closing time", position, stops.get(symbol, ()))
                last_seen = {}
            else:
                settled = latest_settled_bar_close(pd.Timestamp.now(tz="UTC"))
                if settled != bars_as_of:
                    closes_by_symbol = fetch_closes(api, pool)
                    bars_as_of = settled
                    log_bar_summary(closes_by_symbol, positions, pool)

                if watch.book.active:
                    stagnancy_pass(api, managed(pool), positions, stops, closes_by_symbol, acted_on_bar)
                if scan_pool(api, pool, positions, stops, closes_by_symbol, acted_on_bar,
                             buys_allowed=not buys_paused(clock)):
                    time.sleep(2)  # let the buys fill, so their stop orders go on straight away
                positions = {p.symbol: p for p in api.list_positions()}
                # no new stop order for a position whose TIMEOUT_STAGNANT sell is filling
                protect(api, managed(pool), {s: p for s, p in positions.items() if not watch.closing(s)},
                        own_stops(api))
                last_seen = {s: p for s, p in positions.items() if s in managed(pool)}
            seen_at = pd.Timestamp.now(tz="UTC")
        except Exception as e:  # anything unexpected counts toward the safety cutoff, not a crash
            consecutive_errors += 1
            log.error(f"[{consecutive_errors}] Error this loop: {e}")
            sleep_after_error(consecutive_errors)
            continue

        consecutive_errors = 0
        dashboard.sleep(LOOP_INTERVAL_SECONDS, lambda: report_to_dashboard(api, managed(pool)))  # reports fall due while it waits


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        log.info("Bot stopped manually (KeyboardInterrupt). Goodbye.")
        sys.exit(0)
