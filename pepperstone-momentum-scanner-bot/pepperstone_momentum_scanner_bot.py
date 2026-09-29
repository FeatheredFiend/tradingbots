#!/usr/bin/env python3
"""
Pepperstone Demo Momentum Streak Scanner (MetaTrader 5) — HIGH RISK
=====================================================================

The IG momentum scanner (ig_momentum_scanner_bot.py) moved to a Pepperstone
MetaTrader 5 demo account. Same signal, same longs AND shorts, same
stop-loss / take-profit attached to the order.

Pepperstone has no web API of its own. The way in from Python is
MetaQuotes' `MetaTrader5` package, which drives a MetaTrader 5 terminal
running on the same machine — so this bot needs **Windows** and
Pepperstone's MT5 terminal installed (it starts the terminal itself if it
isn't open). Choose MetaTrader 5 as the platform when opening the demo
account; MT4 and cTrader accounts can't be reached this way.

Strategy — momentum streak (no smoothing, reacts fast, whipsaws more)
-----------------------------------------------------------------------
- Timeframe : 15-minute bars by default (SCANNER_TIMEFRAME env var: M1, M5,
  M15 or M30, shared with the other scanners); MT5's own history, closed
  bars only
- Buy       : STREAK_LENGTH consecutive HIGHER closes -> open LONG
- Sell      : STREAK_LENGTH consecutive LOWER closes  -> open SHORT
- A reversal streak closes an opposing position; the same-direction streak
  while already positioned is a no-op (no pyramiding).
- Risk mgmt : 2% stop-loss / 5% take-profit by default (STOP_LOSS_PERCENT /
  TAKE_PROFIT_PERCENT env vars, shared with the other scanners), attached
  to the order, so Pepperstone enforces them even while the bot is stopped.

Sizing
------
PEPPERSTONE_BUDGET (default 10,000, in the account's currency) is split into
PEPPERSTONE_MAX_POSITIONS (default 5) slices, and each trade is worth up to
one slice — 2,000 by default, so a 2% stop-loss costs about 40. The budget
is exposure (what the positions are worth), not margin. The default is this
high because MT5's smallest trade is 0.01 lots: 1,000 units of a currency
pair, worth roughly £750-£1,000, and similar or more for gold and indices.
Any market whose smallest trade is worth more than a slice is skipped at
startup, with the reason logged. For trades of a few pounds, see
oanda_momentum_scanner_bot.py — OANDA trades single units.

Once PEPPERSTONE_MAX_POSITIONS positions are open, further signals are
skipped; when several markets signal at once, the biggest move across its
streak gets the slot first.

Differences from the IG scanner
-------------------------------
- Real price history: MT5 keeps its own bars, so no locally built bars and
  no warm-up.
- Closed bars only, acted on as they close: each market is traded at most
  once per bar, and never on a bar that closed before the bot started — so
  after a start, nothing happens until the next bar closes (up to one bar).
- Its positions are tagged with a magic number and it ignores all others,
  so it can share an account with pepperstone_ema_bot.py and your own
  manual trades — on a hedging account, that is (the startup log shows the
  account's mode and warns if it isn't hedging).

Setup
-----
1. Open a Pepperstone demo account on MetaTrader 5, install Pepperstone's
   MT5 terminal, log into the demo account in it once, and switch on the
   "Algo Trading" button on its toolbar.
2. pip install -r requirements.txt   (MetaTrader5 — Windows only)
3. Either leave the terminal logged in, or set PEPPERSTONE_LOGIN,
   PEPPERSTONE_PASSWORD and PEPPERSTONE_SERVER (the server name shown at
   login, e.g. "PepperstoneUK-Demo"). MT5_TERMINAL_PATH points at
   terminal64.exe if the package can't find the terminal on its own.
4. Run:
       python pepperstone_momentum_scanner_bot.py

It checks the account is a demo account at startup and exits otherwise —
logging the terminal into a live account never makes it trade live money.
"""

import logging
import math
import os
import sys
import time

import MetaTrader5 as mt5

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))
from dashboard_reporter import DashboardReporter  # noqa: E402 - needs the path above

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
LOGIN = os.environ.get("PEPPERSTONE_LOGIN", "")
PASSWORD = os.environ.get("PEPPERSTONE_PASSWORD", "")
SERVER = os.environ.get("PEPPERSTONE_SERVER", "")
TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH", "")

