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
"""

import json
import logging
import math
import os
import sys
import time

from . import clock
from .brokers.base import BrokerError
from .settings import BROKER_NAMES, STRATEGY_NAMES, TIMEFRAMES, SettingsError, bot_settings
from .strategies import make_strategy

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "shared"))
from dashboard_reporter import DashboardReporter  # noqa: E402 - needs the path above

STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
LOOP_SECONDS = 30
BAR_SETTLE_SECONDS = 10          # wait this long after a bar's close before fetching it
MAX_RETRY_SECONDS = 900          # a bar that's late (market shut) is looked for at least this often
CLOSE_RETRY_SECONDS = 300        # a failed close is tried again this often
FILL_GRACE_SECONDS = 180         # a position just opened may take this long to show up
ERROR_BACKOFF_SECONDS = 60
MAX_CONSECUTIVE_ERRORS = 10
ROLLOVER_QUIET = (15, 45)        # minutes before / after the rollover without new trades


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
        try:
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
            self.positions, self.trades = saved.get("positions", {}), saved.get("trades", {})
            self.own_ids = saved.get("own_ids", [])
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning(f"Couldn't read {self.path} ({e}); starting without saved notes.")

    def save(self) -> None:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"positions": self.positions, "trades": self.trades, "own_ids": self.own_ids}, f, indent=1)
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

    # -- startup --------------------------------------------------------------------
    def start(self) -> None:
        s, log = self.settings, self.log
        account = self.broker.connect()
        self.currency = account.currency

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
        self.dashboard.describe(account=account.id, currency=self.currency, config=config)

        for symbol, market in self.markets.items():
            self.feeds[symbol] = {role: Feed(self.broker, market, tf, keep)
                                  for role, (tf, keep) in self.strategy.feeds().items()}
        self.started_at = time.time()
        self.refresh_feeds(self.started_at)
        self.seen = {s: (f["exec"].bars[-1].time if f["exec"].bars else 0.0) for s, f in self.feeds.items()}
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
            self.dashboard.sleep(LOOP_SECONDS, self.report, self.broker.dashboard_every)

    def report(self) -> None:
        """Account, positions and trades for the dashboard - when due, which
        can be in the middle of the wait between passes."""
        try:
            self.broker.report(self.markets, self.state.positions)
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

        own, others = {}, {}
        for symbol, position in self.broker.positions(self.markets).items():
            notes = self.state.positions.get(symbol)
            mine = position.own if position.own is not None else (
                notes is not None and notes.get("direction") == position.direction)
            (own if mine else others)[symbol] = position
        self.reconcile(own, others, now)
        self.time_exits(own, now)
        if new_bars:
            self.on_new_bars(new_bars, own, others, now)

    def reconcile(self, own: dict, others: dict, now: float) -> None:
        """Match the saved notes to what's really open."""
        changed = False
        for symbol in list(self.state.positions):
            if symbol not in own:
                notes = self.state.positions[symbol]
                if now - (notes.get("opened_at") or 0) < FILL_GRACE_SECONDS:
                    continue  # just opened - Alpaca fills a moment after accepting the order
                del self.state.positions[symbol]
                changed = True
                self.log.info(f"{symbol}: the bot's {notes.get('direction', '')} position has closed - by its "
                              f"stop-loss or take-profit at {self.broker.name}, or by hand.")
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
        if changed:
            self.state.save()

    def time_exits(self, positions: dict, now: float) -> None:
        quotes = None
        if positions and not self.broker.native_stops:
            quotes = self.broker.quotes([self.markets[s] for s in positions])
        for symbol, position in list(positions.items()):
            market, notes = self.markets[symbol], self.state.positions.get(symbol, {})
            reason = self.strategy.exit_on_time(market, position, notes, now)
            if not reason and quotes is not None:
                reason = self.bot_side_stop(position, notes, quotes.get(symbol))
            if reason and self.close(market, position, reason, now):
                del positions[symbol]

    @staticmethod
    def bot_side_stop(position, notes: dict, quote):
        """The stop-loss / take-profit, checked here for brokers that can't hold them."""
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
        if self.settings.dry_run:
            self.log.info(f"DRY RUN: would close {market.symbol} {position.direction} {position.size:g} ({reason}).")
            self.close_retry_at[market.symbol] = now + CLOSE_RETRY_SECONDS
            return False
        if self.broker.close(market, position, reason):
            self.state.positions.pop(market.symbol, None)
            self.state.save()
            self.close_retry_at.pop(market.symbol, None)
            return True
        self.close_retry_at[market.symbol] = now + CLOSE_RETRY_SECONDS
        self.log.warning(f"{market.symbol}: will try closing again in {CLOSE_RETRY_SECONDS // 60} minutes.")
        return False

    def on_new_bars(self, symbols: list, positions: dict, others: dict, now: float) -> None:
        signals = []
        for symbol in symbols:
            market = self.markets[symbol]
            bars = {role: feed.bars for role, feed in self.feeds[symbol].items()}
            closed_at = clock.local("london", bars["exec"][-1].time + self.strategy.bar_seconds).strftime("%a %H:%M")
            if symbol in others:
                self.log.info(f"[{closed_at} London] {symbol} | not trading it - someone else's position is open")
                continue
            position = positions.get(symbol)
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
            assessment = self.strategy.assess(market, bars, now)
            self.log.info(f"[{closed_at} London] {symbol} | {assessment.note}")
            if assessment.signal is not None:
                signals.append((assessment.signal.score, symbol, assessment.signal))
        if signals:
            self.enter(signals, positions, now)

    # -- entries ------------------------------------------------------------------------
    def enter(self, signals: list, positions: dict, now: float) -> None:
        s, p, log = self.settings, self.p, self.log
        if clock.near_rollover(now, *ROLLOVER_QUIET):
            for _, symbol, signal in signals:
                log.info(f"{symbol}: {signal.direction} signal skipped - too close to the daily rollover (spreads widen).")
            return
        quotes = self.broker.quotes([self.markets[symbol] for _, symbol, _ in signals])
        held = len(positions)
        for _, symbol, signal in sorted(signals, key=lambda item: item[0], reverse=True):
            market, quote, direction = self.markets[symbol], quotes.get(symbol), signal.direction

            def skip(why):
                log.info(f"{symbol}: {direction} signal skipped - {why}.")

            day = self.strategy.trade_day(market, now)
            if direction == "short" and not self.broker.can_short:
                skip(f"{self.broker.name} can't sell short here")
                continue
            if held >= s.max_positions:
                skip(f"{s.max_positions} position(s) already open")
                continue
            if self.state.trades_on(symbol, day) >= p["max_trades_per_day"]:
                skip(f"already traded {p['max_trades_per_day']} time(s) today")
                continue
            if quote is None:
                skip("no live price")
                continue
            if not quote.tradeable:
                skip(quote.why_not or "not tradeable right now")
                continue

            sign = 1 if direction == "long" else -1
            entry = quote.ask if sign > 0 else quote.bid
            distance = (entry - signal.stop) * sign
            if distance <= 0:
                skip(f"the price {entry:g} is already through the stop {signal.stop:g}")
                continue
            if quote.spread > p["max_spread_percent"] / 100 * distance:
                skip(f"the spread {quote.spread:g} is {quote.spread / distance:.0%} of the stop distance "
                     f"(max {p['max_spread_percent']:g}%)")
                continue
            if signal.take_profit is not None:
                take_profit = signal.take_profit
            elif signal.reward_risk:
                take_profit = entry + sign * signal.reward_risk * distance
            else:
                take_profit = None
            if take_profit is not None and (take_profit - entry) * sign <= 0:
                skip(f"the price {entry:g} is already past the take-profit {take_profit:g}")
                continue
            if self.strategy.holds_overnight:
                swap = self.broker.swap_percent_per_night(market, direction)
                if swap is not None and -swap > p["max_swap_percent"]:
                    skip(f"holding it costs {-swap:.4f}% a night in swap (max {p['max_swap_percent']:g}%)")
                    continue

            size, sizing = self.size(market, quote, distance)
            if size == 0:
                skip(sizing)
                continue
            log.info(f"{symbol}: {signal.why} | entry ~{entry:g}, stop {signal.stop:g}, take-profit "
                     f"{f'{take_profit:g}' if take_profit is not None else 'none'} | {sizing}")
            if s.dry_run:
                log.info(f"DRY RUN: would open {direction} {symbol} size {size:g}.")
                continue
            if not self.broker.open(market, direction, size, signal.stop, take_profit, quote):
                continue
            held += 1
            notes = {"direction": direction, "opened_at": now, "entry": entry, "stop": signal.stop,
                     "take_profit": take_profit, "why": signal.why}
            self.strategy.on_open(market, signal, entry, notes)
            self.state.positions[symbol] = notes
            self.state.own_ids = sorted(self.broker.own_ids)
            self.state.count_trade(symbol, day)
            self.state.save()

    def size(self, market, quote, distance: float) -> tuple:
        """(size, how it was worked out) - (0, why not) if nothing fits."""
        if self.broker.fixed_min_size:
            return market.min_size, f"size {market.min_size:g} ({self.broker.name}'s minimum)"
        if not quote.unit_value:
            return 0.0, f"can't value a trade in {self.currency}"
        per_point = quote.unit_value / quote.mid              # account currency per unit per 1.0 of price
        risk_budget = self.settings.budget * self.p["risk_percent"] / 100
        by_risk = risk_budget / (distance * per_point)
        by_exposure = self.slice_cap() / quote.unit_value
        size = self.broker.round_size(market, min(by_risk, by_exposure))
        if size <= 0:
            smallest = max(market.min_size, market.size_step)
            return 0.0, (f"its smallest trade ({smallest:g}) would be worth {smallest * quote.unit_value:,.2f} and risk "
                         f"{smallest * distance * per_point:,.2f} {self.currency} - over this bot's "
                         f"{risk_budget:,.2f} risk or {self.slice_cap():,.2f} per-trade limit; raise "
                         f"{self.settings.env_prefix}BUDGET for it")
        exposure, risk = size * quote.unit_value, size * distance * per_point
        limit = "risk" if by_risk <= by_exposure else "leverage cap"
        return size, (f"size {size:g}: {exposure:,.2f} {self.currency} exposure, {risk:,.2f} at risk "
                      f"({risk / self.settings.budget:.2%} of the budget; sized by {limit})")


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
        strategy = make_strategy(strategy_key, settings.params)
        # Off unless DASHBOARD_URL and DASHBOARD_TOKEN are set (see shared/dashboard_reporter.py).
        dashboard = DashboardReporter(settings.slug, settings.name, broker=BROKER_NAMES[broker_key],
                                      strategy=STRATEGY_NAMES[strategy_key])
        broker = make_broker(broker_key, settings, dashboard, log)
        StrategyBot(settings, strategy, broker, dashboard, log).run()
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
