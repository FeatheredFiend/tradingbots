"""
Technical indicators on plain lists, oldest value first. Each returns a
list as long as its input, with None where there isn't enough history yet.
The smoothed ones (EMA, ATR, RSI, ADX) are seeded with a simple average of
their first `period` values and then use the textbook recursions, so they
match what charting platforms show once there's a few periods of history.
"""

import math
from dataclasses import dataclass


@dataclass
class Bar:
    time: float        # start of the bar, Unix seconds (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def ema(values: list, period: int) -> list:
    """Exponential moving average, alpha = 2 / (period + 1)."""
    out = [None] * len(values)
    if period < 1 or len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    current = sum(values[:period]) / period
    out[period - 1] = current
    for i in range(period, len(values)):
        current += alpha * (values[i] - current)
        out[i] = current
    return out


def true_ranges(bars: list) -> list:
    """max(high - low, |high - previous close|, |low - previous close|)."""
    ranges = []
    for i, bar in enumerate(bars):
        if i == 0:
            ranges.append(bar.high - bar.low)
        else:
            previous = bars[i - 1].close
            ranges.append(max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous)))
    return ranges


def _wilder(values: list, period: int, start: int = 0) -> list:
    """Wilder's smoothing (an EMA with alpha = 1 / period) of values[start:]."""
    out = [None] * len(values)
    if len(values) - start < period:
        return out
    current = sum(values[start:start + period]) / period
    out[start + period - 1] = current
    for i in range(start + period, len(values)):
        current = (current * (period - 1) + values[i]) / period
        out[i] = current
    return out


def atr(bars: list, period: int = 14) -> list:
    """Average true range (Wilder). The first bar has no previous close, so
    smoothing starts from the second."""
    return _wilder(true_ranges(bars), period, start=1)


def rsi(closes: list, period: int = 14) -> list:
    """Relative strength index (Wilder), 0-100."""
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [0.0] + [max(b - a, 0.0) for a, b in zip(closes, closes[1:])]
    losses = [0.0] + [max(a - b, 0.0) for a, b in zip(closes, closes[1:])]
    avg_gain, avg_loss = _wilder(gains, period, start=1), _wilder(losses, period, start=1)
    for i in range(len(closes)):
        if avg_gain[i] is None:
            continue
        if avg_loss[i] == 0:
            out[i] = 100.0 if avg_gain[i] > 0 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + avg_gain[i] / avg_loss[i])
    return out


def adx(bars: list, period: int = 14) -> list:
    """Average directional index (Wilder), 0-100: trend strength, whichever
    way. Needs about 2 x period bars before it has a value."""
    n = len(bars)
    out = [None] * n
    if n < 2 * period + 1:
        return out
    plus_dm, minus_dm = [0.0], [0.0]
    for previous, bar in zip(bars, bars[1:]):
        up, down = bar.high - previous.high, previous.low - bar.low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    tr = true_ranges(bars)
    # Wilder smooths the sums (not averages) of +DM, -DM and TR; the ratio is the same.
    s_tr, s_plus, s_minus = _wilder(tr, period, 1), _wilder(plus_dm, period, 1), _wilder(minus_dm, period, 1)
    dx = [None] * n
    for i in range(n):
        if s_tr[i] is None or s_tr[i] == 0:
            continue
        plus_di, minus_di = 100 * s_plus[i] / s_tr[i], 100 * s_minus[i] / s_tr[i]
        total = plus_di + minus_di
        dx[i] = 0.0 if total == 0 else 100 * abs(plus_di - minus_di) / total
    first = next((i for i, v in enumerate(dx) if v is not None), None)
    if first is None or n - first < period:
        return out
    smoothed = _wilder([v if v is not None else 0.0 for v in dx], period, start=first)
    for i in range(first + period - 1, n):
        out[i] = smoothed[i]
    return out


def session_vwap(bars: list) -> tuple:
    """(VWAP, sigma) lists over `bars`, which should be one session's bars
    from its open: the volume-weighted mean of each bar's typical price
    (high + low + close) / 3 so far, and the volume-weighted standard
    deviation of typical price around it. CFD volume is the broker's tick
    count; bars without any are weighted 1, which makes it a plain average."""
    vwap, sigma = [], []
    weight = weighted_price = weighted_square = 0.0
    for bar in bars:
        typical = (bar.high + bar.low + bar.close) / 3
        volume = bar.volume if bar.volume and bar.volume > 0 else 1.0
        weight += volume
        weighted_price += volume * typical
        weighted_square += volume * typical * typical
        mean = weighted_price / weight
        vwap.append(mean)
        sigma.append(math.sqrt(max(weighted_square / weight - mean * mean, 0.0)))
    return vwap, sigma


def last(values: list):
    """The latest value, or None."""
    return values[-1] if values else None