# The IG scanner's pool in Pepperstone's MT5 symbol names. A name missing
# from the account is also tried as a prefix, for brokers that add suffixes.
# Anything that can't be traded within one slice is skipped at startup.
DEFAULT_POOL = (
    "UK100,US500,US30,NAS100,GER40,JPN225,"   # indices
    "XAUUSD,XAGUSD,SpotBrent,SpotCrude,"      # commodities
    "EURUSD,GBPUSD,USDJPY,EURGBP,AUDUSD"      # FX majors
)
POOL = [s.strip() for s in os.environ.get("PEPPERSTONE_POOL", DEFAULT_POOL).split(",") if s.strip()]

STREAK_LENGTH = int(os.environ.get("STREAK_LENGTH", "3"))  # consecutive up/down bars to trigger

BUDGET = float(os.environ.get("PEPPERSTONE_BUDGET", "10000"))  # total exposure, in the account's currency
MAX_OPEN_POSITIONS = int(os.environ.get("PEPPERSTONE_MAX_POSITIONS", "5"))
TRADE_EXPOSURE = BUDGET / MAX_OPEN_POSITIONS

# Percent of the entry price, e.g. STOP_LOSS_PERCENT=0.5 for 0.5%. Each
# symbol has a minimum stop distance, so a very tight value can get a trade
# rejected (the rejection reason is logged).
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PERCENT", "2")) / 100
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PERCENT", "5")) / 100

MAGIC = 928001                # tags this bot's positions; pepperstone_ema_bot.py uses 928002
ORDER_COMMENT = "momentum scanner"
DEVIATION_POINTS = 20         # accepted slippage, where the symbol's execution mode honours it
BAR_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30}
TIMEFRAME = os.environ.get("SCANNER_TIMEFRAME", "").strip().upper() or "M15"  # bar length
assert TIMEFRAME in BAR_MINUTES, "SCANNER_TIMEFRAME must be M1, M5, M15 or M30"
MT5_TIMEFRAME = getattr(mt5, f"TIMEFRAME_{TIMEFRAME}")
BAR_SECONDS = BAR_MINUTES[TIMEFRAME] * 60
LOOP_INTERVAL_SECONDS = min(30, BAR_SECONDS // 4)  # how often to look for newly closed bars
ERROR_BACKOFF_SECONDS = 60
MAX_CONSECUTIVE_ERRORS = 10

assert STREAK_LENGTH >= 2, "STREAK_LENGTH must be at least 2 to mean anything"
assert MAX_OPEN_POSITIONS >= 1, "PEPPERSTONE_MAX_POSITIONS must be at least 1"
assert BUDGET > 0, "PEPPERSTONE_BUDGET must be positive"
assert STOP_LOSS_PCT > 0 and TAKE_PROFIT_PCT > 0, "STOP_LOSS_PERCENT and TAKE_PROFIT_PERCENT must be positive"
assert POOL, "PEPPERSTONE_POOL resolved to an empty pool"

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pepperstone_momentum_bot")

# Off unless DASHBOARD_URL and DASHBOARD_TOKEN are set (see shared/dashboard_reporter.py).
dashboard = DashboardReporter("pepperstone-momentum-scanner", "Pepperstone momentum scanner", broker="Pepperstone", strategy="Momentum streak")


# ---------------------------------------------------------------------------
# CLIENT / STARTUP CHECKS
# ---------------------------------------------------------------------------
def connect():
    """Attach to the MT5 terminal (starting it if needed) and return the
    account. Exits unless it's a demo account the terminal will trade on."""
    kwargs = {"path": TERMINAL_PATH} if TERMINAL_PATH else {}
    if LOGIN:
        if not PASSWORD or not SERVER:
            log.error("PEPPERSTONE_LOGIN is set, so PEPPERSTONE_PASSWORD and PEPPERSTONE_SERVER must be too.")
            sys.exit(1)
        kwargs.update(login=int(LOGIN), password=PASSWORD, server=SERVER)
    if not mt5.initialize(**kwargs):
        log.error(
            f"Couldn't connect to MetaTrader 5: {mt5.last_error()}. Check Pepperstone's MT5 terminal is installed "
            f"(or set MT5_TERMINAL_PATH to its terminal64.exe) and logged into the demo account, "
            f"or set PEPPERSTONE_LOGIN / PEPPERSTONE_PASSWORD / PEPPERSTONE_SERVER."
        )
        sys.exit(1)

    account = mt5.account_info()
    if account is None:
        log.error(f"Connected to MetaTrader 5, but it isn't logged into an account: {mt5.last_error()}")
        sys.exit(1)
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        log.critical(f"Account {account.login} on {account.server} is not a demo account. "
                     f"This bot only trades demo accounts. Exiting.")
        sys.exit(1)

    terminal = mt5.terminal_info()
    if terminal is not None and (not terminal.trade_allowed or terminal.tradeapi_disabled):
        log.critical("The MetaTrader 5 terminal isn't allowing automated trading, so it would refuse every "
                     "order. Switch on the Algo Trading button on its toolbar (and, under Tools > Options > "
                     "Expert Advisors, don't disable trading via the Python API), then restart.")
        sys.exit(1)
    return account


# ---------------------------------------------------------------------------
# MARKETS / SIZING
# ---------------------------------------------------------------------------
def lot_value(symbol: str, price: float):
    """What one lot is worth in the account's currency — the profit MT5
    works out for a 1% move, times 100, so MT5 does the currency conversion."""
    profit = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 1.0, price, price * 1.01)
    return profit * 100 if profit else None


