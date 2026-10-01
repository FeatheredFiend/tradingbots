"""
The loop every strategy bot runs: fetch each market's bars as they close,
ask the strategy what to do, check the CFD risk rules, size the trade and
send it through the broker's adapter.

Risk rules the runner applies to every strategy
-----------------------------------------------
- Sizing: each bot trades a budget (<BROKER>_<STRATEGY>_BUDGET) as if it
  were its whole account. A trade risks at most RISK_PERCENT of the budget
  between the fill and the stop-loss:
      size = budget x RISK_PERCENT / (stop distance x value of one unit per price point)
  and is worth at most budget x MAX_LEVERAGE / MAX_POSITIONS, then rounded
  DOWN to a size the broker accepts. A market whose smallest trade is over
  that limit is skipped at startup. IG trades its minimum size instead.
- Spread: skipped when the spread is more than MAX_SPREAD_PERCENT of the
  stop distance - a wide spread eats the trade's edge before it starts.
- Swap: strategies that hold overnight skip entries whose financing for
  that direction costs more than MAX_SWAP_PERCENT of the trade per night.
- Rollover: no new trades from 15 minutes before to 45 minutes after the
  17:00 New York rollover, when spreads widen.
- Slots: at most MAX_POSITIONS open, and MAX_TRADES_PER_DAY per market;
  when several markets signal at once, the strongest signal goes first.
- Brokers that can't hold a stop-loss (Alpaca) have it checked by the bot
  every loop instead.

Only bars that close after the bot starts are traded on, so after a start
nothing opens until the next bar closes.

A bot only ever manages the trades it opened itself - known by the
broker's trade/deal ids it saves, or on MetaTrader 5 by its magic number.
Anyone else's position in one of its markets (another bot's, a manual
trade) is left alone, and the bot doesn't trade that market while it's
open, since most accounts net a market's buys and sells together. The
ids, notes on each position (entry, stop, best price so far) and the
day's trade counts are saved to strategy-bots/state/<bot>.json, so a
restart carries on where it stopped.

With DASHBOARD_COMMANDS=1, the dashboard can close (part of) the bot's own
positions too - see close_from_dashboard().

With DASHBOARD_BROADCAST=1, it takes broadcast trades from the dashboard
(shared/broadcast.py): the admin's instrument and side, the strategy's own
stop-loss and take-profit for it (Strategy.manual_signal()), and every one
of the rules above - entry() is the one place they're applied, to the
strategy's signals and broadcasts alike - except the strategy's signal
filters. A broadcast in a market outside the bot's list makes it a "guest":
watched for that trade's exits (bars, stop-loss / take-profit, time stops,
the stagnancy timeout), never traded by the strategy, and dropped once the
trade has closed. Its notes say so, so a restart looks it up again.

The stagnancy timeout (shared/stagnancy.py) looks at every position on
every pass, after the strategy's own exits: one that has gone nowhere for a
while is logged (shadow mode, the default) or closed with the reason
TIMEOUT_STAGNANT (enforce), freeing its slot. Its notes are saved with the
rest, so a restart doesn't close anything twice. A position's slot is only
ever freed in release() - once.

The tick scalper (engine/scalper.py) has no bars: every SCALPER_POLL_SECONDS
the runner reads every market's price, hands it to the strategy, checks the
scalper's own stop-loss / take-profit (the broker's may sit further out, at
its minimum distance) and looks for entries - read_prices() and on_prices().
The same risk rules apply.

The opening surge followers (engine/surge.py: FollowerBot, built on this
one) have no market list: they trade the US shares the surge scanner
signals, looked up on their broker as the signals arrive, through the same
entry, sizing and exit code as here.

The portfolio bots - slow trend on OANDA, monthly ETF rotation on Alpaca
(engine/rebalancer.py: RebalanceBot, built on this one) - hold all their
markets at a target size and rebalance on a schedule instead; they keep
this one's saved notes, own-trades-only rule and dashboard handling.
"""

import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass

from . import clock
from .brokers.base import BrokerError, Position, Quote
from .indicators import atr, last
from .settings import BROKER_NAMES, REBALANCERS, STRATEGY_NAMES, TIMEFRAMES, SettingsError, bot_settings
from .strategies import ATR_PERIOD, Skip, make_strategy

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "shared"))
from dashboard_reporter import CommandError, DashboardReporter  # noqa: E402 - needs the path above
import broadcast  # noqa: E402 - needs the path above
import stagnancy  # noqa: E402 - needs the path above
import symbols  # noqa: E402 - needs the path above

STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
LOOP_SECONDS = 30
BAR_SETTLE_SECONDS = 10          # wait this long after a bar's close before fetching it
MAX_RETRY_SECONDS = 900          # a bar that's late (market shut) is looked for at least this often
CLOSE_RETRY_SECONDS = 300        # a failed close is tried again this often
FILL_GRACE_SECONDS = 180         # a position just opened may take this long to show up
ERROR_BACKOFF_SECONDS = 60
MAX_CONSECUTIVE_ERRORS = 10
ROLLOVER_QUIET = (15, 45)        # minutes before / after the rollover without new trades
PRICE_CLOSE_RETRY_SECONDS = 20   # the scalper and surge follower hold for minutes, so a failed close is retried sooner
STATUS_EVERY_SECONDS = 300       # the scalper logs each market's state this often (it reads every few seconds)
# After a trade opens or closes, report to the dashboard this many seconds
# later, not at the next 15-second snapshot: soon, then again once the
# broker's history lists the close with its profit.
TRADE_REPORT_DELAYS = (1, 8)
ROLLOVER_SKIP = "too close to the daily rollover (spreads widen)"
PREVIEWED_SECONDS = 300          # a guest market looked up for a broadcast's preview is kept this long for its open
GUEST_REPORT_SECONDS = 3600      # a guest market's closed trade is still reported this long after it closes


@dataclass
class Entry:
    """A trade that passed every entry rule, sized - see StrategyBot.entry()."""
    direction: str
    price: float                 # the price it would fill at now: the ask to buy, the bid to sell
    stop: float
    take_profit: float           # None: no take-profit
    distance: float              # from the price to the stop
    size: float
    sizing: str                  # how the size was worked out, for the log
    capped: str = ""             # a broadcast's quantity, cut to the bot's limits - why, else ""
    exposure: float = None       # what it's worth, in the account's currency
    risk: float = None           # what it loses at its stop, in the account's currency


def make_broker(key: str, settings, dashboard, log):
    try:
        if key == "oanda":
            from .brokers.oanda import OandaBroker as cls
        elif key == "capital":
            from .brokers.capital import CapitalBroker as cls
        elif key == "pepperstone":
            from .brokers.pepperstone import PepperstoneBroker as cls
        elif key == "ig":
            from .brokers.ig import IGBroker as cls
        elif key == "alpaca":
            from .brokers.alpaca import AlpacaBroker as cls
        else:
            raise SettingsError(f"unknown broker {key!r}")
    except ImportError as e:
        environment = {"oanda": "ig-bot-env", "capital": "ig-bot-env", "ig": "ig-bot-env",
                       "pepperstone": "pepperstone-bot-env", "alpaca": "alpaca-bot-env"}[key]
        raise SettingsError(f"{e.name or e} isn't installed in this Python ({sys.executable}) - "
                            f"{BROKER_NAMES[key]} bots run in {environment}") from None
    return cls(settings, dashboard, log)


