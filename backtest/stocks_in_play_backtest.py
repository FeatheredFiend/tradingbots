#!/usr/bin/env python3
"""
Stocks in play backtest — the opening-range breakout of Zarattini, Barbon &
Aziz (2024), "A Profitable Day Trading Strategy For The U.S. Equity Market"
============================================================================

At 09:35 New York time every day, takes the US shares trading the most
volume in their first 5 minutes compared with their own usual first 5
minutes ("stocks in play"), and trades each one's breakout in the direction
of that first 5-minute bar: a buy stop at its high if the bar rose, a sell
stop at its low if it fell. Stop-loss a small fraction of the share's usual
daily range (ATR) from the entry; otherwise out at the close. Never held
overnight.

    python backtest/stocks_in_play_backtest.py   # alpaca-bot-env: Jan 2024 to yesterday, then cached

    --start 2024-01-02           first test day. The paper's data ended in
                                 2023, so everything from 2024 on is out of
                                 sample for it.
    --top 20                     stocks in play per day
    --spread-samples 400         trades whose spreads at entry and exit are looked up
    --per-minute 190             requests a minute. Alpaca allows 200 per account,
                                 shared with any Alpaca bot running on the same
                                 keys: use ~140 while the market is open and
                                 they're running, or they get turned away.

Credentials come from the same env vars as the Alpaca bots. Everything
fetched is cached in backtest/data/, a month at a time, so a stopped run
picks up where it was and reruns make no requests. The first run makes
roughly 6,000 requests (about 45 minutes at 140 a minute).

Rules, as in the paper:
- Universe each day: price over $5 (the first 5-minute bar's open), average
  volume over the 14 sessions before of at least 1,000,000 shares, and
  ATR(14) over $0.50.
- Relative volume: the first 5-minute bar's volume divided by the average
  of the same bar over the 14 sessions before. The --top highest with a
  relative volume of at least 1 are "in play". A first bar that closed
  where it opened gives no direction and no trade.
- Entry: a stop order at the first bar's high (long) or low (short), live
  from 09:35 to the close; filled at that level, or at a 1-minute bar's open
  if the price jumped past it. A day it never reaches is no trade.
- Stop-loss: 10% of ATR(14) from the entry (other widths are compared, and
  the first bar's opposite end). A jump past the stop fills at that bar's
  open. When the entry's own 1-minute bar also reached the stop, the usual
  guess at a bar's path decides which came first (open, low, high, close
  for a rising bar; open, high, low, close for a falling one); a second row
  assumes the worst every time.
- Exit: the stop, or the day's last 1-minute close.
- Results are per trade, in % of the price and in R (the profit or loss
  divided by the amount risked: entry to stop). The paper's sizing (1% of
  the account risked per trade, 4x leverage cap) isn't modelled.

Prices: consolidated (SIP) bars, unadjusted, so prices and volumes are what
a bot would have seen that day. A share whose price jumped 80% or halved
overnight (usually a split) is left out for the 14 sessions after, and that
day's gap is left out of its ATR.

Costs: the bid/ask spread at the moment of entry and exit, from SIP quotes
looked up for --spread-samples trades, averaged by price band (under $20,
$20-100, over $100) and applied to every trade: half going in, half coming
out. Stop orders fill as market orders, and a fast market can fill worse
than the quote; the "2x costs" column shows how much that would matter.
Alpaca charges no commission.

Shares delisted since (bought out, merged, gone bust) are in: the day a
buyout or a collapse is announced is a classic "in play" day, and leaving
them out would flatter the result. Alpaca's asset list forgets most of
them, so they're found from its corporate actions (merger targets and
shares written off since the start). A delisted ticker that an active share
has since taken is left out, as the two can't be told apart.

Not modelled: whether a share could be borrowed to short (Alpaca shorts
easy-to-borrow shares only, in whole shares, on a $2,000+ margin account);
halts; and filling worse than the quote.
"""

