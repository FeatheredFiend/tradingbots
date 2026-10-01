#!/usr/bin/env python3
"""
Pepperstone Demo EMA(9/21) Crossover Bot (MetaTrader 5)
=========================================================

The same strategy as ig_cfd_ema_bot.py, on a Pepperstone MetaTrader 5 demo
account — and here the default watchlist of shares actually works: MT5
has price history for Pepperstone's share CFDs, which IG's API never gave.

Pepperstone has no web API of its own. The way in from Python is
MetaQuotes' `MetaTrader5` package, which drives a MetaTrader 5 terminal
running on the same machine — so this bot needs **Windows** and
Pepperstone's MT5 terminal installed (it starts the terminal itself if it
isn't open). Choose MetaTrader 5 as the platform when opening the demo
account; MT4 and cTrader accounts can't be reached this way.

Strategy
--------
- Timeframe : 15-minute bars by default (EMA_TIMEFRAME env var: M1, M5, M15
  or M30, shared with the other EMA bots); MT5's own history, closed bars only
- Entry     : 9-period EMA crosses ABOVE the 21-period EMA -> BUY (open long)
- Exit      : 9-period EMA crosses BELOW the 21-period EMA -> close the long
- Risk mgmt : 2% stop-loss / 5% take-profit, attached to the order, so
              Pepperstone enforces them even while the bot is stopped.
- Stagnancy : one of its positions (its magic number) that has gone nowhere
              for a while is logged (shadow, the default) or closed with the
              reason TIMEOUT_STAGNANT, freeing its slot (EMA_STAGNANT_*
              settings; see shared/stagnancy.py).

Sizing
------
The same as pepperstone_momentum_scanner_bot.py: PEPPERSTONE_BUDGET
(default 10,000, in the account's currency) split into
PEPPERSTONE_MAX_POSITIONS (default 5) slices, and each buy is worth up to
one slice. The budget is exposure, not margin. A market whose smallest
trade is worth more than a slice is skipped at startup, with the reason
logged. A US share CFD trades in whole shares, so one Apple share (~£170)
is its smallest trade.

Watchlist
---------
PEPPERSTONE_WATCHLIST is a fixed list of MT5 symbol names, not a screener.
The default is the IG bot's US names in Pepperstone's form — "AAPL.US" is
Apple during the US session; Pepperstone also lists "AAPL.US-24" for
round-the-clock trading. A name that isn't on the account, or that matches
several symbols, stops the bot at startup (like the IG bot, a hand-picked
list should be exactly right), listing what it did find.

Bars are acted on as they close, so after a start nothing happens until the
next bar closes; a share's last bar of the day is only seen as
closed at the next day's open. The bot's positions are tagged with a magic
number and it ignores all others, so it can share a (hedging) account with
pepperstone_momentum_scanner_bot.py and your own trades.

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
4. (Optional) PEPPERSTONE_WATCHLIST="AAPL.US,MSFT.US,EURUSD"
5. Run:
       python pepperstone_ema_bot.py

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
import stagnancy  # noqa: E402 - needs the path above

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
LOGIN = os.environ.get("PEPPERSTONE_LOGIN", "")
PASSWORD = os.environ.get("PEPPERSTONE_PASSWORD", "")
SERVER = os.environ.get("PEPPERSTONE_SERVER", "")
TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH", "")

DEFAULT_WATCHLIST = "AAPL.US,MSFT.US,AMZN.US,GOOGL.US,TSLA.US,NVDA.US,META.US"
WATCHLIST = [
    s.strip()
    for s in os.environ.get("PEPPERSTONE_WATCHLIST", DEFAULT_WATCHLIST).split(",")
    if s.strip()
]

EMA_SHORT_PERIOD = 9
EMA_LONG_PERIOD = 21
BARS_LOOKBACK = 200           # plenty for a 21-EMA warm-up.

BUDGET = float(os.environ.get("PEPPERSTONE_BUDGET", "10000"))  # total exposure, in the account's currency
MAX_OPEN_POSITIONS = int(os.environ.get("PEPPERSTONE_MAX_POSITIONS", "5"))
TRADE_EXPOSURE = BUDGET / MAX_OPEN_POSITIONS

STOP_LOSS_PCT = 0.02           # 2% hard stop-loss, attached to the order itself.
TAKE_PROFIT_PCT = 0.05         # 5% take-profit, attached to the order itself.

MAGIC = 928002                # tags this bot's positions; the momentum scanner uses 928001
ORDER_COMMENT = "ema bot"
DEVIATION_POINTS = 20         # accepted slippage, where the symbol's execution mode honours it
BAR_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30}
TIMEFRAME = os.environ.get("EMA_TIMEFRAME", "").strip().upper() or "M15"  # bar length
assert TIMEFRAME in BAR_MINUTES, "EMA_TIMEFRAME must be M1, M5, M15 or M30"
MT5_TIMEFRAME = getattr(mt5, f"TIMEFRAME_{TIMEFRAME}")
LOOP_INTERVAL_SECONDS = min(60, BAR_MINUTES[TIMEFRAME] * 15)  # how often to look for newly closed bars
MAX_CONSECUTIVE_ERRORS = 10

assert MAX_OPEN_POSITIONS >= 1, "PEPPERSTONE_MAX_POSITIONS must be at least 1"
assert BUDGET > 0, "PEPPERSTONE_BUDGET must be positive"
assert WATCHLIST, "PEPPERSTONE_WATCHLIST resolved to an empty list"

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pepperstone_ema_bot")

# Off unless DASHBOARD_URL and DASHBOARD_TOKEN are set (see shared/dashboard_reporter.py).
dashboard = DashboardReporter("pepperstone-ema-bot", "Pepperstone EMA crossover", broker="Pepperstone", strategy="EMA 9/21 crossover")


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


def resolve_watchlist(currency: str) -> dict:
    """{symbol: MT5 symbol info}. Exits on a name that isn't on the account
    or is ambiguous; skips (logging why) one too big for a slice."""
    all_names = [s.name for s in (mt5.symbols_get() or ())]
    found = {}
    for entry in WATCHLIST:
        symbol, reason = find_symbol(entry, all_names)
        if symbol is None:
            log.critical(f"'{entry}': {reason}. Fix PEPPERSTONE_WATCHLIST (MT5 symbol names). Exiting.")
            sys.exit(1)
        if not mt5.symbol_select(symbol, True):  # adds it to Market Watch, which prices and bars need
            log.critical(f"Couldn't add {symbol} to Market Watch ({mt5.last_error()}). Exiting.")
            sys.exit(1)
        found[entry] = symbol

    usable = {}
    for entry, symbol in found.items():
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
        log.critical("Nothing on the watchlist is tradable within the budget. Exiting.")
        sys.exit(1)
    return usable


# ---------------------------------------------------------------------------
# MARKET DATA / SIGNAL
# ---------------------------------------------------------------------------
def fetch_closes(symbol: str, count: int):
    """(start time of the latest closed bar, its last `count` closes, oldest
    first), or None if MT5 has no bars for it. Position 0 is the bar still
    forming, so this starts at 1. MT5 bars are built from bid prices."""
    rates = mt5.copy_rates_from_pos(symbol, MT5_TIMEFRAME, 1, count)
    if rates is None or len(rates) == 0:
        return None
    return int(rates["time"][-1]), rates["close"].tolist()


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


def submit_buy(symbol: str, info, currency: str) -> bool:
    """Market buy with the stop-loss and take-profit attached. True if it filled."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log.error(f"No live price for {symbol}; can't buy.")
        return False
    value = lot_value(symbol, tick.ask)
    volume = volume_for_slice(info, value) if value else 0.0
    if volume == 0:
        log.info(f"{symbol}: bullish crossover, but its smallest trade is now worth more than a slice; skipping.")
        return False
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY,
        "price": tick.ask,
        "sl": to_tick(info, tick.ask * (1 - STOP_LOSS_PCT)),
        "tp": to_tick(info, tick.ask * (1 + TAKE_PROFIT_PCT)),
        "deviation": DEVIATION_POINTS,
        "magic": MAGIC,
        "comment": ORDER_COMMENT,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_type(info),
    }
    done, outcome = send(request)
    log.info(f"BUY submitted -> {symbol} volume={volume:g} (~{volume * value:,.2f} {currency}) "
             f"stop={request['sl']} limit={request['tp']} result={outcome}")
    return done


