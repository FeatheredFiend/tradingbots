"""
5. Opening surge (the surge scanner and its followers, settings SURGE_*)
-------------------------------------------------------------------------
The first minutes after the 09:30 New York open are the wildest of the US
trading day. This strategy watches the whole US stock market then, and
jumps on any share whose price keeps jumping the same way poll after poll,
betting the surge carries on for a while.

It's two kinds of bot, which talk through a file on this PC:
- The surge scanner (engine/surge_scanner.py - one bot, on Alpaca's market
  data) watches every liquid US share and writes each surge it finds to
  strategy-bots/state/surge-signals-<New York date>.jsonl.
- A surge follower on each broker that offers US shares - Alpaca,
  Capital.com and Pepperstone - reads that file every second and trades
  each surge in that share on its own account. IG doesn't give share prices
  over its API and OANDA has no shares, so they have no follower.

Scanner
- Shares: every active, tradable share or fund on NYSE, Nasdaq, NYSE Arca,
  NYSE American or Cboe BZX that closed at MIN_PRICE ($5) or more, with a
  median of MIN_DOLLAR_VOLUME ($20 million) or more traded a day over the
  last week's sessions - the MAX_SHARES (1,500) most traded of them. Picked
  before each open from past data, and kept for the day.
- Reads: every share's latest trade, every POLL_SECONDS (5), from a minute
  before the open to WATCH_MINUTES (15) after it.
- Surge: the latest trade price has jumped at least JUMP_PERCENT (0.2%)
  the same way on each of the last CONFIRM_POLLS (3) polls - each a new
  trade, all at or after the open -> a long signal after a rise, a short
  one after a fall. Each share signals at most once a day; when more than
  10 surge on the same poll, the 10 biggest go out.

Followers
- A signal older than MAX_SIGNAL_AGE (20s) is dropped - the surge has moved
  on. So is a short on Alpaca (buys only), and a share the broker doesn't
  offer (<BROKER>_SURGE_SYMBOL: "{}" - the ticker as it is - on Alpaca and
  Capital.com, "{}.US" on Pepperstone).
- Within those 20s, a signal whose share has no price yet, or whose spread
  is too wide (below), is tried again every 2 seconds.
- Some brokers start quoting US shares after the open - Pepperstone's share
  CFDs at 09:31 New York. A signal from the first FIRST_PRICE_WAIT (90)
  seconds after the open whose share has had no price since waits until
  then for its first one, and its 20s count from that price.
- Stop-loss: back where the surge started - the surge's size, as a % of
  the follower's own entry price. Take-profit: REWARD_RISK (2) x that.
  Checked by the bot every few seconds, as well as sitting at the broker.
- Time stop: MAX_HOLD_MINUTES (15). Anything still open 15 minutes before
  the US close is closed, so nothing is held overnight.
- At most MAX_TRADES_PER_DAY (1) per share, plus the runner's usual rules:
  RISK_PERCENT (0.5) of the budget at risk per trade, the leverage cap,
  <BROKER>_SURGE_MAX_POSITIONS (3), spread at most MAX_SPREAD_PERCENT (25%)
  of the stop distance, and only the bot's own trades.

backtest/movers_backtest.py tested a close cousin of this over six months
(chasing the whole market's fastest 5-60 minute movers): nothing before
costs, a loss after the spread. These bots try it live, second by second,
at the open - on demo accounts.
"""

import json
import os
import time
from collections import deque
from dataclasses import dataclass

from . import clock, runner
from .brokers.base import BrokerError
from .strategies import Signal, Strategy

PRE_OPEN_SECONDS = 60            # the scanner starts reading this long before the open, for a first price
SIGNAL_CHECK_SECONDS = 1         # how often a follower reads the signals file (on this PC - costs nothing)
PENDING_RETRY_SECONDS = 2        # a signal whose share has no price yet, or too wide a spread, is looked at again this often
HOLD_CHECK_SECONDS = 5           # while holding, the follower checks its stop / target / time stop this often
FORGET_AFTER_SECONDS = 3600      # a share not held or signalled for this long drops off the follower's list
FLAT_BEFORE_CLOSE_MINUTES = 15
SCANNER_SILENT_SECONDS = 180     # a scanner heartbeat older than this at the open gets a warning
US = clock.INDEX_SESSIONS["us"]


# ---------------------------------------------------------------------------
# THE SIGNALS FILE - the scanner appends one JSON line per surge; each
# follower reads what's new every second. One file per New York date.
# ---------------------------------------------------------------------------
def new_york_day(ts: float) -> str:
    return clock.local_date("new_york", ts).isoformat()


