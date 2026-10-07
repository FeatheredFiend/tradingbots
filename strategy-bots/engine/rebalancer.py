"""
The portfolio bots: instead of trading one signal at a time with a stop and
a target, they hold every one of their markets at a target size and move
each position towards it on a schedule.

1. Slow trend (OANDA, `slow-trend`, SLOW_TREND_*) - daily trend following
   on commodity CFDs. Each market is long, flat or short: the average of
   "the 50-day EMA is above / below the 200-day" and "the price is up /
   down on a year ago" (both up = long, both down = short, split = flat).
   Each position is sized so its usual daily moves add up to
   TARGET_VOLATILITY (10%) a year of the budget across all the markets
   together - quieter markets get bigger positions. Checked once a day at
   TRADE_TIME (15:00 London, when every market is open); a position is only
   resized when it's REBALANCE_BAND (25%) off its target, or the signal
   changes side. Each trade carries a far-away safety stop (SAFETY_STOP,
   3 months' usual movement), in case the bot isn't running. Backtested
   2008-2026 (see the README), this is the one approach here with an edge
   before costs (Sharpe ~0.5), but OANDA's financing (2.5% a year on every
   position) takes most of it: expect 0.1-0.3, i.e. near nothing.

2. Monthly ETF rotation (Alpaca, `etf-rotation`, ROTATION_*) - the budget
   is split equally between the funds (VOO, EFA, IEF, DBC, VNQ). Once a
   month, ROTATION_MINUTES_AFTER_OPEN (30) into the first session, each fund
   is held if its last month-end close is above the average of the last
   SMA_MONTHS (10) month-end closes, and otherwise its share goes into the
   cash fund (SHY, short-dated Treasuries). Bought outright, no leverage,
   no overnight costs; drift under REBALANCE_BAND (5%) of a fund's share is
   left alone. Backtested 2007-2026: ~5% a year with an 11% worst fall
   (holding all five: ~6% a year, 44% worst fall) - an investment, not an
   edge.

3. Weekly ETF trend rotation (Alpaca, `etf-trend`, ETF_TREND_*) - the slow
   trend's signal and sizing on 35 US funds (shares, bonds, property,
   commodities, currencies), bought only: a fund is held while its signal
   is long (EMA 50/200 and the year's move both up) and sold when it isn't,
   each sized so its usual moves add TARGET_VOLATILITY (10%) / sqrt(35) of
   the budget a year. Never more than the budget in all (scaled down when
   they'd add up to more); the rest sits in the cash fund (BIL, 1-3 month
   Treasury bills). Rebalanced once a week, MINUTES_AFTER_OPEN (30) into the
   week's first session, with the slow trend's 25% band. Backtested
   2008-2026: ~3% a year, 6% volatility, 14% worst fall - below the 5-fund
   rotation (4.6%, 14%) and a plain 60/40 (8.3%, 31%) - run to compare
   against the rotation, not because it tested better. Funds the other
   Alpaca bots trade are swapped for near-twins (IVV for SPY, IAU for GLD,
   ...), so they can share the account.

4. 60/40 dip rotation (Alpaca, `dip-rotation`, DIP_*) - SHARE_PERCENT (60%)
   of the budget in a share fund (VTI) and the rest in a bond fund (GOVT),
   except during a dip in the share fund, when it's all in shares. A dip
   starts at a close with RSI(2) under ENTRY_RSI (10) while the close is
   above its TREND_DAYS (200) day average, and ends at the first close
   above the EXIT_DAYS (5) day average (Connors' RSI(2) rule). Decided and
   traded every trading day MINUTES_BEFORE_CLOSE (10) before the NYSE close,
   on the price then; drift under REBALANCE_BAND (5%) of the budget is left
   alone. Backtested on daily closes (2012-2026, VTI/GOVT): ~11% a year
   against ~9% for a plain 60/40, Sharpe 0.84 against 0.77, the same worst
   fall (-22%); deciding at 15:50 rather than the close kept about +1% a
   year (SPY 1-minute bars, 2016-2026). A better-timed 60/40, not a trading
   edge: in a dip about one day in eight.

All four run on the strategy bots' runner (StrategyBot: saved notes and own
trades only, anyone else's position left alone, dashboard reports and
closes, the error cutoff) and the same broker adapters. A position closed
from the dashboard is taken back to its target at the next rebalance (the
next day / month). The budget isn't compounded: profits aren't reinvested
and losses aren't topped up beyond it.
"""

