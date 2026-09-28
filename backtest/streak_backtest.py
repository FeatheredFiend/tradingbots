#!/usr/bin/env python3
"""
Streak-strategy backtest — which stop-loss / take-profit suit the scanners?
============================================================================

Replays the momentum-streak rule (STREAK_LENGTH consecutive higher or lower
15-minute closes) over past bars, for a grid of stop-loss / take-profit
percentages, and reports per-trade results for each pair.

    python backtest/streak_backtest.py alpaca   # alpaca-bot-env: the Alpaca scanner's US shares
    python backtest/streak_backtest.py ig       # ig-bot-env: the IG scanner's 15 markets

Credentials come from the same env vars as the bots. Bars are cached in
backtest/data/, so reruns cost no API calls — delete a file there to refetch.
The IG fetch spends 15 × --points of IG's 10,000-points-a-week price-history
allowance (the scanner itself no longer uses any) and makes ~16 requests:
stop the IG scanner first, or the two together overrun IG's 30-a-minute limit.

How closely it mimics each bot:
- ig: long AND short; a reversal streak closes the trade and opens the
  opposite one; stop/limit sit at IG. Judged on closed bars (the live bot
  also acts mid-bar on the still-forming one). Each trade pays the bid/ask
  spread IG quoted on its entry bar.
- alpaca: long only, regular US hours only; a falling streak closes. A flat
  0.02% a side for spread/slippage on large US shares (no commission). The
  5-position cap isn't modelled — results are per trade.
- Stops and limits trigger when a bar's low/high crosses them; a gap past
  one fills at the bar's open; if one bar crosses both, the stop is assumed
  to have come first (the conservative guess).

A week, or even a few months, of history is a small sample, and the best
cell of any grid is partly luck — prefer settings that do well across a
region of the grid over a single standout.
"""

import argparse
import importlib.util
import os
import sys
import time
from pathlib import Path

import pandas as pd

STREAK_LENGTH = 3
STOP_LOSS_GRID = [0.25, 0.5, 1.0, 2.0]        # percent of entry price
TAKE_PROFIT_GRID = [0.5, 1.0, 1.5, 2.5, 5.0]  # percent of entry price
BOT_DEFAULT = (2.0, 5.0)                      # what the bots use unless told otherwise

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"

# The IG scanner's resolved pool (its startup log lists these).
IG_EPICS = {
    "IX.D.FTSE.IFM.IP": "indices", "IX.D.SPTRD.IFS.IP": "indices", "IX.D.DOW.IFS.IP": "indices",
    "IX.D.NASDAQ.IFS.IP": "indices", "IX.D.DAX.IFS.IP": "indices", "IX.D.NIKKEI.IFM.IP": "indices",
    "CS.D.CFPGOLD.CFP.IP": "commodities", "CS.D.CFDSILVER.CFM.IP": "commodities",
    "CC.D.LCO.UMP.IP": "commodities", "CC.D.CL.UMP.IP": "commodities",
    "CS.D.EURUSD.MINI.IP": "fx", "CS.D.GBPUSD.MINI.IP": "fx", "CS.D.USDJPY.MINI.IP": "fx",
    "CS.D.GBPEUR.MINI.IP": "fx", "CS.D.AUDUSD.MINI.IP": "fx",
}
ALPACA_COST_PER_SIDE = 0.0002


# ---------------------------------------------------------------------------
# DATA — one frame per source: symbol, open, high, low, close, spread
# (spread = round-trip cost as a fraction of price), oldest bar first.
# ---------------------------------------------------------------------------
def fetch_alpaca(days: int) -> pd.DataFrame:
    import alpaca_trade_api as tradeapi
    from alpaca_trade_api.rest import TimeFrame, TimeFrameUnit

    bot_path = HERE.parent / "alpaca-momentum-scanner-bot" / "alpaca_momentum_scanner_bot.py"
    spec = importlib.util.spec_from_file_location("alpaca_bot", bot_path)
    bot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bot)  # just for its POOL, so the two stay in step

    api = tradeapi.REST(os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"],
                        "https://paper-api.alpaca.markets", api_version="v2")
    end = pd.Timestamp.now(tz="UTC").floor("15min")  # excludes the still-forming bar
    start = end - pd.Timedelta(days=days)
    df = api.get_bars(list(bot.POOL), TimeFrame(15, TimeFrameUnit.Minute),
                      start=start.isoformat(), end=end.isoformat(), feed="iex").df
    df = df.tz_convert("America/New_York").between_time("09:30", "15:45")
    df = df[["symbol", "open", "high", "low", "close"]].copy()
    df["spread"] = 2 * ALPACA_COST_PER_SIDE
    return df


def fetch_ig(points: int) -> pd.DataFrame:
    from trading_ig import IGService

    ig = IGService(os.environ["IG_USERNAME"], os.environ["IG_PASSWORD"], os.environ["IG_API_KEY"], "DEMO")
    ig.create_session()
    frames = []
    for epic in IG_EPICS:
        time.sleep(3)  # ~20 requests a minute, well under IG's 30
        data = ig.fetch_historical_prices_by_epic_and_num_points(epic, "15Min", points)
        raw = data["prices"].iloc[:-1]  # the last bar is still forming
        bid, ask = raw["bid"], raw["ask"]
        mid = (bid + ask) / 2
        frame = pd.DataFrame({
            "symbol": epic, "open": mid["Open"], "high": mid["High"], "low": mid["Low"],
            "close": mid["Close"], "spread": (ask["Close"] - bid["Close"]) / mid["Close"],
        })
        frames.append(frame.dropna())
        allowance = data.get("allowance") or data.get("metadata", {}).get("allowance")
        left = f", allowance left {allowance['remainingAllowance']}" if allowance else ""
        print(f"  {epic}: {len(frame)} bars{left}", file=sys.stderr)
    return pd.concat(frames)


