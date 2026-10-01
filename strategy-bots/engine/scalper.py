"""
4. Tick scalper (scalper, settings SCALPER_*) - the "HFT-style" bot
----------------------------------------------------------------------
Real high-frequency trading (microsecond reactions, servers in the
exchange's building, order-book data, rebates for providing liquidity)
can't be done through a retail broker's API: every price read and order
here is a web request taking a tenth of a second or more, and the broker
quotes its own spread. This is the nearest thing that can: a scalper that
reads live bid/ask prices every few seconds instead of waiting for bars,
trades short bursts, and is out again within minutes.

Everything is measured in spreads, because the spread is what a scalper
has to beat. "The usual spread" (U) is the median bid/ask spread of the
last five minutes' reads, and at least one price step.

- Reads: every POLL_SECONDS (2; IG at least 10 - its request allowance),
  one read of every market's bid and ask. A gap in the reads (a slow
  broker, a sleeping PC) starts the history again.
- Session: entries only from SESSION_START to SESSION_END, London time
  (07:00-21:00: London and New York, never the thin Asian hours), on
  weekdays; anything still open at SESSION_END is closed.
- Trigger: the mid price has moved at least TRIGGER_SPREADS (4) x U over
  the last WINDOW_SECONDS (60) and is at the window's high (after a rise)
  or low (after a fall) right now - the burst is still going.
- MODE momentum (the default) goes with the burst; reversion fades it.
- Spread filter: no entry while the spread is over MAX_SPREAD_RATIO (1.5)
  x U - spreads widen on news, exactly when a burst shows up.
- Stop-loss STOP_SPREADS (3) x U and take-profit TAKE_PROFIT_SPREADS (3)
  x U from the fill. The bot checks both on every read itself, and they
  also ride on the order as the broker's stop-loss and take-profit -
  pushed out to the broker's minimum distance where it has one (IG: 2
  pips on EUR/USD), so they still protect the trade if the bot stops.
- Time stop: closed after MAX_HOLD_SECONDS (300) whatever happens.
- Pace: after a signal in a market, COOLDOWN_SECONDS (60) before the next
  one there; at most MAX_TRADES_PER_DAY (30) per market.

With the stop and target a few spreads away, a trade starts one spread
down (it buys at the ask and sells at the bid), so it needs to be right
well over half the time just to break even. backtest/scalper_backtest.py
measures that on real tick prices.
"""

from collections import deque
from dataclasses import dataclass

from . import clock
from .strategies import Assessment, Signal, Skip, Strategy

SPREAD_LOOKBACK_SECONDS = 300    # "the usual spread" is the median over this long
GAP_SECONDS = 30                 # reads further apart than this (or 5 polls) start the history again


