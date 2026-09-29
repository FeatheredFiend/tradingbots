#!/usr/bin/env python3
"""
Tick scalper backtest — does the "HFT-style" bot beat the spread?
==================================================================

Replays the strategy bots' tick scalper (strategy-bots/engine/scalper.py —
the bot's own code, not a copy) over real bid/ask prices, for a grid of
settings, and reports what each would have made per trade.

    python backtest/scalper_backtest.py oanda         # ig-bot-env: OANDA's 5-second bid/ask candles
    python backtest/scalper_backtest.py pepperstone   # pepperstone-bot-env: every MT5 tick

    --days 7              calendar days of history (default 7)
    --markets A,B         default: the scalper bot's markets on that broker
    --windows 30,60,120   also try other SCALPER_WINDOW_SECONDS (default: the bot's)
    --delay 1             fill one read after the signal instead of on it

Credentials come from the same env vars as the bots; other SCALPER_*
settings (session, cooldown, hold time, spread filter) are read from the
environment too, so the replay matches what the bot would run with.
Prices are cached in backtest/data/ — delete a file there to refetch.

Neither broker's history carries quite the spread its account pays: MT5's
tick history is Pepperstone's raw feed (EUR/USD 0.0-0.1 pips; the Standard
demo account quotes 1.0), and OANDA's candles show EUR/USD at 1.6 pips
against 0.8 live. So when it fetches, it compares the account's live spread
with the history's last few minutes and widens or narrows every price by
the difference (printed per market). Fetch while the markets are open, or
the history is used as it is.

How closely it mimics the bot:
- Reads: every 5 seconds on OANDA (the finest candles it has, so a little
  slower than the bot's 2), every SCALPER_POLL_SECONDS on Pepperstone,
  each at the last bid/ask before that moment. A market with no new price
  for 5 minutes counts as shut, as the bot's Pepperstone adapter does.
- Entries: the bot's own measure() and decide() on each read, with its
  cooldown, trades-per-day cap and the runner's spread-vs-stop check. The
  fill is that read's ask (buys) or bid (sells) — as if the order took no
  time at all, which flatters it (see --delay).
- Exits: stop-loss and take-profit trigger when a bid (long) or ask (short)
  between reads crosses them — as the broker's own stop / take-profit do.
  A price that jumps past the stop fills at the jumped-to price; if both
  are crossed between two reads, the stop is assumed to have come first.
  Time stop and session end close at the next read's price.
- Each market on its own: the bot's MAX_POSITIONS across markets isn't
  modelled, nor the rollover pause (the session ends before it).

"Before costs" is the same trades valued at mid prices: what they'd make
if the spread were free. The gap between that and the net figure is what
the spread took.
"""

import argparse
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
sys.path.insert(0, str(HERE.parent / "strategy-bots"))

from engine import clock  # noqa: E402 - needs the path above
from engine.brokers.base import Market, Quote  # noqa: E402
from engine.scalper import Burst, Scalper  # noqa: E402
from engine.settings import DEFAULT_MARKETS, strategy_params  # noqa: E402

STALE_SECONDS = 300
MODES = ["momentum", "reversion"]
TRIGGERS = [2.0, 4.0, 6.0]
STOPS_AND_TARGETS = [(2.0, 2.0), (3.0, 3.0), (3.0, 6.0), (6.0, 3.0)]


# ---------------------------------------------------------------------------
# DATA — per market: "events" = rows of (start, end, bid o/h/l/c, ask o/h/l/c),
# either candles (OANDA) or single ticks (start = end, o = h = l = c).
# ---------------------------------------------------------------------------
def calibrate(rows: np.ndarray, symbol: str, history_spread, live_spread, point: float) -> np.ndarray:
    """Widen (or narrow) every price's spread by the difference between what
    the account quotes now and what the history shows for the last few
    minutes - the history isn't always the account's own prices."""
    if history_spread is None or live_spread is None:
        print(f"  {symbol}: market shut, so the history's spreads can't be checked against the account's - used as is")
        return rows
    change = live_spread - history_spread
    print(f"  {symbol}: account spread now {live_spread / point:.1f} points, history {history_spread / point:.1f}"
          + (f" -> every price's spread {'widened' if change > 0 else 'narrowed'} by {abs(change) / point:.1f}"
             if abs(change) >= point / 2 else " - matches"))
    if abs(change) < point / 2:
        return rows
    rows = rows.copy()
    rows[:, 2:6] -= change / 2
    rows[:, 6:10] += change / 2
    for b, a in ((2, 6), (3, 7), (4, 8), (5, 9)):  # never ask below bid
        crossed = rows[:, a] < rows[:, b]
        rows[crossed, b] = rows[crossed, a] = (rows[crossed, b] + rows[crossed, a]) / 2
    return rows