def volume_for_slice(info, value_per_lot: float) -> float:
    """Lots worth up to one budget slice, rounded down to the symbol's lot
    step; 0 if even its smallest trade is worth more than a slice."""
    steps = math.floor(TRADE_EXPOSURE / value_per_lot / info.volume_step + 1e-9)
    volume = min(round(steps * info.volume_step, 8), info.volume_max)
    return volume if volume >= info.volume_min else 0.0


def find_symbol(name: str, all_names: list):
    """(MT5 symbol name, "") or (None, why not). Tries the exact name
    (ignoring case), then the one symbol whose name starts with it."""
    exact = [n for n in all_names if n.upper() == name.upper()]
    matches = exact or [n for n in all_names if n.upper().startswith(name.upper())]
    if len(matches) == 1:
        return matches[0], ""
    if matches:
        return None, f"several symbols start with it ({', '.join(matches[:8])}) — use the exact one"
    return None, "no such symbol on this account"


def resolve_pool(currency: str) -> dict:
    """{symbol: MT5 symbol info} for the usable part of the pool. Like the IG
    scanner, this SKIPS (logging why) anything it can't trade instead of
    exiting — the pool is about breadth."""
    all_names = [s.name for s in (mt5.symbols_get() or ())]
    usable = {}
    for entry in POOL:
        symbol, reason = find_symbol(entry, all_names)
        if symbol is None:
            log.warning(f"Skipping {entry}: {reason}.")
            continue
        if not mt5.symbol_select(symbol, True):  # adds it to Market Watch, which prices and bars need
            log.warning(f"Skipping {symbol}: couldn't add it to Market Watch ({mt5.last_error()}).")
            continue
        info = mt5.symbol_info(symbol)
        if info is None or info.trade_mode in (mt5.SYMBOL_TRADE_MODE_DISABLED, mt5.SYMBOL_TRADE_MODE_CLOSEONLY):
            log.warning(f"Skipping {symbol}: not open for new trades on this account.")
            continue
        # A symbol just added to Market Watch can show no price until its
        # first tick arrives; its last bar's close is close enough to size by.
        bars = fetch_closes(symbol, 1)
        value = lot_value(symbol, info.ask or info.bid or (bars[1][-1] if bars else 0))
        if not value:
            log.warning(f"Skipping {symbol}: MT5 couldn't value a trade in it ({mt5.last_error()}).")
            continue
        if volume_for_slice(info, value) == 0:
            log.warning(
                f"Skipping {symbol}: its smallest trade ({info.volume_min:g} lots) is worth about "
                f"{info.volume_min * value:,.0f} {currency}, more than a {TRADE_EXPOSURE:,.2f} {currency} "
                f"slice — raise PEPPERSTONE_BUDGET to include it."
            )
            continue
        usable[symbol] = info
        log.info(f"Using {symbol} ({info.description})" + (f" for '{entry}'" if symbol != entry else ""))

    if not usable:
        log.critical("Nothing in the pool is tradable within the budget. Exiting.")
        sys.exit(1)
    return usable