import argparse
import hashlib
import math
import os
import pickle
import re
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import movers_backtest  # noqa: E402 - needs the path above
from movers_backtest import (DATA_DIR, EXCHANGES, NY, TRADING, WORKERS, Alpaca, cached,  # noqa: E402
                             candidates, chunks, fetch_entry_spreads, iso)

LOOKBACK = 14                     # sessions, for relative volume, average volume and ATR
WARM_UP_SESSIONS = 20             # before the first test day
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 1_000_000
MIN_ATR = 0.50
MIN_RELATIVE_VOLUME = 1.0
SPLIT_JUMP = (0.55, 1.8)          # open / previous close outside this: treated as a split
STOPS = [0.05, 0.10, 0.20, 0.50, "bar", None]   # fraction of ATR; "bar" = the first bar's other end; None = no stop
PAPER_STOP = 0.10
PRICE_BANDS = [(0, 20), (20, 100), (100, math.inf)]
CORPORATE_ACTIONS = "https://data.alpaca.markets/v1/corporate-actions"


# ---------------------------------------------------------------------------
# DATA
# ---------------------------------------------------------------------------
def sessions(api: Alpaca, first_test: date, last: date) -> list:
    calendar = api.get(f"{TRADING}/calendar", {"start": (first_test - timedelta(days=45)).isoformat(),
                                                "end": last.isoformat()})
    days = []
    for c in calendar:
        day = date.fromisoformat(c["date"])
        opens = datetime.combine(day, datetime.strptime(c["open"], "%H:%M").time(), NY)
        closes = datetime.combine(day, datetime.strptime(c["close"], "%H:%M").time(), NY)
        days.append({"date": c["date"], "open": opens.timestamp(), "close": closes.timestamp(),
                     "test": day >= first_test})
    warm_up = [d for d in days if not d["test"]][-WARM_UP_SESSIONS:]
    return warm_up + [d for d in days if d["test"]]


def fetch_bars(api: Alpaca, symbols: list, timeframe: str, first: str, last: str, row, batch: int = 200) -> dict:
    """{symbol: [row(bar), ...]} over [first, last], batches in parallel."""
    def one(names):
        found = {}
        params = {"symbols": ",".join(names), "timeframe": timeframe, "start": first, "end": last,
                  "feed": "sip", "adjustment": "raw", "limit": 10000}
        for page in api.pages("/stocks/bars", params, "bars"):
            for symbol, bars in page.items():
                found.setdefault(symbol, []).extend(row(b) for b in bars)
        return found

    out = {}
    batches = list(chunks(symbols, batch))
    with ThreadPoolExecutor(WORKERS) as pool:
        for n, found in enumerate(pool.map(one, batches), 1):
            for symbol, rows in found.items():
                out.setdefault(symbol, []).extend(rows)
            if n % 10 == 0 or n == len(batches):
                print(f"  {timeframe} bars: batch {n}/{len(batches)}", flush=True)
    return out


def delisted(api: Alpaca, active: list, first: str, last: str) -> list:
    """Shares no longer listed (bought out, merged, gone bust), whose ticker
    no active share has taken since. Their SIP bars can still be fetched, and
    leaving them out would flatter the result: the day a buyout or a collapse
    is announced is a classic "in play" day. Alpaca's asset list forgets most
    of them (Discover, Hess, Juniper... aren't even "inactive"), so they come
    from its corporate actions too: every cash or stock merger's target and
    every share written off as worthless since `first`."""
    symbols = {a["symbol"] for a in api.get(f"{TRADING}/assets", {"status": "inactive", "asset_class": "us_equity"})
               if a["exchange"] in EXCHANGES}
    params = {"types": "cash_merger,stock_merger,stock_and_cash_merger,worthless_removal",
              "start": first, "end": last, "limit": 1000}
    token = None
    while True:
        body = api.get(CORPORATE_ACTIONS, {**params, **({"page_token": token} if token else {})})
        for rows in (body.get("corporate_actions") or {}).values():
            symbols.update(row.get("acquiree_symbol") or row.get("symbol") or "" for row in rows)
        token = body.get("next_page_token")
        if not token:
            break
    taken = set(active)
    # Tickers only: both lists also hold CUSIP-style codes for rights and
    # CVRs ("003CVR016"), which the data API rejects outright.
    return sorted(s for s in symbols if s not in taken and re.fullmatch(r"[A-Z]+(\.[A-Z]+)?", s))