def fetch_oanda(symbols: list, start: float, end: float) -> dict:
    import requests

    http = requests.Session()
    http.headers.update({"Authorization": f"Bearer {os.environ['OANDA_API_TOKEN']}",
                         "Accept-Datetime-Format": "UNIX"})
    base = "https://api-fxpractice.oanda.com"
    account = os.environ.get("OANDA_ACCOUNT_ID") or http.get(base + "/v3/accounts").json()["accounts"][0]["id"]
    instruments = {i["name"]: i for i in http.get(f"{base}/v3/accounts/{account}/instruments").json()["instruments"]}
    data = {}
    for symbol in symbols:
        if symbol not in instruments:
            print(f"  {symbol}: not offered on this account - skipped")
            continue
        rows, since = [], start
        while since < end:
            response = http.get(f"{base}/v3/instruments/{symbol}/candles",
                                params={"price": "BA", "granularity": "S5", "from": f"{since:.0f}", "count": 5000})
            response.raise_for_status()
            candles = [c for c in response.json().get("candles", []) if c.get("complete")]
            if not candles:
                break
            for c in candles:
                t = float(c["time"])
                if t >= end:
                    break
                b, a = c["bid"], c["ask"]
                rows.append((t, t + 5, float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]),
                             float(a["o"]), float(a["h"]), float(a["l"]), float(a["c"])))
            since = float(candles[-1]["time"]) + 5
            time.sleep(0.05)
        rows = np.array(rows, dtype=float).reshape(-1, 10)
        # OANDA's candles can carry a wider spread than its live prices (EUR/USD:
        # 1.6 pips against 0.8 on 29 September 2026).
        recent = http.get(f"{base}/v3/instruments/{symbol}/candles",
                          params={"price": "BA", "granularity": "S5", "count": 120}).json().get("candles", [])
        history = (float(np.median([float(c["ask"]["c"]) - float(c["bid"]["c"]) for c in recent]))
                   if recent and time.time() - float(recent[-1]["time"]) < 600 else None)
        live = []
        for _ in range(5):
            price = http.get(f"{base}/v3/accounts/{account}/pricing", params={"instruments": symbol}).json()["prices"][0]
            if price.get("tradeable") and price.get("bids") and price.get("asks"):
                live.append(float(price["asks"][0]["price"]) - float(price["bids"][0]["price"]))
            time.sleep(0.4)
        digits = int(instruments[symbol]["displayPrecision"])
        print(f"  {symbol}: {len(rows):,} five-second candles")
        rows = calibrate(rows, symbol, history, float(np.median(live)) if live else None, 10.0 ** -digits)
        data[symbol] = (rows, digits, 5)
    return data


def fetch_pepperstone(symbols: list, start: float, end: float, poll: float) -> dict:
    import MetaTrader5 as mt5

    path = os.environ.get("MT5_TERMINAL_PATH", "")
    if not mt5.initialize(**({"path": path} if path else {})):
        sys.exit(f"Couldn't connect to MetaTrader 5: {mt5.last_error()}")
    data = {}
    try:
        for symbol in symbols:
            info = mt5.symbol_info(symbol)
            if info is None or not mt5.symbol_select(symbol, True):
                print(f"  {symbol}: no such symbol - skipped")
                continue
            # Asked for in server time (New York + 7h) with a day's slack either side.
            ticks = mt5.copy_ticks_range(symbol, datetime.fromtimestamp(start - 86400, timezone.utc),
                                         datetime.fromtimestamp(end + 86400, timezone.utc), mt5.COPY_TICKS_INFO)
            if ticks is None or len(ticks) == 0:
                print(f"  {symbol}: no ticks ({mt5.last_error()}) - skipped")
                continue
            server = ticks["time_msc"].astype(float) / 1000
            hours = np.floor(server / 3600)
            offsets = {h: (clock.utc_offset_hours("new_york", h * 3600 - 3 * 3600) + 7) * 3600 for h in np.unique(hours)}
            t = server - np.vectorize(offsets.get)(hours)
            bid, ask = ticks["bid"].astype(float), ticks["ask"].astype(float)
            valid = (bid > 0) & (ask >= bid)
            # MT5's tick history is the raw feed: a Standard account's markup isn't
            # in it (EUR/USD: 0.0-0.1 pips there against 1.0 live, 29 September 2026).
            fresh = valid & (t > time.time() - 600)
            history = float(np.median((ask - bid)[fresh][-200:])) if fresh.any() else None
            live = []
            for _ in range(5 if history is not None else 0):  # only while the market's open
                tick = mt5.symbol_info_tick(symbol)
                if tick is not None and tick.bid > 0:
                    live.append(tick.ask - tick.bid)
                time.sleep(0.5)
            keep = valid & (t >= start) & (t < end)
            t, bid, ask = t[keep], bid[keep], ask[keep]
            rows = np.column_stack([t, t, bid, bid, bid, bid, ask, ask, ask, ask])
            print(f"  {symbol}: {len(rows):,} ticks")
            rows = calibrate(rows, symbol, history, float(np.median(live)) if live else None, info.point)
            data[symbol] = (rows, info.digits, poll)
    finally:
        mt5.shutdown()
    return data


