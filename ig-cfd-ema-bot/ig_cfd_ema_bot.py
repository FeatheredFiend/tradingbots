#!/usr/bin/env python3
"""
IG Markets Demo (Paper) CFD Bot — EMA(9/21) Crossover Trend Follower
========================================================================

Same strategy as the Alpaca bot (alpaca_ema_bot.py), re-targeted at IG
Markets' CFD demo account instead of Alpaca — this is the answer to
"can we do this on Trading212 CFDs": Trading212's public API only covers
Invest/ISA (real shares), never CFD accounts, even in demo. IG's API does
support CFDs, on a proper demo/practice environment, so that's what this
targets instead.

Strategy
--------
- Timeframe : 15-minute bars
- Entry     : 9-period EMA crosses ABOVE the 21-period EMA  -> BUY (open long CFD)
- Exit      : 9-period EMA crosses BELOW the 21-period EMA  -> SELL (close position)
- Risk mgmt : 2% stop-loss / 5% take-profit, attached DIRECTLY to the order
              as native stopDistance/limitDistance. Unlike the Alpaca bot,
              this does not need to be polled and enforced manually — IG's
              CFD orders support real broker-side stop/limit levels, so once
              set they execute even if this script isn't running.

IMPORTANT — read before running
--------------------------------
1. IG's API requires an API key generated from a LIVE account (even though
   this script only ever trades the linked DEMO account — that's an IG
   platform requirement, not a choice made here). Sign up for a live IG
   account, then go to My IG > Settings > API keys to generate one.
2. This script hardcodes ACCOUNT_TYPE = "DEMO" and never trades live.
3. A few IG-specific conversions below (position-response column names,
   and the points-distance scaling used for stop/limit levels) are
   best-effort from IG's public docs and the `trading-ig` library source —
   they're exactly the kind of thing that may need a small fix once run
   against your real account's actual response shapes, same as the crypto
   symbol format needed one fix on the Alpaca bot. Paste back whatever
   error/output you see and it'll get corrected quickly.

Setup
-----
1. pip install trading-ig pandas
2. $env:IG_USERNAME = "your_ig_username"
   $env:IG_PASSWORD = "your_ig_password"
   $env:IG_API_KEY  = "your_ig_api_key"
3. (Optional) $env:IG_WATCHLIST = "Apple,Microsoft,BP,HSBC,Google:UB.D.GOOGL.CASH.IP"
   (comma-separated search terms, UK and US freely mixed — resolved to IG
   "epics" at startup; append ":EPIC" to a name to pin an exact market when
   a plain search is ambiguous, same as Google's share classes above)
4. (Optional) $env:IG_CURRENCY_CODE = "GBP"
5. Run:
       python ig_cfd_ema_bot.py
"""

import logging
import os
import re
import sys
import time
from typing import Optional

import pandas as pd

from trading_ig import IGService
from trading_ig.rest import IGException

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
IG_USERNAME = os.environ.get("IG_USERNAME", "")
IG_PASSWORD = os.environ.get("IG_PASSWORD", "")
IG_API_KEY = os.environ.get("IG_API_KEY", "")
ACCOUNT_TYPE = "DEMO"  # Hardcoded — this script never trades a LIVE account.
CURRENCY_CODE = os.environ.get("IG_CURRENCY_CODE", "GBP")

# "Google" is pinned to an explicit epic because IG lists Alphabet's Class A
# (GOOGL, voting) and Class C shares as separate markets with the same search
# hit — defaulting to Class A, the ticker most people mean by "Google stock".
# Override any entry with "Name:EPIC" (e.g. "Google:UB.D.GOOGUS.CASH.IP" for
# Class C instead).
DEFAULT_WATCHLIST = (
    "Apple,Microsoft,Amazon,Google:UB.D.GOOGL.CASH.IP,Tesla,"
    "BP,HSBC,Tesco,Vodafone,AstraZeneca"
)
WATCHLIST_SEARCH_TERMS = [
    s.strip()
    for s in os.environ.get("IG_WATCHLIST", DEFAULT_WATCHLIST).split(",")
    if s.strip()
]