import math
import time
from dataclasses import dataclass

from . import clock
from .brokers.base import BrokerError
from .indicators import ema, rsi
from .runner import TRADE_REPORT_DELAYS, StrategyBot

YEAR_SECONDS = 365.25 * 86400
RETRY_SECONDS = 600          # a market that couldn't be traded (shut, wide spread, refused) is tried again this often


@dataclass
class Target:
    symbol: str
    size: float = None       # units, + long / - short / 0 flat; None = no view (history missing) - leave it be
    band: float = 0.0        # units it may be off by before it's worth a trade
    note: str = ""
    volatility: float = None  # a year, as a fraction (slow trend: for the safety stop)


# ---------------------------------------------------------------------------
# SIGNALS - plain functions on daily bars, oldest first
# ---------------------------------------------------------------------------
def trend_signal(bars: list, fast: int, slow: int, momentum_days: int) -> tuple:
    """(+1 / 0 / -1, why) - the average of the fast/slow EMA trend and the
    sign of the move over `momentum_days`; (None, why) without the history."""
    closes = [b.close for b in bars]
    if len(closes) < slow + 5:
        return None, f"only {len(closes)} daily bars, needs {slow + 5}"
    fast_now, slow_now = ema(closes, fast)[-1], ema(closes, slow)[-1]
    back = bars[-1].time - momentum_days * 86400
    earlier = [b.close for b in bars if b.time <= back]
    if not earlier:
        return None, f"less than {momentum_days} days of history"
    move = closes[-1] / earlier[-1] - 1
    trend = 1 if fast_now > slow_now else -1
    momentum = 1 if move > 0 else -1
    return (trend + momentum) // 2, (f"EMA{fast} {'above' if trend > 0 else 'below'} EMA{slow}, "
                                     f"{move:+.1%} on {momentum_days} days ago")


def annual_volatility(bars: list, span: int):
    """The usual size of a daily move, as a fraction a year: an exponentially
    weighted mean square of daily returns (span `span` bars), scaled by the
    bars there are in a year. None without `span` + 1 bars."""
    closes = [b.close for b in bars]
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(returns) < span:
        return None
    alpha = 2.0 / (span + 1)
    variance = sum(r * r for r in returns[:span]) / span
    for r in returns[span:]:
        variance += alpha * (r * r - variance)
    years = (bars[-1].time - bars[0].time) / YEAR_SECONDS
    per_year = (len(closes) - 1) / years if years > 0 else 252
    return math.sqrt(variance * per_year)


def month_end_closes(bars: list, now: float, zone: str = "new_york") -> list:
    """[(month "YYYY-MM", its last close)] for every month before `now`'s."""
    this_month = clock.local(zone, now).strftime("%Y-%m")
    closes = {}
    for bar in bars:  # daily bars start at local midnight; a few hours in is safely the same date
        month = clock.local(zone, bar.time + 6 * 3600).strftime("%Y-%m")
        if month < this_month:
            closes[month] = bar.close
    return sorted(closes.items())


def dip_state(closes: list, entry_rsi: float, trend_days: int, exit_days: int) -> tuple:
    """(in a dip after the last close?, why). A dip starts at a close with
    RSI(2) under `entry_rsi` above the `trend_days` average, and ends at a
    close above the `exit_days` average (no new one that same day). Replayed
    over all the closes, so there's nothing to remember between days.
    (None, why) without enough of them."""
    if len(closes) < trend_days + 2:
        return None, f"only {len(closes)} daily closes, needs {trend_days + 2}"
    strength = rsi(closes, 2)
    inside, started = False, None
    for i in range(trend_days - 1, len(closes)):
        if inside and closes[i] > sum(closes[i - exit_days + 1:i + 1]) / exit_days:
            inside = False
        elif not inside and strength[i] is not None and strength[i] < entry_rsi and \
                closes[i] > sum(closes[i - trend_days + 1:i + 1]) / trend_days:
            inside, started = True, i
    trend, recent = sum(closes[-trend_days:]) / trend_days, sum(closes[-exit_days:]) / exit_days
    why = (f"RSI2 {strength[-1]:.0f}, close {closes[-1]:.2f} vs {trend_days}-day average {trend:.2f}, "
           f"{exit_days}-day {recent:.2f}")
    if inside:
        days = len(closes) - 1 - started
        why += " -> in a dip " + ("from today" if not days else f"for {days} day(s)")
    else:
        why += " -> no dip"
    return inside, why


