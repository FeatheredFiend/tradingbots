#!/usr/bin/env python3
"""
IG Markets Demo (Paper) Momentum Streak Scanner — HIGH-RISK / EXPERIMENTAL
=============================================================================

A third, deliberately riskier bot: instead of a small named watchlist, it
scans a broad, curated POOL of ~15 liquid CFDs (indices, commodities, FX)
every cycle and trades whichever ones show a raw momentum streak — no
named symbol is chosen in advance, the bot decides purely from the data.
No shares: IG's API gives no share prices at all (see below).

Strategy — momentum streak (no smoothing, reacts fast, whipsaws more)
-----------------------------------------------------------------------
- Timeframe : 15-minute bars by default (SCANNER_TIMEFRAME env var: M1, M5,
  M15 or M30, shared with the other scanners)
- Buy       : STREAK_LENGTH consecutive HIGHER closes in a row  -> open LONG
- Sell      : STREAK_LENGTH consecutive LOWER closes in a row   -> open SHORT
- A reversal streak closes an opposing open position; the same-direction
  streak while already positioned is a no-op (no pyramiding).
- Risk mgmt : 2% stop-loss / 5% take-profit by default (STOP_LOSS_PERCENT /
  TAKE_PROFIT_PERCENT env vars, shared with the other scanners;
  IG_TAKE_PROFIT_PERCENT sets this bot's own take-profit), attached natively to the order (same
  mechanism as ig_cfd_ema_bot.py — IG's CFD orders support this directly,
  no manual polling needed).
- Loss caps : IG's smallest trade is still big, so money limits sit on top
  (IG_MAX_TRADE_LOSS / IG_MAX_POSITIONS / IG_DAILY_LOSS_LIMIT, 0 = off):
  a stop is pulled in closer wherever a stop-out would lose more than
  £25, at most 5 positions are open at once, and once the day is £250
  down the bot closes its positions and opens nothing until the next
  daily rollover. See "Loss limits" in the configuration below.
- Profit/time: optional, all off by default (IG_MAX_TRADE_PROFIT /
  IG_DAILY_GIVEBACK / IG_PAUSE_TIMES): a take-profit is pulled in wherever
  reaching it would make more than a set amount, a day that has been up
  stops once it gives back a set amount from its best, and streaks open
  nothing during set UK times. See "Profit and time limits" below.
- Overnight : the positions it opened are closed 15 minutes before the
  daily rollover (22:00 UK), so no overnight funding is paid, and nothing
  new opens from an hour before it to 45 minutes after
  (SCANNER_FLAT_MINUTES / SCANNER_LAST_ENTRY_MINUTES, 0 = off; see
  shared/rollover.py). It knows its own positions by the deal IDs saved
  in own_trades.json beside it.
- Stagnancy : one of its own positions that has gone nowhere for a while is
  logged (shadow, the default) or closed with the reason TIMEOUT_STAGNANT,
  freeing its slot (SCANNER_STAGNANT_* settings; see shared/stagnancy.py).
  Prices come from the positions it reads each pass - no extra requests.
- Broadcasts: with DASHBOARD_BROADCAST=1 it takes broadcast trades from the
  dashboard (shared/broadcast.py) - the admin's market and side, IG's
  minimum size, its own stop-loss, take-profit and loss limits. A market
  outside the pool is managed until its trade closes (brackets, the loss
  limits, the pre-rollover close, the stagnancy timeout) but never traded on
  a streak. Dashboard commands are picked up between markets, not once a pass.

Unlike ig_cfd_ema_bot.py, this bot can go SHORT (CFDs support it) — a
genuinely different, higher-risk capability than the long-only Alpaca and
named-watchlist IG bots.

Bars are built locally, not fetched
-----------------------------------
IG's price-history endpoint (/prices) is no use for a broad scanner:
- It refuses every share with a 403 "unauthorised.access.to.equity.exception"
  — IG's data vendors don't license equity prices over the API.
- It allows only 10,000 data points per week. Polling even 50 bars for
  ~15 markets every pass burns through that in about half an hour, after
  which every /prices call fails until the allowance resets.
So the bot never calls it. Each pass already fetches every market's details
for its status; the snapshot there carries the live bid/offer, and the mid
is recorded into buckets one bar long (the last sample in a bucket is its
close). Shares get no bid/offer in that snapshot either — the same
licensing block — so there's no way to price them and they're left out of
the pool. The catch: a symbol needs STREAK_LENGTH + 1 buckets (45 minutes
on 15-minute bars at the default 3) before it can signal. Bars are saved to
BARS_FILE as they change and reloaded at startup, so only a first start (or
one after a long stop — see SAVED_BARS_MAX_AGE_HOURS — or a change of bar
length) sits through that warm-up. Each pass samples a market once, and
IG's request limit stretches a pass to about 35 seconds for the default
pool, so a 1-minute bar gets a sample or two, and one a slow pass misses is
skipped: 5 minutes or longer suits this scanner.

Why this is riskier than the other two bots, on purpose:
- No trend confirmation (no EMA smoothing) — a streak of 3 candles is a much
  weaker, noisier signal than a moving-average crossover, so expect more
  false signals and more round-trips hitting the stop-loss.
- The universe is broad and resolved automatically, not hand-verified one by
  one the way the 10-name IG watchlist was — see "soft resolution" below.

Soft resolution (deliberately different from ig_cfd_ema_bot.py)
-----------------------------------------------------------------
The named-watchlist bot HARD-EXITS on any ambiguous or unresolved name,
because getting a specific hand-picked symbol wrong matters. This bot's
whole point is breadth, not precision on any one name, so instead it
SKIPS any pool entry that fails to resolve cleanly (logs why) and carries
on with whatever did resolve. When a name has several plausible matches
it auto-picks one and logs every candidate, so a wrong pick can be fixed by
changing that DEFAULT_POOL entry to "Name:EPIC".

Rate limiting
-------------
IG allows only ~30 non-trading requests per minute, account-wide. Every
search, market-details and positions call here is paced to stay under
that, so startup resolution takes about a minute and a full pass over the
pool (one market-details call per symbol) takes a minute or two. Running
this alongside ig_cfd_ema_bot.py on the same IG account shares that one
budget between them. Reporting to the dashboard adds about one request a
minute (the balance) plus one every five (closed trades); the open
positions it sends are the ones each pass fetches anyway.

Setup
-----
1. pip install trading-ig pandas   (same deps as ig_cfd_ema_bot.py — reuse
   the ig-bot-env venv, no need for a separate one)
2. Same IG_USERNAME / IG_PASSWORD / IG_API_KEY env vars as ig_cfd_ema_bot.py
3. Run:
       python ig_momentum_scanner_bot.py
"""

import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from trading_ig import IGService
from trading_ig.rest import IGException

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))
from dashboard_reporter import CommandError, DashboardReporter  # noqa: E402 - needs the path above
import broadcast  # noqa: E402 - needs the path above
import rollover  # noqa: E402 - needs the path above
from engine import clock  # noqa: E402 - rollover puts strategy-bots on the path
import stagnancy  # noqa: E402 - needs the path above
import symbols  # noqa: E402 - needs the path above

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
IG_USERNAME = os.environ.get("IG_USERNAME", "")
IG_PASSWORD = os.environ.get("IG_PASSWORD", "")
IG_API_KEY = os.environ.get("IG_API_KEY", "")
ACCOUNT_TYPE = "DEMO"  # Hardcoded — this script never trades a LIVE account.
ACCOUNT_MODE = "demo" if ACCOUNT_TYPE == "DEMO" else "live"  # for the dashboard's broadcast previews
CURRENCY_CODE = os.environ.get("IG_CURRENCY_CODE", "GBP")

# (search term or "term:EPIC" override, expected IG instrumentType)
# ~15 liquid instruments across categories, deliberately broader than a
# hand-picked watchlist — this is the "random CFD" scanning pool. No shares:
# IG's API never gives a price for them (see "Bars are built locally").
DEFAULT_POOL = [
    # Indices
    ("FTSE 100", "INDICES"), ("US 500", "INDICES"), ("Wall Street", "INDICES"),
    ("US Tech 100", "INDICES"), ("Germany 40", "INDICES"), ("Japan 225", "INDICES"),
    # Commodities
    ("Spot Gold", "COMMODITIES"), ("Spot Silver", "COMMODITIES"),
    ("Oil - Brent Crude", "COMMODITIES"), ("Oil - US Crude", "COMMODITIES"),
    # FX majors
    ("EUR/USD", "CURRENCIES"), ("GBP/USD", "CURRENCIES"), ("USD/JPY", "CURRENCIES"),
    ("GBP/EUR", "CURRENCIES"), ("AUD/USD", "CURRENCIES"),
]
POOL_ENTRIES = [
    p.strip() for p in os.environ.get("IG_POOL", "").split(",") if p.strip()
] or None  # env override replaces the whole pool (as "term" or "term:EPIC", no type filter)

STREAK_LENGTH = int(os.environ.get("STREAK_LENGTH", "3"))  # consecutive up/down bars to trigger

