"""
The three CFD strategies, written once for every broker. Each looks at a
market's closed bars and says what to do; runner.py checks the spread,
swap and slots, sizes the trade and sends it.

Every price here is the broker's own (mid) price; every time is UTC Unix
seconds. "ATR" is the 14-bar average true range of the timeframe named.

1. Forex session breakout (session-breakout, settings BREAKOUT_*)
--------------------------------------------------------------------
High-volatility currency pairs (GBP/USD, EUR/USD, GBP/JPY, EUR/JPY),
traded only in the London / New York overlap - never in the quiet Asian
session - and always flat before the daily rollover, so no swap is paid.

- Range: the high (RH) and low (RL) of the 15-minute bars from
  RANGE_START to RANGE_END, London time (07:00-13:00: London's morning,
  up to New York's arrival). Width W = RH - RL.
- Filters: W must be MIN_RANGE_PERCENT..MAX_RANGE_PERCENT of the price
  (0.15%-1.0%: a coiled range, not a move that has already happened);
  with TREND_EMA > 0, longs need the close above the 50-bar EMA and shorts
  below it; at most MAX_TRADES_PER_DAY (1) per pair per day.
- Trigger: a bar that starts at or after RANGE_END and closes by
  ENTRY_END (16:00) closes above RH + BUFFER_ATR x ATR (0.2) -> long, or
  below RL - 0.2 x ATR -> short, but not more than MAX_EXTENSION x W
  (0.5) beyond the range edge (no chasing).
- Stop-loss: STOP_RANGE_FRACTION (0.5) of the range back from the broken
  edge - the range's midpoint by default.
- Take-profit: REWARD_RISK (1.5) x the stop distance from the fill.
- Time exit: every position closes at FLAT_TIME (20:00 London), before
  the 17:00 New York rollover (22:00 UK most of the year).

2. Index mean reversion (index-reversion, settings REVERSION_*)
------------------------------------------------------------------
Major indices (S&P 500, FTSE 100, DAX), fading overextended intraday
moves back towards the day's mean, only during each index's cash session
and always closed the same day - so never an overnight swap.

- Mean: the session VWAP - the volume-weighted average of each bar's
  typical price (H + L + C) / 3 since the cash open (CFD volume is the
  tick count). Sigma: the volume-weighted standard deviation around it.
- Bands: VWAP +/- BAND_STDEV (2.0) x sigma.
- Filters: no entries in the first SKIP_OPEN_MINUTES (60) of the session
  or the last LAST_ENTRY_MINUTES (60); ADX(14) at most MAX_ADX (25) -
  mean reversion is for ranging days, not trending ones (0 turns this
  off); at most MAX_TRADES_PER_DAY (2) per index per session.
- Trigger: a closed bar at or below the lower band with RSI(RSI_PERIOD,
  14) at or below RSI_OVERSOLD (30) -> long; at or above the upper band
  with RSI at or above RSI_OVERBOUGHT (70) -> short.
- Stop-loss: STOP_ATR (1.5) x ATR from the close.
- Take-profit: the VWAP at entry; the trade is skipped unless that's at
  least MIN_REWARD_RISK (1.0) x the stop distance away.
- Exits: a bar closing back through the (moving) VWAP; MAX_HOLD_BARS (8)
  bars without reverting; and FLAT_MINUTES (15) before the cash close,
  whatever happens.

3. Commodity multi-timeframe trend (commodity-trend, settings TREND_*)
------------------------------------------------------------------------
Gold and Brent crude: the trend is confirmed on 4-hour bars and entered
on 15-minute bars. This one holds overnight, so it watches the swap.

- Trend (HIGHER_TIMEFRAME, 4-hour): up when the close is above
  EMA(HTF_SLOW_EMA, 200), EMA(HTF_FAST_EMA, 50) is above EMA(200) and
  ADX(14) is at least MIN_ADX (20; 0 turns that off). Down is the mirror.
- Trigger (TIMEFRAME, 15-minute): EMA(FAST_EMA, 9) crosses above
  EMA(SLOW_EMA, 21) on the latest closed bar in an up trend -> long;
  crosses below in a down trend -> short.
- Stop-loss: STOP_ATR (2.0) x the 15-minute ATR from the close.
- Take-profit: REWARD_RISK (3.0) x the stop distance (0 = none - let the
  trailing stop decide).
- Exits: a 15-minute close more than TRAIL_ATR (3.0) x ATR back from the
  best price since entry (a chandelier trailing stop; 0 = off); a 4-hour
  close back through EMA(200); MAX_HOLD_DAYS (10) days held; and, with
  WEEKEND_FLAT on, Friday 20:00 London (no entries after Friday 16:00).
- Swap: an entry is skipped when the broker's overnight financing for
  that direction costs more than MAX_SWAP_PERCENT (0.05%) of the trade's
  value per night.
"""

