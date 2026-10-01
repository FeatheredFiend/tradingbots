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
- Overnight : its positions are closed 15 minutes before the daily
  rollover (22:00 UK), so no swap is paid, and nothing new opens from an
  hour before it to 45 minutes after (SCANNER_FLAT_MINUTES /
  SCANNER_LAST_ENTRY_MINUTES, 0 = off; see shared/rollover.py). Only
  positions with its magic number are closed.
- Stagnancy : one of its positions that has gone nowhere for a while is
  logged (shadow, the default) or closed with the reason TIMEOUT_STAGNANT,
  freeing its slot (SCANNER_STAGNANT_* settings; see shared/stagnancy.py).
- Broadcasts: with DASHBOARD_BROADCAST=1 it takes broadcast trades from the
  dashboard (shared/broadcast.py) - the admin's market and side, its own
  slice size (or less), stop-loss, take-profit and limits, the order's
  comment naming the broadcast. A market outside the pool is managed until
  its trade closes (brackets, the pre-rollover close, the stagnancy timeout)
  but never traded on a streak.

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
from dashboard_reporter import CommandError, DashboardReporter  # noqa: E402 - needs the path above
import broadcast  # noqa: E402 - needs the path above
import rollover  # noqa: E402 - needs the path above
import stagnancy  # noqa: E402 - needs the path above
import symbols  # noqa: E402 - needs the path above

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
ACCOUNT_MODE = "demo"  # connect() exits unless MT5 says the account is a demo one
BROADCASTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "broadcasts.json")

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
broadcasts = broadcast.Book(path=BROADCASTS_FILE, log=log)  # the broadcast trades it acted on (shared/broadcast.py)


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


_open_problem = ""  # why the last open_position() didn't fill, for a broadcast's answer


def open_position(symbol: str, info, direction: str, currency: str, volume: float = None,
                  comment: str = None) -> bool:
    """Market order with the stop-loss and take-profit attached: one budget
    slice, or `volume` lots (a broadcast's), its comment `comment` if given.
    True if it filled."""
    global _open_problem
    _open_problem = ""
    if rollover.entries_paused():
        log.info(f"{symbol}: {direction} signal, but it's too near the daily rollover to open a trade; skipping.")
        _open_problem = "too near the daily rollover"
        return False
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log.error(f"No live price for {symbol}; can't {direction}.")
        _open_problem = "no live price"
        return False
    buy = direction == "BUY"
    price = tick.ask if buy else tick.bid
    value = lot_value(symbol, price)
    if volume is None:
        volume = volume_for_slice(info, value) if value else 0.0
    if volume == 0:
        log.info(f"{symbol}: {direction} signal, but its smallest trade is now worth more than a slice; skipping.")
        _open_problem = "its smallest trade is worth more than a slice"
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
        "comment": (comment or ORDER_COMMENT)[:31],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_type(info),
    }
    done, outcome = send(request)
    log.info(f"{direction} submitted -> {symbol} volume={volume:g} (~{volume * value:,.2f} {currency}) "
             f"stop={request['sl']} limit={request['tp']} result={outcome}")
    if not done:
        _open_problem = outcome
    return done


def close_position(position, info, reason: str, volume: float = None) -> bool:
    """Close the whole position at market, or just `volume` lots of it. True if it filled."""
    volume = volume or position.volume
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        log.error(f"No live price for {position.symbol}; can't close it ({reason}).")
        return False
    closing_long = position.type == mt5.POSITION_TYPE_BUY
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": position.ticket,
        "symbol": position.symbol,
        "volume": volume,
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
             f"{volume:g} lots result={outcome}")
    if done and volume >= position.volume:
        CLOSE_REASONS[position.ticket] = reason
    return done


def close_before_rollover(pool: dict) -> None:
    """Close every position with this bot's magic number, so none is
    charged a night's swap. Other bots' and hand-made positions are left alone."""
    positions = mt5.positions_get()
    if positions is None:
        raise RuntimeError(f"couldn't read positions: {mt5.last_error()}")
    for position in positions:
        if position.magic == MAGIC:
            close_position(position, pool.get(position.symbol) or mt5.symbol_info(position.symbol), rollover.REASON)