def signals_path(day: str) -> str:
    return os.path.join(runner.STATE_DIR, f"surge-signals-{day}.jsonl")


def heartbeat_path() -> str:
    return os.path.join(runner.STATE_DIR, "surge-scanner.json")


def write_signal(signal: dict) -> None:
    os.makedirs(runner.STATE_DIR, exist_ok=True)
    with open(signals_path(new_york_day(signal["time"])), "a", encoding="utf-8") as f:
        f.write(json.dumps(signal) + "\n")  # one write per line, so a reader never sees half of one for long


class SignalReader:
    """A follower's view of the signals file: each read() returns the
    signals written since the last one, from today's file (New York date)."""

    def __init__(self, log):
        self.log = log
        self.day, self.offset = None, 0

    def read(self, now: float) -> list:
        day = new_york_day(now)
        if day != self.day:
            self.day, self.offset = day, 0
        try:
            with open(signals_path(day), "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except FileNotFoundError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []  # nothing new, or a line still being written
        self.offset += end + 1
        signals = []
        for line in data[:end].splitlines():
            try:
                signal = json.loads(line)
                valid = (signal["direction"] in ("long", "short") and signal["ticker"]
                         and float(signal["move_percent"]) > 0 and float(signal["time"]) > 0)
            except (ValueError, KeyError, TypeError):
                valid = False
            if valid:
                signals.append(signal)
            else:
                self.log.warning(f"Ignoring a line of the signals file that isn't a surge signal: {line[:120]!r}")
        return signals


# ---------------------------------------------------------------------------
# THE SCANNER'S RULE
# ---------------------------------------------------------------------------
@dataclass
class Surge:
    ticker: str
    direction: str               # "long" after a rise, "short" after a fall
    start_price: float           # the trade before the surge
    price: float                 # the latest trade
    steps: list                  # each poll's move, in %
    started: float               # poll times of the first and last read
    at: float

    @property
    def move_percent(self) -> float:
        return abs(self.price / self.start_price - 1) * 100


class Detector:
    """Spots a surge in a stream of polls of every share's latest trade: a
    new trade on each of the last `confirm_polls` polls, each at least
    `jump_percent` beyond the one before, all the same way."""

    def __init__(self, confirm_polls: int, jump_percent: float, poll_seconds: float):
        self.confirm, self.jump = confirm_polls, jump_percent
        self.gap = 2.5 * poll_seconds    # polls further apart than this start a share's reads again
        self.reads = {}                  # ticker -> deque of (poll time, trade time, price)

    def add(self, ticker: str, now: float, trade_time: float, price: float, not_before: float = 0.0):
        """One poll's latest trade in `ticker`. Returns a Surge if this poll
        completes one - its trades all at or after `not_before` (the open)."""
        reads = self.reads.get(ticker)
        if reads is None:
            reads = self.reads[ticker] = deque(maxlen=self.confirm + 1)
        if reads and now - reads[-1][0] > self.gap:
            reads.clear()
        reads.append((now, trade_time, price))
        if len(reads) <= self.confirm:
            return None
        history = list(reads)
        steps = []
        for (_, before_time, before), (_, trade_at, after) in zip(history, history[1:]):
            if trade_at <= before_time or trade_at < not_before or before <= 0:
                return None  # no new trade on that poll, or it was before the open
            steps.append((after / before - 1) * 100)
        if all(step >= self.jump for step in steps):
            direction = "long"
        elif all(step <= -self.jump for step in steps):
            direction = "short"
        else:
            return None
        reads.clear()  # the next surge in this share needs polls of its own
        return Surge(ticker, direction, history[0][2], price, steps, history[0][0], now)


# ---------------------------------------------------------------------------
# THE FOLLOWERS' STRATEGY
# ---------------------------------------------------------------------------
class SurgeFollower(Strategy):
    key = "surge-follower"
    uses_signals = True
    checks_own_levels = True     # the surge's size can be inside a broker's minimum stop distance

    def __init__(self, params: dict):
        # No bars and no markets of its own, so none of the base class's set-up.
        self.p = params
        self.timeframe = "signals"
        self.bar_seconds = None

    def feeds(self) -> dict:
        return {}

    def signal_for(self, surge: dict, quote) -> Signal:
        """The trade for one of the scanner's signals, at this broker's price:
        the stop back where the surge started, as a share of the price - the
        broker's price for a share needn't match the scanner's to the cent."""
        direction, move = surge["direction"], float(surge["move_percent"])
        sign = 1 if direction == "long" else -1
        entry = quote.ask if sign > 0 else quote.bid
        seconds = surge.get("seconds")
        span = f" in {surge.get('polls', '?')} polls ({seconds:.0f}s)" if isinstance(seconds, (int, float)) else ""
        return Signal(direction, stop=entry * (1 - sign * move / 100), reward_risk=self.p["reward_risk"], score=move,
                      why=f"{direction} after a {sign * move:+.2f}% surge{span} at the US open")

    def exit_on_time(self, market, position, state: dict, now: float):
        opened = state.get("opened_at") or position.opened_at or now
        if now - opened >= self.p["max_hold_minutes"] * 60:
            return f"time stop - held {(now - opened) / 60:.0f} min"
        bounds = US.bounds_at(opened)
        if bounds is None or now >= bounds[1] - FLAT_BEFORE_CLOSE_MINUTES * 60:
            return f"{FLAT_BEFORE_CLOSE_MINUTES} min before the US close - never held overnight"
        return None

    def on_open(self, market, signal: Signal, entry: float, state: dict) -> None:
        state["requested"] = market.requested  # to look the share up again after a restart

    def trade_day(self, market, now: float) -> str:
        return new_york_day(now)

    def summary(self) -> str:
        p = self.p
        return (f"Follows the surge scanner | stop where the surge started | take-profit {p['reward_risk']:g}R | "
                f"out after {p['max_hold_minutes']} min | signals older than {p['max_signal_age']}s dropped "
                f"(a share with no price yet at the open is waited for up to {p['first_price_wait']}s after it) | "
                f"{p['max_trades_per_day']} trade(s)/day per share")


@dataclass
class Waiting:
    """A signal the follower hasn't acted on yet: its share has no price
    yet, or too wide a spread for now."""
    signal: dict
    market: object
    since: float                 # its MAX_SIGNAL_AGE counts from here: when it was sent, or its share's first price
    opening: bool = False        # waited for the broker's first price after the open
    priced: bool = False         # has had a price
    noted: str = ""              # what was last logged about it, so a retry every 2s doesn't log it again


# ---------------------------------------------------------------------------
# THE FOLLOWER BOT
# ---------------------------------------------------------------------------
class FollowerBot(runner.StrategyBot):
    """A strategy bot with no market list: it trades the scanner's signals,
    looking each share up on its broker when its first signal arrives, and
    otherwise runs the runner's own entry, sizing and exit code."""

    def __init__(self, settings, strategy, broker, dashboard, log):
        super().__init__(settings, strategy, broker, dashboard, log)
        self.loop_seconds = SIGNAL_CHECK_SECONDS
        self.hold_check_seconds = max(HOLD_CHECK_SECONDS, broker.min_poll_seconds)
        self.reader = SignalReader(log)
        self.pending = []            # [Waiting]: signals not acted on yet
        self.next_retry = 0.0
        self.lookups = {}            # ticker -> Market, or None if the broker doesn't offer it
        self.lookup_day = None
        self.used_at = {}            # symbol -> when a signal last named it
        self.next_pass = 0.0
        self.scanner_checked = None  # the New York date the scanner's heartbeat was last checked

    # -- startup --------------------------------------------------------------------
    def start(self) -> None:
        s, p, log = self.settings, self.p, self.log
        account = self.broker.connect()
        self.currency = account.currency
        if self.watch is not None:
            self.watch.currency = self.currency
        # Positions it had open before a restart: its notes name each share.
        held = sorted({notes.get("requested") or symbol for symbol, notes in self.state.positions.items()})
        if held:
            self.markets = self.broker.resolve(held)

        log.info("=" * 78)
        log.info(f"{s.name} starting - DEMO / PRACTICE ACCOUNT ONLY" + (" - DRY RUN, NO ORDERS" if s.dry_run else ""))
        log.info(account.description)
        for warning in account.warnings:
            log.warning(warning)
        log.info(f"Budget {s.budget:,.2f} {self.currency} | risk {p['risk_percent']:g}% "
                 f"({s.budget * p['risk_percent'] / 100:,.2f}) per trade | max leverage {self.leverage:g}x | "
                 f"max {s.max_positions} positions ({self.slice_cap():,.2f} each) | spread max "
                 f"{p['max_spread_percent']:g}% of the stop")
        log.info(self.strategy.summary())
        if self.watch is not None:
            log.info(self.watch.book.describe(self.currency))
        log.info(f"Trades the US shares the surge scanner signals, as {self.broker.name} names them "
                 f"({s.symbol_format.format('AAPL')} for AAPL)" + ("" if self.broker.can_short else " - buys only"))
        if self.markets:
            log.info(f"Managing the positions it had open: {', '.join(self.markets)}")
        log.info("=" * 78)

        config = {"signalsFrom": "surge-scanner", "symbolFormat": s.symbol_format, "maxPositions": s.max_positions,
                  "budget": s.budget, "riskPercent": p["risk_percent"], "maxLeverage": self.leverage,
                  "spreadMaxPercentOfStop": p["max_spread_percent"], "dryRun": s.dry_run}
        config.update({k: v for k, v in p.items() if k not in ("risk_percent", "max_leverage", "max_spread_percent")})
        if getattr(self.broker, "magic", None):
            config["magicNumber"] = self.broker.magic
        if self.watch is not None:
            config.update(self.watch.book.config())
        self.dashboard.describe(account=account.id, currency=self.currency, config=config,
                                account_mode=self.broker.account_mode)
        self.dashboard.broadcast_off("A surge follower trades only the surge scanner's signals - no broadcast trades.")

        self.started_at = time.time()
        self.dashboard.accept_closes(self.close_from_dashboard)
        opens = US.bounds(clock.local_date("new_york", self.started_at))
        uk = f" ({clock.local('london', opens[0]).strftime('%H:%M')} UK)" if opens else ""
        log.info(f"Waiting for the surge scanner's signals - it watches the first minutes after the 09:30 New York "
                 f"open{uk}. Start the scanner too, or nothing will come.")

    # -- the loop -------------------------------------------------------------------
    def cycle(self) -> None:
        now = time.time()
        if self.dashboard.due(self.broker.dashboard_every):
            self.report()
        new = self.reader.read(now)
        if new or (self.pending and now >= self.next_retry):
            self.on_signals(new, now)
        if now >= self.next_pass:
            own, others = self.split_positions(self.markets)
            self.reconcile(own, others, now)
            self.time_exits(own, now)
            self.forget_idle(own, now)
            holding = own or self.state.positions
            self.next_pass = now + (self.hold_check_seconds if holding else runner.LOOP_SECONDS)
        self.check_scanner(now)

    def market_for(self, ticker: str):
        """The broker's market for a US ticker, or None - looked up once a day."""
        day = new_york_day(time.time())
        if day != self.lookup_day:  # try the ones it didn't have again each day
            self.lookup_day = day
            self.lookups = {t: m for t, m in self.lookups.items() if m is not None}
        if ticker not in self.lookups:
            name = self.settings.symbol_format.format(ticker)
            try:
                found = self.broker.resolve([name])
            except BrokerError as e:
                self.log.warning(f"{ticker}: couldn't look up {name} at {self.broker.name} ({e}) - signal skipped.")
                return None
            self.lookups[ticker] = next(iter(found.values()), None)
            if self.lookups[ticker] is None:
                self.log.info(f"{ticker}: {self.broker.name} doesn't offer it as {name} - its signals are skipped today.")
        return self.lookups[ticker]

    def on_signals(self, new: list, now: float) -> None:
        p, log = self.p, self.log
        max_age = p["max_signal_age"]
        for signal in new:
            ticker, direction, sent = signal["ticker"], signal["direction"], float(signal["time"])
            if now - sent > max_age:
                if sent >= self.started_at:  # sent while this bot ran, but read too late to act on
                    log.info(f"{ticker}: {direction} surge signal skipped - {now - sent:.0f}s old when read "
                             f"(max {max_age}s).")
                continue  # else from before this bot started - long gone
            if direction == "short" and not self.broker.can_short:
                log.info(f"{ticker}: short surge signal skipped - {self.broker.name} can't sell short here.")
                continue
            market = self.market_for(ticker)
            if market is None:
                continue
            self.markets[market.symbol] = market
            self.used_at[market.symbol] = now
            log.info(f"{ticker}: {direction} surge signal ({float(signal['move_percent']):.2f}%, "
                     f"{now - sent:.1f}s ago) -> {market.symbol}")
            self.pending.append(Waiting(signal, market, since=sent))
        if not self.pending:
            return

        markets = {w.market.symbol: w.market for w in self.pending}
        quotes = self.broker.quotes(list(markets.values()))
        own, others = self.split_positions(self.markets)
        signals, waiting = [], []
        for w in self.pending:
            symbol, direction = w.market.symbol, w.signal["direction"]
            quote = quotes.get(symbol)
            if quote is None or not quote.tradeable:
                why = (quote.why_not if quote is not None else "") or "no price"
                until = self.first_price_until(w)
                if now < until:
                    w.opening = True
                    self.note(w, "first price", f"{symbol}: no price yet ({why}) - {self.broker.name} may start "
                              f"quoting it after the open, so the {direction} surge signal waits for its first "
                              f"price until {clock.local('london', until).strftime('%H:%M:%S')} UK.")
                    waiting.append(w)
                elif now - w.since <= max_age:
                    waiting.append(w)  # e.g. just added to MT5's Market Watch - no tick yet
                elif w.opening and not w.priced:
                    log.info(f"{symbol}: {direction} surge signal dropped - still no price {p['first_price_wait']}s "
                             f"after the open ({why}).")
                else:
                    log.info(f"{symbol}: {direction} surge signal dropped - {why} within {max_age}s.")
                continue
            if not w.priced:
                w.priced = True
                if w.opening:  # its MAX_SIGNAL_AGE starts from the share's first price
                    w.since = now
                    log.info(f"{symbol}: first price {quote.bid:g} / {quote.ask:g}, "
                             f"{now - float(w.signal['time']):.0f}s after its {direction} surge signal.")
            if symbol in others:
                log.info(f"{symbol}: {direction} surge signal skipped - someone else's position is open in it.")
                continue
            if symbol in own:
                log.info(f"{symbol}: {direction} surge signal skipped - already holding it.")
                continue
            if any(symbol == taken for _, taken, _ in signals):
                continue
            signal = self.strategy.signal_for(w.signal, quote)
            entry = quote.ask if direction == "long" else quote.bid
            too_wide = self.spread_problem(quote, abs(entry - signal.stop))
            if too_wide:
                if now - w.since < max_age:  # spreads jump about at the open - it may narrow in a few seconds
                    self.note(w, "spread", f"{symbol}: {direction} surge signal waiting - {too_wide}; trying "
                              f"again every {PENDING_RETRY_SECONDS}s for up to {max_age - (now - w.since):.0f}s.")
                    waiting.append(w)
                else:
                    log.info(f"{symbol}: {direction} surge signal skipped - {too_wide}, still {max_age}s on.")
                continue
            signals.append((signal.score, symbol, signal))
        self.pending = waiting
        self.next_retry = now + PENDING_RETRY_SECONDS
        if signals:
            self.enter(signals, own, now, quotes)
            self.next_pass = min(self.next_pass, now + self.hold_check_seconds)

    def first_price_until(self, w: Waiting) -> float:
        """Until when a signal that came just after the open may wait for its
        share's first price - Pepperstone's US share CFDs quote from 09:31
        New York - or 0 if it may not (it's had a price, or came later)."""
        sent, wait = float(w.signal["time"]), self.p["first_price_wait"]
        bounds = US.bounds_at(sent)
        if w.priced or not wait or bounds is None or not bounds[0] <= sent < bounds[0] + wait:
            return 0.0
        return bounds[0] + wait

    def note(self, w: Waiting, what: str, text: str) -> None:
        """Logs `text` the first time a waiting signal waits for `what`."""
        if w.noted != what:
            w.noted = what
            self.log.info(text)

    def forget_idle(self, own: dict, now: float) -> None:
        """Drop shares it no longer holds and hasn't had a signal for in a
        while, so the list it asks the broker about doesn't grow for ever."""
        for symbol in list(self.markets):
            if (symbol not in own and symbol not in self.state.positions
                    and now - self.used_at.get(symbol, 0) > FORGET_AFTER_SECONDS):
                del self.markets[symbol]

    def check_scanner(self, now: float) -> None:
        """Once a trading day, just after the open, warn if the scanner isn't
        running - the follower trades nothing without it."""
        bounds = US.bounds_at(now)
        day = new_york_day(now)
        if bounds is None or self.scanner_checked == day or not (bounds[0] + 60 <= now < bounds[0] + 600):
            return
        self.scanner_checked = day
        try:
            with open(heartbeat_path(), encoding="utf-8") as f:
                silent = now - float(json.load(f)["at"])
        except (OSError, ValueError, KeyError, TypeError):
            silent = None
        if silent is None or silent > SCANNER_SILENT_SECONDS:
            heard = "never" if silent is None else f"{silent / 60:.0f} min ago"
            self.log.warning(f"The surge scanner doesn't seem to be running (last heard from {heard}) - this bot only "
                             f"trades its signals. Start it from the launcher (Alpaca: Surge scanner).")