import re
from dataclasses import dataclass

from . import clock
from .indicators import adx, atr, ema, last, rsi, session_vwap
from .settings import TIMEFRAMES, SettingsError

ATR_PERIOD = 14
ADX_PERIOD = 14


@dataclass
class Signal:
    direction: str                 # "long" or "short"
    stop: float                    # stop-loss price
    take_profit: float = None      # a fixed take-profit price, or
    reward_risk: float = None      # a take-profit this many stop distances from the fill (None = no take-profit)
    why: str = ""
    score: float = 0.0             # ranks signals on the same bar; higher first


@dataclass
class Assessment:
    signal: Signal = None
    note: str = ""                 # the market's state, for the log


def _fmt(price: float) -> str:
    return f"{price:.5g}" if abs(price) < 1000 else f"{price:,.1f}"


class Strategy:
    key = ""
    holds_overnight = False

    def __init__(self, params: dict):
        self.p = params
        self.timeframe = params["timeframe"]
        self.bar_seconds = TIMEFRAMES[self.timeframe]

    def feeds(self) -> dict:
        """{role: (timeframe, bars to keep)} - "exec" is the one traded on."""
        raise NotImplementedError

    def prepare(self, markets: dict, log) -> dict:
        """Last look at the resolved markets before trading; may drop some."""
        return markets

    def assess(self, market, bars: dict, now: float) -> Assessment:
        raise NotImplementedError

    def exit_on_bar(self, market, bars: dict, position, state: dict, now: float):
        """A reason to close `position` now its market's bar has closed, or None.
        `state` is the bot's saved notes on the position; it may be updated."""
        return None

    def exit_on_time(self, market, position, state: dict, now: float):
        """A reason to close `position` at this moment, whatever the bars say, or None."""
        return None

    def on_open(self, market, signal: Signal, entry: float, state: dict) -> None:
        """Notes to keep about a position the bot just opened."""

    def trade_day(self, market, now: float) -> str:
        """The day MAX_TRADES_PER_DAY counts trades in."""
        return clock.local_date("london", now).isoformat()

    def summary(self) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 1. Forex session breakout