# ---------------------------------------------------------------------------
# BARS
# ---------------------------------------------------------------------------
class Feed:
    """One market's closed bars on one timeframe, kept up to date with as
    few requests as possible: the full history once, then only the newest
    few bars, and only once the next bar should have closed."""

    def __init__(self, broker, market, timeframe: str, keep: int):
        self.broker, self.market, self.timeframe, self.keep = broker, market, timeframe, keep
        self.seconds = TIMEFRAMES[timeframe]
        self.bars = []
        self.next_check = 0.0
        self.retry = LOOP_SECONDS

    def refresh(self, now: float) -> None:
        if now < self.next_check:
            return
        latest = self.bars[-1].time if self.bars else None
        if latest is None:
            count = self.keep
        else:
            # The bars that should have closed since the latest one held, plus a
            # couple of overlap - none on IG, whose history is metered per bar.
            overlap = 0 if self.broker.metered_history else 2
            count = min(self.keep, max(1, math.floor((now - latest) / self.seconds) - 1) + overlap)
        fetched = self.broker.bars(self.market, self.timeframe, count)
        merged = {b.time: b for b in self.bars}
        merged.update({b.time: b for b in fetched})
        self.bars = [merged[t] for t in sorted(merged)][-self.keep:]
        if self.bars and (latest is None or self.bars[-1].time > latest):
            self.retry = LOOP_SECONDS
            self.next_check = self.bars[-1].time + 2 * self.seconds + BAR_SETTLE_SECONDS
        else:  # not there yet - e.g. the market is shut; look again, less and less often
            self.next_check = now + self.retry
            self.retry = min(self.retry * 2, MAX_RETRY_SECONDS)


# ---------------------------------------------------------------------------
# SAVED NOTES
# ---------------------------------------------------------------------------
class State:
    def __init__(self, slug: str, log):
        self.path = os.path.join(STATE_DIR, f"{slug}.json")
        self.log = log
        self.positions = {}   # symbol -> notes on the bot's open position
        self.trades = {}      # symbol -> {"day": ..., "count": ...}
        self.own_ids = []     # the broker's ids for every trade this bot opened
        self.stagnancy = {}   # the stagnancy timeout's notes (shared/stagnancy.py Watch)
        self.broadcasts = {}  # the broadcasts it acted on and the trades they opened (shared/broadcast.py Book)
        try:
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
            self.positions, self.trades = saved.get("positions", {}), saved.get("trades", {})
            self.own_ids = saved.get("own_ids", [])
            self.stagnancy = saved.get("stagnancy") or {}
            self.broadcasts = saved.get("broadcasts") or {}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning(f"Couldn't read {self.path} ({e}); starting without saved notes.")

    def save(self) -> None:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"positions": self.positions, "trades": self.trades, "own_ids": self.own_ids,
                           "stagnancy": self.stagnancy, "broadcasts": self.broadcasts}, f, indent=1)
            os.replace(self.path + ".tmp", self.path)
        except OSError as e:
            self.log.warning(f"Couldn't save {self.path}: {e}")

    def trades_on(self, symbol: str, day: str) -> int:
        entry = self.trades.get(symbol) or {}
        return entry.get("count", 0) if entry.get("day") == day else 0

    def count_trade(self, symbol: str, day: str) -> None:
        self.trades[symbol] = {"day": day, "count": self.trades_on(symbol, day) + 1}