def load(source: str, symbols: list, days: int, poll: float) -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    end = math.floor(time.time() / 3600) * 3600
    start = end - days * 86400
    stamp = datetime.fromtimestamp(end, timezone.utc).strftime("%Y%m%d%H")
    data, missing = {}, []
    for symbol in symbols:
        cache = DATA_DIR / f"scalper_{source}_{symbol}_{days}d_{stamp}.npz"
        if cache.exists():
            saved = np.load(cache)
            data[symbol] = (saved["rows"], int(saved["digits"]), float(saved["period"]))
        else:
            missing.append(symbol)
    if missing:
        print(f"Fetching {days} days of {source} prices to "
              f"{datetime.fromtimestamp(end, timezone.utc):%Y-%m-%d %H:%M} UTC ...")
        fetched = (fetch_oanda(missing, start, end) if source == "oanda"
                   else fetch_pepperstone(missing, start, end, poll))
        for symbol, (rows, digits, period) in fetched.items():
            np.savez_compressed(DATA_DIR / f"scalper_{source}_{symbol}_{days}d_{stamp}.npz",
                                rows=rows, digits=digits, period=period)
            data[symbol] = (rows, digits, period)
    return data


# ---------------------------------------------------------------------------
# READS — the events turned into what the bot would see: one read every
# `period` seconds, plus the highest and lowest bid/ask since the last one.
# ---------------------------------------------------------------------------
class Reads:
    def __init__(self, rows: np.ndarray, period: float):
        rows = rows[np.argsort(rows[:, 1], kind="stable")]
        t0 = math.floor(rows[0, 1] / period) * period - period
        # Read k happens at t0 + (k + 1) x period and sees every price known by then.
        slot = (np.ceil((rows[:, 1] - t0) / period) - 1).astype(np.int64)
        n = int(slot[-1]) + 1
        present, first = np.unique(slot, return_index=True)
        last = np.r_[first[1:], len(slot)] - 1

        def spread_out(values):  # one value per slot, carried forward into slots without prices
            full = np.full(n, np.nan)
            full[present] = values
            index = np.where(np.isnan(full), -1, np.arange(n))
            return full[np.maximum.accumulate(index)]

        self.end = t0 + (np.arange(n) + 1) * period
        seen = spread_out(rows[last, 1])
        self.tradeable = (self.end - seen) <= STALE_SECONDS
        self.bid_close, self.ask_close = spread_out(rows[last, 5]), spread_out(rows[last, 9])
        has = np.zeros(n, bool)
        has[present] = True
        # Slots without prices: open/high/low are the carried-forward close.
        self.bid_open = np.where(has, spread_out(rows[first, 2]), self.bid_close)
        self.ask_open = np.where(has, spread_out(rows[first, 6]), self.ask_close)
        self.bid_high = np.where(has, spread_out(np.maximum.reduceat(rows[:, 3], first)), self.bid_close)
        self.bid_low = np.where(has, spread_out(np.minimum.reduceat(rows[:, 4], first)), self.bid_close)
        self.ask_high = np.where(has, spread_out(np.maximum.reduceat(rows[:, 7], first)), self.ask_close)
        self.ask_low = np.where(has, spread_out(np.minimum.reduceat(rows[:, 8], first)), self.ask_close)
        self.n = n


def measure(reads: Reads, market: Market, params: dict) -> dict:
    """The bot's measure() on every read: arrays of move / usual spread /
    at-the-extreme, NaN where it wouldn't look (shut, out of session...)."""
    strategy = Scalper(params)
    move, unit, extreme = np.full(reads.n, np.nan), np.full(reads.n, np.nan), np.zeros(reads.n, bool)
    for k in range(reads.n):
        now = float(reads.end[k])
        strategy.on_price(market, Quote(float(reads.bid_close[k]), float(reads.ask_close[k]), bool(reads.tradeable[k])),
                          now)
        burst, _ = strategy.measure(market, now)
        if burst is not None:
            move[k], unit[k], extreme[k] = burst.move, burst.unit, burst.at_extreme
    return {"move": move, "unit": unit, "extreme": extreme}


