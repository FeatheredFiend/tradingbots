#!/usr/bin/env python3
"""
Market movers backtest — does buying what just rose fast (and selling what
just fell fast) pay, across the whole US stock market?
==========================================================================

At the close of every 5-minute bar of the regular session, ranks every
liquid US share by how far it has just moved, buys the fastest risers and
short-sells the fastest fallers at the next bar's open, holds them for a
fixed time, and reports what the trades made before and after the bid/ask
spread - for a grid of look-backs, thresholds and holding times.

    python backtest/movers_backtest.py        # alpaca-bot-env: 6 months, then cached

    --months 6                 how much history to test on
    --min-price 5              the day before, in dollars
    --min-dollar-volume 20     median daily dollars traded over the 20 sessions before, in millions
    --top 3                    most new trades per side at each 5-minute step
    --entry-spread-samples 200 trades whose spread at the moment of entry is looked up (see Costs)

Credentials come from the same env vars as the Alpaca bots. Everything
fetched is cached in backtest/data/, so reruns make no requests. The first
6-month run makes ~12,000 requests: Alpaca hands back multi-share bars only
~2,200 to a page and allows 200 requests a minute, so it takes about an hour.

How it works:
- Universe: every active, tradable share or fund on NYSE, Nasdaq, NYSE
  Arca, NYSE American or Cboe BZX (Alpaca's asset list), picked afresh each
  day from the 20 sessions before it: price at least --min-price and median
  daily dollar volume at least --min-dollar-volume, and a usual daily
  movement of at least 0.5% (which leaves out T-bill and bond funds).
  Nothing from the future picks the names - but shares delisted since are
  missing (Alpaca lists active assets only), which flatters the result a
  little.
- Prices: consolidated (SIP) 5-minute bars, split-adjusted, regular hours.
- Signal, at each bar's close: the move over the last 5/15/30/60 minutes,
  scored two ways -
    pct    the plain % move ("up 2% in 15 minutes")
    sigma  the move divided by that share's usual movement over that long
           (from its daily high-low ranges over the previous 20 sessions),
           so a jumpy share isn't picked just for being jumpy.
  Of the shares over the threshold, the --top strongest are taken.
- Trades: filled at the next bar's open, closed at the close of the bar the
  hold ends on (or the session's last bar). Time exits only - no stop or
  target. One trade per share at a time. A faller is sold short at the same
  prices; the backtest assumes it can be borrowed.
- Before costs: each trade's % move, in basis points (0.01%). "vs SPY": the
  same minus SPY's move over the same minutes, taking out the market's drift.
- Costs: each share's usual bid/ask spread, from SIP quotes sampled at 18
  moments across the period. A round trip pays one spread, half going in and
  half coming out. Spreads widen when prices move fast, so the spread at the
  moment of entry is looked up for --entry-spread-samples of the trades, and
  the entry half is scaled by how much wider it was on average. Alpaca charges
  no commission; share CFDs at Pepperstone or Capital.com cost more.
- Halves: the first and second half of the period are reported apart. A
  setting that pays in one half only is probably luck.

Not modelled: filling worse than the quoted price (a market order in a fast
market can walk the book), trading halts beyond missing bars, whether a
share can be borrowed (Alpaca shorts easy-to-borrow shares only, in whole
shares), and any cap on positions open at once. Trades that overlap in time
aren't independent, so the t figures flatter the evidence.
"""

import argparse
import hashlib
import json
import math
import os
import pickle
import sys
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import requests

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
TRADING = "https://paper-api.alpaca.markets/v2"
DATA = "https://data.alpaca.markets/v2"
NY = ZoneInfo("America/New_York")

EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
SLOTS = 78                                   # 5-minute bars in a full session
LOOKBACKS = [1, 3, 6, 12]                    # in bars: 5, 15, 30, 60 minutes
THRESHOLDS = {"pct": [1.0, 2.0, 4.0], "sigma": [3.0, 5.0, 8.0]}
HOLDS = [3, 6, 12, None]                     # in bars: 15, 30, 60 minutes; None = to the close
SIDES = {1: "BUY what rose fast", -1: "SELL what fell fast"}
WARM_UP_SESSIONS = 20
MIN_USUAL_DAILY_MOVE = 0.5                   # %: leaves out T-bill and bond funds, whose one-cent ticks
                                             # would otherwise score as huge "sigma" moves