BAR_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30}
TIMEFRAME = os.environ.get("SCANNER_TIMEFRAME", "").strip().upper() or "M15"  # bar length
assert TIMEFRAME in BAR_MINUTES, "SCANNER_TIMEFRAME must be M1, M5, M15 or M30"
BAR_SECONDS = BAR_MINUTES[TIMEFRAME] * 60  # locally built bars — see "Bars are built locally" above
BARS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "momentum_bars.json")
# Saved bars older than this are dropped at startup and that market warms up
# again. Long enough to survive an overnight stop (the gap then matches the
# closed-market gap IG's own bars have); stopping for hours while a market is
# open means the bars either side of the gap are treated as consecutive.
SAVED_BARS_MAX_AGE_HOURS = 16
OWN_TRADES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "own_trades.json")
BROADCASTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "broadcasts.json")

# Percent of the entry price, e.g. STOP_LOSS_PERCENT=0.5 for 0.5%. IG sets a
# minimum stop/limit distance per market, so a very tight value can get a
# trade rejected (the rejection reason is logged).
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PERCENT", "2")) / 100


def _take_profit_setting() -> float:
    """IG_TAKE_PROFIT_PERCENT, this bot's own take-profit, or the
    TAKE_PROFIT_PERCENT the scanners share when it's empty - as a fraction.
    IG_MAX_TRADE_LOSS pulls most stops here far closer than
    STOP_LOSS_PERCENT (Japan 225: about 0.1%, not 0.5%), so the shared
    target can be 15 times the stop - over 1,000 points on Japan 225."""
    text = os.environ.get("IG_TAKE_PROFIT_PERCENT", "").strip() or os.environ.get("TAKE_PROFIT_PERCENT", "5")
    return float(text) / 100


TAKE_PROFIT_PCT = _take_profit_setting()


def _limit_setting(name: str, default: float) -> float:
    """A loss limit from the environment: empty means the default, 0 = off."""
    text = os.environ.get(name, "").strip()
    value = float(text) if text else default  # the launcher may save "5.0"
    if value < 0:
        raise ValueError(f"{name} can't be negative")
    return value


# Loss limits, in the account's currency (IG_CURRENCY_CODE); 0 switches one
# off. IG's smallest trade is still big - a 0.5% stop on gold loses about
# £200 - and with a position in every market the account swings hard, so:
# - MAX_TRADE_LOSS pulls a trade's stop in closer than STOP_LOSS_PERCENT
#   wherever a stop-out would lose more than this. A market where even IG's
#   closest allowed stop would lose more isn't traded.
# - MAX_POSITIONS caps how many positions are open at once in the pool's
#   markets.
# - DAILY_LOSS_LIMIT: once the day's closed trades in the pool's markets plus
#   the bot's open positions are down this much, it closes its positions and
#   opens nothing more until the next daily rollover (22:00 UK), which starts
#   a new day. It counts every closed trade in those markets on the account,
#   as the dashboard does, since IG's trade history doesn't say who opened one.
MAX_TRADE_LOSS = _limit_setting("IG_MAX_TRADE_LOSS", 25)
MAX_POSITIONS = round(_limit_setting("IG_MAX_POSITIONS", 5))
DAILY_LOSS_LIMIT = _limit_setting("IG_DAILY_LOSS_LIMIT", 250)
# The day's closed trades are read again this often, and on the next pass
# after one of the bot's positions closes.
DAY_TRADES_EVERY_SECONDS = 120


def _pause_times(text: str) -> list:
    """IG_PAUSE_TIMES, e.g. "13:00-17:00" or "13:00-17:00,19:30-21:00" (UK
    time), as [(start, end)] pairs of datetime.time. A window may run past
    midnight, e.g. "23:00-01:00"."""
    windows = []
    for part in text.split(","):
        if not part.strip():
            continue
        try:
            start, end = (clock.parse_hhmm(t) for t in part.split("-"))
        except ValueError:
            raise ValueError(f"IG_PAUSE_TIMES: {part.strip()!r} isn't a UK time window like 13:00-17:00") from None
        if start == end:
            raise ValueError(f"IG_PAUSE_TIMES: {part.strip()!r} starts and ends at the same time")
        windows.append((start, end))
    return windows


# Profit and time limits, in the account's currency; 0 (or empty) switches
# one off, and all three are off by default:
# - MAX_TRADE_PROFIT pulls a trade's take-profit in closer than the
#   percentage wherever reaching it would make more than this, the way
#   MAX_TRADE_LOSS pulls the stop in. 1.5% is about £400 on Japan 225 but
#   £13 on an EUR/USD Mini, so only the big contracts' targets move.
# - DAILY_GIVEBACK: once the day (counted as DAILY_LOSS_LIMIT counts it) has
#   been this much up, falling this far below its best closes the bot's
#   positions and opens nothing more until the rollover - so a day that got
#   that far ends at about break-even or better. The best is rebuilt from
#   the day's closed trades at a restart (open positions' highs are lost).
# - PAUSE_TIMES: UK times when a streak opens nothing (closes still happen,
#   and so do the dashboard's broadcast trades).
MAX_TRADE_PROFIT = _limit_setting("IG_MAX_TRADE_PROFIT", 0)
DAILY_GIVEBACK = _limit_setting("IG_DAILY_GIVEBACK", 0)
PAUSE_TIMES = _pause_times(os.environ.get("IG_PAUSE_TIMES", ""))

# No fixed loop interval: the rate limiter below paces every request, so a
# full pass over ~15 symbols naturally takes well under a minute — each
# symbol gets sampled many times per 15-minute bar, so a bar's close is
# never more than one pass stale (a big part of a 1-minute bar, though).
# IG's limit is ~30/min for the whole account. Bots don't coordinate, so if
# two run at once on the same account, give each a share (e.g. 18 and 10).
REQUESTS_PER_MINUTE = int(os.environ.get("IG_REQUESTS_PER_MINUTE", "28"))
# Retry waits for a name that fails to resolve at startup — nearly always a
# rate-limit 403, which clears within a minute, not a missing market.
RESOLVE_RETRY_WAITS = (20, 40, 60)
ERROR_BACKOFF_SECONDS = 60
MAX_CONSECUTIVE_ERRORS = 10

assert STREAK_LENGTH >= 2, "STREAK_LENGTH must be at least 2 to mean anything"
assert STOP_LOSS_PCT > 0 and TAKE_PROFIT_PCT > 0, \
    "STOP_LOSS_PERCENT and TAKE_PROFIT_PERCENT (or IG_TAKE_PROFIT_PERCENT) must be positive"

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ig_momentum_bot")

# Off unless DASHBOARD_URL and DASHBOARD_TOKEN are set (see shared/dashboard_reporter.py).
dashboard = DashboardReporter("ig-momentum-scanner", "IG momentum scanner", broker="IG", strategy="Momentum streak")
own_trades = rollover.OwnTrades(OWN_TRADES_FILE)  # the positions this bot opened - only these close before the rollover
broadcasts = broadcast.Book(path=BROADCASTS_FILE, log=log)  # the broadcast trades it acted on (shared/broadcast.py)


# ---------------------------------------------------------------------------
# RATE LIMITING
# ---------------------------------------------------------------------------
class _RateLimiter:
    """IG allows only ~30 non-trading requests/minute, account-wide, shared
    across every non-trading endpoint (search, market details, bars,
    positions). A burst of unpaced calls (e.g. resolving the whole pool)
    blows through that almost immediately and gets 403'd — including calls
    for symbols that had just succeeded moments earlier. trading-ig's own
    built-in pacing does not catch this (it reacts to 429s, IG returns 403
    for this specific case), so every non-trading call in this script goes
    through this pacer instead of relying on the library."""

    def __init__(self, max_per_minute: int = 28):  # a little under IG's 30 for margin
        self._min_interval = 60.0 / max_per_minute
        self._last_call = 0.0

    def wait(self) -> None:
        remaining = self._min_interval - (time.monotonic() - self._last_call)
        if remaining > 0:
            time.sleep(remaining)
        self._last_call = time.monotonic()


_rate_limiter = _RateLimiter(REQUESTS_PER_MINUTE)


# ---------------------------------------------------------------------------
# CLIENT
# ---------------------------------------------------------------------------
def get_ig_service() -> IGService:
    if not IG_USERNAME or not IG_PASSWORD or not IG_API_KEY:
        log.error("Missing credentials. Set IG_USERNAME, IG_PASSWORD and IG_API_KEY.")
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

    global _account_id
    try:
        _account_id = session.get("currentAccountId")
        log.info(f"IG session established | account={_account_id or 'N/A'} ({ACCOUNT_TYPE})")
    except Exception:
        pass

    return ig_service


_account_id = None  # the account the session trades on, for the dashboard's balance


# Products that are never "the market itself": fund/leveraged wrappers (a
# plain "JPMorgan" search once picked "JPMorgan Active Growth ETF"), old
# rights issues, retired markets, and IG's separate weekend-only markets.
NOT_THE_MARKET_MARKERS = [
    "Leverage", "GraniteShares", "IncomeShares", "ETP", "ETF",
    "Rights Issue", "NOT IN USE", "Weekend",
]

# Per-point contract value in IG's market names: "(£10)", "($250)", "(E25)",
# "(GBP1)", "(500oz)", "(£1 Contract)" — but not "(24 Hours)".
CONTRACT_SIZE_PATTERN = r"\((?:£|\$|€|E|GBP|USD|EUR)?([\d.]+)\s*(?:oz|Contract)?\)"