def load_bars(source: str, args) -> pd.DataFrame:
    cache = DATA_DIR / f"{source}_bars.csv"
    if cache.exists():
        print(f"Using cached bars from {cache} (delete it to refetch)", file=sys.stderr)
        return pd.read_csv(cache, index_col=0, parse_dates=[0])
    print(f"Fetching {source} bars...", file=sys.stderr)
    df = fetch_alpaca(args.days) if source == "alpaca" else fetch_ig(args.points)
    DATA_DIR.mkdir(exist_ok=True)
    df.to_csv(cache)
    return df


# ---------------------------------------------------------------------------
# SIMULATION
# ---------------------------------------------------------------------------
def simulate(bars: pd.DataFrame, stop: float, take: float, allow_short: bool) -> list:
    """One market's closed trades as (return as a fraction of entry, exit kind)."""
    o, h, l, c, spread = (bars[k].to_numpy() for k in ("open", "high", "low", "close", "spread"))
    trades = []
    pos, entry, cost = 0, 0.0, 0.0  # pos: +1 long, -1 short, 0 flat
    for i in range(len(c)):
        if pos:
            stop_level = entry * (1 - pos * stop)
            take_level = entry * (1 + pos * take)
            hit_stop = l[i] <= stop_level if pos > 0 else h[i] >= stop_level
            hit_take = h[i] >= take_level if pos > 0 else l[i] <= take_level
            if hit_stop or hit_take:
                if hit_stop:  # a gap past the stop fills at the (worse) open
                    fill = min(o[i], stop_level) if pos > 0 else max(o[i], stop_level)
                else:         # a gap past the limit fills at the (better) open
                    fill = max(o[i], take_level) if pos > 0 else min(o[i], take_level)
                trades.append((pos * (fill / entry - 1) - cost, "stop" if hit_stop else "take"))
                pos = 0

        if i < STREAK_LENGTH:
            continue
        window = c[i - STREAK_LENGTH:i + 1]
        diffs = window[1:] - window[:-1]
        signal = 1 if (diffs > 0).all() else -1 if (diffs < 0).all() else 0

        if pos and signal == -pos:
            trades.append((pos * (c[i] / entry - 1) - cost, "reversal"))
            pos = 0
        if not pos and (signal > 0 or (signal < 0 and allow_short)):
            pos, entry, cost = signal, c[i], spread[i]
    return trades


def grid(bars: pd.DataFrame, allow_short: bool) -> pd.DataFrame:
    rows = []
    for stop in STOP_LOSS_GRID:
        for take in TAKE_PROFIT_GRID:
            trades = [t for _, market in bars.groupby("symbol")
                      for t in simulate(market, stop / 100, take / 100, allow_short)]
            returns = pd.Series([r for r, _ in trades], dtype=float)
            kinds = pd.Series([k for _, k in trades]).value_counts(normalize=True)
            rows.append({
                "stop %": stop, "take %": take, "trades": len(trades),
                "win %": 100 * (returns > 0).mean(),
                "avg %/trade": 100 * returns.mean(),
                "total %": 100 * returns.sum(),
                "exits stop/take/rev %": "/".join(f"{100 * kinds.get(k, 0):.0f}"
                                                  for k in ("stop", "take", "reversal")),
            })
    return pd.DataFrame(rows)


def report(title: str, bars: pd.DataFrame, allow_short: bool) -> None:
    results = grid(bars, allow_short)
    first, last = bars.index.min(), bars.index.max()
    print(f"\n=== {title}: {bars['symbol'].nunique()} markets, {len(bars)} bars, {first:%d %b} – {last:%d %b %Y} ===")
    print("Average return per trade, % of entry (after costs) — rows: stop-loss %, columns: take-profit %")
    print(results.pivot(index="stop %", columns="take %", values="avg %/trade").round(3).to_string())
    default = results[(results["stop %"] == BOT_DEFAULT[0]) & (results["take %"] == BOT_DEFAULT[1])]
    best = results.sort_values("avg %/trade", ascending=False).head(5)
    print("\nBot default, then the 5 best cells:")
    print(pd.concat([default, best]).round(3).to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("source", choices=["alpaca", "ig"])
    parser.add_argument("--days", type=int, default=90, help="alpaca: calendar days of history (default 90)")
    parser.add_argument("--points", type=int, default=500, help="ig: bars per market (default 500)")
    args = parser.parse_args()

    bars = load_bars(args.source, args)
    if args.source == "alpaca":
        report("Alpaca scanner — US shares, long only", bars, allow_short=False)
    else:
        report("IG scanner — all markets, long and short", bars, allow_short=True)
        classes = bars["symbol"].map(IG_EPICS)
        for name in ("indices", "commodities", "fx"):
            report(f"IG scanner — {name} only", bars[classes == name], allow_short=True)


if __name__ == "__main__":
    main()