def close_position(position, info, reason: str, volume: float = None) -> bool:
    """Close the whole (long) position at market, or just `volume` lots of it. True if it filled."""
    volume = volume or position.volume
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        log.error(f"No live price for {position.symbol}; can't close it ({reason}).")
        return False
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": position.ticket,
        "symbol": position.symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_SELL,
        "price": tick.bid,
        "deviation": DEVIATION_POINTS,
        "magic": MAGIC,
        "comment": ORDER_COMMENT,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_type(info),
    }
    done, outcome = send(request)
    log.info(f"CLOSE submitted ({reason}) -> {position.symbol} {volume:g} lots result={outcome}")
    if done and volume >= position.volume:
        CLOSE_REASONS[position.ticket] = reason
    return done


# ---------------------------------------------------------------------------
# STAGNANCY TIMEOUT (shared/stagnancy.py)
# ---------------------------------------------------------------------------
watch = None  # the timeout, set up in run_bot()
STALE_TICK_SECONDS = 300  # no tick for this long = market closed


def stagnancy_pass(watchlist: dict) -> None:
    """The stagnancy timeout over this bot's positions (its magic number): one
    that has gone nowhere for a while is logged (shadow) or closed
    (enforce). Its slot is free once MT5 no longer lists it - the next bar's
    count of positions doesn't include it. Mid prices come from MT5's ticks
    (its bars are built from the bid)."""
    now = time.time()
    positions = mt5.positions_get()
    if positions is None:
        raise RuntimeError(f"couldn't read positions: {mt5.last_error()}")
    own = [p for p in positions if p.magic == MAGIC and p.symbol in watchlist]
    offset = server_offset(watchlist)  # MT5 stamps ticks and positions with the server's clock
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
    watch.run(held, now, lambda h: close_ticket_now(h, watchlist))