@dataclass
class Burst:
    """A market's latest read, measured against its window."""
    move: float                  # the mid's change over the window, in usual spreads (+ = up)
    unit: float                  # the usual spread
    bid: float
    ask: float
    at_extreme: bool             # the mid is at the window's high (after a rise) or low (after a fall)

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class Scalper(Strategy):
    key = "scalper"
    holds_overnight = False
    uses_prices = True           # trades on every read of the live prices, not on closed bars
    checks_own_levels = True     # its stop is a few spreads away - inside some brokers' minimum distance
    takes_broadcasts = True

    def __init__(self, params: dict):
        # No bars, so none of the base class's timeframe set-up.
        self.p = params
        self.timeframe = f"{params['poll_seconds']:g}s"
        self.bar_seconds = None
        self.session_start = clock.parse_hhmm(params["session_start"])
        self.session_end = clock.parse_hhmm(params["session_end"])
        self.keep = max(params["window_seconds"], SPREAD_LOOKBACK_SECONDS)
        self.gap = max(GAP_SECONDS, 5 * params["poll_seconds"])
        self.prices = {}         # symbol -> deque of (time, bid, ask), oldest first

    def feeds(self) -> dict:
        return {}

    def in_session(self, now: float) -> bool:
        london = clock.local("london", now)
        return london.weekday() < 5 and self.session_start <= london.time() < self.session_end

    # -- reads ----------------------------------------------------------------------
    def on_price(self, market, quote, now: float) -> None:
        """Remember one read of the market's price."""
        history = self.prices.setdefault(market.symbol, deque())
        if not quote.tradeable or quote.bid <= 0 or quote.ask < quote.bid:
            history.clear()  # shut, or a broken price: the window starts again when it's back
            return
        if history and now - history[-1][0] > self.gap:
            history.clear()
        history.append((now, quote.bid, quote.ask))
        while history[0][0] < now - self.keep:
            history.popleft()

    @staticmethod
    def usual_spread(market, history) -> float:
        spreads = sorted(ask - bid for _, bid, ask in history)
        step = 10.0 ** -market.digits if market.digits is not None else 0.0
        return max(spreads[len(spreads) // 2], step)

    def measure(self, market, now: float) -> tuple:
        """(Burst, "") for the market's latest read, or (None, why not)."""
        p = self.p
        history = self.prices.get(market.symbol)
        if not history:
            return None, "no price yet"
        if not self.in_session(now):
            return None, f"outside the {p['session_start']}-{p['session_end']} London session"
        window = p["window_seconds"]
        if now - history[0][0] < window:
            return None, f"collecting prices ({now - history[0][0]:.0f}s of the {window}s window)"
        unit = self.usual_spread(market, history)
        if unit <= 0:
            return None, "no spread to measure by yet"
        _, bid, ask = history[-1]
        mid, start = (bid + ask) / 2, now - window
        mids = [(b + a) / 2 for t, b, a in history if t >= start]
        move = (mid - mids[0]) / unit
        at_extreme = mid >= max(mids) if move > 0 else mid <= min(mids)
        return Burst(move, unit, bid, ask, at_extreme), ""

    def decide(self, burst: Burst) -> Assessment:
        """Whether a measured burst is a trade, and which way."""
        p = self.p
        note = (f"{p['window_seconds']}s move {burst.move:+.1f} spreads | spread {burst.spread:.5g} "
                f"(usual {burst.unit:.5g})")
        if burst.spread > p["max_spread_ratio"] * burst.unit:
            return Assessment(note=note + f" | spread over {p['max_spread_ratio']:g}x the usual - waiting")
        if abs(burst.move) < p["trigger_spreads"]:
            return Assessment(note=note)
        if not burst.at_extreme:
            return Assessment(note=note + " | the burst is already fading")
        momentum = p["mode"] == "momentum"
        direction = "long" if (burst.move > 0) == momentum else "short"
        sign = 1 if direction == "long" else -1
        entry = burst.ask if direction == "long" else burst.bid
        return Assessment(
            signal=Signal(direction, stop=entry - sign * p["stop_spreads"] * burst.unit,
                          reward_risk=p["take_profit_spreads"] / p["stop_spreads"], score=abs(burst.move),
                          why=f"{direction} {'with' if momentum else 'against'} a {burst.move:+.1f}-spread move "
                              f"in {p['window_seconds']}s"),
            note=note + f" | SCALP {direction.upper()}",
        )

    def assess_price(self, market, now: float) -> Assessment:
        burst, why_not = self.measure(market, now)
        return self.decide(burst) if burst is not None else Assessment(note=why_not)

    # -- exits ----------------------------------------------------------------------
    def exit_on_time(self, market, position, state: dict, now: float):
        opened = state.get("opened_at") or position.opened_at or now
        if now - opened >= self.p["max_hold_seconds"]:
            return f"time stop - held {now - opened:.0f}s"
        if not self.in_session(now):
            return f"session over ({self.p['session_end']} London) - scalps are never held past it"
        return None

    def manual_signal(self, market, bars: dict, quote, direction: str, now: float) -> Signal:
        """Its stop and take-profit, in usual spreads - measured over the last
        five minutes' reads, or on a market it hasn't been reading (a
        broadcast's guest), the spread right now."""
        p = self.p
        history = self.prices.get(market.symbol)
        step = 10.0 ** -market.digits if market.digits is not None else 0.0
        unit = self.usual_spread(market, history) if history else max(quote.spread, step)
        if unit <= 0:
            raise Skip("closed", "no spread to measure its stop-loss by")
        sign = 1 if direction == "long" else -1
        entry = quote.ask if sign > 0 else quote.bid
        return Signal(direction, stop=entry - sign * p["stop_spreads"] * unit,
                      reward_risk=p["take_profit_spreads"] / p["stop_spreads"],
                      why=f"broadcast {direction}: stop {p['stop_spreads']:g} spreads ({unit:.5g} each)")

    def exits(self) -> str:
        p = self.p
        return (f"take-profit {p['take_profit_spreads']:g} spreads; out after {p['max_hold_seconds']}s, or at "
                f"{p['session_end']} London")

    def summary(self) -> str:
        p = self.p
        return (f"Reads every {p['poll_seconds']:g}s | {p['mode']}: a {p['trigger_spreads']:g}-spread move in "
                f"{p['window_seconds']}s | stop {p['stop_spreads']:g} / take-profit {p['take_profit_spreads']:g} "
                f"spreads | spread max {p['max_spread_ratio']:g}x the usual | out after {p['max_hold_seconds']}s | "
                f"cooldown {p['cooldown_seconds']}s | {p['max_trades_per_day']} trades/day per market | "
                f"{p['session_start']}-{p['session_end']} London")