# ---------------------------------------------------------------------------
class SessionBreakout(Strategy):
    key = "session-breakout"

    def __init__(self, params: dict):
        super().__init__(params)
        self.range_start = clock.parse_hhmm(params["range_start"])
        self.range_end = clock.parse_hhmm(params["range_end"])
        self.entry_end = clock.parse_hhmm(params["entry_end"])
        self.flat_time = clock.parse_hhmm(params["flat_time"])
        if not (self.range_start < self.range_end < self.entry_end <= self.flat_time):
            raise SettingsError("the breakout times must run in order: BREAKOUT_RANGE_START < BREAKOUT_RANGE_END "
                                "< BREAKOUT_ENTRY_END <= BREAKOUT_FLAT_TIME (all London time)")
        if params["min_range_percent"] >= params["max_range_percent"]:
            raise SettingsError("BREAKOUT_MIN_RANGE_PERCENT must be below BREAKOUT_MAX_RANGE_PERCENT")

    def feeds(self) -> dict:
        day = 86400 // self.bar_seconds
        return {"exec": (self.timeframe, max(day + 20, 3 * self.p["trend_ema"] + 20))}

    def assess(self, market, bars: dict, now: float) -> Assessment:
        p, tf = self.p, self.bar_seconds
        series = bars["exec"]
        if len(series) < max(ATR_PERIOD + 2, p["trend_ema"] + 1):
            return Assessment(note=f"not enough bars yet ({len(series)})")
        bar = series[-1]
        day = clock.local_date("london", bar.time)
        if day.weekday() >= 5:
            return Assessment(note="weekend")
        range_start = clock.at("london", day, self.range_start)
        range_end = clock.at("london", day, self.range_end)
        entry_end = clock.at("london", day, self.entry_end)

        range_bars = [b for b in series if range_start <= b.time and b.time + tf <= range_end]
        if not range_bars:
            return Assessment(note=f"waiting for the {p['range_start']} London range to start")
        high, low = max(b.high for b in range_bars), min(b.low for b in range_bars)
        width = high - low
        width_pct = width / ((high + low) / 2) * 100
        range_text = f"range {_fmt(low)}-{_fmt(high)} ({width_pct:.2f}%)"
        if bar.time + tf <= range_end:
            return Assessment(note=f"building the {p['range_start']}-{p['range_end']} London {range_text}")
        if bar.time < range_end or bar.time + tf > entry_end:
            return Assessment(note=f"{range_text} | outside the {p['range_end']}-{p['entry_end']} London entry window")

        expected = (range_end - range_start) / tf
        if len(range_bars) < 0.8 * expected:
            return Assessment(note=f"{range_text} | only {len(range_bars)} of {expected:.0f} range bars - skipping today")
        if not p["min_range_percent"] <= width_pct <= p["max_range_percent"]:
            size = "narrow" if width_pct < p["min_range_percent"] else "wide"
            return Assessment(note=f"{range_text} | too {size} to trade "
                                   f"({p['min_range_percent']:g}-{p['max_range_percent']:g}%)")

        closes = [b.close for b in series]
        atr_now = last(atr(series, ATR_PERIOD))
        buffer = p["buffer_atr"] * atr_now
        close = bar.close
        trend = last(ema(closes, p["trend_ema"])) if p["trend_ema"] else None
        note = f"{range_text} | close {_fmt(close)}"
        if trend is not None:
            note += f" | EMA{p['trend_ema']} {_fmt(trend)}"

        if close > high + buffer:
            direction, beyond, edge = "long", close - high, high
        elif close < low - buffer:
            direction, beyond, edge = "short", low - close, low
        else:
            return Assessment(note=note + " | inside the range")

        if beyond > p["max_extension"] * width:
            return Assessment(note=note + f" | broke {'up' if direction == 'long' else 'down'} but already "
                                          f"{beyond / width:.0%} of the range past it - not chasing")
        if trend is not None and (close < trend if direction == "long" else close > trend):
            return Assessment(note=note + f" | {direction} breakout against the EMA{p['trend_ema']} trend - skipped")

        sign = 1 if direction == "long" else -1
        stop = edge - sign * p["stop_range_fraction"] * width
        return Assessment(
            signal=Signal(direction, stop=stop, reward_risk=p["reward_risk"], score=beyond / atr_now,
                          why=f"{direction} breakout of the {p['range_start']}-{p['range_end']} London range"),
            note=note + f" | BREAKOUT {direction.upper()}",
        )

    def exit_on_time(self, market, position, state: dict, now: float):
        london_now = clock.local("london", now)
        opened = state.get("opened_at") or position.opened_at or now
        if london_now.time() >= self.flat_time or london_now.date() > clock.local_date("london", opened):
            return f"{self.p['flat_time']} London flat time - never held past the rollover, so no swap"
        return None

    def summary(self) -> str:
        p = self.p
        return (f"Range {p['range_start']}-{p['range_end']} London, entries to {p['entry_end']}, flat at "
                f"{p['flat_time']} | range {p['min_range_percent']:g}-{p['max_range_percent']:g}% | "
                f"buffer {p['buffer_atr']:g} ATR | stop at {p['stop_range_fraction']:g} of the range | "
                f"take-profit {p['reward_risk']:g}R | trend EMA {p['trend_ema'] or 'off'} | "
                f"{p['max_trades_per_day']} trade(s)/day | {self.timeframe}")


# ---------------------------------------------------------------------------
# 2. Index mean reversion
# ---------------------------------------------------------------------------
SESSION_PATTERNS = [
    ("us", r"SPX|US500|US 500|US30|US 30|WALL ?ST|DOW|DJ|NAS|US100|USTEC|US TECH|US2000|US 2000|RUSSELL|"
           r"^SPY$|^QQQ$|^DIA$|^IWM$|^VOO$|^IVV$"),
    ("uk", r"UK100|UK 100|FTSE"),
    ("eu", r"DE30|DE40|GER|DAX|GERMANY|EU50|EU 50|ESTX|STOXX|FRA40|FRANCE|CAC|ESP35|SPAIN|IBEX|NETH|AEX"),
    ("jp", r"JP225|JPN225|J225|JAPAN|NIKKEI"),
]