def _normalized_name(name: str) -> str:
    """'FTSE 100 Cash (£10)' -> 'ftse 100', 'GBP/EUR Mini' -> 'gbp/eur'."""
    name = re.sub(r"\([^)]*\)", " ", name.lower())
    name = re.sub(r"\b(?:cash|mini)\b", " ", name)
    return " ".join(name.split())


def _contract_size(name: str) -> float:
    """Currency is ignored — this only needs to rank versions small to large."""
    match = re.search(CONTRACT_SIZE_PATTERN, name)
    return float(match.group(1)) if match else float("inf")


def _describe(candidates: pd.DataFrame) -> str:
    return "; ".join(f"{r['epic']} = {r.get('instrumentName', '?')}" for _, r in candidates.iterrows())


def _narrow(candidates: pd.DataFrame, mask) -> pd.DataFrame:
    """Apply a preference only if something survives it."""
    narrowed = candidates[mask]
    return narrowed if len(narrowed) > 0 else candidates


def _call_with_retry(what: str, call):
    """Retry a startup lookup instead of dropping the name for the whole run:
    failures here are nearly always a rate-limit 403 that clears within a
    minute, not a missing market. Returns None only after every retry fails."""
    for wait in (*RESOLVE_RETRY_WAITS, None):
        try:
            result = call()
            reason = "no response"
        except Exception as e:
            result, reason = None, str(e) or "empty response - likely rate limited"
        if result is not None:
            return result
        if wait is None:
            log.warning(f"Giving up on {what} ({reason}).")
            return None
        log.warning(f"{what} failed ({reason}); retrying in {wait}s.")
        time.sleep(wait)


def _paced_search(ig_service: IGService, term: str):
    _rate_limiter.wait()
    return ig_service.search_markets(term)


def deal_currency(instrument: dict) -> str:
    """CURRENCY_CODE if the market offers it, else the market's own default.
    IG's API restricts a deal's currency to the instrument's listed ones;
    FX Minis such as AUD/USD are priced in their quote currency, and GBP
    orders on them came back REJECTED (reason UNKNOWN)."""
    currencies = instrument.get("currencies") or []
    codes = [c.get("code") for c in currencies]
    if not codes or CURRENCY_CODE in codes:
        return CURRENCY_CODE
    return next((c["code"] for c in currencies if c.get("isDefault")), codes[0])


# currency -> how much of it one unit of the account's currency buys (USD:
# 1.32 on a GBP account), as IG's market details last gave it. For putting
# open positions' profit in the account's currency on the dashboard.
_exchange_rates = {}


def note_exchange_rates(instrument: dict) -> None:
    """Every market's details list the currencies it deals in, each with
    IG's rate against the account's currency ("baseExchangeRate")."""
    for currency in instrument.get("currencies") or ():
        rate = _number(currency.get("baseExchangeRate"))
        if currency.get("code") and rate:
            _exchange_rates[currency["code"]] = rate


def fetch_market_details(ig_service: IGService, epic: str) -> Optional[dict]:
    _rate_limiter.wait()
    try:
        market = ig_service.fetch_market_by_epic(epic)
    except IGException as e:
        log.warning(f"IG API error fetching market details for {epic}: {e}")
        return None
    except Exception as e:
        log.warning(f"Network error fetching market details for {epic}: {e}")
        return None

    try:
        instrument = market["instrument"]
        dealing_rules = market["dealingRules"]
        snapshot = market["snapshot"]
        note_exchange_rates(instrument)
        return {
            "expiry": instrument.get("expiry", "-"),
            "currency": deal_currency(instrument),
            "min_deal_size": float(dealing_rules["minDealSize"]["value"]),
            "contract_size": float(instrument.get("contractSize") or 1),
            "min_stop": dealing_rules.get("minNormalStopOrLimitDistance") or {},  # {"value": 6, "unit": "POINTS"}
            "scaling_factor": float(snapshot.get("scalingFactor", 1)),
            "market_status": snapshot.get("marketStatus", "UNKNOWN"),
            "bid": snapshot.get("bid"),
            "offer": snapshot.get("offer"),
        }
    except (KeyError, TypeError) as e:
        log.warning(f"Unexpected market-details shape for {epic}: {e}")
        return None


def resolve_entry(ig_service: IGService, entry: str, expected_type=None) -> Optional[dict]:
    """One pool entry - "term" or "term:EPIC" - as {"term", "epic", "name"},
    or None (logged why) if it doesn't resolve cleanly."""
    if ":" in entry:
        term, explicit_epic = entry.split(":", 1)
        term, explicit_epic = term.strip(), explicit_epic.strip()
        details = _call_with_retry(
            f"'{term}' ({explicit_epic})", lambda: fetch_market_details(ig_service, explicit_epic)
        )
        if details is None:
            log.warning(f"Skipping '{term}': explicit epic '{explicit_epic}' did not resolve.")
            return None
        log.info(f"Resolved '{term}' -> epic={explicit_epic} (explicit)")
        return {"term": term, "epic": explicit_epic, "name": term}

    term = entry
    markets = _call_with_retry(f"search for '{term}'", lambda: _paced_search(ig_service, term))
    if markets is None:
        log.warning(f"Skipping '{term}': search kept failing.")
        return None
    if len(markets) == 0:
        log.warning(f"Skipping '{term}': no markets found.")
        return None

    candidates = markets
    if expected_type and "instrumentType" in candidates:
        candidates = _narrow(candidates, candidates["instrumentType"] == expected_type)

    if "instrumentName" in candidates:
        names = candidates["instrumentName"]
        noise = "|".join(re.escape(m) for m in NOT_THE_MARKET_MARKERS)
        candidates = _narrow(candidates, ~names.str.contains(noise, case=False, regex=True))

        # Exact name first — a "GBP/EUR" search lists the inverse EUR/GBP first.
        candidates = _narrow(
            candidates, candidates["instrumentName"].map(_normalized_name) == _normalized_name(term)
        )

    # Undated (rolling) markets over dated futures — no expiry to roll.
    if "expiry" in candidates:
        candidates = _narrow(candidates, candidates["expiry"].isin(["-", "DFB"]))

    if len(candidates) > 1 and "instrumentName" in candidates:
        candidates = _narrow(candidates, candidates["instrumentName"].str.contains("24 Hours", case=False))

        # Plainest name first ("BP PLC" over "BP PLC - Pfd"), then the
        # smallest contract for a small-capital bot — US 500 at £1 a point,
        # not $250. "Mini" breaks ties where names carry no size (FX).
        order = candidates["instrumentName"].map(
            lambda n: (len(_normalized_name(n)), _contract_size(n), 0 if "mini" in n.lower() else 1)
        )
        candidates = candidates.loc[order.sort_values(kind="stable").index]

    row = candidates.iloc[0]
    if len(candidates) > 1:
        log.info(
            f"'{term}' had {len(candidates)} plausible matches — auto-picked the first "
            f"(plainest name, then smallest contract). "
            f"If wrong, change its DEFAULT_POOL entry to '{term}:EPIC'. Candidates: {_describe(candidates)}"
        )

    log.info(f"Resolved '{term}' -> epic={row['epic']} ({row.get('instrumentName', term)})")
    return {"term": term, "epic": row["epic"], "name": row.get("instrumentName", term)}


def resolve_pool_epics(ig_service: IGService) -> list:
    """Best-effort resolution: SKIP (don't exit) any entry that fails to
    resolve cleanly, logging why, so one bad search term in the pool doesn't
    take the whole bot down. See module docstring for why this differs from
    ig_cfd_ema_bot.py's strict, hard-exit resolution."""
    pool = [(p, None) for p in POOL_ENTRIES] if POOL_ENTRIES else DEFAULT_POOL
    resolved = [item for item in (resolve_entry(ig_service, entry, expected_type) for entry, expected_type in pool)
                if item is not None]

    if len(resolved) == 0:
        log.critical("Nothing in the pool resolved to a usable epic. Exiting.")
        sys.exit(1)

    return resolved