def above_average(month_closes: list, months: int) -> tuple:
    """(True/False, why): the last month-end close above the average of the
    last `months` of them; (None, why) with fewer months than that."""
    if len(month_closes) < months:
        return None, f"only {len(month_closes)} month-ends, needs {months}"
    closes = [c for _, c in month_closes[-months:]]
    average = sum(closes) / months
    last = closes[-1]
    return last > average, f"{month_closes[-1][0]} close {last:.2f} vs {months}-month average {average:.2f}"


# ---------------------------------------------------------------------------
# THE TWO STRATEGIES - when to rebalance, and to what
# ---------------------------------------------------------------------------
class PortfolioStrategy:
    key = ""
    uses_prices = False          # (what the runner's StrategyBot asks of a strategy)
    uses_signals = False
    holds_overnight = True
    history_bars = 0

    def __init__(self, params: dict):
        self.p = params

    def extra_markets(self) -> list:
        """Markets traded besides the bot's list (the rotation's cash fund)."""
        return []

    def period(self, broker, now: float):
        """The day / month a rebalance now belongs to, or None if it isn't time."""
        raise NotImplementedError

    def targets(self, markets: dict, history: dict, quotes: dict, budget: float, broker, now: float) -> dict:
        raise NotImplementedError

    def stop(self, target: Target, direction: str, entry: float, broker, market):
        """The safety stop-loss to put on a new trade, or None for none."""
        return None

    def summary(self) -> str:
        raise NotImplementedError