def liquid_somewhere(api: Alpaca, symbols: list, first: str, last: str, tag: str) -> list:
    """Shares that, in some calendar month, traded above $5 and 7M+ shares:
    anything that ever averaged 1M a day over 14 sessions passes, so this
    only saves fetching daily bars for the thousands that never could."""
    monthly = cached(DATA_DIR / f"play_monthly_{tag}.pkl",
                     lambda: fetch_bars(api, symbols, "1Month", first, last, lambda b: (b["h"], b["v"]), batch=400),
                     binary=True)
    return sorted(s for s, rows in monthly.items()
                  if any(h > MIN_PRICE for h, _ in rows) and any(v >= 7 * MIN_AVG_VOLUME for _, v in rows))


def daily_matrices(daily: dict, days: list, symbols: list):
    """open, high, low, close, volume as [day, symbol] arrays (NaN where no bar)."""
    row = {d["date"]: i for i, d in enumerate(days)}
    col = {s: j for j, s in enumerate(symbols)}
    m = np.full((5, len(days), len(symbols)), np.nan)
    for symbol, bars in daily.items():
        j = col.get(symbol)
        if j is None:
            continue
        for day, *ohlcv in bars:
            i = row.get(day)
            if i is not None:
                m[:, i, j] = ohlcv
    return m