# ---------------------------------------------------------------------------
# THE BOT
# ---------------------------------------------------------------------------
class StrategyBot:
    def __init__(self, settings, strategy, broker, dashboard, log):
        self.settings, self.strategy, self.broker, self.dashboard, self.log = settings, strategy, broker, dashboard, log
        self.p = settings.params
        self.state = State(settings.slug, log)
        self.markets = {}
        self.feeds = {}
        self.seen = {}
        self.started_at = 0.0
        self.currency = ""
        self.feed_problems = {}
        self.close_retry_at = {}
        self.others = set()      # markets where someone else's position is open, as last logged
        leverage_cap = broker.max_leverage if broker.max_leverage is not None else float("inf")
        self.leverage = min(self.p["max_leverage"], leverage_cap)
        broker.own_ids = set(self.state.own_ids)
        # The scalper reads prices every few seconds (not faster than the broker
        # allows); the bar strategies look every LOOP_SECONDS.
        self.loop_seconds = (max(self.p["poll_seconds"], broker.min_poll_seconds) if strategy.uses_prices
                             else LOOP_SECONDS)
        self.quiet_until = {}    # symbol -> no new price signals there before this time (the scalper's cooldown)
        self.status = {}         # symbol -> the latest read's state, logged every STATUS_EVERY_SECONDS
        self.next_status = 0.0
        # Broadcast trades from the dashboard (shared/broadcast.py).
        self.broadcasts = broadcast.Book(self.state.broadcasts, save=self.state.save)
        self.guests = set()      # markets held only for a broadcast trade: watched for its exits, never traded
        self.previewed = {}      # code -> (Market, {role: Feed}, when) - a guest looked up for a broadcast's preview
        self.left_guests = {}    # symbol -> (Market, until) - a guest whose trade closed, still reported a while
        # The stagnancy timeout - none for the portfolio bots. The bar strategies
        # hand it their own bars (a "4bars" window, ATR); the scalper and the
        # surge followers their price reads.
        try:
            book = stagnancy.RuleBook.for_bot(settings.strategy, settings.broker,
                                              bar_seconds=getattr(strategy, "bar_seconds", None),
                                              has_bars=not (strategy.uses_prices or strategy.uses_signals))
        except stagnancy.ConfigError as e:
            raise SettingsError(str(e)) from None
        self.watch = None
        if book is not None:
            self.watch = stagnancy.Watch(book, settings.slug, broker.name, log, gap=max(3 * self.loop_seconds, 30),
                                         dry_run=settings.dry_run, store=self.state.stagnancy)
            broker.stagnancy = self.watch

    # -- startup --------------------------------------------------------------------
    def start(self) -> None:
        s, log = self.settings, self.log
        account = self.broker.connect()
        self.currency = account.currency
        if self.watch is not None:
            self.watch.currency = self.currency

        names, hints = [], {}
        for entry in s.markets:
            name, _, hint = entry.partition("@")
            names.append(name.strip())
            if hint:
                hints[name.strip()] = hint.strip().lower()
        markets = self.broker.resolve(names)
        for market in markets.values():
            market.session_hint = hints.get(market.requested)
        markets = self.strategy.prepare(markets, log)
        self.markets = self.check_sizes(markets)
        if not self.markets:
            raise BrokerError(f"none of the markets ({', '.join(s.markets)}) can be traded - see above")
        self.restore_guests()

        log.info("=" * 78)
        log.info(f"{s.name} starting - DEMO / PRACTICE ACCOUNT ONLY" + (" - DRY RUN, NO ORDERS" if s.dry_run else ""))
        log.info(account.description)
        for warning in account.warnings:
            log.warning(warning)
        if self.broker.fixed_min_size:
            log.info(f"Size: each market's minimum deal | max {s.max_positions} positions | "
                     f"spread max {self.p['max_spread_percent']:g}% of the stop")
        else:
            log.info(f"Budget {s.budget:,.2f} {self.currency} | risk {self.p['risk_percent']:g}% "
                     f"({s.budget * self.p['risk_percent'] / 100:,.2f}) per trade | max leverage {self.leverage:g}x | "
                     f"max {s.max_positions} positions ({self.slice_cap():,.2f} each) | spread max "
                     f"{self.p['max_spread_percent']:g}% of the stop")
        log.info(self.strategy.summary())
        if self.watch is not None:
            log.info(self.watch.book.describe(self.currency))
        log.info(f"Markets: {', '.join(self.markets)}")
        for symbol, market in self.markets.items():
            swaps = [self.broker.swap_percent_per_night(market, d) for d in ("long", "short")]
            if None not in swaps and any(swaps):
                log.info(f"{symbol} swap per night: long {swaps[0]:+.4f}%, short {swaps[1]:+.4f}% of the trade's value"
                         + ("" if self.strategy.holds_overnight else " (never paid: flat before the rollover)"))
        log.info("=" * 78)

        # One row per setting on the dashboard's bot page.
        config = {"markets": list(self.markets), "maxPositions": s.max_positions,
                  "spreadMaxPercentOfStop": self.p["max_spread_percent"], "dryRun": s.dry_run}
        if not self.broker.fixed_min_size:
            config.update(budget=s.budget, riskPercent=self.p["risk_percent"], maxLeverage=self.leverage)
        config.update({k: v for k, v in self.p.items() if k not in ("risk_percent", "max_leverage", "max_spread_percent")})
        if getattr(self.broker, "magic", None):
            config["magicNumber"] = self.broker.magic
        if self.watch is not None:
            config.update(self.watch.book.config())
        self.dashboard.describe(account=account.id, currency=self.currency, config=config,
                                account_mode=self.broker.account_mode)

        if not self.strategy.uses_prices:
            for symbol, market in self.markets.items():
                self.feeds[symbol] = {role: Feed(self.broker, market, tf, keep)
                                      for role, (tf, keep) in self.strategy.feeds().items()}
        self.started_at = time.time()
        self.refresh_feeds(self.started_at)
        self.seen = {s: (f["exec"].bars[-1].time if f["exec"].bars else 0.0) for s, f in self.feeds.items()}
        self.dashboard.accept_closes(self.close_from_dashboard)
        self.dashboard.tag_rows(self.broadcasts.tags)
        if self.strategy.takes_broadcasts:
            self.dashboard.accept_broadcasts(self.broadcast_preview, self.broadcast_open, self.broadcast_symbols())
        else:
            self.dashboard.broadcast_off(f"A {STRATEGY_NAMES[s.strategy].lower()} bot doesn't take broadcast trades.")
        if self.strategy.uses_prices:
            log.info(f"Reading prices every {self.loop_seconds:g}s; trading starts once each market has "
                     f"{self.p['window_seconds']}s of them.")
        else:
            log.info(f"Waiting for the next {self.strategy.timeframe} bar to close before trading.")

    def slice_cap(self) -> float:
        return self.settings.budget * self.leverage / self.settings.max_positions

    def check_sizes(self, markets: dict) -> dict:
        """Drop markets whose smallest trade is bigger than one trade's limit."""
        if self.broker.fixed_min_size or not markets:
            return markets
        quotes = self.broker.quotes(list(markets.values()))
        usable = {}
        for symbol, market in markets.items():
            quote = quotes.get(symbol)
            if quote is None or quote.unit_value is None:
                if quote is not None:
                    self.log.warning(f"Skipping {symbol}: can't value a trade in it in {self.currency}.")
                    continue
                self.log.info(f"{symbol}: no price right now, so its smallest trade is checked when it signals.")
            elif market.min_size * quote.unit_value > self.slice_cap():
                self.log.warning(
                    f"Skipping {symbol}: its smallest trade ({market.min_size:g}) is worth about "
                    f"{market.min_size * quote.unit_value:,.0f} {self.currency}, more than this bot's "
                    f"{self.slice_cap():,.2f} limit per trade (budget x max leverage / max positions) - "
                    f"raise {self.settings.env_prefix}BUDGET to include it.")
                continue
            usable[symbol] = market
        return usable

    def restore_guests(self) -> None:
        """After a restart: the markets outside the bot's list it holds a
        broadcast trade in, looked up again so the trade is still managed."""
        wanted = {notes["guest"]: symbol for symbol, notes in self.state.positions.items()
                  if notes.get("guest") and symbol not in self.markets}
        if not wanted:
            return
        try:
            found = self.strategy.prepare(self.broker.resolve(list(wanted)), self.log)
        except BrokerError as e:
            self.log.warning(f"Couldn't look up the markets of its broadcast trades ({e}).")
            found = {}
        for symbol, market in found.items():
            self.markets[symbol] = market
            self.guests.add(symbol)
            self.log.info(f"{symbol}: managing the broadcast trade the bot holds in it (not one of its own markets).")
        for symbol in set(wanted.values()) - set(found):
            self.log.warning(f"{symbol}: couldn't look its market up again, so its broadcast trade is left as it is at "
                             f"{self.broker.name}, its stop-loss and take-profit there. It's tried again at the next "
                             f"restart.")

    def broadcast_symbols(self) -> list:
        """The instrument names the dashboard can offer for this bot: the
        symbol map's for its broker, and its own markets'."""
        names = set(symbols.names(self.settings.broker))
        for symbol, market in self.markets.items():
            if symbol not in self.guests:
                names.add(symbols.canonical(market.requested.split(":")[0]) or symbols.canonical(symbol) or symbol)
        return sorted(names)

    # -- the loop -------------------------------------------------------------------
    def run(self) -> None:
        self.start()
        errors = 0
        while True:
            try:
                self.cycle()
                errors = 0
            except Exception as e:  # anything unexpected counts toward the safety cutoff, not a crash
                errors += 1
                self.log.error(f"[{errors}] Error this loop: {e}")
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    self.log.critical(f"{MAX_CONSECUTIVE_ERRORS} errors in a row - exiting for safety. Check the "
                                      f"connection and credentials before restarting.")
                    sys.exit(1)
                backoff = min(ERROR_BACKOFF_SECONDS * errors, 300)
                self.log.info(f"Retrying in {backoff}s...")
                time.sleep(backoff)
                continue
            self.dashboard.sleep(self.loop_seconds, self.report, self.broker.dashboard_every)

    def report(self) -> None:
        """Account, positions and trades for the dashboard - when due, which
        can be in the middle of the wait between passes. A guest market whose
        broadcast trade has just closed is still included for a while, as
        some brokers list a closed trade only by its market."""
        now = time.time()
        self.left_guests = {s: (m, until) for s, (m, until) in self.left_guests.items() if until > now}
        markets = {**{s: m for s, (m, _) in self.left_guests.items()}, **self.markets}
        try:
            self.broker.report(markets, self.state.positions)
        except Exception as e:
            self.log.warning(f"Couldn't report to the dashboard this time: {e}")

    def refresh_feeds(self, now: float) -> None:
        for symbol, feeds in self.feeds.items():
            for feed in feeds.values():
                try:
                    feed.refresh(now)
                    self.feed_problems.pop((symbol, feed.timeframe), None)
                except BrokerError as e:
                    key = (symbol, feed.timeframe)
                    last_text, last_time = self.feed_problems.get(key, (None, 0))
                    if str(e) != last_text or now - last_time > 600:
                        self.log.warning(f"{symbol}: couldn't get {feed.timeframe} bars ({e}); trying again shortly.")
                        self.feed_problems[key] = (str(e), now)
                    feed.next_check = now + LOOP_SECONDS

    def cycle(self) -> None:
        now = time.time()
        if self.dashboard.due(self.broker.dashboard_every):
            self.report()

        self.refresh_feeds(now)
        new_bars = []
        for symbol, feeds in self.feeds.items():
            bars = feeds["exec"].bars
            if bars and bars[-1].time > self.seen.get(symbol, 0):
                self.seen[symbol] = bars[-1].time
                # Never act on a bar that closed before the bot started - e.g. one
                # that only arrived late because the broker was still loading history.
                if bars[-1].time + feeds["exec"].seconds >= self.started_at:
                    new_bars.append(symbol)

        own, others = self.split_positions(self.markets)
        self.reconcile(own, others, now)
        quotes = self.read_prices(now) if self.strategy.uses_prices else None
        self.time_exits(own, now, quotes)
        if new_bars:
            self.on_new_bars(new_bars, own, others, now)
        if quotes is not None:
            self.on_prices(own, others, quotes, now)

    def split_positions(self, markets: dict) -> tuple:
        """({symbol: the bot's own position}, {symbol: anyone else's}) in these markets."""
        own, others = {}, {}
        for symbol, position in self.broker.positions(markets).items():
            notes = self.state.positions.get(symbol)
            mine = position.own if position.own is not None else (
                notes is not None and notes.get("direction") == position.direction)
            (own if mine else others)[symbol] = position
        return own, others

    def close_from_dashboard(self, symbol: str, ref, direction: str, size) -> str:
        """A close asked for on the dashboard: all of the bot's own position
        in `symbol`, or `size` of it. Only ever the bot's own, and only if
        it's still the position the dashboard showed (same side, same ref)."""
        # IG's positions go to the dashboard under the market's name, the rest under the symbol.
        market = self.markets.get(symbol) or next((m for m in self.markets.values() if m.name == symbol), None)
        if market is None:
            raise CommandError(f"{symbol} isn't one of this bot's markets.")
        position = self.split_positions({market.symbol: market})[0].get(market.symbol)
        if position is None:
            raise CommandError(f"The bot has no open {symbol} position now - it may have closed already.")
        if position.direction != direction:
            raise CommandError(f"The bot's {symbol} position is {position.direction} now, not {direction}, so it "
                               f"was left alone.")
        refs = self.broker.refs(position)
        if ref is not None and refs and ref not in refs:
            raise CommandError(f"That {symbol} position has closed; the bot's open one now is a different trade, "
                               f"so it was left alone.")
        if size is not None:
            step = market.size_step or market.min_size or 1.0
            size = round(math.floor(size / step + 1e-9) * step, 10)
            if size <= 0:
                raise CommandError(f"That's less than the smallest step {self.broker.name} takes ({step:g}).")
            if size >= position.size - 1e-9:
                size = None  # all of it
        if self.settings.dry_run:
            raise CommandError("The bot is on a dry run (STRATEGY_DRY_RUN), so it sends no orders - nothing was closed.")

        self.broker.close_problem = ""
        if not self.broker.close(market, position, "closed from the dashboard", size=size):
            raise CommandError(f"{self.broker.name} didn't close {symbol}: "
                               f"{self.broker.close_problem or 'see the bot log for why'}.")
        if size is not None:
            return f"Closed {size:g} of the {symbol} {direction} position ({position.size:g}) at {self.broker.name}."
        self.release(market.symbol, time.time())
        return f"Closed the {symbol} {direction} position ({position.size:g}) at {self.broker.name}."

    def release(self, symbol: str, now: float, fields: dict = None) -> bool:
        """Free a position's slot once its close is confirmed, or it has gone
        from the broker: forget its notes, and pass the stagnancy timeout's
        fields for its trade record to the broker's report. The one place a
        slot is freed; False if it already was, so a close confirmed twice
        frees one slot."""
        notes = self.state.positions.pop(symbol, None)
        if notes is None:
            return False
        if self.watch is not None:
            if fields is None:
                market = self.markets.get(symbol)
                fill = self.broker.close_fill(market) if self.watch.sent(symbol) and market is not None else None
                fields = self.watch.gone(symbol, now, **(fill or {}))
            if fields and symbol in self.markets:
                self.broker.attach_exit(self.markets[symbol], fields)
        self.close_retry_at.pop(symbol, None)
        if notes.get("broadcast") is not None:
            self.broadcasts.closed(symbol, now)
        if symbol in self.guests:
            self.drop_guest(symbol, now)
        self.save_state()
        self.dashboard.report_soon(*TRADE_REPORT_DELAYS)
        return True

    def drop_guest(self, symbol: str, now: float) -> None:
        """A guest market's broadcast trade has closed: stop watching it."""
        self.guests.discard(symbol)
        market = self.markets.pop(symbol, None)
        for held in (self.feeds, self.seen, self.status, self.quiet_until):
            held.pop(symbol, None)
        if market is not None:
            self.left_guests[symbol] = (market, now + GUEST_REPORT_SECONDS)
        self.log.info(f"{symbol}: its broadcast trade has closed, and it isn't one of the bot's markets - no longer "
                      f"watching it.")

    def save_state(self) -> None:
        self.state.save()
        if self.watch is not None:
            self.watch.changed = False

    def reconcile(self, own: dict, others: dict, now: float) -> None:
        """Match the saved notes to what's really open."""
        changed = False
        for symbol in list(self.state.positions):
            if symbol not in own:
                notes = self.state.positions[symbol]
                if notes.get("guest") and symbol not in self.markets:
                    continue  # a broadcast trade whose market couldn't be looked up again: unseen, not closed
                seen = self.watch is not None and symbol in self.watch.marks  # it has shown up open before
                if not seen and now - (notes.get("opened_at") or 0) < FILL_GRACE_SECONDS:
                    continue  # just opened - Alpaca fills a moment after accepting the order
                if self.watch is None or not self.watch.closing(symbol):  # else the timeout says what happened
                    self.log.info(f"{symbol}: the bot's {notes.get('direction', '')} position has closed - by its "
                                  f"stop-loss or take-profit at {self.broker.name}, or by hand.")
                self.release(symbol, now)
        if self.watch is not None:  # the timeout's notes on positions whose own notes were lost
            for symbol in [s for s in self.watch.marks if s not in own and s not in self.state.positions]:
                self.watch.gone(symbol, now)
        for symbol, position in own.items():
            notes = self.state.positions.get(symbol)
            if notes is None or notes.get("direction") != position.direction:  # e.g. the notes file was lost
                self.state.positions[symbol] = {
                    "direction": position.direction, "opened_at": position.opened_at or now, "entry": position.entry,
                    "stop": position.stop, "take_profit": position.take_profit,
                }
                changed = True
                self.log.info(f"{symbol}: found this bot's open {position.direction} position ({position.size:g} @ "
                              f"{position.entry:g}) - managing it.")
        for symbol in set(others) - self.others:
            p = others[symbol]
            self.log.info(f"{symbol}: there's an open {p.direction} position this bot didn't open ({p.size:g} @ "
                          f"{p.entry:g}) - leaving it alone, and not trading {symbol} until it's closed.")
        for symbol in self.others - set(others):
            self.log.info(f"{symbol}: the other position has closed - {symbol} can be traded again.")
        self.others = set(others)
        if changed or (self.watch is not None and self.watch.changed):
            self.save_state()

    def time_exits(self, positions: dict, now: float, quotes: dict = None) -> None:
        # The scalper and the surge follower check their own stop-loss /
        # take-profit on every pass too: the broker's may sit further out, at
        # its minimum distance.
        check_levels = not self.broker.native_stops or self.strategy.checks_own_levels
        # The stagnancy timeout needs price reads for a window counted in time,
        # and a live price to close at.
        watching = self.watch is not None and bool(positions) and (
            self.watch.book.needs_reads or any(self.watch.closing(s) for s in positions))
        if positions and (check_levels or watching) and quotes is None:
            quotes = self.broker.quotes([self.markets[s] for s in positions])
        if self.watch is not None and quotes is not None:
            for symbol in positions:
                quote = quotes.get(symbol)
                self.watch.observe(symbol, now, quote.bid if quote else None, quote.ask if quote else None,
                                   quote.tradeable if quote else False)
        for symbol, position in list(positions.items()):
            market, notes = self.markets[symbol], self.state.positions.get(symbol, {})
            if self.watch is not None and self.watch.closing(symbol):
                # The timeout's close is under way: nothing else closes it meanwhile.
                if self.stagnancy_exit(market, position, notes, quotes, now):
                    del positions[symbol]
                continue
            reason = self.strategy.exit_on_time(market, position, notes, now)
            if not reason and check_levels:
                reason = self.bot_side_stop(position, notes, quotes.get(symbol))
            if reason:
                if self.close(market, position, reason, now):
                    del positions[symbol]
                continue
            if self.watch is not None and self.stagnancy_exit(market, position, notes, quotes, now):
                del positions[symbol]
        if self.watch is not None:
            self.watch.prune(now)
            if self.watch.changed:
                self.save_state()

    def stagnancy_exit(self, market, position, notes: dict, quotes, now: float) -> bool:
        """The stagnancy timeout's look at one of the bot's positions. True if
        it closed it and the broker confirmed it - the slot is free."""
        watch, symbol = self.watch, market.symbol
        quote = (quotes or {}).get(symbol)
        rule = watch.book.rule(symbol, (market.name, market.requested))
        bars = self.feeds[symbol]["exec"].bars if symbol in self.feeds else None
        atr_now = last(atr(bars, ATR_PERIOD)) if bars and rule.range.unit == "atr" else None
        held = self.held(market, position, notes, quote, rule)
        live = quote is not None and quote.tradeable
        if not watch.assess(held, now, mid=quote.mid if live else None,
                            tradeable=quote.tradeable if quote is not None else True, bars=bars, atr=atr_now):
            return False
        if self.settings.dry_run:
            return False
        if not live:
            quote = self.broker.quotes([market]).get(symbol)  # the mid just before the close, for its slippage
        self.broker.close_problem, self.broker.closing_fill = "", None
        done = self.broker.close(market, position, stagnancy.TIMEOUT_REASON)
        fill = self.broker.closing_fill or {}
        fields = watch.closed(held, now, done, filled=self.broker.fills_on_close, price=fill.get("price"),
                              pnl=fill.get("pnl"), refs=fill.get("refs", ()),
                              mid=quote.mid if quote is not None and quote.tradeable else None,
                              problem=self.broker.close_problem)
        if fields is None:
            if watch.changed:
                self.save_state()
            return False
        self.release(symbol, now, fields)
        return True

    def held(self, market, position, notes: dict, quote, rule) -> "stagnancy.Held":
        """One of the bot's positions, as the stagnancy timeout sees it."""
        risk = notes.get("risk")
        if risk is None and rule.pnl.unit == "R" and notes.get("stop") is not None and notes.get("entry"):
            # Opened before the bot noted what it risked: worked out once, from its notes.
            quote = quote or self.broker.quotes([market]).get(market.symbol)
            risk = self.broker.risk_money(market, quote, position.size, abs(notes["entry"] - notes["stop"])) or 0.0
            notes["risk"] = risk
            self.state.save()
        return stagnancy.Held(
            key=market.symbol, symbol=market.symbol, direction=position.direction, size=position.size,
            entry=position.entry, opened_at=notes.get("opened_at") or position.opened_at,
            pnl=self.broker.net_pnl(market, position, quote), risk=risk,
            refs=tuple(sorted(str(r) for r in self.broker.refs(position))), aliases=(market.name, market.requested))

    @staticmethod
    def bot_side_stop(position, notes: dict, quote):
        """The stop-loss / take-profit, checked here for brokers that can't
        hold them (and by the scalper, whose broker ones may sit further out)."""
        if quote is None or not quote.tradeable:
            return None
        long = position.direction == "long"
        price = quote.bid if long else quote.ask
        stop, target = notes.get("stop"), notes.get("take_profit")
        if stop is not None and ((price <= stop) if long else (price >= stop)):
            return f"stop-loss ({price:g} vs {stop:g})"
        if target is not None and ((price >= target) if long else (price <= target)):
            return f"take-profit ({price:g} vs {target:g})"
        return None

    def close(self, market, position, reason: str, now: float) -> bool:
        if now < self.close_retry_at.get(market.symbol, 0):
            return False
        retry = PRICE_CLOSE_RETRY_SECONDS if self.strategy.checks_own_levels else CLOSE_RETRY_SECONDS
        if self.settings.dry_run:
            self.log.info(f"DRY RUN: would close {market.symbol} {position.direction} {position.size:g} ({reason}).")
            self.close_retry_at[market.symbol] = now + retry
            return False
        if self.broker.close(market, position, reason):
            self.release(market.symbol, now)
            return True
        self.close_retry_at[market.symbol] = now + retry
        self.log.warning(f"{market.symbol}: will try closing again in {retry}s.")
        return False

    def on_new_bars(self, symbols: list, positions: dict, others: dict, now: float) -> None:
        signals = []
        for symbol in symbols:
            if symbol not in self.markets:
                continue  # a guest market dropped this pass, its broadcast trade closed
            market = self.markets[symbol]
            bars = {role: feed.bars for role, feed in self.feeds[symbol].items()}
            closed_at = clock.local("london", bars["exec"][-1].time + self.strategy.bar_seconds).strftime("%a %H:%M")
            if symbol in others:
                self.log.info(f"[{closed_at} London] {symbol} | not trading it - someone else's position is open")
                continue
            position = positions.get(symbol)
            if position is not None and self.watch is not None and self.watch.closing(symbol):
                self.log.info(f"[{closed_at} London] {symbol} | closing it ({stagnancy.TIMEOUT_REASON})")
                continue
            if position is not None:
                notes = self.state.positions.setdefault(symbol, {})
                reason = self.strategy.exit_on_bar(market, bars, position, notes, now)
                self.state.save()
                pnl = f", P/L {position.pnl:+.2f}" if position.pnl is not None else ""
                self.log.info(f"[{closed_at} London] {symbol} | holding {position.direction} {position.size:g} @ "
                              f"{position.entry:g}{pnl}" + (f" | exit: {reason}" if reason else ""))
                if reason and self.close(market, position, reason, now):
                    del positions[symbol]
                continue
            if symbol in self.guests:
                continue  # watched for its broadcast trade only - never traded by the strategy
            assessment = self.strategy.assess(market, bars, now)
            self.log.info(f"[{closed_at} London] {symbol} | {assessment.note}")
            if assessment.signal is not None:
                signals.append((assessment.signal.score, symbol, assessment.signal))
        if signals:
            self.enter(signals, positions, now)

    # -- the scalper: every read of the prices ------------------------------------------
    def read_prices(self, now: float) -> dict:
        """One read of every market's bid and ask, handed to the strategy."""
        quotes = self.broker.quotes(list(self.markets.values()))
        for symbol, market in self.markets.items():
            quote = quotes.get(symbol)
            self.strategy.on_price(market, quote or Quote(0.0, 0.0, False), now)
        return quotes

    def on_prices(self, positions: dict, others: dict, quotes: dict, now: float) -> None:
        """Look for entries on this read. Quiet in the log: each market's
        state is logged every STATUS_EVERY_SECONDS, and signals when they come."""
        s, p = self.settings, self.p
        signals = []
        for symbol, market in self.markets.items():
            position = positions.get(symbol)
            if position is not None:
                pnl = f", P/L {position.pnl:+.2f}" if position.pnl is not None else ""
                self.status[symbol] = f"holding {position.direction} {position.size:g} @ {position.entry:g}{pnl}"
                continue
            if symbol in self.guests:
                continue  # watched for its broadcast trade only - never traded by the strategy
            if symbol in others:
                self.status[symbol] = "not trading it - someone else's position is open"
                continue
            quote = quotes.get(symbol)
            if quote is None or not quote.tradeable:
                self.status[symbol] = (quote.why_not if quote is not None else "") or "no price right now"
                continue
            assessment = self.strategy.assess_price(market, now)
            self.status[symbol] = assessment.note
            signal = assessment.signal
            if signal is None or now < self.quiet_until.get(symbol, 0):
                continue
            if signal.direction == "short" and not self.broker.can_short:
                continue  # Alpaca: buys only - not worth a log line every minute
            day = self.strategy.trade_day(market, now)
            if self.state.trades_on(symbol, day) >= p["max_trades_per_day"]:
                continue
            self.quiet_until[symbol] = now + p["cooldown_seconds"]
            signals.append((signal.score, symbol, signal))
        if now >= self.next_status:
            self.next_status = now + STATUS_EVERY_SECONDS
            for symbol, note in self.status.items():
                self.log.info(f"{symbol} | {note}")
        if signals and len(positions) < s.max_positions:
            self.enter(signals, positions, now, quotes)

    # -- entries ------------------------------------------------------------------------
    def enter(self, signals: list, positions: dict, now: float, quotes: dict = None) -> None:
        log = self.log
        if clock.near_rollover(now, *ROLLOVER_QUIET):
            for _, symbol, signal in signals:
                log.info(f"{symbol}: {signal.direction} signal skipped - {ROLLOVER_SKIP}.")
            return
        if quotes is None:
            quotes = self.broker.quotes([self.markets[symbol] for _, symbol, _ in signals])
        held = len(positions)
        for _, symbol, signal in sorted(signals, key=lambda item: item[0], reverse=True):
            market, quote = self.markets[symbol], quotes.get(symbol)
            try:
                entry = self.entry(market, quote, signal, held, now)
            except Skip as skip:
                log.info(f"{symbol}: {signal.direction} signal skipped - {skip.why}.")
                continue
            log.info(f"{symbol}: {signal.why} | entry ~{entry.price:g}, stop {entry.stop:g}, take-profit "
                     f"{f'{entry.take_profit:g}' if entry.take_profit is not None else 'none'} | {entry.sizing}")
            if self.settings.dry_run:
                log.info(f"DRY RUN: would open {entry.direction} {symbol} size {entry.size:g}.")
                continue
            if self.open_entry(market, signal, entry, quote, now):
                held += 1

    def entry(self, market, quote, signal, held: int, now: float, exposure: float = None, most: float = None) -> Entry:
        """Every rule an entry must pass, and its size - the one place they're
        applied, to the strategy's signals and broadcast trades alike. `held`
        is how many positions the bot has open; a broadcast can ask for less
        `exposure` than the bot would trade, and its open no more than the
        size it previewed (`most`). Raises Skip saying why not."""
        s, p = self.settings, self.p
        symbol, direction = market.symbol, signal.direction
        if direction == "short" and not self.broker.can_short:
            raise Skip(broadcast.UNAVAILABLE, f"{self.broker.name} can't sell short here")
        if held >= s.max_positions:
            raise Skip(broadcast.NO_SLOT, f"{s.max_positions} position(s) already open")
        cooling = self.watch.cooling(symbol, now) if self.watch is not None else 0
        if cooling:  # only with a <TYPE>_STAGNANT_COOLDOWN set
            raise Skip(broadcast.RISK, f"closed by the stagnancy timeout - no new trade in it before "
                                       f"{clock.local('london', cooling).strftime('%H:%M')} London")
        day = self.strategy.trade_day(market, now)
        if self.state.trades_on(symbol, day) >= p["max_trades_per_day"]:
            raise Skip(broadcast.RISK, f"already traded {p['max_trades_per_day']} time(s) today")
        if clock.near_rollover(now, *ROLLOVER_QUIET):
            raise Skip(broadcast.CLOSED, ROLLOVER_SKIP)
        if quote is None:
            raise Skip(broadcast.CLOSED, "no live price")
        if not quote.tradeable:
            raise Skip(broadcast.CLOSED, quote.why_not or "not tradeable right now")

        sign = 1 if direction == "long" else -1
        price = quote.ask if sign > 0 else quote.bid
        distance = (price - signal.stop) * sign
        if distance <= 0:
            raise Skip(broadcast.RISK, f"the price {price:g} is already through the stop {signal.stop:g}")
        too_wide = self.spread_problem(quote, distance)
        if too_wide:
            raise Skip(broadcast.RISK, too_wide)
        if signal.take_profit is not None:
            take_profit = signal.take_profit
        elif signal.reward_risk:
            take_profit = price + sign * signal.reward_risk * distance
        else:
            take_profit = None
        if take_profit is not None and (take_profit - price) * sign <= 0:
            raise Skip(broadcast.RISK, f"the price {price:g} is already past the take-profit {take_profit:g}")
        if self.strategy.holds_overnight:
            swap = self.broker.swap_percent_per_night(market, direction)
            if swap is not None and -swap > p["max_swap_percent"]:
                raise Skip(broadcast.RISK, f"holding it costs {-swap:.4f}% a night in swap "
                                           f"(max {p['max_swap_percent']:g}%)")

        size, sizing, capped = self.sizing(market, quote, distance, exposure)
        if size == 0:
            raise Skip(broadcast.RISK, sizing)
        if most is not None and size > most + 1e-12:
            size = market.min_size if self.broker.fixed_min_size else self.broker.round_size(market, most)
            sizing += f", cut to the {most:g} it previewed"
            if size == 0:
                raise Skip(broadcast.RISK, f"the {most:g} it previewed is now below {self.broker.name}'s smallest trade")
        return Entry(direction, price, signal.stop, take_profit, distance, size, sizing, capped,
                     exposure=size * quote.unit_value if quote.unit_value else None,
                     risk=self.broker.risk_money(market, quote, size, distance))

    def open_entry(self, market, signal, entry: Entry, quote, now: float, notes: dict = None) -> bool:
        """Send an entry that passed entry() to the broker, and note the
        position. True if the broker took it."""
        symbol, sign = market.symbol, 1 if entry.direction == "long" else -1
        broker_stop, broker_target = self.broker_levels(market, quote, sign, entry.price, entry.stop, entry.take_profit)
        if not self.broker.open(market, entry.direction, entry.size, broker_stop, broker_target, quote):
            return False
        notes = {"direction": entry.direction, "opened_at": now, "entry": entry.price, "stop": entry.stop,
                 "take_profit": entry.take_profit, "why": signal.why,
                 # what it stands to lose at its stop - the stagnancy timeout's "R"
                 "risk": entry.risk, **(notes or {})}
        self.strategy.on_open(market, signal, entry.price, notes)
        self.state.positions[symbol] = notes
        self.state.own_ids = sorted(self.broker.own_ids)
        self.state.count_trade(symbol, self.strategy.trade_day(market, now))
        self.state.save()
        self.dashboard.report_soon(TRADE_REPORT_DELAYS[0])
        return True

    def spread_problem(self, quote, distance: float) -> str:
        """Why the spread is too wide for a stop `distance` from the entry, or
        "" if it isn't (the surge follower asks this before enter() does)."""
        most = self.p["max_spread_percent"]
        if quote.spread > most / 100 * distance:
            return f"the spread {quote.spread:g} is {quote.spread / distance:.0%} of the stop distance (max {most:g}%)"
        return ""

    def broker_levels(self, market, quote, sign: int, entry: float, stop: float, take_profit) -> tuple:
        """The stop-loss / take-profit to put on the order. The scalper's (and
        the surge follower's) can be inside some brokers' minimum distance, so
        those go at the minimum instead and the bot closes at its own levels."""
        least = self.broker.min_stop_distance(market, quote) * 1.1 if self.strategy.checks_own_levels else 0.0
        if not least:
            return stop, take_profit
        broker_stop = stop if (entry - stop) * sign >= least else entry - sign * least
        broker_target = take_profit
        if take_profit is not None and (take_profit - entry) * sign < least:
            broker_target = entry + sign * least
        if (broker_stop, broker_target) != (stop, take_profit):
            self.log.info(f"{market.symbol}: {self.broker.name}'s minimum stop distance is {least / 1.1:g}, so its "
                          f"stop-loss / take-profit go at {broker_stop:g} / "
                          f"{f'{broker_target:g}' if broker_target is not None else 'none'}; the bot closes at its "
                          f"own levels itself.")
        return broker_stop, broker_target

    def size(self, market, quote, distance: float) -> tuple:
        """(size, how it was worked out) - (0, why not) if nothing fits."""
        size, sizing, _ = self.sizing(market, quote, distance)
        return size, sizing

    def sizing(self, market, quote, distance: float, exposure: float = None) -> tuple:
        """(size, how it was worked out, "" or why a broadcast's `exposure`
        was cut) - (0, why not, "") if nothing fits. A broadcast's exposure,
        in the account's currency, only ever makes the trade smaller than the
        bot's own limits allow."""
        cur = self.currency
        if self.broker.fixed_min_size:
            text = f"size {market.min_size:g} ({self.broker.name}'s minimum)"
            if exposure is None:
                return market.min_size, text, ""
            worth = market.min_size * quote.unit_value if quote.unit_value else None
            if worth is not None and exposure < worth * (1 - 1e-9):
                return 0.0, (f"its smallest trade ({market.min_size:g}) is worth {worth:,.2f} {cur}, more than the "
                             f"{exposure:,.2f} asked for"), ""
            return market.min_size, text, f"{self.broker.name} always trades the market's minimum size"
        if not quote.unit_value:
            return 0.0, f"can't value a trade in {cur}", ""
        per_point = quote.unit_value / quote.mid              # account currency per unit per 1.0 of price
        risk_budget = self.settings.budget * self.p["risk_percent"] / 100
        by_risk = risk_budget / (distance * per_point)
        by_exposure = self.slice_cap() / quote.unit_value
        limit = "risk" if by_risk <= by_exposure else "leverage cap"
        wanted = min(by_risk, by_exposure)
        capped = ""
        if exposure is not None:
            asked = exposure / quote.unit_value
            if asked > wanted * (1 + 1e-9):
                capped = f"capped at {wanted * quote.unit_value:,.2f} {cur} exposure (the bot's {limit} limit)"
            else:
                wanted, limit = asked, "the quantity asked for"
        size = self.broker.round_size(market, wanted)
        if size <= 0:
            smallest = max(market.min_size, market.size_step)
            if limit == "the quantity asked for":
                return 0.0, (f"its smallest trade ({smallest:g}) would be worth {smallest * quote.unit_value:,.2f} "
                             f"{cur}, more than the {exposure:,.2f} asked for"), ""
            return 0.0, (f"its smallest trade ({smallest:g}) would be worth {smallest * quote.unit_value:,.2f} and risk "
                         f"{smallest * distance * per_point:,.2f} {cur} - over this bot's "
                         f"{risk_budget:,.2f} risk or {self.slice_cap():,.2f} per-trade limit; raise "
                         f"{self.settings.env_prefix}BUDGET for it"), ""
        value, risk = size * quote.unit_value, size * distance * per_point
        return size, (f"size {size:g}: {value:,.2f} {cur} exposure, {risk:,.2f} at risk "
                      f"({risk / self.settings.budget:.2%} of the budget; sized by {limit})"), capped

    # -- broadcast trades from the dashboard (shared/broadcast.py) ------------------------
    def broadcast_preview(self, command: dict) -> tuple:
        """What the bot would do with a broadcast trade: (a line, {figures})."""
        request = broadcast.Request.parse(command)
        plan = self.broadcast_plan(request, time.time())
        e, figures = plan["entry"], plan["figures"]
        take_profit = figures.get("takeProfit")
        line = (f"Would {request.side} {e.size:g} {self.broker.size_unit} of {figures['symbol']} at ~{e.price:g}: "
                f"stop-loss {figures['stopLoss']:g}, "
                + (f"take-profit {take_profit:g}" if take_profit is not None else "no take-profit")
                + (f" ({e.capped})" if e.capped else ""))
        self.log.info(f"Broadcast #{request.id}: {line} | {e.sizing}")
        return line + ".", figures

    def broadcast_open(self, command: dict) -> tuple:
        """Open a broadcast trade the admin confirmed: every rule checked again
        on fresh prices, never bigger than the preview. (a line, {figures})."""
        now = time.time()
        request = broadcast.Request.parse(command)
        self.broadcasts.check_new(request)
        request.check_age(now)
        plan = self.broadcast_plan(request, now, most=request.size)
        market, e = plan["market"], plan["entry"]
        symbol = market.symbol
        self.broadcasts.opening(request, symbol, (market.name, market.requested), now)
        before = set(self.broker.own_ids)
        self.broker.order_tag, self.broker.open_problem = f"broadcast-{request.id}", ""
        notes = {"broadcast": request.id, "entry_source": broadcast.ENTRY_SOURCE}
        if plan["guest"]:
            notes["guest"] = market.requested  # looked up again by this after a restart
        try:
            opened = self.open_entry(market, plan["signal"], e, plan["quote"], now, notes)
        finally:
            self.broker.order_tag = None
        if not opened:
            self.broadcasts.failed(request)
            raise CommandError(f"{self.broker.name} didn't open it: {self.broker.open_problem or 'see the bot log'}.")
        refs = sorted(str(r) for r in self.broker.own_ids - before)
        self.broadcasts.opened(request, refs, now)
        if plan["guest"]:
            self.add_guest(market, plan["feeds"])
        verb = "Bought" if request.side == "buy" else "Sold"
        self.log.info(f"Broadcast #{request.id}: {verb.lower()} {e.size:g} {symbol} ({e.sizing}).")
        return (f"{verb} {e.size:g} {self.broker.size_unit} of {symbol} at ~{e.price:g}"
                + (f" ({self.broker.name} ref {refs[0]})" if refs else "") + "."), \
            {"symbol": symbol, "size": e.size, "price": e.price, **({"ref": refs[0]} if refs else {})}

    def broadcast_plan(self, request, now: float, most: float = None) -> dict:
        """The trade the bot would make for a broadcast, every rule applied:
        {"market", "guest", "feeds", "quote", "signal", "entry", "figures"}.
        Raises broadcast.Declined saying why it won't."""
        if self.settings.dry_run:
            raise broadcast.Declined(broadcast.PAUSED, "The bot is on a dry run (STRATEGY_DRY_RUN), so it sends no "
                                                       "orders.")
        market, candidate, feeds, guest = self.broadcast_market(request, now)
        symbol = market.symbol
        own, others = self.split_positions(self.markets if not guest else {**self.markets, symbol: market})
        if symbol in own or symbol in self.state.positions:
            raise broadcast.Declined(broadcast.NO_SLOT, f"The bot already holds {symbol}, and holds one position per "
                                                        f"market.")
        if symbol in others:
            raise broadcast.Declined(broadcast.NO_SLOT, f"Someone else's {symbol} position is open, and the bot doesn't "
                                                        f"trade a market while one is.")
        quote = self.broker.quotes([market]).get(symbol)
        if quote is None or not quote.tradeable:
            why = (quote.why_not if quote is not None else "") or "no live price"
            raise broadcast.Declined(broadcast.CLOSED, f"{symbol}: {why}.")
        bars = {role: feed.bars for role, feed in feeds.items()}
        try:
            self.exits_at_once(market, bars, quote, request.direction, now)
            signal = self.strategy.manual_signal(market, bars, quote, request.direction, now)
            entry = self.entry(market, quote, signal, len(set(own) | set(self.state.positions)), now,
                               exposure=request.quantity, most=most)
        except Skip as skip:
            raise broadcast.Declined(skip.kind, f"{symbol}: {skip.why}.") from None
        rule = self.watch.book.rule(symbol, (market.name, market.requested)) if self.watch is not None else None
        exits = self.strategy.exits() + (f"; stagnancy timeout ({rule.mode})" if rule and rule.mode != "off" else "")
        if guest:
            exits += " - watched until it closes, as it isn't one of the bot's markets"
        figures = broadcast.figures(
            symbol=symbol, name=market.name, size=entry.size, sizeUnit=self.broker.size_unit,
            exposure=round(entry.exposure, 2) if entry.exposure is not None else None,
            risk=round(entry.risk, 2) if entry.risk is not None else None, currency=self.currency,
            entry=entry.price, stopLoss=self.broker.round_price(market, entry.stop),
            takeProfit=self.broker.round_price(market, entry.take_profit) if entry.take_profit is not None else None,
            accountMode=self.broker.account_mode, exits=exits, capped=entry.capped or None,
            standIn=candidate.canonical if candidate.stand_in else None, previewedAt=now)
        return {"market": market, "guest": guest, "feeds": feeds, "quote": quote, "signal": signal, "entry": entry,
                "figures": figures}

    def broadcast_market(self, request, now: float) -> tuple:
        """(Market, symbols.Candidate, {role: Feed}, guest?) for a broadcast's
        instrument: one of the bot's own markets, or else looked up on its
        broker as a guest (its bars fetched, for the stop-loss). Raises
        broadcast.Declined if the broker hasn't got it."""
        found = symbols.candidates(self.settings.broker, request.symbol)
        unavailable = broadcast.Declined(broadcast.UNAVAILABLE, f"{request.symbol} isn't available on "
                                                                f"{self.broker.name}" + (
            f" (looked for {', '.join(c.code for c in found)})." if found else "."))
        for candidate in found:
            market = self.own_market(candidate.code)
            if market is not None:
                return market, candidate, self.feeds.get(market.symbol, {}), market.symbol in self.guests
        self.previewed = {c: v for c, v in self.previewed.items() if now - v[2] < PREVIEWED_SECONDS}
        for candidate in found:
            if candidate.code in self.previewed:
                market, feeds, _ = self.previewed[candidate.code]
            else:
                try:
                    markets = self.broker.resolve([candidate.code])
                except BrokerError as e:
                    self.log.warning(f"Broadcast #{request.id}: couldn't look up {candidate.code} ({e}).")
                    continue
                if not markets:
                    continue
                markets = self.strategy.prepare(markets, self.log)
                if not markets:
                    raise broadcast.Declined(broadcast.UNAVAILABLE, f"{candidate.code} isn't a market this bot can "
                                                                    f"trade - its log says why.")
                market = next(iter(markets.values()))
                feeds = {role: Feed(self.broker, market, tf, keep) for role, (tf, keep) in self.strategy.feeds().items()}
            try:
                for feed in feeds.values():
                    feed.refresh(now)
            except BrokerError as e:
                raise broadcast.Declined(broadcast.CLOSED, f"Couldn't get {market.symbol}'s bars ({e}).") from None
            self.previewed[candidate.code] = (market, feeds, now)
            return market, candidate, feeds, True
        raise unavailable

    def own_market(self, code: str):
        """The bot's market (its own, or a guest it holds) a broker code names, or None."""
        wanted = {part.strip().upper() for part in code.split(":") if part.strip()}
        for market in self.markets.values():
            names = {market.symbol.upper(), market.requested.split(":")[0].strip().upper(), (market.name or "").upper()}
            if wanted & names:
                return market
        return None

    def exits_at_once(self, market, bars: dict, quote, direction: str, now: float) -> None:
        """Raise Skip if the bot's own exits would close a trade opened now
        straight away - outside its hours, past its flat time, on the wrong
        side of its trend - so a broadcast doesn't open one only to close it."""
        price = quote.ask if direction == "long" else quote.bid
        position = Position(market.symbol, direction, 0.0, price, opened_at=now, own=True)
        notes = {"direction": direction, "opened_at": now, "entry": price}
        reason = self.strategy.exit_on_time(market, position, notes, now)
        if reason:
            raise Skip(broadcast.CLOSED, f"its own exit would close it straight away ({reason})")
        if bars.get("exec"):
            reason = self.strategy.exit_on_bar(market, bars, position, dict(notes), now)
            if reason:
                raise Skip(broadcast.OTHER, f"its own exit would close it at the next bar ({reason})")

    def add_guest(self, market, feeds: dict) -> None:
        """A market outside the bot's list, now holding a broadcast trade:
        watched for that trade's exits until it closes - never traded."""
        symbol = market.symbol
        self.markets[symbol] = market
        self.guests.add(symbol)
        self.left_guests.pop(symbol, None)
        self.previewed = {c: v for c, v in self.previewed.items() if v[0].symbol != symbol}
        if feeds:
            self.feeds[symbol] = feeds
            self.seen[symbol] = feeds["exec"].bars[-1].time if feeds["exec"].bars else 0.0
        self.log.info(f"{symbol}: not one of the bot's markets - watching it for its broadcast trade's exits until "
                      f"it closes.")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