# ---------------------------------------------------------------------------
# STAGNANCY TIMEOUT (shared/stagnancy.py)
# ---------------------------------------------------------------------------
watch = None  # the timeout, set up in run_bot()
STALE_TICK_SECONDS = 300  # no tick for this long = market closed


def stagnancy_pass(pool: dict) -> None:
    """The stagnancy timeout over this bot's positions (its magic number): one
    that has gone nowhere for a while is logged (shadow) or closed
    (enforce). Its slot is free once MT5 no longer lists it - the next bar's
    count of positions doesn't include it. Mid prices come from MT5's ticks
    (its bars are built from the bid)."""
    now = time.time()
    positions = mt5.positions_get()
    if positions is None:
        raise RuntimeError(f"couldn't read positions: {mt5.last_error()}")
    own = [p for p in positions if p.magic == MAGIC and p.symbol in pool]
    offset = server_offset(pool)  # MT5 stamps ticks and positions with the server's clock
    for symbol in {p.symbol for p in own}:
        tick = mt5.symbol_info_tick(symbol)
        fresh = bool(tick and tick.bid and tick.ask and offset is not None
                     and now - (tick.time - offset) <= STALE_TICK_SECONDS)
        watch.observe(symbol, now, tick.bid if fresh else None, tick.ask if fresh else None, fresh)
    held = []
    for p in own:
        # MT5's profit is at the closing price (net of the spread); add the swap, the
        # opening deal's commission (share CFDs: $0.02 a share each way) and as much again to close.
        paid = sum(2 * d.commission + d.fee for d in mt5.history_deals_get(position=p.ticket) or ()
                   if d.entry == mt5.DEAL_ENTRY_IN)
        value = lot_value(p.symbol, p.price_open)  # one lot's worth in the account's currency
        distance = abs(p.price_open - p.sl) if p.sl else p.price_open * STOP_LOSS_PCT
        held.append((stagnancy.Held(
            key=str(p.ticket), symbol=p.symbol, direction="long" if p.type == mt5.POSITION_TYPE_BUY else "short",
            size=p.volume, entry=p.price_open, opened_at=p.time - offset if offset is not None else None,
            pnl=p.profit + p.swap + paid, risk=p.volume * distance * value / p.price_open if value else None,
            refs=(str(p.ticket),),
        ), watch.last_mid(p.symbol) is not None))
    watch.run(held, now, lambda h: close_ticket_now(h, pool))