TARGET_NOTIONAL = 2.00        # Best-effort target exposure; clamped up to
                               # each market's minimum deal size (CFD minimums
                               # are usually well above this on small markets).
EMA_SHORT_PERIOD = 9
EMA_LONG_PERIOD = 21
BAR_RESOLUTION = "15Min"      # trading_ig's conv_resol() maps this to MINUTE_15.
BARS_LOOKBACK = 200           # ~50 hours of 15-min bars — plenty for a 21-EMA warm-up.

STOP_LOSS_PCT = 0.02           # 2% hard stop-loss, attached to the order itself.
TAKE_PROFIT_PCT = 0.05         # 5% take-profit, attached to the order itself.

LOOP_INTERVAL_SECONDS = 60
MAX_CONSECUTIVE_ERRORS = 10

assert TARGET_NOTIONAL > 0, "TARGET_NOTIONAL must be positive"

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ig_cfd_bot")


# ---------------------------------------------------------------------------
# CLIENT / STARTUP CHECKS
# ---------------------------------------------------------------------------
def get_ig_service() -> IGService:
    if not IG_USERNAME or not IG_PASSWORD or not IG_API_KEY:
        log.error(
            "Missing credentials. Set IG_USERNAME, IG_PASSWORD and IG_API_KEY "
            "environment variables before running this bot."
        )
        sys.exit(1)
    ig_service = IGService(IG_USERNAME, IG_PASSWORD, IG_API_KEY, ACCOUNT_TYPE)
    try:
        session = ig_service.create_session()
    except IGException as e:
        log.error(f"IG login failed: {e}")
        sys.exit(1)
    except Exception as e:
        log.error(f"Network error during IG login: {e}")
        sys.exit(1)

    try:
        accounts = session.get("accounts", [])
        current = next(
            (a for a in accounts if a.get("accountId") == session.get("currentAccountId")),
            accounts[0] if accounts else {},
        )
        balance = current.get("balance", {})
        log.info(
            f"IG session established | account={session.get('currentAccountId', 'N/A')} "
            f"({ACCOUNT_TYPE}) | balance={balance.get('balance', 'N/A')} "
            f"available={balance.get('available', 'N/A')}"
        )
    except Exception as e:
        log.warning(f"Session established, but couldn't parse account summary: {e}")

    return ig_service


# Search results for a company name include leveraged/inverse ETPs and
# regional cross-listings alongside the actual underlying-stock CFD — these
# substring markers reliably identify the noise, not the real thing.
NOISE_NAME_MARKERS = ["Leverage", "GraniteShares", "IncomeShares", "ETP", "(DE)", "(FR)", "(ES)", "(IT)", "(CH)", "(NL)"]