# ---------------------------------------------------------------------------
# REPLAY
# ---------------------------------------------------------------------------
def simulate(reads: Reads, bursts: dict, params: dict, delay: int = 0) -> list:
    strategy = Scalper(params)
    move, unit, extreme = bursts["move"], bursts["unit"], bursts["extreme"]
    trigger = params["trigger_spreads"]
    candidates = np.flatnonzero(~np.isnan(move) & (np.abs(move) >= trigger))
    trades, quiet_until, per_day, busy_until = [], 0.0, {}, -1
    for k in candidates:
        now = float(reads.end[k])
        if k <= busy_until or now < quiet_until:
            continue
        burst = Burst(float(move[k]), float(unit[k]), float(reads.bid_close[k]), float(reads.ask_close[k]), bool(extreme[k]))
        signal = strategy.decide(burst).signal
        if signal is None:
            continue
        quiet_until = now + params["cooldown_seconds"]
        day = clock.local_date("london", now).isoformat()
        if per_day.get(day, 0) >= params["max_trades_per_day"]:
            continue
        e = k + delay
        if e >= reads.n or not reads.tradeable[e]:
            continue
        long = signal.direction == "long"
        sign = 1 if long else -1
        entry = float(reads.ask_close[e] if long else reads.bid_close[e])
        distance = (entry - signal.stop) * sign
        spread = float(reads.ask_close[e] - reads.bid_close[e])
        if distance <= 0 or spread > params["max_spread_percent"] / 100 * distance:
            continue
        target = entry + sign * signal.reward_risk * distance
        trade = exit_trade(reads, e, long, entry, signal.stop, target, params, strategy)
        if trade is None:
            continue
        per_day[day] = per_day.get(day, 0) + 1
        busy_until = trade.pop("exit_slot")
        trade.update(entry=entry, distance=distance, unit=burst.unit, sign=sign, opened=float(reads.end[e]),
                     entry_mid=(reads.bid_close[e] + reads.ask_close[e]) / 2)
        trades.append(trade)
    return trades


def exit_trade(reads: Reads, e: int, long: bool, entry: float, stop: float, target: float, params: dict, strategy):
    opened = float(reads.end[e])
    for j in range(e + 1, reads.n):
        if not reads.tradeable[j]:
            continue
        if long:
            if reads.bid_low[j] <= stop:
                price, reason = min(stop, reads.bid_open[j]), "stop"
            elif reads.bid_high[j] >= target:
                price, reason = max(target, reads.bid_open[j]), "target"
            else:
                price = None
        else:
            if reads.ask_high[j] >= stop:
                price, reason = max(stop, reads.ask_open[j]), "stop"
            elif reads.ask_low[j] <= target:
                price, reason = min(target, reads.ask_open[j]), "target"
            else:
                price = None
        now = float(reads.end[j])
        if price is None and (now - opened >= params["max_hold_seconds"] or not strategy.in_session(now)):
            price, reason = (reads.bid_close[j] if long else reads.ask_close[j]), "time"
        if price is not None:
            spread = reads.ask_close[j] - reads.bid_close[j]
            return {"exit": float(price), "reason": reason, "closed": now, "exit_slot": j,
                    "exit_mid": float(price + (spread / 2 if long else -spread / 2))}
    return None  # still open when the data ends


def summarize(trades: list) -> dict:
    if not trades:
        return {"trades": 0}
    pnl = np.array([t["sign"] * (t["exit"] - t["entry"]) for t in trades])
    gross = np.array([t["sign"] * (t["exit_mid"] - t["entry_mid"]) for t in trades])
    distance = np.array([t["distance"] for t in trades])
    unit = np.array([t["unit"] for t in trades])
    entry = np.array([t["entry"] for t in trades])
    return {
        "trades": len(trades),
        "win": float(np.mean(pnl > 0)),
        "r": float(np.mean(pnl / distance)),
        "total_r": float(np.sum(pnl / distance)),
        "spreads": float(np.mean(pnl / unit)),
        "gross": float(np.mean(gross / unit)),
        "pct": float(np.mean(pnl / entry * 100)),
        "hold": float(np.mean([t["closed"] - t["opened"] for t in trades])),
        "reasons": {r: sum(t["reason"] == r for t in trades) for r in ("target", "stop", "time")},
    }