# ---------------------------------------------------------------------------
# MARKET DATA / SIGNAL
# ---------------------------------------------------------------------------
def fetch_closes(symbol: str, count: int):
    """(start time of the latest closed bar, its last `count` closes, oldest
    first), or None if MT5 has no bars for it. Position 0 is the bar still
    forming, so this starts at 1. MT5 bars are built from bid prices, and
    their times are in the broker's server time zone, not UTC."""
    rates = mt5.copy_rates_from_pos(symbol, MT5_TIMEFRAME, 1, count)
    if rates is None or len(rates) == 0:
        return None
    return int(rates["time"][-1]), rates["close"].tolist()


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
    """{symbol: MT5 position} for this bot's own open positions."""
    positions = mt5.positions_get()
    if positions is None:
        raise RuntimeError(f"couldn't read positions: {mt5.last_error()}")
    return {p.symbol: p for p in positions if p.magic == MAGIC}


def filling_type(info) -> int:
    """A fill policy the symbol accepts — its filling_mode flags are 1 for
    fill-or-kill and 2 for immediate-or-cancel."""
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    if info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def to_tick(info, price: float) -> float:
    """Round a price to the symbol's tick size (0.5 for UK100, for example)."""
    tick = info.trade_tick_size or info.point
    return round(round(price / tick) * tick, info.digits)


def send(request: dict) -> tuple:
    """(done?, e.g. 'DONE 0.02 @ 1.08512' or 'REJECTED (10019: No money)')."""
    result = mt5.order_send(request)
    if result is None:
        return False, f"NOT SENT ({mt5.last_error()})"
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        return True, f"DONE {result.volume:g} @ {result.price}"
    return False, f"REJECTED ({result.retcode}: {result.comment})"