def close_ticket_now(held, watchlist: dict) -> dict:
    """Close one of the bot's positions at market for the stagnancy timeout,
    by its ticket. What it filled at and made come from its deals."""
    position = next(iter(mt5.positions_get(ticket=int(held.key)) or ()), None)
    if position is None:
        # Its stop-loss or take-profit closed it a moment before: the next pass finds it gone.
        return {"done": False, "problem": "MT5 no longer lists it"}
    # A part fill (TRADE_RETCODE_DONE_PARTIAL) isn't done: the timeout closes the rest next pass.
    if not close_position(position, watchlist.get(position.symbol) or mt5.symbol_info(position.symbol),
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
    if it's still open. Answers what happened, or raises CommandError."""
    if symbol not in markets:
        raise CommandError(f"{symbol} isn't on this bot's watchlist.")
    if direction != "long":
        raise CommandError("This bot only buys, so it has no short to close.")
    own = [p for p in (mt5.positions_get(symbol=symbol) or ())
           if p.magic == MAGIC and p.type == mt5.POSITION_TYPE_BUY and (not ref or str(p.ticket) == ref)]
    if not own:
        raise CommandError(f"The bot has no open {symbol} position" + (f" {ref}" if ref else "")
                           + " now - it may have closed already.")
    position, info = own[0], markets[symbol]
    volume = position.volume
    if size is not None and size < position.volume:
        volume = round(math.floor(size / info.volume_step + 1e-9) * info.volume_step, 8)
        if volume < info.volume_min:
            raise CommandError(f"The smallest amount Pepperstone closes in {symbol} is {info.volume_min:g} lots.")
    if not close_position(position, info, "closed from the dashboard", volume):
        raise CommandError("Pepperstone didn't close it - the bot's log says why.")
    return f"Closed {volume:g} of {position.volume:g} lots of the {symbol} long position."


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
def trade_new_bars(watchlist: dict, new_bars: dict, currency: str) -> None:
    """Act on the markets whose bar just closed. `new_bars` maps symbol ->
    (bar time, closes)."""
    positions = fetch_positions()
    held = [s for s in positions if s in watchlist]

    for symbol, (_, closes) in new_bars.items():
        ema_short, ema_long = ema(closes, EMA_SHORT_PERIOD), ema(closes, EMA_LONG_PERIOD)
        signal = detect_crossover(ema_short, ema_long)
        position = positions.get(symbol)
        position_desc = f"{position.volume:g} lots P/L={position.profit:+.2f}" if position else "FLAT"
        log.info(
            f"{symbol} | price={closes[-1]} | EMA9={ema_short[-1]:.5f} | EMA21={ema_long[-1]:.5f} | "
            f"signal={signal or 'none'} | position={position_desc}"
        )

        if signal == "bullish":
            if position is not None:
                log.info(f"{symbol}: bullish crossover, but already holding a position; skipping buy.")
            elif len(held) >= MAX_OPEN_POSITIONS:
                log.info(f"{symbol}: bullish crossover, but {MAX_OPEN_POSITIONS} positions are already open; "
                         f"skipping buy.")
            elif watch is not None and watch.cooling(symbol, time.time()):  # only with EMA_STAGNANT_COOLDOWN set
                log.info(f"{symbol}: bullish crossover, but the stagnancy timeout closed it lately (cooldown); "
                         f"skipping buy.")
            elif submit_buy(symbol, watchlist[symbol], currency):
                held.append(symbol)
        elif signal == "bearish" and position is not None:
            if watch is not None and watch.closing_in(symbol):
                log.info(f"{symbol}: bearish crossover, but its {stagnancy.TIMEOUT_REASON} close is under way.")
            elif close_position(position, watchlist[symbol], "EMA bearish crossover"):
                held.remove(symbol)


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
    backoff = min(LOOP_INTERVAL_SECONDS * consecutive_errors, 300)
    log.info(f"Retrying in {backoff}s...")
    time.sleep(backoff)


def run_bot() -> None:
    account = connect()
    currency = account.currency
    watchlist = resolve_watchlist(currency)

    log.info("=" * 78)
    log.info("Pepperstone EMA(9/21) Crossover Bot starting — DEMO ACCOUNT ONLY")
    log.info(f"Account {account.login} on {account.server} | balance={account.balance:,.2f} {currency} | "
             f"equity={account.equity:,.2f} | free margin={account.margin_free:,.2f} | "
             f"leverage 1:{account.leverage}")
    if account.margin_mode != mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
        log.warning("This is not a hedging account: positions in one symbol net together, so this bot and any "
                    "other trading in its markets on this account would close each other's trades.")
    log.info(f"Watchlist: {', '.join(watchlist)}")
    log.info(
        f"Budget={BUDGET:,.2f} {currency} in {MAX_OPEN_POSITIONS} slices of {TRADE_EXPOSURE:,.2f} | "
        f"Stop-loss={STOP_LOSS_PCT:.0%} | Take-profit={TAKE_PROFIT_PCT:.0%} | "
        f"Timeframe={TIMEFRAME} | EMA periods={EMA_SHORT_PERIOD}/{EMA_LONG_PERIOD}"
    )
    global watch
    watch = stagnancy.start_watch("ema", "pepperstone", "pepperstone-ema-bot", "Pepperstone", log,
                                  bar_seconds=BAR_MINUTES[TIMEFRAME] * 60, loop_seconds=LOOP_INTERVAL_SECONDS,
                                  currency=currency)
    log.info("=" * 78)
    dashboard.describe(account=f"{account.login} on {account.server}", currency=currency, config={
        "watchlist": list(watchlist), "budget": BUDGET, "maxPositions": MAX_OPEN_POSITIONS,
        "timeframe": TIMEFRAME, "emaPeriods": f"{EMA_SHORT_PERIOD}/{EMA_LONG_PERIOD}",
        "stopLossPercent": STOP_LOSS_PCT * 100, "takeProfitPercent": TAKE_PROFIT_PCT * 100, "magicNumber": MAGIC,
        **watch.book.config(),
    })
    dashboard.accept_closes(lambda *command: close_from_dashboard(watchlist, *command))

    seen_bar = None  # symbol -> start time of the latest closed bar already dealt with
    consecutive_errors = 0

    while True:
        try:
            if dashboard.due():
                report_to_dashboard(watchlist)
            if watch.book.active:
                stagnancy_pass(watchlist)

            latest = {}
            for symbol in watchlist:
                bars = fetch_closes(symbol, BARS_LOOKBACK)
                if bars is not None and len(bars[1]) > EMA_LONG_PERIOD:
                    latest[symbol] = bars

            if seen_bar is None:
                # Bars that closed before startup are never traded on.
                seen_bar = {s: t for s, (t, _) in latest.items()}
                log.info(f"Waiting for the next {BAR_MINUTES[TIMEFRAME]}-minute bar to close before trading.")
            else:
                new_bars = {s: bars for s, bars in latest.items() if bars[0] != seen_bar.get(s)}
                if new_bars:
                    trade_new_bars(watchlist, new_bars, currency)
                    seen_bar.update({s: t for s, (t, _) in new_bars.items()})
        except Exception as e:  # anything unexpected counts toward the safety cutoff, not a crash
            consecutive_errors += 1
            log.error(f"[{consecutive_errors}] Error this loop: {e}")
            sleep_after_error(consecutive_errors)
            continue

        consecutive_errors = 0
        dashboard.sleep(LOOP_INTERVAL_SECONDS, lambda: report_to_dashboard(watchlist))  # reports fall due while it waits


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        log.info("Bot stopped manually (KeyboardInterrupt). Goodbye.")
    finally:
        mt5.shutdown()