def resolve_watchlist_epics(ig_service: IGService) -> list:
    """Resolve each watchlist entry to exactly one IG epic.

    An entry can be either a plain search term ("Apple") or an explicit
    override "Apple:UA.D.AAPL.CASH.IP" to skip searching entirely. Resolution
    is based on instrument identity only — NOT current tradability, since
    markets are legitimately closed/edits-only outside trading hours and that
    must never block picking which epic to use (tradability is re-checked
    every cycle in trading_cycle() instead). Exits with the candidate list
    shown if a term is still ambiguous after filtering out obvious noise
    (leveraged ETPs, options, foreign cross-listings).
    """
    resolved = []
    for entry in WATCHLIST_SEARCH_TERMS:
        if ":" in entry:
            term, explicit_epic = entry.split(":", 1)
            term, explicit_epic = term.strip(), explicit_epic.strip()
            details = fetch_market_details(ig_service, explicit_epic)
            if details is None:
                log.critical(f"Explicit epic '{explicit_epic}' for '{term}' could not be resolved. Exiting.")
                sys.exit(1)
            log.info(f"Using explicit epic for '{term}' -> {explicit_epic}")
            resolved.append({"term": term, "epic": explicit_epic, "name": term})
            continue

        term = entry
        try:
            markets = ig_service.search_markets(term)
        except IGException as e:
            log.error(f"IG API error searching for '{term}': {e}")
            sys.exit(1)
        except Exception as e:
            log.error(f"Network error searching for '{term}': {e}")
            sys.exit(1)

        if markets is None or len(markets) == 0:
            log.critical(f"No markets found for search term '{term}'. Exiting.")
            sys.exit(1)

        shares = markets[markets["instrumentType"] == "SHARES"] if "instrumentType" in markets else markets
        noise_pattern = "|".join(re.escape(marker) for marker in NOISE_NAME_MARKERS)
        clean = shares[~shares["instrumentName"].str.contains(noise_pattern, case=False, regex=True)]

        if len(clean) == 0:
            candidates = markets[["epic", "instrumentName", "instrumentType", "marketStatus"]].to_string(index=False)
            log.critical(f"'{term}' matched markets, but none look like the underlying share CFD. Candidates:\n{candidates}")
            sys.exit(1)
        elif len(clean) == 1:
            row = clean.iloc[0]
        else:
            # Prefer the near-continuous-hours variant when there's a tie —
            # it stays tradable through more of the bot's polling loop.
            extended_hours = clean[clean["instrumentName"].str.contains("24 Hours", case=False)]
            if len(extended_hours) == 1:
                row = extended_hours.iloc[0]
            else:
                candidates = clean[["epic", "instrumentName", "instrumentType"]].to_string(index=False)
                log.critical(
                    f"'{term}' still matches {len(clean)} plausible markets after filtering — too ambiguous "
                    f"to pick automatically. Pin the exact epic in IG_WATCHLIST as 'Name:EPIC' instead. Candidates:\n{candidates}"
                )
                sys.exit(1)

        log.info(f"Resolved '{term}' -> epic={row['epic']} ({row.get('instrumentName', term)})")
        resolved.append({"term": term, "epic": row["epic"], "name": row.get("instrumentName", term)})

    return resolved


def fetch_market_details(ig_service: IGService, epic: str) -> Optional[dict]:
    try:
        market = ig_service.fetch_market_by_epic(epic)
    except IGException as e:
        log.error(f"IG API error fetching market details for {epic}: {e}")
        return None
    except Exception as e:
        log.error(f"Network error fetching market details for {epic}: {e}")
        return None

    try:
        instrument = market["instrument"]
        dealing_rules = market["dealingRules"]
        snapshot = market["snapshot"]
        return {
            "expiry": instrument.get("expiry", "-"),
            "min_deal_size": float(dealing_rules["minDealSize"]["value"]),
            "scaling_factor": float(snapshot.get("scalingFactor", 1)),
            "market_status": snapshot.get("marketStatus", "UNKNOWN"),
            "bid": snapshot.get("bid"),
            "offer": snapshot.get("offer"),
        }
    except (KeyError, TypeError) as e:
        log.error(f"Unexpected market-details shape for {epic}: {e}")
        return None


# ---------------------------------------------------------------------------
# MARKET DATA
# ---------------------------------------------------------------------------
def fetch_bars(ig_service: IGService, epic: str) -> Optional[pd.DataFrame]:
    """Fetch recent 15-minute bars as a flat OHLC frame (mid of bid/ask)."""
    try:
        data = ig_service.fetch_historical_prices_by_epic_and_num_points(
            epic, BAR_RESOLUTION, BARS_LOOKBACK
        )
    except IGException as e:
        log.error(f"IG API error fetching bars for {epic}: {e}")
        return None
    except Exception as e:
        log.error(f"Unexpected error fetching bars for {epic}: {e}")
        return None

    raw = data.get("prices")
    if raw is None or raw.empty:
        return None

    # Raw frame has MultiIndex columns ("bid"/"ask", "Open"/"High"/"Low"/"Close").
    # Use the bid/ask midpoint as the working close price for the EMA calc.
    df = pd.DataFrame(index=raw.index)
    for field in ("Open", "High", "Low", "Close"):
        df[field] = (raw["bid"][field] + raw["ask"][field]) / 2.0

    return df