def open_position(symbol: str, info, direction: str, currency: str) -> bool:
    """Market order with the stop-loss and take-profit attached. True if it filled."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log.error(f"No live price for {symbol}; can't {direction}.")
        return False
    buy = direction == "BUY"
    price = tick.ask if buy else tick.bid
    value = lot_value(symbol, price)
    volume = volume_for_slice(info, value) if value else 0.0
    if volume == 0:
        log.info(f"{symbol}: {direction} signal, but its smallest trade is now worth more than a slice; skipping.")
        return False
    sign = 1 if buy else -1
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": to_tick(info, price * (1 - sign * STOP_LOSS_PCT)),
        "tp": to_tick(info, price * (1 + sign * TAKE_PROFIT_PCT)),
        "deviation": DEVIATION_POINTS,
        "magic": MAGIC,
        "comment": ORDER_COMMENT,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_type(info),
    }
    done, outcome = send(request)
    log.info(f"{direction} submitted -> {symbol} volume={volume:g} (~{volume * value:,.2f} {currency}) "
             f"stop={request['sl']} limit={request['tp']} result={outcome}")
    return done


def close_position(position, info, reason: str) -> bool:
    """Close the whole position at market. True if it filled."""
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        log.error(f"No live price for {position.symbol}; can't close it ({reason}).")
        return False
    closing_long = position.type == mt5.POSITION_TYPE_BUY
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": position.ticket,
        "symbol": position.symbol,
        "volume": position.volume,
        "type": mt5.ORDER_TYPE_SELL if closing_long else mt5.ORDER_TYPE_BUY,
        "price": tick.bid if closing_long else tick.ask,
        "deviation": DEVIATION_POINTS,
        "magic": MAGIC,
        "comment": ORDER_COMMENT,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_type(info),
    }
    done, outcome = send(request)
    log.info(f"CLOSE submitted ({reason}) -> {position.symbol} {'long' if closing_long else 'short'} "
             f"{position.volume:g} lots result={outcome}")
    if done:
        CLOSE_REASONS[position.ticket] = reason
    return done


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
DEAL_REASONS = {
    mt5.DEAL_REASON_SL: "stop-loss",
    mt5.DEAL_REASON_TP: "take-profit",
    mt5.DEAL_REASON_EXPERT: "closed by the bot",
    mt5.DEAL_REASON_CLIENT: "closed by hand",
    mt5.DEAL_REASON_MOBILE: "closed by hand (phone)",
    mt5.DEAL_REASON_WEB: "closed by hand (web)",
    mt5.DEAL_REASON_SO: "stop-out",
}
_server_offset = None  # seconds MT5's server clock runs ahead of UTC, once known
CLOSE_REASONS = {}  # position ticket -> why this bot closed it (MT5 only records "expert")


def server_offset(symbols) -> int | None:
    """How far the broker's server clock (which MT5 stamps everything with -
    UTC+2/+3 at Pepperstone) is ahead of UTC, read off a tick from the last
    two minutes. Stays unknown while every market is shut, e.g. at weekends."""
    global _server_offset
    now = time.time()
    for symbol in symbols:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            continue
        ahead = tick.time - now
        rounded = round(ahead / 1800) * 1800  # zones are whole or half hours
        if abs(ahead - rounded) < 120 and abs(rounded) <= 14 * 3600:
            _server_offset = rounded
            break
    return _server_offset


def report_to_dashboard(markets) -> None:
    """Account, this bot's open positions and its trades from the last week,
    every 15 seconds or so. All local calls to the terminal; a failure just
    skips it. Trades wait until the server's time offset is known, so their
    times are right."""
    try:
        account = mt5.account_info()
        positions = fetch_positions()
        offset = server_offset(markets)
        now = int(time.time())
        deals = mt5.history_deals_get(now - 8 * 86400, now + 3 * 86400) or ()  # wide: the window is in server time
    except Exception:
        return
    if account is None:
        return
    utc = (lambda server_time: server_time - offset) if offset is not None else (lambda server_time: None)

    opened = {d.position_id: d for d in deals if d.entry == mt5.DEAL_ENTRY_IN and d.magic == MAGIC}
    closing = {}
    for deal in deals:
        if deal.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY) and deal.position_id in opened:
            closing.setdefault(deal.position_id, []).append(deal)

    trades = []
    for position_id, outs in closing.items():
        entry = opened[position_id]
        volume = sum(d.volume for d in outs)
        trades.append({
            "ref": position_id,
            "symbol": entry.symbol,
            "direction": "long" if entry.type == mt5.DEAL_TYPE_BUY else "short",
            "size": entry.volume,
            "entryPrice": entry.price,
            "exitPrice": sum(d.price * d.volume for d in outs) / volume if volume else None,
            "openedAt": utc(entry.time),
            "closedAt": utc(outs[-1].time),
            "pnl": sum(d.profit + d.swap + d.commission + d.fee for d in outs) + entry.commission + entry.fee,
            "closeReason": (CLOSE_REASONS.get(position_id) if outs[-1].reason == mt5.DEAL_REASON_EXPERT else None)
            or DEAL_REASONS.get(outs[-1].reason, "closed"),
        })

    dashboard.update(
        account={"balance": account.balance, "equity": account.equity, "unrealizedPl": account.profit},
        positions=[{
            "symbol": p.symbol,
            "direction": "long" if p.type == mt5.POSITION_TYPE_BUY else "short",
            "size": p.volume,
            "entryPrice": p.price_open,
            "currentPrice": p.price_current,
            "pnl": p.profit + p.swap,
            "stopLoss": p.sl or None,
            "takeProfit": p.tp or None,
            "openedAt": utc(p.time),
        } for p in positions.values()],
        trades=trades if offset is not None else [],
    )


# ---------------------------------------------------------------------------
# TRADING CYCLE
# ---------------------------------------------------------------------------
def trade_new_bars(pool: dict, new_bars: dict, positions: dict, currency: str) -> None:
    """Act on the markets whose bar just closed. `new_bars` maps symbol ->
    (bar time, closes); `positions` is this bot's open positions."""
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
        elif (position.type == mt5.POSITION_TYPE_SELL) == (signal == "bullish"):
            if close_position(position, pool[symbol], f"{signal} reversal"):
                held.remove(symbol)

    # Biggest streak first, while slots last.
    for move, symbol, direction in sorted(candidates, reverse=True):
        if len(held) >= MAX_OPEN_POSITIONS:
            log.info(f"{symbol}: {direction} streak ({move:.2%}), but {MAX_OPEN_POSITIONS} positions "
                     f"are already open; skipping this bar.")
        elif open_position(symbol, pool[symbol], direction, currency):
            held.append(symbol)