QUOTE_DAYS = 4                               # days x QUOTE_TIMES = spread samples per share
QUOTE_TIMES = ["10:30", "12:30", "15:00"]
QUOTE_WINDOWS = [0.25, 3, 30]                # seconds looked back for a share's quote, widening for the quiet ones
REQUEST_GAP = 0.31                           # seconds between requests: under 200 a minute
WORKERS = 6                                  # requests in flight at once - enough to keep up with REQUEST_GAP


# ---------------------------------------------------------------------------
# ALPACA — plain REST, paced under the rate limit (across threads), retrying
# the odd failure.
# ---------------------------------------------------------------------------
class Alpaca:
    def __init__(self):
        key, secret = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
        if not key or not secret:
            sys.exit("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY (the Alpaca bots' keys) first.")
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self.local = threading.local()
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.requests = 0

    def session(self) -> requests.Session:
        if not hasattr(self.local, "http"):
            self.local.http = requests.Session()
            self.local.http.headers.update(self.headers)
        return self.local.http

    def get(self, url: str, params: dict = None):
        for attempt in range(8):
            with self.lock:  # book the next free slot, so all threads together stay under the limit
                now = time.time()
                at = max(now, self.next_at)
                self.next_at = at + REQUEST_GAP
            if at > now:
                time.sleep(at - now)
            sent = time.time()
            try:
                response = self.session().get(url, params=params, timeout=60)
            except requests.RequestException as e:
                print(f"    (request failed: {e}; retrying)", flush=True)
                time.sleep(3 * (attempt + 1))
                continue
            self.requests += 1
            if time.time() - sent > 10:
                print(f"    (slow answer: {time.time() - sent:.0f}s)", flush=True)
            if response.status_code == 429 or response.status_code >= 500:
                print(f"    (HTTP {response.status_code}: {response.text[:120]!r}; retrying)", flush=True)
                time.sleep(min(60, 3 * (attempt + 1)))
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError(f"{url} kept failing")

    def pages(self, path: str, params: dict, key: str):
        """Each page's `key` dict ({symbol: [items]}) until there are no more."""
        token = None
        while True:
            body = self.get(DATA + path, {**params, **({"page_token": token} if token else {})})
            yield body.get(key) or {}
            token = body.get("next_page_token")
            if not token:
                return


def chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def cached(path: Path, make, binary: bool = False):
    """Load `path` if it exists, else make() and save it."""
    if path.exists():
        with open(path, "rb" if binary else "r") as f:
            return pickle.load(f) if binary else json.load(f)
    value = make()
    DATA_DIR.mkdir(exist_ok=True)
    with open(path, "wb" if binary else "w") as f:
        pickle.dump(value, f) if binary else json.dump(value, f)
    return value