def row(label: str, s: dict) -> str:
    if not s["trades"]:
        return f"{label:<40} {0:>6}"
    return (f"{label:<40} {s['trades']:>6} {s['win']:>6.0%} {s['r']:>+8.3f} {s['total_r']:>+8.1f} {s['spreads']:>+8.2f} "
            f"{s['gross']:>+8.2f} {s['pct']:>+9.4f} {s['hold']:>6.0f}")


HEADER = (f"{'':<40} {'trades':>6} {'wins':>6} {'R/trade':>8} {'total R':>8} {'net':>8} {'pre-cost':>8} "
          f"{'% /trade':>9} {'hold s':>6}\n{'':<40} {'':>6} {'':>6} {'':>8} {'':>8} {'(spreads/trade)':>17} {'':>9}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("source", choices=["oanda", "pepperstone"])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--markets", default="")
    parser.add_argument("--windows", default="")
    parser.add_argument("--delay", type=int, default=0)
    args = parser.parse_args()

    base = strategy_params("scalper")
    symbols = [s.strip() for s in (args.markets or DEFAULT_MARKETS[(args.source, "scalper")]).split(",") if s.strip()]
    data = load(args.source, symbols, args.days, base["poll_seconds"])
    windows = [int(w) for w in args.windows.split(",") if w.strip()] or [base["window_seconds"]]
    started = time.time()

    reads, bursts = {}, {}
    for symbol, (rows, digits, period) in data.items():
        if len(rows) < 2:
            continue
        reads[symbol] = Reads(rows, period)
        market = Market(symbol, symbol, symbol, 0.0, 0.0, digits=digits)
        for window in windows:
            bursts[(symbol, window)] = measure(reads[symbol], market, {**base, "window_seconds": window})
        first, last = reads[symbol].end[0], reads[symbol].end[-1]
        print(f"  {symbol}: {reads[symbol].n:,} reads every {period:g}s, "
              f"{datetime.fromtimestamp(first, timezone.utc):%a %d %b %H:%M} to "
              f"{datetime.fromtimestamp(last, timezone.utc):%a %d %b %H:%M} UTC")
    print(f"(measured in {time.time() - started:.0f}s)\n")

    default_key = (base["mode"], base["trigger_spreads"], base["stop_spreads"], base["take_profit_spreads"])
    print(f"Every market together, {args.days} days, {base['session_start']}-{base['session_end']} London, "
          f"out after {base['max_hold_seconds']}s, cooldown {base['cooldown_seconds']}s"
          + (f", filled {args.delay} read(s) late" if args.delay else ", filled on the signal's read"))
    for window in windows:
        print(f"\nWindow {window}s — mode / trigger / stop / take-profit (in usual spreads)")
        print(HEADER)
        for mode in MODES:
            for trigger in TRIGGERS:
                for stop, target in STOPS_AND_TARGETS:
                    params = {**base, "window_seconds": window, "mode": mode, "trigger_spreads": trigger,
                              "stop_spreads": stop, "take_profit_spreads": target}
                    trades = [t for symbol in reads for t in simulate(reads[symbol], bursts[(symbol, window)], params,
                                                                      args.delay)]
                    mark = " <- bot default" if (mode, trigger, stop, target) == default_key and \
                        window == base["window_seconds"] else ""
                    print(row(f"{mode:<9} {trigger:>3g} | {stop:g} / {target:g}{mark}", summarize(trades)))

    window = base["window_seconds"] if base["window_seconds"] in windows else windows[0]
    print(f"\nThe bot's defaults ({base['mode']}, trigger {base['trigger_spreads']:g}, stop {base['stop_spreads']:g} / "
          f"take-profit {base['take_profit_spreads']:g}, window {window}s), market by market")
    print(HEADER)
    for symbol in reads:
        s = summarize(simulate(reads[symbol], bursts[(symbol, window)], {**base, "window_seconds": window}, args.delay))
        print(row(symbol, s) + (f"   ({s['reasons']['target']} targets, {s['reasons']['stop']} stops, "
                                f"{s['reasons']['time']} timed out)" if s["trades"] else ""))
    breakeven = base["stop_spreads"] / (base["stop_spreads"] + base["take_profit_spreads"])
    print(f"\nAt stop {base['stop_spreads']:g} / take-profit {base['take_profit_spreads']:g} a trade needs to win more "
          f"than {breakeven:.0%} of the time before costs, and more after (it starts a spread down).")
    print(f"Done in {time.time() - started:.0f}s.")


if __name__ == "__main__":
    main()