def close_ticket_now(held, pool: dict) -> dict:
    """Close one of the bot's positions at market for the stagnancy timeout,
    by its ticket. What it filled at and made come from its deals."""
    position = next(iter(mt5.positions_get(ticket=int(held.key)) or ()), None)
    if position is None:
        # Its stop-loss or take-profit closed it a moment before: the next pass finds it gone.
        return {"done": False, "problem": "MT5 no longer lists it"}
    # A part fill (TRADE_RETCODE_DONE_PARTIAL) isn't done: the timeout closes the rest next pass.
    if not close_position(position, pool.get(position.symbol) or mt5.symbol_info(position.symbol),
                          stagnancy.TIMEOUT_REASON):
        return {"done": False, "problem": "Pepperstone didn't close it (see the line above)"}
    deals = mt5.history_deals_get(position=position.ticket) or ()
    outs = [d for d in deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
    volume = sum(d.volume for d in outs)
    return {"done": True, "price": sum(d.price * d.volume for d in outs) / volume if volume else None,
            "pnl": round(sum(d.profit + d.swap + d.commission + d.fee for d in deals), 2) if outs else None}


def close_from_dashboard(markets: dict, symbol: str, ref, direction: str, size) -> str:
    """A close asked for on the dashboard (DASHBOARD_COMMANDS=1): the bot's
    position it showed as `ref` (its ticket) - all of it, or `size` lots -
    if it's still open on the same side. Answers what happened, or raises
    CommandError."""
    if symbol not in markets:
        raise CommandError(f"{symbol} isn't one of this bot's markets.")
    own = [p for p in (mt5.positions_get(symbol=symbol) or ())
           if p.magic == MAGIC and (not ref or str(p.ticket) == ref)]
    if not own:
        raise CommandError(f"The bot has no open {symbol} position" + (f" {ref}" if ref else "")
                           + " now - it may have closed already.")
    position, info = own[0], markets[symbol]
    side = "long" if position.type == mt5.POSITION_TYPE_BUY else "short"
    if side != direction:
        raise CommandError(f"The {symbol} position is {side} now, not {direction}, so it was left alone.")
    volume = position.volume
    if size is not None and size < position.volume:
        volume = round(math.floor(size / info.volume_step + 1e-9) * info.volume_step, 8)
        if volume < info.volume_min:
            raise CommandError(f"The smallest amount Pepperstone closes in {symbol} is {info.volume_min:g} lots.")
    if not close_position(position, info, "closed from the dashboard", volume):
        raise CommandError("Pepperstone didn't close it - the bot's log says why.")
    return f"Closed {volume:g} of {position.volume:g} lots of the {symbol} {side} position."


# ---------------------------------------------------------------------------
# BROADCAST TRADES FROM THE DASHBOARD (shared/broadcast.py)
# ---------------------------------------------------------------------------
guests = {}  # symbol -> MT5 symbol info: a broadcast trade's market outside the pool, managed but never traded


def managed(pool: dict) -> dict:
    """The pool, plus the markets outside it holding a broadcast trade."""
    return {**guests, **pool}


def symbol_details(name: str):
    """(MT5 symbol, its info) for a symbol open for new trades on this account, or (None, why not)."""
    symbol, why = find_symbol(name, [s.name for s in (mt5.symbols_get() or ())])
    if symbol is None:
        return None, why
    if not mt5.symbol_select(symbol, True):  # Market Watch, which prices need
        return None, f"couldn't add it to Market Watch ({mt5.last_error()})"
    info = mt5.symbol_info(symbol)
    if info is None or info.trade_mode in (mt5.SYMBOL_TRADE_MODE_DISABLED, mt5.SYMBOL_TRADE_MODE_CLOSEONLY):
        return None, "not open for new trades on this account"
    return symbol, info


def restore_guests(pool: dict) -> None:
    """After a restart: look up the markets outside the pool its open broadcast trades are in."""
    for name in sorted(broadcasts.open_symbols() - set(pool)):
        symbol, info = symbol_details(name)
        if symbol is None:
            log.warning(f"{name}: couldn't look up its broadcast trade's market ({info}); Pepperstone still holds its "
                        f"stop-loss and take-profit.")
            continue
        guests[symbol] = info
        log.info(f"{symbol}: managing its broadcast trade (not in the pool).")


def broadcast_plan(pool: dict, currency: str, request, now: float, most: float = None) -> dict:
    """The trade this bot would make for a broadcast, every limit applied -
    or broadcast.Declined saying why not."""
    found = symbols.candidates("pepperstone", request.symbol)
    symbol = info = candidate = None
    for candidate in found:
        known = next((s for s in managed(pool) if s.upper() == candidate.code.upper()), None)
        symbol, info = (known, managed(pool)[known]) if known else symbol_details(candidate.code)
        if symbol is not None:
            break
    if symbol is None:
        raise broadcast.Declined(broadcast.UNAVAILABLE, f"{request.symbol} isn't available on Pepperstone"
                                 + (f" (looked for {', '.join(c.code for c in found)})." if found else "."))
    if rollover.entries_paused(now):
        raise broadcast.Declined(broadcast.CLOSED, f"{symbol}: too near the daily rollover to open a trade.")
    positions = fetch_positions()
    if symbol in positions:
        raise broadcast.Declined(broadcast.NO_SLOT, f"The bot already holds {symbol}.")
    held = [s for s in positions if s in pool or s in guests]
    if len(held) >= MAX_OPEN_POSITIONS:
        raise broadcast.Declined(broadcast.NO_SLOT, f"{len(held)} positions are already open (max {MAX_OPEN_POSITIONS}).")
    if watch is not None and watch.cooling(symbol, now):
        raise broadcast.Declined(broadcast.RISK, f"{symbol}: the stagnancy timeout closed it lately (cooldown).")
    tick = mt5.symbol_info_tick(symbol)
    offset = server_offset([symbol, *pool])  # MT5 stamps ticks with the server's clock
    if not (tick and tick.bid and tick.ask and offset is not None and now - (tick.time - offset) <= STALE_TICK_SECONDS):
        raise broadcast.Declined(broadcast.CLOSED, f"{symbol}: no fresh price - the market looks closed.")

    sign = 1 if request.side == "buy" else -1
    entry = tick.ask if sign > 0 else tick.bid
    value = lot_value(symbol, entry)
    if not value:
        raise broadcast.Declined(broadcast.UNAVAILABLE, f"{symbol}: MT5 couldn't value a trade in it ({mt5.last_error()}).")
    exposure, capped = broadcast.exposure_for(request, TRADE_EXPOSURE, currency, "budget slice")
    volume = math.floor(exposure / value / info.volume_step + 1e-9) * info.volume_step
    if most is not None:
        volume = min(volume, math.floor(most / info.volume_step + 1e-9) * info.volume_step)
    volume = min(round(volume, 8), info.volume_max)
    if volume < info.volume_min or volume <= 0:
        raise broadcast.Declined(broadcast.RISK, f"{symbol}: its smallest trade ({info.volume_min:g} lots) is worth "
                                                 f"about {info.volume_min * value:,.2f} {currency}, more than the "
                                                 f"{exposure:,.2f} it may trade.")
    rule = watch.book.rule(symbol) if watch is not None else None
    exits = (f"stop-loss {STOP_LOSS_PCT * 100:g}% / take-profit {TAKE_PROFIT_PCT * 100:g}% at Pepperstone; "
             + (f"closed {rollover.FLAT_MINUTES} min before the rollover" if rollover.FLAT_MINUTES else "held overnight")
             + ("; streak reversal" if symbol in pool else "; managed until it closes (not in the pool)")
             + (f"; stagnancy timeout ({rule.mode})" if rule and rule.mode != "off" else ""))
    return {"symbol": symbol, "info": info, "volume": volume, "direction": "BUY" if sign > 0 else "SELL",
            "capped": capped,
            "figures": broadcast.figures(
                symbol=symbol, name=info.description or None, size=volume, sizeUnit="lots",
                exposure=round(volume * value, 2), risk=round(volume * value * STOP_LOSS_PCT, 2), currency=currency,
                entry=entry, stopLoss=to_tick(info, entry * (1 - sign * STOP_LOSS_PCT)),
                takeProfit=to_tick(info, entry * (1 + sign * TAKE_PROFIT_PCT)), accountMode=ACCOUNT_MODE, exits=exits,
                capped=capped or None, standIn=candidate.canonical if candidate.stand_in else None, previewedAt=now)}


def broadcast_preview(pool: dict, currency: str, command: dict) -> tuple:
    """What this bot would do with a broadcast trade: (a line, {figures})."""
    request = broadcast.Request.parse(command)
    plan = broadcast_plan(pool, currency, request, time.time())
    f = plan["figures"]
    line = (f"Would {request.side} {f['size']:g} lots of {f['symbol']} at ~{f['entry']:g}: stop-loss {f['stopLoss']:g}, "
            f"take-profit {f['takeProfit']:g}" + (f" ({plan['capped']})" if plan["capped"] else ""))
    log.info(f"Broadcast #{request.id}: {line}")
    return line + ".", f


def broadcast_open(pool: dict, currency: str, command: dict) -> tuple:
    """Open a broadcast trade the admin confirmed - every limit checked again
    on fresh prices, never bigger than previewed. (a line, {figures})."""
    now = time.time()
    request = broadcast.Request.parse(command)
    broadcasts.check_new(request)
    request.check_age(now)
    plan = broadcast_plan(pool, currency, request, now, most=request.size)
    symbol = plan["symbol"]
    broadcasts.opening(request, symbol, (plan["info"].description,), now)
    if not open_position(symbol, plan["info"], plan["direction"], currency, volume=plan["volume"],
                         comment=f"broadcast-{request.id}"):
        broadcasts.failed(request)
        raise CommandError(f"Pepperstone didn't fill it: {_open_problem or 'see the bot log'}.")
    # Its rows are matched by market and opening time: MT5 names the position by a ticket the order doesn't return here.
    broadcasts.opened(request, (), now)
    if symbol not in pool:
        guests[symbol] = plan["info"]
        log.info(f"{symbol}: not in the pool - managing its broadcast trade until it closes, never trading it on a streak.")
    entry = plan["figures"]["entry"]
    verb = "Bought" if request.side == "buy" else "Sold"
    return f"{verb} {plan['volume']:g} lots of {symbol} at ~{entry:g}.", \
        {"symbol": symbol, "size": plan["volume"], "price": entry}


def broadcast_symbols(pool: dict) -> list:
    """The instrument names the dashboard can offer for this bot."""
    return sorted(set(symbols.names("pepperstone")) | {symbols.canonical(s) or s for s in pool})


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
    # Broadcast trades no longer open have closed; their markets outside the pool stop being managed.
    broadcasts.sync(set(positions), now)
    broadcasts.prune(now)
    for symbol in set(guests) - broadcasts.open_symbols():
        del guests[symbol]
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
            **(watch.fields_for(position_id) if watch is not None else {}),  # the stagnancy timeout's, if any
        })

    dashboard.update(
        account={"balance": account.balance, "equity": account.equity, "unrealizedPl": account.profit},
        positions=[{
            "ref": p.ticket,
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
    held = [s for s in positions if s in pool or s in guests]  # a broadcast trade outside the pool holds a slot too

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
            if watch is not None and watch.closing_in(symbol):
                log.info(f"{symbol}: {signal} reversal, but its {stagnancy.TIMEOUT_REASON} close is under way.")
            elif close_position(position, pool[symbol], f"{signal} reversal"):
                held.remove(symbol)

    # Biggest streak first, while slots last.
    for move, symbol, direction in sorted(candidates, reverse=True):
        if len(held) >= MAX_OPEN_POSITIONS:
            log.info(f"{symbol}: {direction} streak ({move:.2%}), but {MAX_OPEN_POSITIONS} positions "
                     f"are already open; skipping this bar.")
        elif watch is not None and watch.cooling(symbol, time.time()):  # only with SCANNER_STAGNANT_COOLDOWN set
            log.info(f"{symbol}: {direction} streak, but the stagnancy timeout closed it lately (cooldown); "
                     f"skipping this bar.")
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
    log.info(rollover.describe())
    global watch
    watch = stagnancy.start_watch("scanner", "pepperstone", "pepperstone-momentum-scanner", "Pepperstone", log,
                                  bar_seconds=BAR_SECONDS, loop_seconds=LOOP_INTERVAL_SECONDS, currency=currency)
    log.info("=" * 78)
    dashboard.describe(account=f"{account.login} on {account.server}", currency=currency, account_mode=ACCOUNT_MODE,
                       config={
        "markets": list(pool), "budget": BUDGET, "maxPositions": MAX_OPEN_POSITIONS,
        "timeframe": TIMEFRAME, "streakLength": STREAK_LENGTH, "stopLossPercent": STOP_LOSS_PCT * 100,
        "takeProfitPercent": TAKE_PROFIT_PCT * 100, "magicNumber": MAGIC, **watch.book.config(),
    })
    dashboard.accept_closes(lambda *command: close_from_dashboard(managed(pool), *command))
    restore_guests(pool)
    dashboard.tag_rows(broadcasts.tags)
    dashboard.accept_broadcasts(lambda command: broadcast_preview(pool, currency, command),
                                lambda command: broadcast_open(pool, currency, command),
                                broadcast_symbols(pool))

    seen_bar = None  # symbol -> start time of the latest closed bar already dealt with
    consecutive_errors = 0

    while True:
        try:
            if dashboard.due():
                report_to_dashboard(managed(pool))
            if rollover.flat_due():
                close_before_rollover(managed(pool))
            if watch.book.active:
                stagnancy_pass(managed(pool))

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
        dashboard.sleep(LOOP_INTERVAL_SECONDS, lambda: report_to_dashboard(managed(pool)))  # reports fall due while it waits


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        log.info("Bot stopped manually (KeyboardInterrupt). Goodbye.")
    finally:
        mt5.shutdown()