def guess_session(*names: str):
    for key, pattern in SESSION_PATTERNS:
        if any(name and re.search(pattern, name.upper()) for name in names):
            return key
    return None


class IndexReversion(Strategy):
    key = "index-reversion"

    def feeds(self) -> dict:
        # The previous day's bars too, so RSI and ADX are warmed up at the open.
        return {"exec": (self.timeframe, max(2 * 86400 // self.bar_seconds, 4 * ADX_PERIOD + 10))}

    def prepare(self, markets: dict, log) -> dict:
        usable = {}
        for symbol, market in markets.items():
            key = market.session_hint or guess_session(market.requested, market.symbol, market.name)
            if key not in clock.INDEX_SESSIONS:
                log.warning(f"Skipping {symbol}: can't tell which exchange's hours it follows - add @us, @uk, @eu "
                            f"or @jp to its name in the market list (e.g. {market.requested}@us).")
                continue
            market.session = clock.INDEX_SESSIONS[key]
            log.info(f"{symbol} trades in the {market.session.name}")
            usable[symbol] = market
        return usable

    def _session_bars(self, market, series: list, at_time: float):
        """(open, close, bars of the session containing at_time up to it), or None."""
        bounds = market.session.bounds_at(at_time)
        if bounds is None:
            return None
        opens, closes = bounds
        tf = self.bar_seconds
        return opens, closes, [b for b in series if opens <= b.time and b.time + tf <= closes and b.time <= at_time]

    def assess(self, market, bars: dict, now: float) -> Assessment:
        p, tf = self.p, self.bar_seconds
        series = bars["exec"]
        if len(series) < 2 * ADX_PERIOD + 2:
            return Assessment(note=f"not enough bars yet ({len(series)})")
        bar = series[-1]
        session = self._session_bars(market, series, bar.time)
        if session is None or not session[2] or session[2][-1] is not bar:
            return Assessment(note="outside the cash session")
        opens, closes, session_bars = session

        vwap_values, sigma_values = session_vwap(session_bars)
        vwap, sigma = vwap_values[-1], sigma_values[-1]
        rsi_now = last(rsi([b.close for b in series], p["rsi_period"]))
        adx_now = last(adx(series, ADX_PERIOD))
        atr_now = last(atr(series, ATR_PERIOD))
        if rsi_now is None or adx_now is None or atr_now is None:
            return Assessment(note=f"not enough bars yet for RSI{p['rsi_period']} / ADX{ADX_PERIOD} ({len(series)})")
        close = bar.close
        lower, upper = vwap - p["band_stdev"] * sigma, vwap + p["band_stdev"] * sigma
        note = (f"VWAP {_fmt(vwap)}, bands {_fmt(lower)}-{_fmt(upper)} | close {_fmt(close)} | "
                f"RSI {rsi_now:.0f} | ADX {adx_now:.0f}")

        bar_close = bar.time + tf
        if bar_close < opens + p["skip_open_minutes"] * 60:
            return Assessment(note=note + f" | first {p['skip_open_minutes']} min of the session - no entries")
        if bar_close > closes - p["last_entry_minutes"] * 60:
            return Assessment(note=note + f" | last {p['last_entry_minutes']} min of the session - no entries")
        if sigma <= 0:
            return Assessment(note=note + " | no spread of prices yet")

        if close <= lower and rsi_now <= p["rsi_oversold"]:
            direction = "long"
        elif close >= upper and rsi_now >= p["rsi_overbought"]:
            direction = "short"
        elif close <= lower:
            return Assessment(note=note + f" | below the band, but RSI isn't oversold (<= {p['rsi_oversold']:g})")
        elif close >= upper:
            return Assessment(note=note + f" | above the band, but RSI isn't overbought (>= {p['rsi_overbought']:g})")
        else:
            return Assessment(note=note + " | inside the bands")

        if p["max_adx"] and adx_now > p["max_adx"]:
            return Assessment(note=note + f" | overextended {'down' if direction == 'long' else 'up'}, but ADX "
                                          f"{adx_now:.0f} > {p['max_adx']:g} says it's trending - skipped")
        sign = 1 if direction == "long" else -1
        stop = close - sign * p["stop_atr"] * atr_now
        reward, risk = abs(vwap - close), abs(close - stop)
        if reward < p["min_reward_risk"] * risk:
            return Assessment(note=note + f" | only {reward / risk:.2f}R back to the VWAP "
                                          f"(needs {p['min_reward_risk']:g}R) - skipped")
        return Assessment(
            signal=Signal(direction, stop=stop, take_profit=vwap, score=reward / sigma,
                          why=f"{direction} fade: {reward / sigma:.1f} sigma from the session VWAP, RSI {rsi_now:.0f}"),
            note=note + f" | REVERSION {direction.upper()}",
        )

    def exit_on_bar(self, market, bars: dict, position, state: dict, now: float):
        series = bars["exec"]
        bar = series[-1]
        session = self._session_bars(market, series, bar.time)
        if session and session[2] and session[2][-1] is bar:
            vwap = session_vwap(session[2])[0][-1]
            if (bar.close >= vwap) if position.direction == "long" else (bar.close <= vwap):
                return f"back at the session VWAP ({_fmt(vwap)})"
        opened = state.get("opened_at") or position.opened_at
        if self.p["max_hold_bars"] and opened:
            held = int((bar.time + self.bar_seconds - opened) // self.bar_seconds)
            if held >= self.p["max_hold_bars"]:
                return f"time stop - {held} bars without reverting"
        return None

    def exit_on_time(self, market, position, state: dict, now: float):
        opened = state.get("opened_at") or position.opened_at or now
        bounds = market.session.bounds_at(opened)
        if bounds is None or now >= bounds[1] - self.p["flat_minutes"] * 60:
            return (f"same-day close, {self.p['flat_minutes']} min before the cash session ends "
                    f"- never held overnight, so no swap")
        return None

    def trade_day(self, market, now: float) -> str:
        return clock.local_date(market.session.zone, now).isoformat()

    def summary(self) -> str:
        p = self.p
        return (f"VWAP +/- {p['band_stdev']:g} sigma | RSI{p['rsi_period']} {p['rsi_oversold']:g}/"
                f"{p['rsi_overbought']:g} | ADX max {p['max_adx'] or 'off'} | stop {p['stop_atr']:g} ATR | "
                f"take-profit at the VWAP (min {p['min_reward_risk']:g}R) | skip first {p['skip_open_minutes']} / "
                f"last {p['last_entry_minutes']} min | flat {p['flat_minutes']} min before the close | "
                f"time stop {p['max_hold_bars'] or 'off'} bars | {p['max_trades_per_day']} trade(s)/day | "
                f"{self.timeframe}")


# ---------------------------------------------------------------------------
# 3. Commodity multi-timeframe trend
# ---------------------------------------------------------------------------
class CommodityTrend(Strategy):
    key = "commodity-trend"
    holds_overnight = True

    def __init__(self, params: dict):
        super().__init__(params)
        self.higher_timeframe = params["higher_timeframe"]

    def feeds(self) -> dict:
        p = self.p
        return {
            "exec": (self.timeframe, max(3 * p["slow_ema"], ATR_PERIOD * 3, 100)),
            "htf": (self.higher_timeframe, max(int(p["htf_slow_ema"] * 1.5), p["htf_slow_ema"] + 3 * ADX_PERIOD)),
        }

    def trend(self, htf: list):
        """("up" | "down" | None, description) from the higher-timeframe bars."""
        p = self.p
        closes = [b.close for b in htf]
        fast, slow = last(ema(closes, p["htf_fast_ema"])), last(ema(closes, p["htf_slow_ema"]))
        strength = last(adx(htf, ADX_PERIOD))
        if fast is None or slow is None or strength is None:
            return None, f"only {len(htf)} {self.higher_timeframe} bars - need {p['htf_slow_ema'] + 1}"
        close = closes[-1]
        text = (f"{self.higher_timeframe} close {_fmt(close)} EMA{p['htf_fast_ema']} {_fmt(fast)} "
                f"EMA{p['htf_slow_ema']} {_fmt(slow)} ADX {strength:.0f}")
        strong = not p["min_adx"] or strength >= p["min_adx"]
        if close > slow and fast > slow and strong:
            return "up", text
        if close < slow and fast < slow and strong:
            return "down", text
        return None, text

    def assess(self, market, bars: dict, now: float) -> Assessment:
        p = self.p
        series = bars["exec"]
        trend, trend_text = self.trend(bars["htf"])
        if len(series) < p["slow_ema"] + 2:
            return Assessment(note=f"not enough {self.timeframe} bars yet ({len(series)})")
        closes = [b.close for b in series]
        fast, slow = ema(closes, p["fast_ema"]), ema(closes, p["slow_ema"])
        crossed_up = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
        crossed_down = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
        note = (f"trend {trend or 'none'} ({trend_text}) | {self.timeframe} EMA{p['fast_ema']} {_fmt(fast[-1])} "
                f"vs EMA{p['slow_ema']} {_fmt(slow[-1])}")

        if trend == "up" and crossed_up:
            direction = "long"
        elif trend == "down" and crossed_down:
            direction = "short"
        else:
            if crossed_up or crossed_down:
                note += f" | crossed {'up' if crossed_up else 'down'} against the {self.higher_timeframe} trend - skipped"
            return Assessment(note=note)

        london_now = clock.local("london", now)
        if p["weekend_flat"] and london_now.weekday() == 4 and london_now.hour >= 16:
            return Assessment(note=note + " | Friday after 16:00 London - no new trades before the weekend")
        atr_now = last(atr(series, ATR_PERIOD))
        close = closes[-1]
        sign = 1 if direction == "long" else -1
        strength = last(adx(bars["htf"], ADX_PERIOD)) or 0.0
        return Assessment(
            signal=Signal(direction, stop=close - sign * p["stop_atr"] * atr_now,
                          reward_risk=p["reward_risk"] or None, score=strength,
                          why=f"{direction}: {self.timeframe} EMA{p['fast_ema']}/{p['slow_ema']} crossover "
                              f"with the {self.higher_timeframe} trend"),
            note=note + f" | TREND {direction.upper()}",
        )

    def on_open(self, market, signal: Signal, entry: float, state: dict) -> None:
        state["best"] = entry

    def exit_on_bar(self, market, bars: dict, position, state: dict, now: float):
        p = self.p
        series = bars["exec"]
        bar = series[-1]
        long = position.direction == "long"
        best = state.get("best")
        if best is None:  # a position the bot didn't open, or from before a restart without notes
            opened = state.get("opened_at") or position.opened_at or bar.time
            since = [b for b in series if b.time + self.bar_seconds > opened] or [bar]
            best = max(b.high for b in since) if long else min(b.low for b in since)
        best = max(best, bar.high) if long else min(best, bar.low)
        state["best"] = best

        if p["trail_atr"]:
            distance = p["trail_atr"] * last(atr(series, ATR_PERIOD))
            if (bar.close < best - distance) if long else (bar.close > best + distance):
                return f"trailing stop - closed {p['trail_atr']:g} ATR back from the best price {_fmt(best)}"
        htf = bars["htf"]
        slow = last(ema([b.close for b in htf], p["htf_slow_ema"]))
        if slow is not None and ((htf[-1].close < slow) if long else (htf[-1].close > slow)):
            return f"{self.higher_timeframe} trend broken - closed {'below' if long else 'above'} EMA{p['htf_slow_ema']}"
        return None

    def exit_on_time(self, market, position, state: dict, now: float):
        p = self.p
        opened = state.get("opened_at") or position.opened_at
        if p["max_hold_days"] and opened and now - opened >= p["max_hold_days"] * 86400:
            return f"held {p['max_hold_days']:g} days - the most swap this bot pays on one trade"
        london_now = clock.local("london", now)
        if p["weekend_flat"] and london_now.weekday() == 4 and london_now.hour >= 20:
            return "Friday 20:00 London - flat for the weekend (no weekend swap or gap)"
        return None

    def summary(self) -> str:
        p = self.p
        take_profit = f"{p['reward_risk']:g}R" if p["reward_risk"] else "none"
        return (f"{self.higher_timeframe} trend: EMA{p['htf_fast_ema']}/{p['htf_slow_ema']}, ADX min "
                f"{p['min_adx'] or 'off'} | {self.timeframe} entry: EMA{p['fast_ema']}/{p['slow_ema']} crossover | "
                f"stop {p['stop_atr']:g} ATR | take-profit {take_profit} | trail {p['trail_atr'] or 'off'} ATR | "
                f"max hold {p['max_hold_days']:g} days | weekend flat {'on' if p['weekend_flat'] else 'off'} | "
                f"max swap {p['max_swap_percent']:g}%/night | {p['max_trades_per_day']} trade(s)/day")


STRATEGIES = {cls.key: cls for cls in (SessionBreakout, IndexReversion, CommodityTrend)}


def make_strategy(key: str, params: dict) -> Strategy:
    return STRATEGIES[key](params)