# ---------------------------------------------------------------------------
# CALENDAR — test days, the warm-up before them, and each session's hours.
# ---------------------------------------------------------------------------
def sessions(api: Alpaca, months: float):
    today = datetime.now(NY).date()
    first_test = today - timedelta(days=round(months * 30.44))
    calendar = api.get(f"{TRADING}/calendar", {"start": (first_test - timedelta(days=45)).isoformat(),
                                                "end": (today - timedelta(days=1)).isoformat()})
    days = []
    for c in calendar:
        day = date.fromisoformat(c["date"])
        opens = datetime.combine(day, datetime.strptime(c["open"], "%H:%M").time(), NY)
        closes = datetime.combine(day, datetime.strptime(c["close"], "%H:%M").time(), NY)
        days.append({"date": c["date"], "open": opens.timestamp(), "last_slot": int((closes - opens).total_seconds() // 300) - 1,
                     "test": day >= first_test})
    warm_up = [d for d in days if not d["test"]][-WARM_UP_SESSIONS:]
    return warm_up + [d for d in days if d["test"]]


# ---------------------------------------------------------------------------
# DATA — the asset list, daily bars (universe and usual movement), 5-minute
# bars (the trades), and quotes (the spreads).
# ---------------------------------------------------------------------------
def candidates(api: Alpaca) -> list:
    assets = api.get(f"{TRADING}/assets", {"status": "active", "asset_class": "us_equity"})
    return sorted(a["symbol"] for a in assets if a["tradable"] and a["exchange"] in EXCHANGES and "/" not in a["symbol"])


def fetch_daily(api: Alpaca, symbols: list, first: str, last: str) -> dict:
    out = {}
    for n, batch in enumerate(chunks(symbols, 400), 1):
        print(f"  daily bars: batch {n}/{math.ceil(len(symbols) / 400)}", flush=True)
        params = {"symbols": ",".join(batch), "timeframe": "1Day", "start": first, "end": last,
                  "feed": "sip", "adjustment": "split", "limit": 10000}
        for page in api.pages("/stocks/bars", params, "bars"):
            for symbol, bars in page.items():
                out.setdefault(symbol, []).extend((b["t"][:10], b["h"], b["l"], b["c"], b["v"]) for b in bars)
    return out


def daily_matrices(daily: dict, days: list, symbols: list):
    """high, low, close, volume as [day, symbol] arrays (NaN where no bar)."""
    row = {d["date"]: i for i, d in enumerate(days)}
    col = {s: j for j, s in enumerate(symbols)}
    high, low, close, volume = (np.full((len(days), len(symbols)), np.nan) for _ in range(4))
    for symbol, bars in daily.items():
        j = col.get(symbol)
        if j is None:
            continue
        for day, h, l, c, v in bars:
            i = row.get(day)
            if i is not None:
                high[i, j], low[i, j], close[i, j], volume[i, j] = h, l, c, v
    return high, low, close, volume


def universe(days: list, symbols: list, daily: dict, min_price: float, min_dollar_volume: float):
    """Per test day, which shares qualify (from the 20 sessions before it), and
    each share's usual 5-minute movement (Parkinson's high-low estimate)."""
    high, low, close, volume = daily_matrices(daily, days, symbols)
    test = [i for i, d in enumerate(days) if d["test"]]
    eligible = np.zeros((len(test), len(symbols)), bool)
    sigma5 = np.full((len(test), len(symbols)), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN columns: shares with no bars yet
        dollars = close * volume
        range_var = np.log(high / low) ** 2 / (4 * math.log(2))
        for n, i in enumerate(test):
            before = slice(i - WARM_UP_SESSIONS, i)
            enough = np.sum(~np.isnan(dollars[before]), axis=0) >= WARM_UP_SESSIONS * 3 // 4
            median_dollars = np.nanmedian(np.where(enough, dollars[before], np.nan), axis=0) if enough.any() else 0
            eligible[n] = enough & (median_dollars >= min_dollar_volume * 1e6) & (close[i - 1] >= min_price)
            sigma5[n] = np.sqrt(np.nanmean(range_var[before], axis=0) / SLOTS)
    sigma5[~(sigma5 > 0)] = np.nan
    eligible &= sigma5 * math.sqrt(SLOTS) * 100 >= MIN_USUAL_DAILY_MOVE
    return eligible, sigma5


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def fetch_intraday(api: Alpaca, symbols: list, test_days: list, stamp: str):
    """open, close, volume as [day, slot, symbol] float32 arrays, fetched a day
    at a time per batch of 125 shares. Asking for regular hours only skips the
    pre- and after-market bars, which would more than double the download; even
    so, Alpaca pages multi-share bars ~2,200 at a time, so this is bound by its
    200-requests-a-minute limit (about an hour for 6 months of ~3,000 shares).
    Each batch is cached as it completes, so a stopped run picks up from there."""
    n_days = len(test_days)
    opens, closes, volumes = (np.full((n_days, SLOTS, len(symbols)), np.nan, np.float32) for _ in range(3))
    batches = list(chunks(symbols, 125))
    for n, batch in enumerate(batches, 1):
        tag = hashlib.md5(",".join(batch).encode()).hexdigest()[:8]
        cache = DATA_DIR / f"movers_5min_{stamp}_{tag}.npz"
        at = slice((n - 1) * 125, (n - 1) * 125 + len(batch))
        if cache.exists():
            saved = np.load(cache)
            opens[:, :, at], closes[:, :, at], volumes[:, :, at] = saved["o"], saved["c"], saved["v"]
            continue
        started = time.time()
        o, c, v = (np.full((n_days, SLOTS, len(batch)), np.nan, np.float32) for _ in range(3))
        col = {s: j for j, s in enumerate(batch)}

        def one_day(i):
            day = test_days[i]
            start = datetime.fromtimestamp(day["open"], timezone.utc)
            end = start + timedelta(minutes=5 * (day["last_slot"] + 1), seconds=-1)
            params = {"symbols": ",".join(batch), "timeframe": "5Min", "start": iso(start), "end": iso(end),
                      "feed": "sip", "adjustment": "split", "limit": 10000}
            open_second = start.hour * 3600 + start.minute * 60
            for page in api.pages("/stocks/bars", params, "bars"):
                for symbol, bars in page.items():
                    j = col[symbol]
                    for b in bars:
                        t = b["t"]
                        slot = (int(t[11:13]) * 3600 + int(t[14:16]) * 60 - open_second) // 300
                        if 0 <= slot <= day["last_slot"]:
                            o[i, slot, j], c[i, slot, j], v[i, slot, j] = b["o"], b["c"], b["v"]

        with ThreadPoolExecutor(WORKERS) as pool:
            list(pool.map(one_day, range(n_days)))
        DATA_DIR.mkdir(exist_ok=True)
        np.savez_compressed(cache, o=o, c=c, v=v)
        opens[:, :, at], closes[:, :, at], volumes[:, :, at] = o, c, v
        print(f"  5-minute bars: batch {n}/{len(batches)} ({time.time() - started:.0f}s)", flush=True)
    return opens, closes, volumes


def spread_bps(quote) -> float:
    bid, ask = quote.get("bp") or 0, quote.get("ap") or 0
    return (ask - bid) / ((ask + bid) / 2) * 1e4 if bid > 0 and ask > bid else math.nan


def fetch_spreads(api: Alpaca, symbols: list, test_days: list) -> dict:
    """{symbol: median spread in bps} from the quote in force at 12 moments
    (QUOTE_DAYS x QUOTE_TIMES). Busy shares quote many times a second, so each
    batch first looks back a quarter of a second and only widens the look-back
    (QUOTE_WINDOWS) for the shares still without a quote."""
    picks = [test_days[round(i * (len(test_days) - 1) / (QUOTE_DAYS - 1))] for i in range(QUOTE_DAYS)]
    moments = []
    for day in picks:
        for hhmm in QUOTE_TIMES:
            at = datetime.combine(date.fromisoformat(day["date"]), datetime.strptime(hhmm, "%H:%M").time(), NY)
            if (at.timestamp() - day["open"]) // 300 <= day["last_slot"]:
                moments.append(at)

    def one(task):
        at, batch = task
        found, missing = {}, list(batch)
        for window in QUOTE_WINDOWS:
            params = {"symbols": ",".join(missing), "start": iso(at - timedelta(seconds=window)), "end": iso(at),
                      "feed": "sip", "limit": 10000}
            for page in api.pages("/stocks/quotes", params, "quotes"):
                for symbol, quotes in page.items():
                    for q in quotes:  # oldest first, so the last good one is the one in force
                        bps = spread_bps(q)
                        if not math.isnan(bps):
                            found[symbol] = bps
            missing = [s for s in missing if s not in found]
            if not missing:
                break
        return found

    tasks = [(at, batch) for at in moments for batch in chunks(symbols, 200)]
    samples = {s: [] for s in symbols}
    with ThreadPoolExecutor(WORKERS) as pool:
        for n, found in enumerate(pool.map(one, tasks), 1):
            for symbol, bps in found.items():
                samples[symbol].append(bps)
            if n % 20 == 0:
                print(f"  spreads: {n}/{len(tasks)}", flush=True)
    return {s: float(np.median(v)) for s, v in samples.items() if len(v) >= 3}


def fetch_entry_spreads(api: Alpaca, entries: list) -> list:
    """For (symbol, epoch) entries: the spread in force at that moment, in bps
    (the newest quote in the 30 seconds before it; NaN if there was none)."""
    def one(entry):
        symbol, at = entry
        moment = datetime.fromtimestamp(at, timezone.utc)
        params = {"symbols": symbol, "start": iso(moment - timedelta(seconds=30)), "end": iso(moment),
                  "feed": "sip", "limit": 20, "sort": "desc"}
        for q in (api.get(DATA + "/stocks/quotes", params).get("quotes") or {}).get(symbol, []):
            bps = spread_bps(q)
            if not math.isnan(bps):
                return bps
        return math.nan

    print(f"  spreads at entry: looking up {len(entries)} trades", flush=True)
    with ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(one, entries))


# ---------------------------------------------------------------------------
# REPLAY
# ---------------------------------------------------------------------------
def carry_forward(closes: np.ndarray) -> np.ndarray:
    """Each slot's last traded close so far that day (NaN before the first trade)."""
    n_days, n_slots, n = closes.shape
    index = np.where(np.isnan(closes), np.int16(-1), np.arange(n_slots, dtype=np.int16)[None, :, None])
    index = np.maximum.accumulate(index, axis=1)
    filled = np.take_along_axis(closes, np.maximum(index, 0), axis=1)
    return np.where(index >= 0, filled, np.nan)


def replay(opens, closes, eligible, sigma5, test_days, spy: int, top: int) -> dict:
    """{(side, score, threshold, lookback, hold): trades as arrays}."""
    filled = carry_forward(closes)
    traded = ~np.isnan(opens)
    found = {}
    for d, day in enumerate(test_days):
        last = day["last_slot"]
        c, o, ok = filled[d], opens[d], traded[d]
        with np.errstate(invalid="ignore", divide="ignore"):
            moves = {b: np.vstack([np.full((b, c.shape[1]), np.nan), c[b:] / c[:-b] - 1]) for b in LOOKBACKS}
        for side in SIDES:
            for score, thresholds in THRESHOLDS.items():
                for b in LOOKBACKS:
                    with np.errstate(invalid="ignore", divide="ignore"):
                        strength = side * (moves[b] * 100 if score == "pct" else moves[b] / (sigma5[d] * math.sqrt(b)))
                    strength = np.where(eligible[d][None, :], strength, np.nan)
                    for threshold in thresholds:
                        over = np.nan_to_num(strength, nan=-np.inf) >= threshold
                        for hold in HOLDS:
                            busy_until = np.full(c.shape[1], -1)
                            rows = []
                            for k in range(b, last):
                                pick = over[k] & ok[k] & ok[k + 1] & (busy_until <= k)
                                if not pick.any():
                                    continue
                                idx = np.flatnonzero(pick)
                                if len(idx) > top:
                                    idx = idx[np.argsort(-strength[k, idx])[:top]]
                                out = last if hold is None else min(k + hold, last)
                                entry, exit_ = o[k + 1, idx], c[out, idx]
                                spy_move = c[out, spy] / o[k + 1, spy] - 1
                                busy_until[idx] = out
                                rows.append(np.column_stack([np.full(len(idx), d), np.full(len(idx), k), idx,
                                                             side * (exit_ / entry - 1), np.full(len(idx), side * spy_move)]))
                            if rows:
                                found.setdefault((side, score, threshold, b, hold), []).append(np.vstack(rows))
    return {key: np.vstack(parts) for key, parts in found.items()}


def summarize(trades: np.ndarray, cost: np.ndarray, n_days: int):
    if trades is None or not len(trades):
        return None
    gross = trades[:, 3]
    net = gross - cost
    first = trades[:, 0] < n_days // 2
    n = len(net)
    sd = net.std(ddof=1) if n > 1 else 0
    return {"n": n, "win": float(np.mean(net > 0)), "gross": gross.mean() * 1e4, "net": net.mean() * 1e4,
            "vs_spy": np.nanmean(gross - trades[:, 4]) * 1e4, "cost": cost.mean() * 1e4,
            "t": float(net.mean() / sd * math.sqrt(n)) if sd > 0 else 0.0,
            "net1": net[first].mean() * 1e4 if first.any() else math.nan,
            "net2": net[~first].mean() * 1e4 if (~first).any() else math.nan}


def minutes(bars) -> str:
    return "to the close" if bars is None else f"{bars * 5} min"


def label(score, threshold, b) -> str:
    return f"{'up/down' if score == 'pct' else 'sigma'} {threshold:g}{'%' if score == 'pct' else ''} in {b * 5} min"


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--months", type=float, default=6)
    parser.add_argument("--min-price", type=float, default=5)
    parser.add_argument("--min-dollar-volume", type=float, default=20)
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--entry-spread-samples", type=int, default=200)
    args = parser.parse_args()
    api = Alpaca()
    started = time.time()

    days = sessions(api, args.months)
    test_days = [d for d in days if d["test"]]
    stamp = f"{test_days[0]['date']}_{test_days[-1]['date']}"
    print(f"Test period {test_days[0]['date']} to {test_days[-1]['date']}: {len(test_days)} sessions "
          f"(+{len(days) - len(test_days)} before it to pick the universe)", flush=True)

    symbols = cached(DATA_DIR / f"movers_assets_{stamp}.json", lambda: candidates(api))
    daily = cached(DATA_DIR / f"movers_daily_{stamp}.pkl",
                   lambda: fetch_daily(api, symbols, days[0]["date"], days[-1]["date"]), binary=True)
    eligible_all, sigma_all = universe(days, symbols, daily, args.min_price, args.min_dollar_volume)
    keep = np.flatnonzero(eligible_all.any(axis=0) | (np.array(symbols) == "SPY"))
    names = [symbols[j] for j in keep]
    eligible, sigma5 = eligible_all[:, keep], sigma_all[:, keep]
    per_day = eligible.sum(axis=1)
    print(f"Universe: {len(symbols):,} listed shares and funds; {int(np.median(per_day)):,} qualify on a typical day "
          f"({per_day.min():,}-{per_day.max():,}), {len(names):,} on at least one", flush=True)

    opens, closes, _ = fetch_intraday(api, names, test_days, stamp)
    spreads = cached(DATA_DIR / f"movers_spreads_{stamp}_{hashlib.md5(','.join(names).encode()).hexdigest()[:8]}.json",
                     lambda: fetch_spreads(api, names, test_days))
    known = np.array([spreads.get(s, np.nan) for s in names])
    fallback = float(np.nanpercentile(known, 90))
    spread = np.where(np.isnan(known), fallback, known) / 1e4
    print(f"Usual spreads: median {np.nanmedian(known):.1f} bps across the universe; {int(np.isnan(known).sum())} "
          f"shares without enough quotes charged the 90th percentile, {fallback:.1f} bps", flush=True)

    print("Replaying ...", flush=True)
    results = replay(opens, closes, eligible, sigma5, test_days, names.index("SPY"), args.top)

    # How much wider the spread is at the moment of entry than usual.
    pool = {(int(t[0]), int(t[1]), int(t[2])) for key, trades in results.items() if key[3] in (3, 6) and key[4] == 6
            for t in trades}
    rng = np.random.default_rng(1)
    chosen = sorted(pool)
    chosen = [chosen[i] for i in rng.choice(len(chosen), min(args.entry_spread_samples, len(chosen)), replace=False)]
    entries = [(names[s], test_days[d]["open"] + 300 * (k + 1)) for d, k, s in chosen]
    tag = hashlib.md5(repr(entries).encode()).hexdigest()[:8]
    measured = cached(DATA_DIR / f"movers_entry_spreads_{stamp}_{tag}.json",
                      lambda: fetch_entry_spreads(api, entries))
    pairs = [(m, spread[s] * 1e4) for m, (d, k, s) in zip(measured, chosen) if not math.isnan(m)]
    widening = sum(m for m, _ in pairs) / sum(u for _, u in pairs) if pairs else 1.0
    print(f"Spread at entry: {widening:.2f}x the usual on average ({len(pairs)} trades looked up)\n", flush=True)

    summaries = {key: summarize(trades, spread[trades[:, 2].astype(int)] * (0.5 * widening + 0.5), len(test_days))
                 for key, trades in results.items()}
    for side, title in SIDES.items():
        print(f"{title} - basis points (0.01%) per trade before costs / after costs, and trades;")
        print("* = made money after costs in both halves of the period")
        print(f"{'':<24}" + "".join(f"{minutes(h):>24}" for h in HOLDS))
        for score, thresholds in THRESHOLDS.items():
            for threshold in thresholds:
                for b in LOOKBACKS:
                    cells = []
                    for hold in HOLDS:
                        s = summaries.get((side, score, threshold, b, hold))
                        if not s:
                            cells.append(f"{'-':>24}")
                            continue
                        star = "*" if s["net1"] > 0 and s["net2"] > 0 else " "
                        cells.append(f"{s['gross']:>+8.1f} {s['net']:>+7.1f} {s['n']:>6,}{star}")
                    print(f"{label(score, threshold, b):<24}" + "".join(cells))
        print()

    ranked = sorted(((k, s) for k, s in summaries.items() if s and s["n"] >= 100), key=lambda kv: -kv[1]["net"])
    both = [(k, s) for k, s in ranked if s["net1"] > 0 and s["net2"] > 0]
    print(f"{sum(1 for _, s in ranked if s['net'] > 0)} of {len(ranked)} settings (with 100+ trades) made money after "
          f"costs over the whole period; {len(both)} in both halves.")
    print(f"\nBest ten after costs:\n{'':<48} {'trades':>7} {'wins':>5} {'before':>7} {'after':>7} {'vs SPY':>7} "
          f"{'cost':>6} {'1st half':>8} {'2nd half':>8} {'t':>5}")
    for (side, score, threshold, b, hold), s in ranked[:10]:
        print(f"{SIDES[side].split()[0]:<5} {label(score, threshold, b):<24} hold {minutes(hold):<12} {s['n']:>7,} "
              f"{s['win']:>5.0%} {s['gross']:>+7.1f} {s['net']:>+7.1f} {s['vs_spy']:>+7.1f} {s['cost']:>6.1f} "
              f"{s['net1']:>+8.1f} {s['net2']:>+8.1f} {s['t']:>+5.1f}")

    for side in SIDES:
        best = next(((k, s) for k, s in ranked if k[0] == side), None)
        if not best:
            continue
        key, s = best
        trades = results[key]
        cost = spread[trades[:, 2].astype(int)] * (0.5 * widening + 0.5)
        print(f"\n{SIDES[side]}, best setting ({label(*key[1:4])}, hold {minutes(key[4])}) by time of day:")
        for name, lo, hi in (("first hour", 0, 12), ("middle of the day", 12, 60), ("last 90 minutes", 60, SLOTS)):
            part = (trades[:, 1] >= lo) & (trades[:, 1] < hi)
            p = summarize(trades[part], cost[part], len(test_days))
            if p:
                print(f"  {name:<18} {p['n']:>6,} trades  before {p['gross']:>+7.1f}  after {p['net']:>+7.1f} bps")
        counts = np.bincount(trades[:, 2].astype(int), minlength=len(names))
        print("  most traded: " + ", ".join(f"{names[j]} {counts[j]}" for j in np.argsort(-counts)[:10]))

    print(f"\nDone in {time.time() - started:.0f}s ({api.requests:,} requests).")


if __name__ == "__main__":
    main()