# ---------------------------------------------------------------------------
# MARKET DATA / SIGNAL
# ---------------------------------------------------------------------------
class _BarBuilder:
    """Closes per epic, one per BAR_SECONDS, built from the live price
    sampled each pass instead of fetched from /prices (see "Bars are built
    locally" above).
    The newest bar is the one still forming, as IG's own last bar would be.
    Only the last STREAK_LENGTH + 1 bars are kept — all a streak needs.
    Every change is saved to `path`, and saved bars are reloaded at startup
    so a restart doesn't repeat the warm-up."""

    def __init__(self, keep: int, path: str):
        self._keep = keep
        self._path = path
        self._bars = self._load()  # epic -> [[bucket, close], ...], oldest first

    def _load(self) -> dict:
        try:
            with open(self._path) as f:
                saved = json.load(f)
            if "bars" not in saved:  # saved before the bar length could change, so 15-minute bars
                saved = {"barSeconds": 900, "bars": saved}
            if saved["barSeconds"] != BAR_SECONDS:
                log.info(f"Saved bars in {self._path} are {saved['barSeconds'] // 60}-minute ones, not "
                         f"{BAR_SECONDS // 60}-minute; every market warms up from scratch.")
                return {}
            oldest_allowed = int((time.time() - SAVED_BARS_MAX_AGE_HOURS * 3600) // BAR_SECONDS)
            bars = {
                epic: [[int(bucket), float(close)] for bucket, close in epic_bars[-self._keep:]]
                for epic, epic_bars in saved["bars"].items()
                if epic_bars and epic_bars[-1][0] >= oldest_allowed
            }
        except FileNotFoundError:
            return {}
        except Exception as e:
            log.warning(f"Couldn't read saved bars from {self._path} ({e}); every market warms up from scratch.")
            return {}
        log.info(f"Restored saved bars for {len(bars)} market(s) from {self._path}")
        return bars

    def _save(self) -> None:
        # Write-then-rename, so a crash mid-write can't leave a truncated file.
        tmp_path = self._path + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump({"barSeconds": BAR_SECONDS, "bars": self._bars}, f)
            os.replace(tmp_path, self._path)
        except OSError as e:
            log.warning(f"Couldn't save bars to {self._path}: {e}")

    def record(self, epic: str, price: float) -> list:
        """Record a price sample; returns this epic's closes, oldest first."""
        bucket = int(time.time() // BAR_SECONDS)
        bars = self._bars.setdefault(epic, [])
        if bars and bars[-1][0] == bucket:
            bars[-1][1] = price
        else:
            bars.append([bucket, price])
            del bars[:-self._keep]
        self._save()
        return [close for _, close in bars]


_bar_builder = _BarBuilder(STREAK_LENGTH + 1, BARS_FILE)


def detect_streak(closes: list) -> Optional[str]:
    """STREAK_LENGTH consecutive higher (or lower) closes in a row."""
    if len(closes) < STREAK_LENGTH + 1:
        return None
    recent = closes[-(STREAK_LENGTH + 1):]
    diffs = [b - a for a, b in zip(recent, recent[1:])]
    if all(d > 0 for d in diffs):
        return "bullish"
    if all(d < 0 for d in diffs):
        return "bearish"
    return None


# ---------------------------------------------------------------------------
# POSITIONS / ORDERS
# ---------------------------------------------------------------------------
def fetch_all_positions(ig_service: IGService) -> Optional[pd.DataFrame]:
    """One call per pass, not per symbol — it returns every open position."""
    _rate_limiter.wait()
    try:
        return ig_service.fetch_open_positions()
    except Exception as e:
        log.error(f"Error fetching positions: {e or 'empty response - likely rate limited'}")
        raise


def find_position(positions: Optional[pd.DataFrame], epic: str) -> Optional[dict]:
    # trading-ig flattens each position's nested "market"/"position" fields
    # into plain columns — "epic", "dealId", ... — with no prefix.
    if positions is None or len(positions) == 0:
        return None
    match = positions[positions["epic"] == epic]
    if len(match) == 0:
        return None
    row = match.iloc[0]
    return {
        "deal_id": row["dealId"],
        "direction": row["direction"],
        "size": float(row["size"]),
    }


def compute_point_distance(price: float, pct: float, scaling_factor: float) -> float:
    return round((price * pct) * scaling_factor, 1)


def exchange_rate(currency) -> Optional[float]:
    """How much of `currency` one unit of the account's currency buys, or None
    until a market dealt in it has been read."""
    return 1.0 if currency == CURRENCY_CODE else _exchange_rates.get(currency)


def money_per_point(details: dict) -> Optional[float]:
    """What a minimum-size trade gains or loses per point of stop distance, in
    the account's currency. A point is the price over the scaling factor -
    one pip on the FX Minis, one index point on FTSE 100 (£1 there)."""
    rate = exchange_rate(details["currency"])
    if not rate:
        return None
    return details["min_deal_size"] * details["contract_size"] / details["scaling_factor"] / rate


def closest_stop(details: dict, price: float) -> float:
    """IG's minimum stop distance for the market, in points."""
    rule = details.get("min_stop") or {}
    value = _number(rule.get("value")) or 0.0
    return price * value / 100 * details["scaling_factor"] if rule.get("unit") == "PERCENTAGE" else value


def stop_distance_for(name: str, epic: str, details: dict, price: float, direction: str) -> Optional[float]:
    """STOP_LOSS_PERCENT from the price, in points, pulled in closer where a
    stop-out would lose more than MAX_TRADE_LOSS. None (logged) if even IG's
    closest allowed stop would lose more than that: the trade is skipped."""
    distance = compute_point_distance(price, STOP_LOSS_PCT, details["scaling_factor"])
    if not MAX_TRADE_LOSS:
        return distance
    per_point = money_per_point(details)
    if per_point is None:
        log.info(f"{name} ({epic}): {direction} signal, but there's no {details['currency']} exchange rate yet to "
                 f"size its stop; skipping.")
        return None
    capped = math.floor(MAX_TRADE_LOSS / per_point * 10 + 1e-9) / 10  # IG takes tenths of a point
    if capped >= distance:
        return distance
    closest = closest_stop(details, price)
    if capped < closest:
        log.info(f"{name} ({epic}): {direction} signal, but even IG's closest stop ({closest:g} points) would lose "
                 f"{closest * per_point:.2f} {CURRENCY_CODE}, more than IG_MAX_TRADE_LOSS={MAX_TRADE_LOSS:g}; "
                 f"skipping.")
        return None
    return capped


def limit_distance_for(details: dict, price: float) -> float:
    """TAKE_PROFIT_PCT from the price, in points, pulled in closer where
    reaching it would make more than MAX_TRADE_PROFIT - but never closer
    than IG's minimum distance."""
    distance = compute_point_distance(price, TAKE_PROFIT_PCT, details["scaling_factor"])
    per_point = money_per_point(details) if MAX_TRADE_PROFIT else None
    if not per_point:  # off, or no exchange rate yet (the stop skips the trade then anyway)
        return distance
    capped = math.floor(MAX_TRADE_PROFIT / per_point * 10 + 1e-9) / 10  # IG takes tenths of a point
    if capped >= distance:
        return distance
    return max(capped, math.ceil(closest_stop(details, price) * 10 - 1e-9) / 10)


def paused_window(now: float = None) -> Optional[str]:
    """The IG_PAUSE_TIMES window `now` falls in, e.g. "13:00-17:00", or None."""
    t = clock.local("london", time.time() if now is None else now).time()
    for start, end in PAUSE_TIMES:
        if (start <= t < end) if start < end else (t >= start or t < end):
            return f"{start:%H:%M}-{end:%H:%M}"
    return None


def deal_outcome(result) -> str:
    """'ACCEPTED', or e.g. 'REJECTED (INSUFFICIENT_FUNDS)' — IG's deal
    confirmation says why a deal failed, and the status alone doesn't."""
    if not isinstance(result, dict):
        return str(result)
    status, reason = result.get("dealStatus", "?"), result.get("reason")
    return status if status == "ACCEPTED" or not reason else f"{status} ({reason})"


_open_problem = ""  # why the last open_position() wasn't accepted, for a broadcast's answer


def open_position(ig_service: IGService, epic: str, name: str, details: dict, price: float, direction: str) -> bool:
    """A minimum-size market order with the stop and limit attached. True if
    IG accepted it. (IG's API here takes no label for an order, so a
    broadcast's is known by its deal ID.)"""
    global _open_problem
    _open_problem = ""
    if rollover.entries_paused():
        log.info(f"{name} ({epic}): {direction} signal, but it's too near the daily rollover to open a trade; skipping.")
        _open_problem = "too near the daily rollover"
        return False
    blocked = loss_limits.why_no_new_trades()
    if blocked:
        log.info(f"{name} ({epic}): {direction} signal, but {blocked}; skipping.")
        _open_problem = blocked
        return False
    # Always the market's minimum deal size — the smallest trade IG allows,
    # and still thousands of pounds of exposure (README: "IG position sizing").
    size = details["min_deal_size"]
    stop_distance = stop_distance_for(name, epic, details, price, direction)
    if stop_distance is None:
        _open_problem = "its stop can't be placed within the loss limit (see the log)"
        return False
    limit_distance = limit_distance_for(details, price)
    try:
        result = ig_service.create_open_position(
            currency_code=details["currency"], direction=direction, epic=epic, expiry=details["expiry"],
            force_open=True, guaranteed_stop=False, level=None, limit_distance=limit_distance,
            limit_level=None, order_type="MARKET", quote_id=None, size=size,
            stop_distance=stop_distance, stop_level=None, trailing_stop=False, trailing_stop_increment=None,
        )
        accepted = isinstance(result, dict) and result.get("dealStatus") == "ACCEPTED"
        if accepted:
            own_trades.add(result.get("dealId"), *(d.get("dealId") for d in result.get("affectedDeals") or ()))
            loss_limits.open_positions += 1
        else:
            _open_problem = deal_outcome(result)
        log.info(f"{direction} submitted -> {name} ({epic}) size={size} {details['currency']} stop_dist={stop_distance} "
                 f"limit_dist={limit_distance} result={deal_outcome(result)}")
        return accepted
    except Exception as e:
        log.error(f"Error submitting {direction} for {name} ({epic}): {e}")
        _open_problem = str(e) or "empty response - likely rate limited"
        return False


def close_position(ig_service: IGService, epic: str, name: str, position: dict, details: dict, reason: str) -> bool:
    """True if IG accepted the close."""
    close_direction = "SELL" if position["direction"] == "BUY" else "BUY"
    try:
        # Close by deal ID alone: IG rejects a close that also names the epic
        # and expiry ("validation.mutual-exclusive-value.request").
        result = ig_service.close_open_position(
            deal_id=position["deal_id"], direction=close_direction, epic=None, expiry=None,
            level=None, order_type="MARKET", quote_id=None, size=position["size"],
        )
        outcome = deal_outcome(result)
        log.info(f"CLOSE submitted ({reason}) -> {name} ({epic}) result={outcome}")
        return outcome == "ACCEPTED"
    except Exception as e:
        log.error(f"Error closing position for {name} ({epic}) ({reason}): {e}")
        return False


def close_own_positions(ig_service: IGService, positions: Optional[pd.DataFrame], pool: list, reason: str):
    """Close every position this bot opened. Other bots' and hand-made
    positions are left alone. Returns `positions` without the ones it closed."""
    if positions is None or len(positions) == 0:
        own_trades.keep_open(())
        return positions
    own_trades.keep_open(positions["dealId"])
    names = {item["epic"]: item["name"] for item in pool}
    closed = []
    for _, row in positions.iterrows():
        if row["dealId"] not in own_trades:
            continue
        position = {"deal_id": row["dealId"], "direction": row["direction"], "size": float(row["size"])}
        if close_position(ig_service, row["epic"], names.get(row["epic"], row["epic"]), position, None, reason):
            closed.append(row["dealId"])
    return positions[~positions["dealId"].isin(closed)]


def close_before_rollover(ig_service: IGService, positions: Optional[pd.DataFrame], pool: list):
    """Close every position this bot opened, so none is charged a night's funding."""
    return close_own_positions(ig_service, positions, pool, rollover.REASON)


# ---------------------------------------------------------------------------
# STAGNANCY TIMEOUT (shared/stagnancy.py)
# ---------------------------------------------------------------------------
watch = None  # the timeout, set up in run_bot()


def stagnancy_pass(ig_service: IGService, positions: Optional[pd.DataFrame], pool: list):
    """The stagnancy timeout over the positions this bot opened: one that has
    gone nowhere for a while is logged (shadow) or closed (enforce), freeing
    its slot. The prices are the positions' own (IG lists each with its
    market's bid, offer and status), so this costs no extra requests.
    Returns `positions` without any it closed."""
    now = time.time()
    rows = [p for p in _pool_positions(positions, pool) if p["dealId"] in own_trades]
    names = {item["epic"]: item["name"] for item in pool}

    def tradeable(p) -> bool:
        status = p.get("marketStatus")
        bid, offer = _number(p.get("bid")), _number(p.get("offer"))
        return bool(bid and offer) and (status == "TRADEABLE" if isinstance(status, str) else True)

    for epic in {p["epic"] for p in rows}:
        p = next(r for r in rows if r["epic"] == epic)
        watch.observe(epic, now, _number(p.get("bid")), _number(p.get("offer")), tradeable(p))
    held = []
    for p in rows:
        level, stop = _number(p["level"]), _number(p.get("stopLevel"))
        size, contract_size = _number(p["size"]), _number(p.get("contractSize"))
        rate = exchange_rate(p.get("currency"))
        # What it stands to lose at its stop, booked the way position_pnl() books a move.
        risk = (abs(level - stop) * size * contract_size / rate
                if None not in (level, stop, size, contract_size) and rate else None)
        held.append((stagnancy.Held(
            key=p["dealId"], symbol=p["epic"], direction="long" if p["direction"] == "BUY" else "short",
            size=size or 0.0, entry=level, opened_at=_utc_seconds(p.get("createdDateUTC")), risk=risk,
            pnl=position_pnl(p),  # at the price it would close at, so net of the spread
            refs=(p["dealId"],), aliases=(names.get(p["epic"], ""),),
        ), tradeable(p)))

    closed = []

    def close(h):
        result = close_for_timeout(ig_service, h, names.get(h.symbol, h.symbol))
        if result.get("done"):
            closed.append(h.key)
            loss_limits.open_positions -= 1
        return result

    watch.run(held, now, close)
    return positions if not closed else positions[~positions["dealId"].isin(closed)]


def close_for_timeout(ig_service: IGService, held, name: str) -> dict:
    """Close one of the bot's positions at market for the stagnancy timeout,
    by deal ID. IG's confirmation says where it closed and what it made."""
    try:
        result = ig_service.close_open_position(
            deal_id=held.key, direction="SELL" if held.direction == "long" else "BUY", epic=None, expiry=None,
            level=None, order_type="MARKET", quote_id=None, size=held.size,
        )
    except Exception as e:
        log.error(f"Error closing position for {name} ({held.symbol}) ({stagnancy.TIMEOUT_REASON}): {e}")
        return {"done": False, "problem": str(e) or "empty response - likely rate limited"}
    outcome = deal_outcome(result)
    log.info(f"CLOSE submitted ({stagnancy.TIMEOUT_REASON}) -> {name} ({held.symbol}) result={outcome}")
    if outcome != "ACCEPTED":
        # A deal its stop or limit closed a moment before comes back REJECTED: the next pass finds it gone.
        return {"done": False, "problem": outcome}
    profit = _number(result.get("profit"))  # in the deal's currency
    in_account_currency = result.get("profitCurrency") in (None, CURRENCY_CODE)
    # IG's trade history names a trade by the end of the closing deal's ID.
    return {"done": True, "price": _number(result.get("level")), "pnl": profit if in_account_currency else None,
            "refs": (str(result["dealId"])[-8:],) if result.get("dealId") else ()}


# ---------------------------------------------------------------------------
# LOSS LIMITS (MAX_POSITIONS, DAILY_LOSS_LIMIT, DAILY_GIVEBACK - MAX_TRADE_LOSS
# is in the stop)
# ---------------------------------------------------------------------------
DAILY_LOSS_REASON = "daily loss limit"  # the close reasons the log shows
GIVEBACK_REASON = "daily giveback"


class _LossLimits:
    """Where the position cap, the daily loss limit and the daily giveback
    stand. The main loop calls update() once a pass, with that pass's
    positions."""

    def __init__(self):
        self.open_positions = 0   # in the pool's markets; open_position() adds the ones it opens
        self.day_start = None     # the rollover the current trading day began at
        self.closed_pl = 0.0      # the day's closed trades in the pool's markets
        self.read_at = None       # time.monotonic() of that read; None = read on the next pass
        self.own_open = set()     # the bot's positions open at the last pass
        self.day_pl = 0.0         # closed_pl + the bot's open positions, as of the last pass
        self.best = 0.0           # the day's best day_pl so far (its start counts as 0)
        self.stopped = None       # why the bot stopped for the day (the close reason), or None

    def why_no_new_trades(self) -> Optional[str]:
        """Why a new trade mustn't open now, or None if it may."""
        until = f"no new trades until the rollover at {rollover.next_rollover_uk()} UK"
        if self.stopped == GIVEBACK_REASON:
            return (f"the day gave back IG_DAILY_GIVEBACK={DAILY_GIVEBACK:g} {CURRENCY_CODE} from its best "
                    f"({self.best:+.2f}) - {until}")
        if self.stopped:
            return f"the day's loss reached IG_DAILY_LOSS_LIMIT={DAILY_LOSS_LIMIT:g} {CURRENCY_CODE} - {until}"
        if MAX_POSITIONS and self.open_positions >= MAX_POSITIONS:
            return f"{self.open_positions} positions are open already (IG_MAX_POSITIONS={MAX_POSITIONS})"
        return None

    def update(self, ig_service: IGService, positions: Optional[pd.DataFrame], pool: list):
        """Counts the open positions and checks the day's loss and giveback,
        closing the bot's positions once either is reached. Returns
        `positions` without any it closed."""
        rows = _pool_positions(positions, pool)
        self.open_positions = len(rows)
        if not DAILY_LOSS_LIMIT and not DAILY_GIVEBACK:
            return positions

        day_start = rollover.trading_day_start()
        if day_start != self.day_start:
            if self.stopped:
                log.info(f"A new trading day has started: the {self.stopped} is reset, new trades are allowed again.")
            self.day_start, self.stopped, self.read_at, self.best = day_start, None, None, 0.0
        own = [p for p in rows if p["dealId"] in own_trades]
        own_open = {p["dealId"] for p in own}
        if self.own_open - own_open:  # a stop, a take-profit or a close since the last pass
            self.read_at = None
        self.own_open = own_open
        if self.read_at is None or time.monotonic() - self.read_at >= DAY_TRADES_EVERY_SECONDS:
            try:
                trades = fetch_closed_trades(ig_service, pool, since=day_start, page_size=500)
                self.closed_pl = sum(t["pnl"] or 0.0 for t in trades)
                self.read_at = time.monotonic()
                # The best the closed trades reached in turn - so a restart remembers a good morning.
                running = 0.0
                for t in sorted(trades, key=lambda t: t["closedAt"]):
                    running += t["pnl"] or 0.0
                    self.best = max(self.best, running)
            except Exception as e:  # keep the last total and try again next pass
                log.warning(f"Couldn't read the day's closed trades for the daily loss limits: "
                            f"{e or 'empty response - likely rate limited'}")

        self.day_pl = self.closed_pl + sum(position_pnl(p) or 0.0 for p in own)
        self.best = max(self.best, self.day_pl)
        if not self.stopped and DAILY_LOSS_LIMIT and self.day_pl <= -DAILY_LOSS_LIMIT:
            self.stopped = DAILY_LOSS_REASON
            log.warning(f"Daily loss limit reached: the day is {self.day_pl:.2f} {CURRENCY_CODE} (closed trades "
                        f"{self.closed_pl:.2f}), limit {DAILY_LOSS_LIMIT:g}. Closing this bot's {len(own)} "
                        f"position(s); no new trades until the rollover at {rollover.next_rollover_uk()} UK.")
        elif (not self.stopped and DAILY_GIVEBACK and self.best >= DAILY_GIVEBACK
              and self.day_pl <= self.best - DAILY_GIVEBACK):
            self.stopped = GIVEBACK_REASON
            log.warning(f"Daily giveback reached: the day is {self.day_pl:+.2f} {CURRENCY_CODE} (closed trades "
                        f"{self.closed_pl:+.2f}), down {DAILY_GIVEBACK:g} from its best of {self.best:+.2f}. "
                        f"Closing this bot's {len(own)} position(s); no new trades until the rollover at "
                        f"{rollover.next_rollover_uk()} UK.")
        if self.stopped and own:
            positions = close_own_positions(ig_service, positions, pool, self.stopped)
            self.open_positions = len(_pool_positions(positions, pool))
        return positions


def _pool_positions(positions: Optional[pd.DataFrame], pool: list) -> list:
    """The open positions (rows) in the pool's markets."""
    if positions is None or len(positions) == 0:
        return []
    epics = {item["epic"] for item in pool}
    return [p for _, p in positions.iterrows() if p["epic"] in epics]


loss_limits = _LossLimits()


def close_from_dashboard(ig_service: IGService, pool: list, symbol: str, ref, direction: str, size) -> str:
    """A close asked for on the dashboard (DASHBOARD_COMMANDS=1): the
    position it showed as `ref` (IG's deal ID) - all of it, or `size` of
    it - if it's still open on the same side. Answers what happened, or
    raises CommandError. The dashboard names markets as this bot does."""
    item = next((i for i in pool if symbol in (i["name"], i["epic"])), None)
    if item is None:
        raise CommandError(f"{symbol} isn't one of this bot's markets.")
    positions = fetch_all_positions(ig_service)
    match = None
    for _, row in (positions.iterrows() if positions is not None else ()):
        if row["epic"] == item["epic"] and (not ref or row["dealId"] == ref):
            match = row
            break
    if match is None:
        raise CommandError(f"There's no open {symbol} position now - it may have closed already.")
    side = "long" if match["direction"] == "BUY" else "short"
    if side != direction:
        raise CommandError(f"The {symbol} position is {side} now, not {direction}, so it was left alone.")
    held = float(match["size"])
    amount = held if size is None or size >= held else size
    _rate_limiter.wait()
    try:
        # By deal ID alone: IG rejects a close that also names the epic and expiry.
        result = ig_service.close_open_position(
            deal_id=match["dealId"], direction="SELL" if match["direction"] == "BUY" else "BUY", epic=None,
            expiry=None, level=None, order_type="MARKET", quote_id=None, size=amount,
        )
    except Exception as e:
        raise CommandError(f"IG refused: {e or 'empty response - likely rate limited'}") from None
    outcome = deal_outcome(result)
    log.info(f"CLOSE submitted (from the dashboard) -> {item['name']} ({item['epic']}) {amount:g} result={outcome}")
    if not outcome.startswith("ACCEPTED"):
        raise CommandError(f"IG didn't close it: {outcome}")
    return f"Closed {amount:g} of the {item['name']} {side} position ({held:g}): {outcome}."


# ---------------------------------------------------------------------------
# BROADCAST TRADES FROM THE DASHBOARD (shared/broadcast.py)
# ---------------------------------------------------------------------------
guests = []       # pool items {"term", "epic", "name"}: a broadcast trade's market outside the pool - managed, never traded
left_guests = {}  # epic -> (item, until): a guest whose trade closed, still in the dashboard reports a while
LEFT_GUEST_SECONDS = 3600  # IG's history lists a closed trade by its market's name, so the name is kept this long


def managed(pool: list) -> list:
    """The pool, plus the markets outside it holding a broadcast trade."""
    epics = {item["epic"] for item in pool}
    return [*pool, *(g for g in guests if g["epic"] not in epics)]


def reported(pool: list) -> list:
    """What the dashboard reports cover: the managed markets, and those of
    broadcast trades that closed lately (their closed trades)."""
    now = time.time()
    for epic in [e for e, (_, until) in left_guests.items() if until <= now]:
        del left_guests[epic]
    items = managed(pool)
    epics = {item["epic"] for item in items}
    return items + [item for epic, (item, _) in left_guests.items() if epic not in epics]


def restore_guests(ig_service: IGService, pool: list) -> None:
    """After a restart: the markets outside the pool its open broadcast trades are in."""
    epics = {item["epic"] for item in pool}
    for epic in sorted(broadcasts.open_symbols() - epics):
        name = next(iter(broadcasts.aliases(epic)), epic)
        if fetch_market_details(ig_service, epic) is None:
            log.warning(f"{name} ({epic}): couldn't look up its broadcast trade's market; IG still holds its stop "
                        f"and limit.")
            continue
        guests.append({"term": name, "epic": epic, "name": name})
        log.info(f"{name} ({epic}): managing its broadcast trade (not in the pool).")


def broadcast_plan(ig_service: IGService, pool: list, request, now: float, most: float = None) -> dict:
    """The trade this bot would make for a broadcast, every limit applied -
    or broadcast.Declined saying why not."""
    found = symbols.candidates("ig", request.symbol)
    item = candidate = None
    for candidate in found:
        parts = {p.strip().upper() for p in candidate.code.split(":") if p.strip()}
        item = next((i for i in managed(pool) if {i["epic"].upper(), i["name"].upper(), i["term"].upper()} & parts),
                    None) or resolve_entry(ig_service, candidate.code)
        if item is not None:
            break
    if item is None:
        raise broadcast.Declined(broadcast.UNAVAILABLE, f"{request.symbol} isn't available on IG"
                                 + (f" (looked for {', '.join(c.code for c in found)})." if found else "."))
    epic, name = item["epic"], item["name"]
    if rollover.entries_paused(now):
        raise broadcast.Declined(broadcast.CLOSED, f"{name}: too near the daily rollover to open a trade.")
    if loss_limits.stopped:
        raise broadcast.Declined(broadcast.PAUSED, f"{loss_limits.why_no_new_trades()[0].upper()}"
                                                   f"{loss_limits.why_no_new_trades()[1:]}.")
    positions = fetch_all_positions(ig_service)
    if find_position(positions, epic) is not None:
        raise broadcast.Declined(broadcast.NO_SLOT, f"A {name} position is already open on the account.")
    open_count = len(_pool_positions(positions, managed(pool)))
    if MAX_POSITIONS and open_count >= MAX_POSITIONS:
        raise broadcast.Declined(broadcast.NO_SLOT, f"{open_count} positions are already open "
                                                    f"(IG_MAX_POSITIONS={MAX_POSITIONS}).")
    if watch is not None and watch.cooling(epic, now):
        raise broadcast.Declined(broadcast.RISK, f"{name}: the stagnancy timeout closed it lately (cooldown).")
    details = fetch_market_details(ig_service, epic)
    if details is None or details["market_status"] != "TRADEABLE" or details["bid"] is None or details["offer"] is None:
        status = details["market_status"] if details is not None else "no market details"
        raise broadcast.Declined(broadcast.CLOSED, f"{name}: not tradeable right now ({status}).")

    price = (float(details["bid"]) + float(details["offer"])) / 2.0
    direction = "BUY" if request.side == "buy" else "SELL"
    stop_distance = stop_distance_for(name, epic, details, price, direction)
    if stop_distance is None:
        raise broadcast.Declined(broadcast.RISK, f"{name}: its stop can't be placed within IG_MAX_TRADE_LOSS="
                                                 f"{MAX_TRADE_LOSS:g} {CURRENCY_CODE}, or there's no exchange rate for "
                                                 f"it yet - the bot log says which.")
    size = details["min_deal_size"]
    if most is not None and most < size - 1e-12:
        raise broadcast.Declined(broadcast.RISK, f"{name}: IG's smallest trade is now {size:g}, more than the {most:g} "
                                                 f"previewed.")
    per_point = money_per_point(details)
    exposure = price * details["scaling_factor"] * per_point if per_point else None
    capped = ""
    if request.quantity is not None:
        if exposure is not None and request.quantity < exposure * (1 - 1e-9):
            raise broadcast.Declined(broadcast.RISK, f"{name}: IG's smallest trade ({size:g}) is worth about "
                                                     f"{exposure:,.2f} {CURRENCY_CODE}, more than the "
                                                     f"{request.quantity:,.2f} asked for.")
        capped = "IG always trades the market's minimum size"
    sign = 1 if direction == "BUY" else -1
    entry = float(details["offer"] if sign > 0 else details["bid"])
    limit_distance = limit_distance_for(details, price)
    rule = watch.book.rule(epic, (name,)) if watch is not None else None
    exits = (f"stop-loss {STOP_LOSS_PCT * 100:g}% (closer if it would lose over {MAX_TRADE_LOSS:g}) / take-profit "
             f"{TAKE_PROFIT_PCT * 100:g}%"
             + (f" (closer if it would make over {MAX_TRADE_PROFIT:g})" if MAX_TRADE_PROFIT else "") + " at IG; "
             + (f"closed {rollover.FLAT_MINUTES} min before the rollover" if rollover.FLAT_MINUTES else "held overnight")
             + ("; streak reversal" if any(i["epic"] == epic for i in pool) else "; managed until it closes (not in the pool)")
             + (f"; stagnancy timeout ({rule.mode})" if rule and rule.mode != "off" else ""))
    return {"item": item, "details": details, "price": price, "direction": direction, "capped": capped,
            "figures": broadcast.figures(
                symbol=epic, name=name, size=size, sizeUnit="contracts",
                exposure=round(exposure, 2) if exposure is not None else None,
                risk=round(stop_distance * per_point, 2) if per_point else None, currency=CURRENCY_CODE, entry=entry,
                stopLoss=entry - sign * stop_distance / details["scaling_factor"],
                takeProfit=entry + sign * limit_distance / details["scaling_factor"], accountMode=ACCOUNT_MODE,
                exits=exits, capped=capped or None, standIn=candidate.canonical if candidate.stand_in else None,
                previewedAt=now)}


def broadcast_preview(ig_service: IGService, pool: list, command: dict) -> tuple:
    """What this bot would do with a broadcast trade: (a line, {figures})."""
    request = broadcast.Request.parse(command)
    plan = broadcast_plan(ig_service, pool, request, time.time())
    f = plan["figures"]
    line = (f"Would {request.side} {f['size']:g} of {f['name']} ({f['symbol']}) at ~{f['entry']:g}: stop-loss "
            f"~{f['stopLoss']:g}, take-profit ~{f['takeProfit']:g}" + (f" ({plan['capped']})" if plan["capped"] else ""))
    log.info(f"Broadcast #{request.id}: {line}")
    return line + ".", f


def broadcast_open(ig_service: IGService, pool: list, command: dict) -> tuple:
    """Open a broadcast trade the admin confirmed - every limit checked again
    on fresh prices, never bigger than previewed. (a line, {figures})."""
    now = time.time()
    request = broadcast.Request.parse(command)
    broadcasts.check_new(request)
    request.check_age(now)
    plan = broadcast_plan(ig_service, pool, request, now, most=request.size)
    item = plan["item"]
    epic, name = item["epic"], item["name"]
    broadcasts.opening(request, epic, (name,), now)
    before = set(own_trades.ids)
    if not open_position(ig_service, epic, name, plan["details"], plan["price"], plan["direction"]):
        broadcasts.failed(request)
        raise CommandError(f"IG didn't accept it: {_open_problem or 'see the bot log'}.")
    refs = sorted(own_trades.ids - before)
    broadcasts.opened(request, refs, now)
    if all(i["epic"] != epic for i in pool):
        guests.append(item)
        left_guests.pop(epic, None)
        log.info(f"{name} ({epic}): not in the pool - managing its broadcast trade until it closes, never trading it "
                 f"on a streak.")
    entry = plan["figures"]["entry"]
    verb = "Bought" if request.side == "buy" else "Sold"
    size = plan["figures"]["size"]
    return (f"{verb} {size:g} of {name} at ~{entry:g}" + (f" (deal {refs[0]})" if refs else "") + "."), \
        {"symbol": epic, "size": size, "price": entry, **({"ref": refs[0]} if refs else {})}


def broadcast_symbols(pool: list) -> list:
    """The instrument names the dashboard can offer for this bot."""
    return sorted(set(symbols.names("ig")) | {symbols.canonical(i["name"]) or i["name"] for i in pool})


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
# Open positions come free with every pass. The account costs one request
# every ACCOUNT_EVERY_SECONDS and closed trades one every
# TRADES_EVERY_SECONDS, both through the same pacer as everything else, so
# a pass takes a little longer but IG's limit still holds. (Other brokers'
# bots send their account every 15 seconds; IG's request limit is too tight.)
ACCOUNT_EVERY_SECONDS = 60
TRADES_EVERY_SECONDS = 300
TRADES_LOOKBACK_DAYS = 7
_last_trades_fetch = 0.0


def _number(value) -> Optional[float]:
    """A float, or None for anything missing — pandas fills gaps with NaN,
    which isn't valid JSON and would get the whole report refused."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _money(text) -> Optional[float]:
    """IG's '£-12.34' / 'E1,234.50' amounts as numbers."""
    return _number(re.sub(r"[^\d.\-]", "", str(text))) if isinstance(text, str) else _number(text)


def _utc_seconds(value) -> Optional[float]:
    """IG's UTC times ('2026-09-28T14:03:12', sometimes with milliseconds)
    as Unix seconds, so the dashboard can't take them for local time."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.rstrip("Z")).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def position_pnl(p) -> Optional[float]:
    """An open position's profit in the account's currency. IG's REST API
    doesn't give it, so it's worked out the way IG books it: the move since
    opening x size x contract size, in the position's currency, converted
    at IG's rate. Checked against IG's account total and its closed-trade
    history. None until a rate for a foreign-currency position is known
    (the first pass fetches them)."""
    is_long = p["direction"] == "BUY"
    close, level = _number(p["bid"] if is_long else p["offer"]), _number(p["level"])
    size, contract_size = _number(p["size"]), _number(p.get("contractSize"))
    rate = exchange_rate(p.get("currency"))
    if None in (close, level, size, contract_size) or not rate:
        return None
    return round((close - level) * (1 if is_long else -1) * size * contract_size / rate, 2)


def dashboard_positions(positions: Optional[pd.DataFrame], pool: list) -> list:
    """This bot's open positions in the dashboard's shape."""
    names = {item["epic"]: item["name"] for item in pool}
    rows = []
    if positions is None or len(positions) == 0:
        return rows
    for _, p in positions.iterrows():
        if p["epic"] not in names:
            continue
        is_long = p["direction"] == "BUY"
        rows.append({
            "ref": p["dealId"],
            "symbol": names[p["epic"]],
            "direction": "long" if is_long else "short",
            "size": _number(p["size"]),
            "entryPrice": _number(p["level"]),
            "currentPrice": _number(p["bid"] if is_long else p["offer"]),
            "pnl": position_pnl(p),
            "stopLoss": _number(p.get("stopLevel")),
            "takeProfit": _number(p.get("limitLevel")),
            "openedAt": _utc_seconds(p.get("createdDateUTC")),
        })
    return rows


def fetch_account(ig_service: IGService) -> Optional[dict]:
    """Balance, equity and unrealised P/L of the account the session trades on."""
    _rate_limiter.wait()
    accounts = ig_service.fetch_accounts()
    if len(accounts) == 0:
        return None
    current = accounts[accounts["accountId"] == _account_id]
    row = (current if len(current) else accounts).iloc[0]
    balance, unrealized = _number(row["balance"]), _number(row["profitLoss"])
    if balance is None:
        return None
    return {"balance": balance, "equity": balance + (unrealized or 0.0), "unrealizedPl": unrealized}


def history_market_name(name) -> str:
    """The market's name as a transaction gives it. A market dealt in another
    currency comes as e.g. 'GBP/EUR Mini converted at 0.864586683820944'."""
    return re.sub(r"\s+converted at [\d.]+$", "", str(name or ""))


def fetch_closed_trades(ig_service: IGService, pool: list, since: Optional[float] = None, page_size: int = 200) -> list:
    """Closed trades in this bot's markets since `since` (Unix seconds; the
    last week by default), newest first, from IG's transaction history
    (which has the realised profit, costs included)."""
    names = {item["name"] for item in pool}
    epics = {item["name"]: item.get("epic") for item in pool}
    if since is None:
        since = time.time() - TRADES_LOOKBACK_DAYS * 86400
    _rate_limiter.wait()
    # IG reads "from" as UK time, so a UTC time asks for up to an hour more; the check below drops it.
    history = ig_service.fetch_transaction_history(
        trans_type="ALL_DEAL", from_date=datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        page_size=page_size,
    )
    trades = []
    for _, t in history.iterrows():
        size = _number(str(t.get("size", "")).replace("+", ""))
        closed_at = _utc_seconds(t.get("dateUtc"))
        name = history_market_name(t.get("instrumentName"))
        if name not in names or not t.get("reference") or not size or closed_at is None or closed_at < since:
            continue
        trades.append({
            "ref": str(t["reference"]),
            "symbol": name,
            "direction": "short" if size < 0 else "long",
            "size": abs(size),
            "entryPrice": _number(t.get("openLevel")),
            "exitPrice": _number(t.get("closeLevel")),
            "openedAt": _utc_seconds(t.get("openDateUtc")),
            "closedAt": closed_at,
            "pnl": _money(t.get("profitAndLoss")),
            **stagnancy_fields(str(t["reference"]), epics.get(name), _utc_seconds(t.get("openDateUtc"))),
        })
    return trades


def stagnancy_fields(ref: str, epic, opened_at) -> dict:
    """The stagnancy timeout's fields for a closed trade: by its closing deal,
    or - for one it only watched - by market and opening time, as IG's
    history doesn't name the deal that opened a trade."""
    if watch is None:
        return {}
    return watch.fields_for(ref) or watch.fields_matching(epic, opened_at)


def report_to_dashboard(ig_service: IGService, positions: Optional[pd.DataFrame], pool: list) -> None:
    """Hands the dashboard reporter this pass's positions, plus the account
    and closed trades when they're due. A failure only skips them."""
    global _last_trades_fetch
    if not dashboard.enabled:
        return
    # Broadcast trades no longer open have closed; their markets outside the pool stop being managed.
    now = time.time()
    own_open = set() if positions is None or len(positions) == 0 else {
        row["epic"] for _, row in positions.iterrows() if row["dealId"] in own_trades}
    broadcasts.sync(own_open, now)
    broadcasts.prune(now)
    for guest in [g for g in guests if g["epic"] not in broadcasts.open_symbols()]:
        guests.remove(guest)
        left_guests[guest["epic"]] = (guest, now + LEFT_GUEST_SECONDS)
    try:
        report = {"positions": dashboard_positions(positions, pool)}
        if dashboard.due(ACCOUNT_EVERY_SECONDS):
            report["account"] = fetch_account(ig_service)
            if time.monotonic() - _last_trades_fetch >= TRADES_EVERY_SECONDS:
                _last_trades_fetch = time.monotonic()
                report["trades"] = fetch_closed_trades(ig_service, pool)
        dashboard.update(**report)
    except Exception as e:
        log.warning(f"Couldn't gather this pass's dashboard report: {e or 'empty response - likely rate limited'}")


# ---------------------------------------------------------------------------
# TRADING CYCLE
# ---------------------------------------------------------------------------
def trading_cycle(ig_service: IGService, item: dict, positions: Optional[pd.DataFrame]) -> None:
    epic, name = item["epic"], item["name"]

    details = fetch_market_details(ig_service, epic)
    if details is None:
        return
    if details["market_status"] != "TRADEABLE":
        log.info(f"{name} ({epic}) | market_status={details['market_status']} — skipping this pass.")
        return

    if details["bid"] is None or details["offer"] is None:
        log.warning(f"{name} ({epic}) | no live bid/offer in its snapshot — skipping this pass.")
        return

    position = find_position(positions, epic)

    price = (float(details["bid"]) + float(details["offer"])) / 2.0
    closes = _bar_builder.record(epic, price)
    position_desc = f"{position['direction']} size={position['size']}" if position else "FLAT"

    if len(closes) < STREAK_LENGTH + 1:
        log.info(f"{name} ({epic}) | price={price:.2f} | warming up "
                 f"({len(closes)}/{STREAK_LENGTH + 1} bars) | position={position_desc}")
        return

    signal = detect_streak(closes)
    log.info(f"{name} ({epic}) | price={price:.2f} | streak={signal or 'none'} | position={position_desc}")
    if position is not None and watch is not None and watch.closing(position["deal_id"]):
        log.info(f"{name} ({epic}): its {stagnancy.TIMEOUT_REASON} close is under way - nothing else done with it.")
        return
    if position is None and signal and watch is not None and watch.cooling(epic, time.time()):
        # only with SCANNER_STAGNANT_COOLDOWN set
        log.info(f"{name} ({epic}): {signal} streak, but the stagnancy timeout closed it lately (cooldown); skipping.")
        return
    window = paused_window() if position is None and signal else None
    if window:
        log.info(f"{name} ({epic}): {signal} streak, but new trades are paused {window} UK (IG_PAUSE_TIMES); "
                 f"skipping.")
        return

    closed = False
    if signal == "bullish":
        if position is None:
            open_position(ig_service, epic, name, details, price, "BUY")
        elif position["direction"] == "SELL":
            closed = close_position(ig_service, epic, name, position, details, "bullish reversal")
    elif signal == "bearish":
        if position is None:
            open_position(ig_service, epic, name, details, price, "SELL")
        elif position["direction"] == "BUY":
            closed = close_position(ig_service, epic, name, position, details, "bearish reversal")
    if closed:
        loss_limits.open_positions -= 1


# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------
def sleep_after_error(consecutive_errors: int) -> None:
    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
        log.critical(f"Reached {MAX_CONSECUTIVE_ERRORS} consecutive errors. Exiting for safety.")
        sys.exit(1)
    backoff = min(ERROR_BACKOFF_SECONDS * consecutive_errors, 300)
    log.info(f"Retrying in {backoff}s...")
    time.sleep(backoff)


def run_bot() -> None:
    ig_service = get_ig_service()
    log.info("Resolving pool (paced to IG's ~30 requests/minute limit — takes about a minute)...")
    pool = resolve_pool_epics(ig_service)

    log.info("=" * 78)
    log.info("IG Momentum Streak Scanner starting — DEMO ACCOUNT ONLY — HIGH RISK / EXPERIMENTAL")
    log.info(f"Resolved {len(pool)}/{len(POOL_ENTRIES or DEFAULT_POOL)} pool entries")
    log.info(
        f"Streak length={STREAK_LENGTH} bars | Size=each market's IG minimum | "
        f"Stop-loss={STOP_LOSS_PCT * 100:g}% | Take-profit={TAKE_PROFIT_PCT * 100:g}% | Timeframe={TIMEFRAME}"
    )
    log.info(
        f"Loss limits ({CURRENCY_CODE}, 0 = off): a trade's stop loses at most {MAX_TRADE_LOSS:g} | "
        f"at most {MAX_POSITIONS} positions open | stops for the day {DAILY_LOSS_LIMIT:g} down"
    )
    log.info(
        f"Profit and time limits (0 = off): a trade's take-profit makes at most {MAX_TRADE_PROFIT:g} | "
        f"stops for the day {DAILY_GIVEBACK:g} below its best, once that far up | streaks open nothing "
        + (", ".join(f"{start:%H:%M}-{end:%H:%M}" for start, end in PAUSE_TIMES) + " UK" if PAUSE_TIMES
           else "never (IG_PAUSE_TIMES empty)")
    )
    pass_seconds = (len(pool) + 1) * 60 / REQUESTS_PER_MINUTE
    if pass_seconds > BAR_SECONDS / 3:
        log.warning(f"A pass over {len(pool)} markets takes about {pass_seconds:.0f}s at IG's pace, so each "
                    f"{BAR_SECONDS // 60}-minute bar gets only a sample or two per market; 5 minutes or longer "
                    f"suits this scanner.")
    log.info(rollover.describe())
    global watch
    watch = stagnancy.start_watch("scanner", "ig", "ig-momentum-scanner", "IG", log, bar_seconds=BAR_SECONDS,
                                  loop_seconds=max(pass_seconds, 30), currency=CURRENCY_CODE)
    log.info("=" * 78)
    dashboard.describe(account=_account_id, currency=CURRENCY_CODE, account_mode=ACCOUNT_MODE, config={
        "markets": [item["name"] for item in pool], "timeframe": TIMEFRAME, "streakLength": STREAK_LENGTH,
        "stopLossPercent": STOP_LOSS_PCT * 100, "takeProfitPercent": TAKE_PROFIT_PCT * 100,
        "maxTradeLoss": MAX_TRADE_LOSS, "maxPositions": MAX_POSITIONS, "dailyLossLimit": DAILY_LOSS_LIMIT,
        "maxTradeProfit": MAX_TRADE_PROFIT, "dailyGiveback": DAILY_GIVEBACK,
        "pauseTimes": [f"{start:%H:%M}-{end:%H:%M}" for start, end in PAUSE_TIMES],
        **watch.book.config(),
    })
    dashboard.accept_closes(lambda *command: close_from_dashboard(ig_service, managed(pool), *command))
    restore_guests(ig_service, pool)
    dashboard.tag_rows(broadcasts.tags)
    dashboard.accept_broadcasts(lambda command: broadcast_preview(ig_service, pool, command),
                                lambda command: broadcast_open(ig_service, pool, command), broadcast_symbols(pool))

    consecutive_errors = 0
    pass_number = 0
    while True:
        pass_number += 1
        pass_started = time.monotonic()
        pass_had_error = False
        dashboard.run_commands()  # closes asked for on the dashboard, before this pass's positions

        try:
            positions = fetch_all_positions(ig_service)
        except Exception:
            consecutive_errors += 1
            sleep_after_error(consecutive_errors)
            continue
        if rollover.flat_due():
            try:
                positions = close_before_rollover(ig_service, positions, managed(pool))
            except Exception as e:
                pass_had_error = True
                log.error(f"Error closing positions {rollover.REASON}: {e or 'empty response - likely rate limited'}")
        try:
            positions = loss_limits.update(ig_service, positions, managed(pool))
        except Exception as e:
            pass_had_error = True
            log.error(f"Error checking the loss limits: {e or 'empty response - likely rate limited'}")
        if watch.book.active:
            try:
                positions = stagnancy_pass(ig_service, positions, managed(pool))
            except Exception as e:
                pass_had_error = True
                log.error(f"Error in the stagnancy check: {e or 'empty response - likely rate limited'}")
        report_to_dashboard(ig_service, positions, reported(pool))

        for item in pool:
            try:
                # Between markets too: a broadcast's preview is answered within the dashboard's 30 s, not a pass later.
                if dashboard.run_commands():
                    positions = fetch_all_positions(ig_service)  # what they opened or closed counts for the rest
                trading_cycle(ig_service, item, positions)
            except Exception as e:
                pass_had_error = True
                log.error(f"Error processing {item['name']} this pass: {e or 'empty response - likely rate limited'}")

        log.info(f"Pass {pass_number} complete in {time.monotonic() - pass_started:.0f}s")

        if pass_had_error:
            consecutive_errors += 1
            sleep_after_error(consecutive_errors)
            continue

        consecutive_errors = 0


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        log.info("Bot stopped manually (KeyboardInterrupt). Goodbye.")
        sys.exit(0)