class SlowTrend(PortfolioStrategy):
    key = "slow-trend"

    def __init__(self, params: dict):
        super().__init__(params)
        # the slow EMA's own warm-up again, and more than a year for the momentum
        self.history_bars = max(3 * params["slow_ema"], params["momentum_days"] * 5 // 7 + 60)

    def period(self, broker, now: float):
        """Weekdays from TRADE_TIME (London) to 15 minutes before the rollover."""
        today = clock.local("london", now)
        if today.weekday() >= 5:
            return None
        start = clock.at("london", today.date(), clock.parse_hhmm(self.p["trade_time"]))
        if now < start or now >= clock.next_rollover(start) - 15 * 60:
            return None
        return today.date().isoformat()

    def targets(self, markets: dict, history: dict, quotes: dict, budget: float, broker, now: float) -> dict:
        p = self.p
        per_market = p["target_volatility"] / 100 / math.sqrt(len(markets))
        views = {}
        for symbol in markets:
            bars = history.get(symbol)
            if not bars:
                views[symbol] = Target(symbol, note="no daily bars")
                continue
            signal, why = trend_signal(bars, p["fast_ema"], p["slow_ema"], p["momentum_days"])
            volatility = annual_volatility(bars, p["volatility_bars"])
            quote = quotes.get(symbol)
            if signal is None or not volatility:
                views[symbol] = Target(symbol, note=why if signal is None else "can't measure its volatility")
                continue
            if quote is None or not quote.unit_value:
                views[symbol] = Target(symbol, note="no price to size it by")
                continue
            exposure = min(budget * per_market / volatility, budget * p["max_market_leverage"])
            views[symbol] = Target(symbol, size=signal * exposure, volatility=volatility,
                                   note=f"{why} -> {('short', 'flat', 'long')[signal + 1]} | volatility {volatility:.0%}")
        # the whole book is capped at MAX_LEVERAGE x the budget
        total = sum(abs(t.size) for t in views.values() if t.size)
        scale = min(1.0, budget * p["max_leverage"] / total) if total else 1.0
        for symbol, t in views.items():
            if t.size is None:
                continue
            market, quote = markets[symbol], quotes[symbol]
            exposure = abs(t.size) * scale
            units = broker.round_size(market, exposure / quote.unit_value)
            if t.size and not units:
                t.note += (f" | its smallest trade ({market.min_size:g}, ~{market.min_size * quote.unit_value:,.0f}) "
                           f"is over the {exposure:,.0f} it should hold - raise the budget to trade it")
            elif t.size:
                t.note += f" | {math.copysign(units, t.size):+g} units = {units * quote.unit_value:,.0f} exposure"
            t.size = math.copysign(units, t.size) if units else 0.0
            t.band = abs(t.size) * p["rebalance_band"] / 100
        return views

    def stop(self, target: Target, direction: str, entry: float, broker, market):
        distance = entry * self.p["safety_stop"] * target.volatility / math.sqrt(12)
        return broker.round_price(market, entry - distance if direction == "long" else entry + distance)

    def summary(self) -> str:
        p = self.p
        return (f"Slow trend: long / flat / short by EMA{p['fast_ema']}/{p['slow_ema']} and the move over "
                f"{p['momentum_days']} days (daily bars); sized for {p['target_volatility']:g}% a year of volatility "
                f"in all (at most {p['max_market_leverage']:g}x the budget a market, {p['max_leverage']:g}x in all); "
                f"checked weekdays from {p['trade_time']} London, resized when {p['rebalance_band']:g}% off; safety "
                f"stop {p['safety_stop']:g} months' usual move away")


class EtfRotation(PortfolioStrategy):
    key = "etf-rotation"

    def __init__(self, params: dict):
        super().__init__(params)
        self.history_bars = (params["sma_months"] + 2) * 23  # trading days

    def extra_markets(self) -> list:
        return [self.p["cash_fund"]]

    def period(self, broker, now: float):
        """Once a month, MINUTES_AFTER_OPEN into the first session the bot sees."""
        opened = broker.session_opened_at(now)
        if opened is None or now < opened + self.p["minutes_after_open"] * 60:
            return None
        return clock.local("new_york", now).strftime("%Y-%m")

    def targets(self, markets: dict, history: dict, quotes: dict, budget: float, broker, now: float) -> dict:
        p, cash = self.p, self.p["cash_fund"]
        funds = [s for s in markets if s != cash]
        share = budget / len(funds)
        views, in_cash = {}, 0.0
        for symbol in funds:
            on, why = above_average(month_end_closes(history.get(symbol) or [], now), p["sma_months"])
            if on is None:
                views[symbol] = Target(symbol, note=why)
                continue
            views[symbol] = Target(symbol, size=share if on else 0.0, note=f"{why} -> {'hold' if on else 'cash'}")
            in_cash += 0.0 if on else share
        views[cash] = Target(cash, size=in_cash, note=f"cash for {in_cash / share:.0f} of {len(funds)} funds")
        for symbol, t in views.items():
            quote = quotes.get(symbol)
            if t.size is None:
                continue
            if quote is None or not quote.mid:
                t.size, t.note = None, t.note + " | no price"
                continue
            dollars = t.size
            t.size = broker.round_size(markets[symbol], dollars / quote.mid)
            t.band = max(1.0, share * p["rebalance_band"] / 100) / quote.mid  # never under Alpaca's $1 order
            t.note += f" | ${dollars:,.2f}"
        return views

    def summary(self) -> str:
        p = self.p
        return (f"Monthly ETF rotation: equal shares, each held while its month-end close is above its "
                f"{p['sma_months']}-month average, else in {p['cash_fund']}; rebalanced once a month, "
                f"{p['minutes_after_open']} min after the open; drift under {p['rebalance_band']:g}% of a share "
                f"left alone")


class EtfTrend(PortfolioStrategy):
    key = "etf-trend"

    def __init__(self, params: dict):
        super().__init__(params)
        self.history_bars = max(3 * params["slow_ema"], params["momentum_days"] * 5 // 7 + 60)

    def extra_markets(self) -> list:
        return [self.p["cash_fund"]]

    def period(self, broker, now: float):
        """Once a week, MINUTES_AFTER_OPEN into the first session the bot sees."""
        opened = broker.session_opened_at(now)
        if opened is None or now < opened + self.p["minutes_after_open"] * 60:
            return None
        return clock.local("new_york", now).strftime("%G-W%V")

    def targets(self, markets: dict, history: dict, quotes: dict, budget: float, broker, now: float) -> dict:
        p, cash = self.p, self.p["cash_fund"]
        funds = [s for s in markets if s != cash]
        per_fund = p["target_volatility"] / 100 / math.sqrt(len(funds))
        views, dollars = {}, {}
        for symbol in funds:
            bars = history.get(symbol)
            if not bars:
                views[symbol] = Target(symbol, note="no daily bars")
                continue
            signal, why = trend_signal(bars, p["fast_ema"], p["slow_ema"], p["momentum_days"])
            volatility = annual_volatility(bars, p["volatility_bars"])
            if signal is None or not volatility:
                views[symbol] = Target(symbol, note=why if signal is None else "can't measure its volatility")
                continue
            if signal <= 0:
                views[symbol] = Target(symbol, size=0.0, note=f"{why} -> {('down', 'mixed')[signal + 1]}, not held")
                continue
            dollars[symbol] = budget * per_fund / volatility
            views[symbol] = Target(symbol, size=0.0, note=f"{why} -> up | volatility {volatility:.0%}")
        # never more than the budget in all: scaled down together, the rest in cash
        total = sum(dollars.values())
        scale = min(1.0, budget * p["max_leverage"] / total) if total else 1.0
        dollars = {s: d * scale for s, d in dollars.items()}
        dollars[cash] = max(budget - sum(dollars.values()), 0.0)
        views[cash] = Target(cash, size=0.0, note=f"cash: {dollars[cash] / budget:.0%} of the budget")
        for symbol, amount in dollars.items():
            t, quote = views[symbol], quotes.get(symbol)
            if quote is None or not quote.mid:
                t.size, t.note = None, t.note + " | no price"
                continue
            if amount < MIN_ORDER:
                t.note += f" | ${amount:,.2f} is under Alpaca's ${MIN_ORDER:g} order - raise the budget to hold it"
                continue
            t.size = broker.round_size(markets[symbol], amount / quote.mid)
            t.band = max(MIN_ORDER, amount * p["rebalance_band"] / 100) / quote.mid
            t.note += f" | ${amount:,.2f}"
        return views

    def summary(self) -> str:
        p = self.p
        return (f"Weekly ETF trend rotation: each fund held while EMA{p['fast_ema']} is above EMA{p['slow_ema']} and "
                f"it's up on {p['momentum_days']} days ago (daily bars), sized for {p['target_volatility']:g}% a year "
                f"of volatility in all, never more than the budget; the rest in {p['cash_fund']}; rebalanced once a "
                f"week, {p['minutes_after_open']} min after the open, when {p['rebalance_band']:g}% off")


class DipRotation(PortfolioStrategy):
    key = "dip-rotation"

    def __init__(self, params: dict):
        super().__init__(params)
        self.history_bars = params["trend_days"] + 60  # the average's own length and plenty for RSI(2)

    def period(self, broker, now: float):
        """Every trading day, from MINUTES_BEFORE_CLOSE before the NYSE close (13:00 on
        half days) to a minute before it."""
        if broker.session_opened_at(now) is None:
            return None
        day = clock.local_date("new_york", now)
        hours = clock.nyse_hours(day)
        closes = hours[1] if hours else clock.at("new_york", day, clock.NYSE_CLOSE)
        if not closes - self.p["minutes_before_close"] * 60 <= now < closes - 60:
            return None
        return day.isoformat()

    def targets(self, markets: dict, history: dict, quotes: dict, budget: float, broker, now: float) -> dict:
        p = self.p
        if len(markets) != 2:
            return {s: Target(s, note="needs two funds: shares, then bonds") for s in markets}
        shares, bonds = markets
        quote = quotes.get(shares)
        if quote is None or not quote.mid:
            return {s: Target(s, note=f"no {shares} price to decide by") for s in markets}
        # The closes before today, and today's price now as its close.
        today = clock.local_date("new_york", now)
        closes = [b.close for b in history.get(shares) or []
                  if clock.local_date("new_york", b.time + 6 * 3600) < today] + [quote.mid]
        dip, why = dip_state(closes, p["entry_rsi"], p["trend_days"], p["exit_days"])
        if dip is None:
            return {s: Target(s, note=why) for s in markets}
        weight = 1.0 if dip else p["share_percent"] / 100
        views = {}
        for symbol, dollars in ((shares, budget * weight), (bonds, budget * (1 - weight))):
            t, price = Target(symbol, note=f"{shares} {why} -> {weight:.0%} in {shares}"), quotes.get(symbol)
            if price is None or not price.mid:
                t.note += " | no price"
            else:
                t.size = broker.round_size(markets[symbol], dollars / price.mid) if dollars >= MIN_ORDER else 0.0
                t.band = max(MIN_ORDER, budget * p["rebalance_band"] / 100) / price.mid
                t.note += f" | ${dollars:,.2f}"
            views[symbol] = t
        return views

    def summary(self) -> str:
        p = self.p
        return (f"60/40 dip rotation: {p['share_percent']:g}% in the share fund, the rest in the bond fund; all in "
                f"shares during a dip (RSI2 under {p['entry_rsi']:g} above the {p['trend_days']}-day average, until "
                f"a close above the {p['exit_days']}-day average); decided and traded {p['minutes_before_close']} min "
                f"before each close; drift under {p['rebalance_band']:g}% of the budget left alone")


MIN_ORDER = 1.0  # dollars - Alpaca's smallest fractional order


def make_portfolio_strategy(key: str, params: dict) -> PortfolioStrategy:
    return {"slow-trend": SlowTrend, "etf-rotation": EtfRotation, "etf-trend": EtfTrend,
            "dip-rotation": DipRotation}[key](params)


def plan(current: float, target: Target, market, broker) -> list:
    """The orders taking a position from `current` to `target.size` units
    (signed): [("close", size or None for all), ("open", direction, size)],
    closes first. [] when it's close enough."""
    want = target.size
    if not current and not want:
        return []
    if current and (not want or (want > 0) != (current > 0)):
        steps = [("close", None)]
        if want:
            steps.append(("open", "long" if want > 0 else "short", abs(want)))
        return steps
    direction = "long" if want > 0 else "short"
    if not current:
        return [("open", direction, abs(want))]
    change = abs(want) - abs(current)
    if abs(change) <= target.band:
        return []
    size = broker.round_size(market, abs(change))
    if not size:
        return []
    if change > 0:
        return [("open", direction, size)]
    return [("close", None if size >= abs(current) - 1e-9 else size)]


# ---------------------------------------------------------------------------
# THE BOT
# ---------------------------------------------------------------------------
class RebalanceBot(StrategyBot):
    def __init__(self, settings, strategy, broker, dashboard, log):
        super().__init__(settings, strategy, broker, dashboard, log)
        self.history, self.history_for = {}, None
        self.retry_at = {}       # symbol -> not tried again before this
        self.said = {}           # symbol -> the last thing logged about skipping it, so it's said once

    def start(self) -> None:
        s, log = self.settings, self.log
        account = self.broker.connect()
        self.currency = account.currency
        names = list(dict.fromkeys(list(s.markets) + self.strategy.extra_markets()))
        self.markets = self.broker.resolve(names)
        missing = [n for n in self.strategy.extra_markets() if n.upper() not in self.markets]
        if missing or not self.markets:
            raise BrokerError(f"can't trade {', '.join(missing) or ', '.join(s.markets)} - see above")

        log.info("=" * 78)
        log.info(f"{s.name} starting - DEMO / PRACTICE ACCOUNT ONLY" + (" - DRY RUN, NO ORDERS" if s.dry_run else ""))
        log.info(account.description)
        for warning in account.warnings:
            log.warning(warning)
        log.info(f"Budget {s.budget:,.2f} {self.currency} (not compounded)")
        log.info(self.strategy.summary())
        log.info(f"Markets: {', '.join(self.markets)}")
        for symbol, market in self.markets.items():
            swaps = [self.broker.swap_percent_per_night(market, d) for d in ("long", "short")]
            if None not in swaps and any(swaps):
                log.info(f"{symbol} financing a night: long {swaps[0]:+.4f}%, short {swaps[1]:+.4f}% "
                         f"({swaps[0] * 365:+.1f}% / {swaps[1] * 365:+.1f}% a year)")
        log.info("=" * 78)
        config = {"markets": list(self.markets), "budget": s.budget, "dryRun": s.dry_run, **self.p}
        self.dashboard.describe(account=account.id, currency=self.currency, config=config,
                                account_mode=self.broker.account_mode)
        self.dashboard.broadcast_off("A portfolio bot holds all its markets at target sizes - no broadcast trades.")
        self.dashboard.accept_closes(self.close_from_dashboard)
        self.started_at = time.time()
        self.preview()

    def preview(self) -> None:
        """Log where every market stands now, so a start shows at once what
        the next rebalance would do."""
        try:
            self.load_history("preview")
            quotes = self.broker.quotes(list(self.markets.values()))
            targets = self.strategy.targets(self.markets, self.history, quotes, self.settings.budget, self.broker,
                                            time.time())
        except BrokerError as e:
            self.log.warning(f"Couldn't work out the targets yet ({e}); the first rebalance will.")
            return
        own = self.split_positions(self.markets)[0]
        for symbol, target in targets.items():
            held = self.signed(own.get(symbol))
            wanted = f"target {target.size:+g}" if target.size is not None else "no target"
            self.log.info(f"{symbol} | {target.note} | holding {held:+g}, {wanted}")
        self.history_for = None  # fetched again, fresh, at the rebalance
        if self.strategy.key == "slow-trend":
            when = f"weekdays from {self.p['trade_time']} London - today's straight away if it's past then"
        elif self.strategy.key == "etf-trend":
            when = (f"in each week's first session, {self.p['minutes_after_open']} min after the open - this "
                    f"week's straight away if the market's open")
        elif self.strategy.key == "dip-rotation":
            when = f"every trading day, {self.p['minutes_before_close']} min before the close"
        else:
            when = (f"in each month's first session, {self.p['minutes_after_open']} min after the open - this "
                    f"month's straight away if the market's open")
        self.log.info(f"Rebalancing {when} (unless already done).")

    # -- the loop -------------------------------------------------------------------
    def cycle(self) -> None:
        now = time.time()
        if self.dashboard.due(self.broker.dashboard_every):
            self.report()
        own, others = self.split_positions(self.markets)
        self.reconcile(own, others, now)
        period = self.strategy.period(self.broker, now)
        if period is None:
            return
        pending = [s for s in self.markets if not self.state.trades_on(s, period) and now >= self.retry_at.get(s, 0)]
        if pending:
            self.rebalance(period, pending, own, others, now)

    def load_history(self, period: str) -> None:
        """Every market's daily bars, once per rebalance period."""
        if self.history_for != period:
            self.history, self.history_for = {}, period
        for symbol, market in self.markets.items():
            if symbol not in self.history:
                bars = self.broker.daily_bars(market, self.strategy.history_bars)
                if bars:
                    self.history[symbol] = bars

    @staticmethod
    def signed(position) -> float:
        if position is None:
            return 0.0
        return position.size if position.direction == "long" else -position.size

    def skip(self, symbol: str, why: str, now: float, retry: bool = True) -> None:
        if self.said.get(symbol) != why:
            self.log.info(f"{symbol}: not rebalanced yet - {why}" + (" (trying again every 10 min)" if retry else ""))
            self.said[symbol] = why
        if retry:
            self.retry_at[symbol] = now + RETRY_SECONDS

    def rebalance(self, period: str, pending: list, own: dict, others: dict, now: float) -> None:
        try:
            self.load_history(period)
        except BrokerError as e:
            for symbol in pending:
                self.skip(symbol, f"couldn't get daily bars ({e})", now)
            return
        quotes = self.broker.quotes(list(self.markets.values()))
        targets = self.strategy.targets(self.markets, self.history, quotes, self.settings.budget, self.broker, now)

        orders = []  # (closes before opens, symbol, step)
        for symbol in pending:
            market, target, quote = self.markets[symbol], targets.get(symbol), quotes.get(symbol)
            held = self.signed(own.get(symbol))
            if target is None or target.size is None:
                self.log.info(f"[{period}] {symbol} | {target.note if target else 'no target'} | holding {held:+g} "
                              f"- left as it is")
                self.done(symbol, period)
                continue
            if symbol in others:
                p = others[symbol]
                self.log.info(f"[{period}] {symbol} | someone else's {p.direction} position is open - not trading "
                              f"it (give this bot an account of its own)")
                self.done(symbol, period)
                continue
            steps = plan(held, target, market, self.broker)
            if not steps:
                self.log.info(f"[{period}] {symbol} | {target.note} | holding {held:+g}, target {target.size:+g} "
                              f"- no trade")
                self.done(symbol, period)
                continue
            if quote is None or not quote.tradeable:
                self.skip(symbol, (quote.why_not if quote else "") or "no price right now", now)
                continue
            spread = quote.spread / quote.mid * 100 if quote.mid else float("inf")
            if spread > self.p["max_spread_percent"]:
                self.skip(symbol, f"the spread is {spread:.2f}% of the price (max {self.p['max_spread_percent']:g}%)",
                          now)
                continue
            self.log.info(f"[{period}] {symbol} | {target.note} | holding {held:+g} -> target {target.size:+g}")
            for step in steps:
                orders.append((0 if step[0] == "close" else 1, symbol, step))

        failed = set()
        for _, symbol, step in sorted(orders, key=lambda o: o[0]):
            if symbol not in failed and not self.execute(symbol, step, targets[symbol], quotes[symbol], now):
                failed.add(symbol)
        for symbol in {o[1] for o in orders}:
            if symbol in failed:
                self.skip(symbol, "an order didn't go through - see above", now)
            else:
                self.done(symbol, period)

    def done(self, symbol: str, period: str) -> None:
        self.state.count_trade(symbol, period)
        self.state.save()
        self.said.pop(symbol, None)
        self.retry_at.pop(symbol, None)

    def execute(self, symbol: str, step: tuple, target: Target, quote, now: float) -> bool:
        market, dry = self.markets[symbol], self.settings.dry_run
        if step[0] == "close":
            size = step[1]
            position = self.split_positions({symbol: market})[0].get(symbol)
            if position is None:
                return True  # already gone
            what = "all" if size is None else f"{size:g}"
            if dry:
                self.log.info(f"DRY RUN: would close {what} of {symbol} {position.direction} {position.size:g}.")
                return True
            self.broker.close_problem = ""
            if not self.broker.close(market, position, "rebalance", size=size):
                return False
            if size is None:
                self.state.positions.pop(symbol, None)
            self.state.save()
            self.dashboard.report_soon(*TRADE_REPORT_DELAYS)
            return True

        _, direction, size = step
        entry = quote.ask if direction == "long" else quote.bid
        stop = self.strategy.stop(target, direction, entry, self.broker, market)
        stop_text = f", safety stop {stop:g}" if stop is not None else ""
        if dry:
            self.log.info(f"DRY RUN: would open {direction} {symbol} {size:g}{stop_text}.")
            return True
        if direction == "short" and not self.broker.can_short:
            self.log.info(f"{symbol}: short skipped - {self.broker.name} can't sell short here.")
            return True
        if not self.broker.open(market, direction, size, stop, None, quote):
            return False
        notes = self.state.positions.get(symbol)
        if notes is None or notes.get("direction") != direction:
            self.state.positions[symbol] = {"direction": direction, "opened_at": now, "entry": entry, "stop": stop,
                                            "take_profit": None, "why": target.note}
        self.state.own_ids = sorted(self.broker.own_ids)
        self.state.save()
        self.dashboard.report_soon(TRADE_REPORT_DELAYS[0])
        return True