def main(broker_key: str, strategy_key: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    log = logging.getLogger(f"{broker_key}-{strategy_key}")
    broker = None
    try:
        settings = bot_settings(broker_key, strategy_key)
        if strategy_key in REBALANCERS:  # the portfolio bots, in engine/rebalancer.py (built on this module)
            from .rebalancer import make_portfolio_strategy
            strategy = make_portfolio_strategy(strategy_key, settings.params)
        else:
            strategy = make_strategy(strategy_key, settings.params)
        # Off unless DASHBOARD_URL and DASHBOARD_TOKEN are set (see shared/dashboard_reporter.py).
        dashboard = DashboardReporter(settings.slug, settings.name, broker=BROKER_NAMES[broker_key],
                                      strategy=STRATEGY_NAMES[strategy_key])
        broker = make_broker(broker_key, settings, dashboard, log)
        if strategy_key in REBALANCERS:
            from .rebalancer import RebalanceBot  # needs this module, so imported here
            bot_class = RebalanceBot
        elif strategy.uses_signals:
            from .surge import FollowerBot  # needs this module, so imported here
            bot_class = FollowerBot
        else:
            bot_class = StrategyBot
        bot_class(settings, strategy, broker, dashboard, log).run()
    except SettingsError as e:
        log.error(f"Can't start: {e}.")
        sys.exit(1)
    except BrokerError as e:
        log.critical(f"Can't carry on: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("Bot stopped manually (Ctrl+C). Goodbye.")
    finally:
        if broker is not None:
            broker.shutdown()