def log_bar_summary(new_bars: dict, positions: dict, pool: dict) -> None:
    """One line per batch of newly closed bars: what's streaking and what's held."""
    rising = [s for s, (_, c) in new_bars.items() if detect_streak(c) == "bullish"]
    falling = [s for s, (_, c) in new_bars.items() if detect_streak(c) == "bearish"]
    held = [f"{s} {'long' if p.type == mt5.POSITION_TYPE_BUY else 'short'} {p.profit:+.2f}"
            for s, p in positions.items() if s in pool]
    # MT5 bar times are server time, labelled as if UTC — fine for a label.
    closed_at = time.strftime("%a %H:%M", time.gmtime(max(t for t, _ in new_bars.values()) + BAR_SECONDS))
    log.info(
        f"Bar closed {closed_at} server time ({len(new_bars)} market(s)) | rising: {', '.join(rising) or 'none'} | "
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
            f"Exiting for safety — check the MT5 terminal is running and connected before restarting."
        )
        sys.exit(1)
    backoff = min(ERROR_BACKOFF_SECONDS * consecutive_errors, 300)
    log.info(f"Retrying in {backoff}s...")
    time.sleep(backoff)


def run_bot() -> None:
    account = connect()
    currency = account.currency
    pool = resolve_pool(currency)

    log.info("=" * 78)
    log.info("Pepperstone Momentum Streak Scanner starting — DEMO ACCOUNT ONLY — HIGH RISK / EXPERIMENTAL")
    margin_mode = {mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING: "hedging",
                   mt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING: "netting"}.get(account.margin_mode, "exchange")
    log.info(f"Account {account.login} on {account.server} ({margin_mode}) | balance={account.balance:,.2f} "
             f"{currency} | equity={account.equity:,.2f} | free margin={account.margin_free:,.2f} | "
             f"leverage 1:{account.leverage}")
    if margin_mode != "hedging":
        log.warning("This is not a hedging account: positions in one symbol net together, so this bot and any "
                    "other trading in its markets on this account would close each other's trades.")
    log.info(f"Pool: {len(pool)}/{len(POOL)} markets usable")
    log.info(
        f"Budget={BUDGET:,.2f} {currency} in {MAX_OPEN_POSITIONS} slices of {TRADE_EXPOSURE:,.2f} | "
        f"Streak length={STREAK_LENGTH} bars | Stop-loss={STOP_LOSS_PCT * 100:g}% | "
        f"Take-profit={TAKE_PROFIT_PCT * 100:g}% | Timeframe={TIMEFRAME}"
    )
    log.info("=" * 78)
    dashboard.describe(account=f"{account.login} on {account.server}", currency=currency, config={
        "markets": list(pool), "budget": BUDGET, "maxPositions": MAX_OPEN_POSITIONS,
        "timeframe": TIMEFRAME, "streakLength": STREAK_LENGTH, "stopLossPercent": STOP_LOSS_PCT * 100,
        "takeProfitPercent": TAKE_PROFIT_PCT * 100, "magicNumber": MAGIC,
    })

    seen_bar = None  # symbol -> start time of the latest closed bar already dealt with
    consecutive_errors = 0

    while True:
        try:
            if dashboard.due():
                report_to_dashboard(pool)

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
                    positions = fetch_positions()
                    log_bar_summary(new_bars, positions, pool)
                    trade_new_bars(pool, new_bars, positions, currency)
                    seen_bar.update({s: t for s, (t, _) in new_bars.items()})
        except Exception as e:  # anything unexpected counts toward the safety cutoff, not a crash
            consecutive_errors += 1
            log.error(f"[{consecutive_errors}] Error this loop: {e}")
            sleep_after_error(consecutive_errors)
            continue

        consecutive_errors = 0
        dashboard.sleep(LOOP_INTERVAL_SECONDS, lambda: report_to_dashboard(pool))  # reports fall due while it waits


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        log.info("Bot stopped manually (KeyboardInterrupt). Goodbye.")
    finally:
        mt5.shutdown()