def compute_emas(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_short"] = df["Close"].ewm(span=EMA_SHORT_PERIOD, adjust=False).mean()
    df["ema_long"] = df["Close"].ewm(span=EMA_LONG_PERIOD, adjust=False).mean()
    return df


def detect_crossover(df: pd.DataFrame) -> Optional[str]:
    """Return 'bullish', 'bearish', or None based on the last two closed bars."""
    if len(df) < 2:
        return None
    prev, curr = df.iloc[-2], df.iloc[-1]
    if prev["ema_short"] <= prev["ema_long"] and curr["ema_short"] > curr["ema_long"]:
        return "bullish"
    if prev["ema_short"] >= prev["ema_long"] and curr["ema_short"] < curr["ema_long"]:
        return "bearish"
    return None


# ---------------------------------------------------------------------------
# POSITIONS
# ---------------------------------------------------------------------------
def get_open_position(ig_service: IGService, epic: str) -> Optional[dict]:
    """Return the open position dict for `epic`, or None if flat."""
    try:
        positions = ig_service.fetch_open_positions()
    except IGException as e:
        log.error(f"IG API error fetching positions: {e}")
        raise
    except Exception as e:
        log.error(f"Network error fetching positions: {e}")
        raise

    if positions is None or len(positions) == 0:
        return None

    match = positions[positions["market.epic"] == epic]
    if len(match) == 0:
        return None

    row = match.iloc[0]
    return {
        "deal_id": row["position.dealId"],
        "direction": row["position.direction"],
        "size": float(row["position.size"]),
        "level": float(row["position.level"]),
    }


# ---------------------------------------------------------------------------
# ORDER EXECUTION
# ---------------------------------------------------------------------------
def compute_deal_size(target_notional: float, price: float, min_deal_size: float) -> float:
    raw_size = max(target_notional / price, min_deal_size)
    return round(raw_size, 2)


def compute_point_distance(price: float, pct: float, scaling_factor: float) -> float:
    """Convert a percentage-of-price move into IG's points-distance unit."""
    return round((price * pct) * scaling_factor, 1)


def submit_buy(ig_service: IGService, epic: str, name: str, details: dict, price: float) -> None:
    size = compute_deal_size(TARGET_NOTIONAL, price, details["min_deal_size"])
    stop_distance = compute_point_distance(price, STOP_LOSS_PCT, details["scaling_factor"])
    limit_distance = compute_point_distance(price, TAKE_PROFIT_PCT, details["scaling_factor"])

    try:
        result = ig_service.create_open_position(
            currency_code=CURRENCY_CODE,
            direction="BUY",
            epic=epic,
            expiry=details["expiry"],
            force_open=True,
            guaranteed_stop=False,
            level=None,
            limit_distance=limit_distance,
            limit_level=None,
            order_type="MARKET",
            quote_id=None,
            size=size,
            stop_distance=stop_distance,
            stop_level=None,
            trailing_stop=False,
            trailing_stop_increment=None,
        )
        log.info(
            f"BUY submitted -> {name} ({epic}) size={size} stop_dist={stop_distance} "
            f"limit_dist={limit_distance} result={result.get('dealStatus', result)}"
        )
    except IGException as e:
        log.error(f"IG API error submitting BUY for {name} ({epic}): {e}")
    except Exception as e:
        log.error(f"Unexpected error submitting BUY for {name} ({epic}): {e}")


def close_open_position_ig(ig_service: IGService, epic: str, name: str, position: dict, details: dict, reason: str) -> None:
    close_direction = "SELL" if position["direction"] == "BUY" else "BUY"
    try:
        result = ig_service.close_open_position(
            deal_id=position["deal_id"],
            direction=close_direction,
            epic=epic,
            expiry=details["expiry"],
            level=None,
            order_type="MARKET",
            quote_id=None,
            size=position["size"],
        )
        log.info(f"CLOSE submitted ({reason}) -> {name} ({epic}) result={result.get('dealStatus', result)}")
    except IGException as e:
        log.error(f"IG API error closing position for {name} ({epic}) ({reason}): {e}")
    except Exception as e:
        log.error(f"Unexpected error closing position for {name} ({epic}) ({reason}): {e}")


# ---------------------------------------------------------------------------
# TRADING CYCLE
# ---------------------------------------------------------------------------
def trading_cycle(ig_service: IGService, watch_item: dict) -> None:
    epic = watch_item["epic"]
    name = watch_item["name"]

    details = fetch_market_details(ig_service, epic)
    if details is None:
        return
    if details["market_status"] != "TRADEABLE":
        log.info(f"{name} ({epic}) | market_status={details['market_status']} — skipping this cycle.")
        return

    position = get_open_position(ig_service, epic)

    df = fetch_bars(ig_service, epic)
    if df is None or len(df) < EMA_LONG_PERIOD + 1:
        log.warning(f"Not enough closed 15-min bars for {name} ({epic}) yet; skipping this cycle.")
        return

    df = compute_emas(df)
    signal = detect_crossover(df)

    last = df.iloc[-1]
    price = float(last["Close"])
    ema_short = float(last["ema_short"])
    ema_long = float(last["ema_long"])

    if position is not None:
        position_desc = f"{position['direction']} size={position['size']} level={position['level']:.2f}"
    else:
        position_desc = "FLAT"

    log.info(
        f"{name} ({epic}) | price={price:.2f} | EMA9={ema_short:.4f} | "
        f"EMA21={ema_long:.4f} | signal={signal or 'none'} | position={position_desc}"
    )

    if signal == "bullish":
        if position is None:
            submit_buy(ig_service, epic, name, details, price)
        else:
            log.info(f"{name}: bullish crossover detected but already holding a position; skipping buy.")
    elif signal == "bearish":
        if position is not None:
            close_open_position_ig(ig_service, epic, name, position, details, "EMA bearish crossover")
        else:
            log.info(f"{name}: bearish crossover detected but no open position; nothing to sell.")


# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------
def sleep_after_error(consecutive_errors: int) -> None:
    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
        log.critical(
            f"Reached {MAX_CONSECUTIVE_ERRORS} consecutive errors. "
            f"Exiting for safety — check connectivity/credentials before restarting."
        )
        sys.exit(1)
    backoff = min(LOOP_INTERVAL_SECONDS * consecutive_errors, 300)
    log.info(f"Retrying in {backoff}s...")
    time.sleep(backoff)


def run_bot() -> None:
    ig_service = get_ig_service()
    watchlist = resolve_watchlist_epics(ig_service)

    log.info("=" * 78)
    log.info("IG Markets EMA(9/21) Crossover CFD Bot starting — DEMO ACCOUNT ONLY")
    watchlist_desc = ", ".join(f"{w['name']} ({w['epic']})" for w in watchlist)
    log.info(f"Watchlist: {watchlist_desc}")
    log.info(
        f"Target notional=${TARGET_NOTIONAL:.2f}/symbol (clamped to each market's minimum) | "
        f"Stop-loss={STOP_LOSS_PCT:.0%} | Take-profit={TAKE_PROFIT_PCT:.0%} | "
        f"Timeframe=15Min | EMA periods={EMA_SHORT_PERIOD}/{EMA_LONG_PERIOD}"
    )
    log.info("=" * 78)

    consecutive_errors = 0

    while True:
        cycle_had_error = False
        for watch_item in watchlist:
            try:
                trading_cycle(ig_service, watch_item)
            except Exception as e:
                cycle_had_error = True
                log.error(f"Error processing {watch_item['name']} this cycle: {e}")

        if cycle_had_error:
            consecutive_errors += 1
            sleep_after_error(consecutive_errors)
            continue

        consecutive_errors = 0
        time.sleep(LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        log.info("Bot stopped manually (KeyboardInterrupt). Goodbye.")
        sys.exit(0)