def screens(days: list, symbols: list, daily: dict):
    """Per day: which shares pass the price / volume / ATR screen (known by the
    open), and each share's ATR(14)."""
    o, h, l, c, v = daily_matrices(daily, days, symbols)
    prev_close = np.vstack([np.full((1, len(symbols)), np.nan), c[:-1]])
    with np.errstate(invalid="ignore", divide="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        jump = o / prev_close
        split = (jump < SPLIT_JUMP[0]) | (jump > SPLIT_JUMP[1])
        true_range = np.where(split, h - l, np.fmax(h - l, np.fmax(abs(h - prev_close), abs(l - prev_close))))
        atr = np.full(c.shape, np.nan)
        passing = np.zeros(c.shape, bool)
        for i in range(LOOKBACK + 1, len(days)):
            before = slice(i - LOOKBACK, i)
            enough = np.sum(~np.isnan(v[before]), axis=0) >= LOOKBACK * 3 // 4
            atr[i] = np.nanmean(true_range[before], axis=0)
            recent_split = split[i - LOOKBACK:i + 1].any(axis=0)
            passing[i] = (enough & ~recent_split & (np.nanmean(v[before], axis=0) >= MIN_AVG_VOLUME)
                          & (atr[i] > MIN_ATR) & (c[i - 1] > MIN_PRICE))
    return passing, atr


def by_month(days: list, stamp: str, what: str, fetch_day, wanted: dict) -> dict:
    """{day index: {symbol: fetch_day's data}} for `wanted` ({day index:
    [symbols]}), cached a calendar month at a time. Only what the cache
    doesn't hold yet is fetched, so a stopped run picks up where it was and a
    wider one fetches just the extra shares."""
    out = {}
    months = sorted({days[i]["date"][:7] for i in wanted})
    for n, month in enumerate(months, 1):
        idx = [i for i in wanted if days[i]["date"].startswith(month)]
        cache = DATA_DIR / f"play_{what}_{stamp}_{month}.pkl"
        saved = {}
        if cache.exists():
            with open(cache, "rb") as f:
                saved = pickle.load(f)
        todo = {i: [s for s in wanted[i] if s not in saved.get(days[i]["date"], {})] for i in idx}
        todo = {i: names for i, names in todo.items() if names}
        if todo:
            started = time.time()
            with ThreadPoolExecutor(WORKERS) as pool:
                for i, found in zip(todo, pool.map(lambda i: fetch_day(i, todo[i]), todo)):
                    saved.setdefault(days[i]["date"], {}).update(found)
            with open(cache, "wb") as f:
                pickle.dump(saved, f)
            print(f"  {what}: {month} ({n}/{len(months)}, {sum(map(len, todo.values())):,} new, "
                  f"{time.time() - started:.0f}s)", flush=True)
        out.update({i: saved.get(days[i]["date"], {}) for i in idx})
    return out


def minute_of(t: str, open_epoch: float) -> int:
    moment = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    return int((moment - open_epoch) // 60)


# ---------------------------------------------------------------------------
# REPLAY
# ---------------------------------------------------------------------------
def simulate(bars: np.ndarray, side: int, bar_high: float, bar_low: float, atr: float, stop,
             worst_case: bool = False):
    """One trade on one day's 1-minute bars (minute, open, high, low, close from
    09:35): (entry, exit, risk per share, how it ended, entry minute, exit
    minute), or None if the entry level was never reached.

    The entry's own 1-minute bar may reach the stop too. Whether that was
    before or after the entry follows the usual guess at a bar's path - open,
    low, high, close if it rose; open, high, low, close if it fell - or, with
    worst_case, the stop is always assumed to have come after the entry."""
    minute, o, h, l, c = bars.T
    level = bar_high if side > 0 else bar_low
    hit = np.flatnonzero(h >= level) if side > 0 else np.flatnonzero(l <= level)
    if not len(hit):
        return None
    i = hit[0]
    gapped = o[i] >= level if side > 0 else o[i] <= level     # past the level already: filled at the open
    entry = o[i] if gapped else level
    if stop == "bar":
        risk = entry - bar_low if side > 0 else bar_high - entry
    else:
        risk = (stop or PAPER_STOP) * atr   # no stop: R still measured against the paper's stop distance
    if stop is not None:
        level = entry - side * risk
        # On the entry bar, after the entry: the whole rest of the bar, unless the
        # bar moved the trade's way from a normal fill (the far extreme came
        # before the entry, so only the close is left after it).
        with_trade = (c[i] >= o[i]) if side > 0 else (c[i] < o[i])
        after_entry = c[i] if with_trade and not gapped and not worst_case else (l[i] if side > 0 else h[i])
        if (after_entry <= level) if side > 0 else (after_entry >= level):
            return entry, level, risk, "stop", minute[i], minute[i]
        crossed = (l[i + 1:] <= level) if side > 0 else (h[i + 1:] >= level)
        if crossed.any():
            j = i + 1 + int(np.argmax(crossed))
            fill = min(level, o[j]) if side > 0 else max(level, o[j])
            return entry, fill, risk, "stop", minute[i], minute[j]
    return entry, c[-1], risk, "close", minute[i], minute[-1]


def band(price: float) -> int:
    return next(k for k, (lo, hi) in enumerate(PRICE_BANDS) if lo <= price < hi)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--start", default="2024-01-02")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--spread-samples", type=int, default=400)
    parser.add_argument("--per-minute", type=float, default=190)
    args = parser.parse_args()
    movers_backtest.REQUEST_GAP = 60 / args.per_minute  # the shared client paces every request by this
    api = Alpaca()
    started = time.time()

    yesterday = datetime.now(NY).date() - timedelta(days=1)
    days = sessions(api, date.fromisoformat(args.start), yesterday)
    test = [i for i, d in enumerate(days) if d["test"]]
    stamp = f"{days[test[0]]['date']}_{days[test[-1]]['date']}"
    print(f"Test period {days[test[0]]['date']} to {days[test[-1]]['date']}: {len(test)} sessions", flush=True)

    listed = cached(DATA_DIR / f"play_assets_{stamp}.json", lambda: candidates(api))
    first, last = days[0]["date"], days[-1]["date"]
    gone = cached(DATA_DIR / f"play_delisted_{stamp}.json", lambda: delisted(api, listed, first, last))
    symbols, daily = [], {}
    for names, tag in ((listed, stamp), (gone, f"delisted_{stamp}")):
        liquid = liquid_somewhere(api, names, first, last, tag)
        daily.update(cached(DATA_DIR / f"play_daily_{tag}.pkl",
                            lambda: fetch_bars(api, liquid, "1Day", first, last,
                                               lambda b: (b["t"][:10], b["o"], b["h"], b["l"], b["c"], b["v"])),
                            binary=True))
        symbols += liquid
    print(f"{len(listed):,} listed shares and funds and {len(gone):,} delisted ones; {len(symbols):,} were ever "
          f"liquid enough ({len(liquid):,} of them since delisted)", flush=True)
    passing, atr = screens(days, symbols, daily)
    per_day = passing[test].sum(axis=1)
    print(f"Screen (over ${MIN_PRICE:g}, {MIN_AVG_VOLUME / 1e6:g}M+ shares a day, ATR over ${MIN_ATR:.2f}): "
          f"{int(np.median(per_day)):,} shares on a typical day ({per_day.min():,}-{per_day.max():,})", flush=True)

    # First 5-minute bars: each day, every share that passes the screen that
    # day or in the next LOOKBACK sessions (it's the history of those).
    ask = np.zeros_like(passing)
    for i in range(len(days)):
        ask[i] = passing[i:i + LOOKBACK + 1].any(axis=0)
    col = {s: j for j, s in enumerate(symbols)}

    def first_bars(i, names):
        start = datetime.fromtimestamp(days[i]["open"], timezone.utc)
        found = {s: (0.0,) * 5 for s in names}  # asked for but didn't trade: volume 0
        for batch in chunks(names, 800):
            params = {"symbols": ",".join(batch), "timeframe": "5Min", "start": iso(start),
                      "end": iso(start + timedelta(seconds=299)), "feed": "sip", "adjustment": "raw", "limit": 10000}
            for page in api.pages("/stocks/bars", params, "bars"):
                for symbol, bars in page.items():
                    for b in bars:
                        if b["t"][11:16] == start.strftime("%H:%M"):
                            found[symbol] = (b["o"], b["h"], b["l"], b["c"], b["v"])
        return found

    wanted = {i: [symbols[j] for j in np.flatnonzero(ask[i])] for i in range(len(days)) if ask[i].any()}
    got = by_month(days, stamp, "first5", first_bars, wanted)
    first5 = np.full((len(days), len(symbols), 5), np.nan, np.float32)
    for i, found in got.items():
        names = [s for s in found if s in col]
        if names:
            first5[i, [col[s] for s in names]] = [found[s] for s in names]

    # Stocks in play, each test day.
    picks = {}
    with np.errstate(invalid="ignore", divide="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for i in test:
            history = first5[i - LOOKBACK:i, :, 4]
            usual = np.where(np.sum(~np.isnan(history), axis=0) >= LOOKBACK * 3 // 4, np.nanmean(history, axis=0), np.nan)
            o, h, l, c, v = first5[i].T
            rv = v / usual
            ok = passing[i] & (o > MIN_PRICE) & (usual > 0) & (rv >= MIN_RELATIVE_VOLUME) & (c != o)
            idx = np.flatnonzero(np.nan_to_num(ok))
            idx = idx[np.argsort(-rv[idx])][:args.top]
            picks[i] = [(int(j), float(rv[j]), 1 if c[j] > o[j] else -1, float(h[j]), float(l[j]), float(atr[i, j]))
                        for j in idx]
    print(f"Stocks in play: {np.mean([len(p) for p in picks.values()]):.1f} a day on average", flush=True)

    def minute_bars(i, names):
        day = days[i]
        start = datetime.fromtimestamp(day["open"], timezone.utc) + timedelta(minutes=5)
        end = datetime.fromtimestamp(day["close"], timezone.utc) - timedelta(seconds=1)
        rows = {s: [] for s in names}  # kept even if empty, so it isn't asked for again
        params = {"symbols": ",".join(names), "timeframe": "1Min", "start": iso(start), "end": iso(end),
                  "feed": "sip", "adjustment": "raw", "limit": 10000}
        for page in api.pages("/stocks/bars", params, "bars"):
            for symbol, bars in page.items():
                rows[symbol].extend((minute_of(b["t"], day["open"]), b["o"], b["h"], b["l"], b["c"]) for b in bars)
        return {s: np.array(sorted(r), np.float64).reshape(-1, 5) for s, r in rows.items()}

    minutes = by_month(days, stamp, "1min", minute_bars,
                       {i: [symbols[j] for j, *_ in picks[i]] for i in test if picks[i]})

    # Replay every stop width. trades[stop] rows: day, symbol, rank, side,
    # entry, exit, risk, stopped?, entry minute, exit minute
    trades = {}
    for stop in STOPS + ["worst"]:
        rows = []
        for i in test:
            for rank, (j, rv, side, bar_high, bar_low, share_atr) in enumerate(picks[i]):
                bars = minutes[i].get(symbols[j])
                if bars is None or not len(bars):
                    continue
                t = simulate(bars, side, bar_high, bar_low, share_atr,
                             PAPER_STOP if stop == "worst" else stop, worst_case=stop == "worst")
                if t:
                    entry, exit_, risk, how, m_in, m_out = t
                    rows.append((i, j, rank, side, entry, exit_, risk, how == "stop", m_in, m_out))
        trades[stop] = np.array(rows, np.float64)
    paper = trades[PAPER_STOP]
    triggered = len(paper) / sum(len(p) for p in picks.values())
    print(f"The entry level was reached on {triggered:.0%} of stocks in play\n", flush=True)

    # Spreads at entry and exit, looked up for a sample of the paper's trades.
    rng = np.random.default_rng(1)
    sample = sorted(rng.choice(len(paper), min(args.spread_samples, len(paper)), replace=False))
    moments = []
    for k in sample:
        i, j, _, _, _, _, _, stopped, m_in, m_out = paper[k]
        day = days[int(i)]
        moments.append((symbols[int(j)], day["open"] + 60 * (m_in + 1)))
        moments.append((symbols[int(j)], day["open"] + 60 * (m_out + 1) if stopped else day["close"] - 30))
    tag = hashlib.md5(repr(moments).encode()).hexdigest()[:8]
    measured = cached(DATA_DIR / f"play_spreads_{stamp}_{tag}.json", lambda: fetch_entry_spreads(api, moments))
    entry_bps = {b: [] for b in range(len(PRICE_BANDS))}
    exit_bps = {(b, s): [] for b in range(len(PRICE_BANDS)) for s in (0, 1)}
    for n, k in enumerate(sample):
        b = band(paper[k][4])
        for into, value in ((entry_bps[b], measured[2 * n]), (exit_bps[(b, int(paper[k][7]))], measured[2 * n + 1])):
            if not math.isnan(value):
                into.append(min(value, 200.0))   # the odd crossed/empty book shouldn't dominate an average
    everything = [v for vs in entry_bps.values() for v in vs]
    mean_entry = {b: np.mean(v) if len(v) >= 10 else np.mean(everything) for b, v in entry_bps.items()}
    all_exit = [v for vs in exit_bps.values() for v in vs]
    mean_exit = {key: np.mean(v) if len(v) >= 10 else np.mean(all_exit) for key, v in exit_bps.items()}
    print("Spreads looked up (bps, mean): " + "; ".join(
        f"{'under $20' if lo == 0 else f'${lo}-{hi}' if hi < math.inf else f'over ${lo}'}: entry {mean_entry[b]:.1f}, "
        f"stop exit {mean_exit[(b, 1)]:.1f}, close {mean_exit[(b, 0)]:.1f}"
        for b, (lo, hi) in enumerate(PRICE_BANDS)))

    def cost(t: np.ndarray) -> np.ndarray:
        """Round-trip cost of each trade, as a fraction of its entry price."""
        return np.array([(mean_entry[band(e)] + mean_exit[(band(e), int(s))]) / 2 / 1e4 for e, s in zip(t[:, 4], t[:, 7])])

    years = sorted({days[i]["date"][:4] for i in test})

    def summary(t: np.ndarray, cost_scale: float = 1.0):
        side, entry, exit_, risk = t[:, 3], t[:, 4], t[:, 5], t[:, 6]
        gross = side * (exit_ / entry - 1)
        net = gross - cost(t) * cost_scale
        r_net = net * entry / risk
        r_gross = gross * entry / risk
        day_r = {}
        for d, r in zip(t[:, 0].astype(int), r_net):
            day_r[d] = day_r.get(d, 0.0) + r
        daily = np.array([day_r.get(i, 0.0) for i in test])   # a day without trades counts as 0
        sd = daily.std(ddof=1)
        by_year = {y: r_net[[days[int(d)]["date"].startswith(y) for d in t[:, 0]]].mean() for y in years}
        return {"n": len(t), "win": np.mean(net > 0), "stopped": np.mean(t[:, 7]), "r_gross": r_gross.mean(),
                "r_net": r_net.mean(), "bps_gross": gross.mean() * 1e4, "bps_net": net.mean() * 1e4,
                "t": daily.mean() / sd * math.sqrt(len(daily)) if sd > 0 else 0.0,
                "sharpe": daily.mean() / sd * math.sqrt(252) if sd > 0 else 0.0, "years": by_year,
                "dollars_per_r": np.mean(entry / risk)}

    def stop_name(stop) -> str:
        return "no stop" if stop is None else "first bar's other end" if stop == "bar" else f"{stop:.0%} of ATR"

    variants = [(f"Paper: top {args.top}, stop {stop_name(PAPER_STOP)}", paper)]
    variants += [(f"  stop {stop_name(PAPER_STOP)}, worst case on the entry bar", trades["worst"])]
    variants += [(f"  stop {stop_name(s)}", trades[s]) for s in STOPS if s != PAPER_STOP]
    variants += [(f"  top {n} only", paper[paper[:, 2] < n]) for n in (5, 10) if n < args.top]
    variants += [("  longs only (all a small Alpaca account can do)", paper[paper[:, 3] > 0]),
                 ("  shorts only", paper[paper[:, 3] < 0]),
                 ("  entered in the first 30 minutes", paper[paper[:, 8] < 35]),
                 ("  entered later", paper[paper[:, 8] >= 35])]
    print(f"\n{'':<50} {'trades':>7} {'wins':>5} {'stopd':>5} {'R before':>8} {'R after':>8} {'2x cost':>8} "
          f"{'bps bef':>8} {'bps aft':>8} {'t':>5} {'Sharpe':>6} " + " ".join(f"{y:>6}" for y in years))
    for name, t in variants:
        if not len(t):
            continue
        s, s2 = summary(t), summary(t, 2.0)
        print(f"{name:<50} {s['n']:>7,} {s['win']:>5.0%} {s['stopped']:>5.0%} {s['r_gross']:>+8.3f} {s['r_net']:>+8.3f} "
              f"{s2['r_net']:>+8.3f} {s['bps_gross']:>+8.1f} {s['bps_net']:>+8.1f} {s['t']:>+5.1f} {s['sharpe']:>+6.2f} "
              + " ".join(f"{s['years'][y]:>+6.3f}" for y in years))
    print("\nR = profit or loss / amount risked (entry to stop), per trade, after the looked-up spreads unless "
          "\"before\". t and Sharpe: from each day's total R (every trade risking the same).")
    s = summary(paper)
    print(f"Sizing: risking $1 on a trade (the paper's stop) takes ${s['dollars_per_r']:,.0f} of shares on average, "
          f"so {args.top} trades a day at 1% risk each need ~{args.top * s['dollars_per_r'] / 100:.0f}x the account.")
    print(f"\nDone in {time.time() - started:.0f}s ({api.requests:,} requests).")


if __name__ == "__main__":
    main()
