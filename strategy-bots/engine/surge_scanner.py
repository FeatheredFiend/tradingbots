"""
The opening surge scanner: reads the latest trade of every liquid US share
for the first minutes after the 09:30 New York open and writes each surge
it finds to the signals file the surge followers trade from. The strategy
is described in engine/surge.py; this is the part that reads the market.

It uses Alpaca's market data with the Alpaca bots' keys and places no
trades. Prices come from the free plan's IEX feed (SURGE_FEED=sip on the
paid plan, for every exchange's trades); the day's shares are picked from
consolidated (SIP) daily bars, which the free plan allows for past days.
Alpaca's calendar says when each session opens, holidays included.

Requests: ~30 to pick the shares before each open, then one per 400 shares
per poll - at the defaults (1,500 shares, every 5s) ~48 a minute for the 16
minutes it watches, out of the 200 a minute Alpaca allows each key (shared
with every other Alpaca bot).
"""

import json
import logging
import os
import re
import statistics
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone

import requests

from . import clock, runner
from .settings import SettingsError, surge_scanner_params
from .surge import PRE_OPEN_SECONDS, Detector, heartbeat_path, write_signal

from dashboard_reporter import DashboardReporter  # noqa: E402 - on the path runner.py adds

TRADING = "https://paper-api.alpaca.markets/v2"  # the asset list and calendar - paper endpoint only
DATA = "https://data.alpaca.markets/v2"
EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
BATCH = 400                      # shares per request - keeps the address well inside URL limits
HISTORY_DAYS = 10                # calendar days of daily bars behind each pick: about a week of sessions
SIP_DELAY_SECONDS = 20 * 60      # the free plan serves consolidated (SIP) data only once it's 15+ minutes old
PICK_LEAD_SECONDS = 600          # the day's shares are picked this long before watching starts
IDLE_SECONDS = 60                # between looks at the clock while waiting for the open
MAX_SIGNALS_PER_POLL = 10
ERROR_BACKOFF_SECONDS = 30
MAX_CONSECUTIVE_ERRORS = 10
TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?$")


class DataError(Exception):
    """Alpaca refused a request, or couldn't be reached; the message says why."""


def utc_seconds(text: str):
    """Alpaca's RFC 3339 times (nanoseconds and all) as Unix seconds, or None."""
    match = TIMESTAMP.match(text or "")
    if not match:
        return None
    whole, fraction, zone = match.groups()
    offset = 0
    if zone and zone != "Z":
        hours, minutes = zone[1:].split(":")
        offset = (1 if zone[0] == "+" else -1) * (int(hours) * 3600 + int(minutes) * 60)
    seconds = datetime.strptime(whole, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() - offset
    return seconds + (float(fraction) if fraction else 0.0)


# ---------------------------------------------------------------------------
# ALPACA - plain REST calls
# ---------------------------------------------------------------------------
class AlpacaData:
    def __init__(self, feed: str):
        key, secret = os.environ.get("APCA_API_KEY_ID", ""), os.environ.get("APCA_API_SECRET_KEY", "")
        if not key or not secret:
            raise SettingsError("set APCA_API_KEY_ID and APCA_API_SECRET_KEY (the Alpaca paper account's keys)")
        self.feed = feed
        self.http = requests.Session()
        self.http.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def get(self, url: str, params: dict = None):
        try:
            response = self.http.get(url, params=params, timeout=15)
        except requests.RequestException as e:
            raise DataError(f"couldn't reach Alpaca: {e}") from None
        if response.status_code == 429:
            raise DataError("Alpaca's rate limit (200 requests a minute, shared by every Alpaca bot) - slowing down")
        if response.status_code >= 400:
            raise DataError(f"Alpaca said {response.status_code}: {response.text[:200]}")
        return response.json()

    def calendar(self, first: date, last: date) -> list:
        return self.get(f"{TRADING}/calendar", {"start": first.isoformat(), "end": last.isoformat()})

    def shares(self) -> list:
        """Every active share or fund Alpaca can trade on the main US exchanges."""
        assets = self.get(f"{TRADING}/assets", {"status": "active", "asset_class": "us_equity"})
        return sorted(a["symbol"] for a in assets
                      if a.get("tradable") and a.get("exchange") in EXCHANGES and "/" not in a["symbol"])

    def daily_bars(self, symbols: list, first: date, end: float, feed: str) -> dict:
        """{symbol: [(close, volume), ...]} for the sessions from `first` to `end` (Unix seconds)."""
        out = {}
        until = datetime.fromtimestamp(end, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(0, len(symbols), BATCH):
            params = {"symbols": ",".join(symbols[i:i + BATCH]), "timeframe": "1Day", "start": first.isoformat(),
                      "end": until, "feed": feed, "adjustment": "split", "limit": 10000}
            while True:
                body = self.get(f"{DATA}/stocks/bars", params)
                for symbol, bars in (body.get("bars") or {}).items():
                    out.setdefault(symbol, []).extend((float(b["c"]), float(b["v"])) for b in bars)
                if not body.get("next_page_token"):
                    break
                params["page_token"] = body["next_page_token"]
        return out

    def latest_trades(self, symbols: list) -> dict:
        """{symbol: (trade time, price)} - the latest trade of each."""
        body = self.get(f"{DATA}/stocks/trades/latest", {"symbols": ",".join(symbols), "feed": self.feed})
        out = {}
        for symbol, trade in (body.get("trades") or {}).items():
            at, price = utc_seconds(trade.get("t")), trade.get("p")
            if at is not None and price:
                out[symbol] = (at, float(price))
        return out


# ---------------------------------------------------------------------------
# THE SCANNER
# ---------------------------------------------------------------------------
class SurgeScanner:
    def __init__(self, params: dict, data, dashboard, log):
        self.p, self.data, self.dashboard, self.log = params, data, dashboard, log
        self.session = None          # (date, open, close) of the open watched next, Unix seconds
        self.shares = None           # the session's shares, once picked
        self.detector = None
        self.signalled = set()       # tickers already signalled this session
        self.sent = 0
        self.announced = False
        self.watching = False
        self.polls = 0
        self.last_problem = 0.0

    # -- startup --------------------------------------------------------------------
    def start(self) -> None:
        p, log = self.p, self.log
        log.info("=" * 78)
        log.info("Opening surge scanner starting - it reads prices only; the surge followers trade its signals")
        log.info(f"Watches the {p['max_shares']:,} most traded US shares over ${p['min_price']:g} (and "
                 f"${p['min_dollar_volume']:g}M a day) from {PRE_OPEN_SECONDS}s before the 09:30 New York open to "
                 f"{p['watch_minutes']} min after it | reads every {p['poll_seconds']:g}s ({p['feed'].upper()} feed) | "
                 f"surge: {p['confirm_polls']} polls in a row, each {p['jump_percent']:g}%+ the same way")
        log.info("=" * 78)
        self.dashboard.describe(config={**p, "signalsFile": "strategy-bots/state/surge-signals-<date>.jsonl"})
        self.next_session(time.time())

    def next_session(self, now: float) -> None:
        """The next open still to watch, from Alpaca's calendar."""
        today = clock.local_date("new_york", now)
        for entry in self.data.calendar(today, today + timedelta(days=10)):
            day = date.fromisoformat(entry["date"])
            opens = clock.at("new_york", day, clock.parse_hhmm(entry["open"]))
            closes = clock.at("new_york", day, clock.parse_hhmm(entry["close"]))
            if now < self.watch_until(opens, closes):
                self.session = (day, opens, closes)
                self.shares, self.detector, self.signalled = None, None, set()
                self.sent, self.polls, self.announced, self.watching = 0, 0, False, False
                return
        raise DataError("Alpaca's calendar shows no US session in the next 10 days")

    def watch_until(self, opens: float, closes: float) -> float:
        return min(opens + self.p["watch_minutes"] * 60, closes)  # an early close cuts it short

    def pick_shares(self, day: date) -> list:
        """The session's shares: listed, priced and traded enough, most traded
        first - from the sessions before `day`, cached for the day."""
        p, log = self.p, self.log
        cache = os.path.join(runner.STATE_DIR, f"surge-shares-{day.isoformat()}.json")
        try:
            with open(cache, encoding="utf-8") as f:
                saved = json.load(f)
            if saved.get("settings") == [p["min_price"], p["min_dollar_volume"], p["max_shares"]]:
                log.info(f"Using the {len(saved['shares']):,} shares picked earlier for {day}.")
                return saved["shares"]
        except (OSError, ValueError, KeyError):
            pass

        started = time.time()
        listed = self.data.shares()
        first = day - timedelta(days=HISTORY_DAYS)
        # Up to the day itself (not its own bar), and - the free plan's rule for
        # SIP - over 15 minutes ago: picked the evening before, midnight is ahead.
        end = min(clock.at("new_york", day, dtime(0)) - 1, started - SIP_DELAY_SECONDS)
        try:
            bars, consolidated = self.data.daily_bars(listed, first, end, "sip"), True
        except DataError as e:
            log.warning(f"Alpaca refused consolidated (SIP) history ({e}); picking by IEX's volumes instead - a "
                        f"small slice of the market's, so SURGE_MIN_DOLLAR_VOLUME is ignored.")
            bars, consolidated = self.data.daily_bars(listed, first, end, "iex"), False
        ranked = []
        for symbol, series in bars.items():
            if not series:
                continue
            close = series[-1][0]
            dollars = statistics.median(c * v for c, v in series) / 1e6
            if close >= p["min_price"] and (not consolidated or dollars >= p["min_dollar_volume"]):
                ranked.append((dollars, symbol))
        ranked.sort(reverse=True)
        shares = [symbol for _, symbol in ranked[:p["max_shares"]]]
        log.info(f"Picked {len(shares):,} shares for {day} ({len(ranked):,} of {len(listed):,} listed qualify; "
                 f"most traded: {', '.join(shares[:8])}) in {time.time() - started:.0f}s.")
        try:
            os.makedirs(runner.STATE_DIR, exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                json.dump({"settings": [p["min_price"], p["min_dollar_volume"], p["max_shares"]], "shares": shares}, f)
        except OSError as e:
            log.warning(f"Couldn't save {cache}: {e}")
        return shares

    # -- the loop -------------------------------------------------------------------
    def run(self) -> None:
        self.start()
        errors = 0
        while True:
            try:
                wait = self.cycle(time.time())
                errors = 0
            except Exception as e:  # anything unexpected counts toward the safety cutoff, not a crash
                errors += 1
                self.log.error(f"[{errors}] Error this loop: {e}")
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    self.log.critical(f"{MAX_CONSECUTIVE_ERRORS} errors in a row - exiting. Check the connection "
                                      f"and the Alpaca keys before restarting.")
                    sys.exit(1)
                wait = ERROR_BACKOFF_SECONDS
            self.dashboard.sleep(wait, lambda: None)

    def cycle(self, now: float) -> float:
        """Whatever is due now; returns how long to wait before the next go."""
        p, log = self.p, self.log
        day, opens, closes = self.session
        watch_from, watch_to = opens - PRE_OPEN_SECONDS, self.watch_until(opens, closes)
        self.heartbeat(now, watch_from <= now < watch_to)
        if now >= watch_to:
            if self.watching:
                log.info(f"Done watching the {day} open: {self.polls} polls, {self.sent} signal(s) sent.")
            self.next_session(now)
            return 0.0
        if self.shares is None and now >= watch_from - PICK_LEAD_SECONDS:
            self.shares = self.pick_shares(day)
            self.detector = Detector(p["confirm_polls"], p["jump_percent"], p["poll_seconds"])
            if not self.shares:
                log.warning(f"No shares qualify for {day} - check the SURGE_MIN_PRICE / SURGE_MIN_DOLLAR_VOLUME "
                            f"settings. Nothing to watch until the next open.")
        if now < watch_from:
            if not self.announced:
                self.announced = True
                uk = clock.local("london", opens).strftime("%H:%M")
                log.info(f"Next open: {day:%a %d %b} 09:30 New York ({uk} UK) - watching from "
                         f"{clock.local('london', watch_from):%H:%M:%S} to {clock.local('london', watch_to):%H:%M} UK.")
            until = watch_from - now if self.shares is not None else watch_from - PICK_LEAD_SECONDS - now
            return max(0.5, min(IDLE_SECONDS, until))
        if not self.shares:
            return max(0.5, min(IDLE_SECONDS, watch_to - now))

        if not self.watching:
            self.watching = True
            log.info(f"Watching {len(self.shares):,} shares for surges.")
        started = time.time()
        self.send(self.poll(now, opens), day)
        return max(0.2, p["poll_seconds"] - (time.time() - started))

    def poll(self, now: float, opens: float) -> list:
        """One read of every share's latest trade; the surges it completes."""
        self.polls += 1
        surges = []
        for i in range(0, len(self.shares), BATCH):
            try:
                trades = self.data.latest_trades(self.shares[i:i + BATCH])
            except DataError as e:
                if time.time() - self.last_problem > 60:  # a missed poll only delays a surge - say so once a minute
                    self.last_problem = time.time()
                    self.log.warning(f"Missed part of a poll: {e}")
                return surges
            for symbol, (at, price) in trades.items():
                if symbol not in self.signalled:
                    surge = self.detector.add(symbol, now, at, price, not_before=opens)
                    if surge is not None:
                        surges.append(surge)
        return surges

    def send(self, surges: list, day: date) -> None:
        surges.sort(key=lambda s: s.move_percent, reverse=True)
        for surge in surges[:MAX_SIGNALS_PER_POLL]:
            self.signalled.add(surge.ticker)
            self.sent += 1
            seconds = surge.at - surge.started
            write_signal({
                "id": f"{day.isoformat()}-{surge.ticker}", "time": surge.at, "ticker": surge.ticker,
                "direction": surge.direction, "price": surge.price, "start_price": surge.start_price,
                "move_percent": round(surge.move_percent, 4), "steps": [round(s, 3) for s in surge.steps],
                "polls": len(surge.steps), "seconds": round(seconds, 1),
            })
            self.log.info(f"SURGE {'UP' if surge.direction == 'long' else 'DOWN'} {surge.ticker} "
                          f"{surge.move_percent if surge.direction == 'long' else -surge.move_percent:+.2f}% in "
                          f"{len(surge.steps)} polls ({seconds:.0f}s): {surge.start_price:g} -> {surge.price:g} | "
                          f"steps {' '.join(f'{s:+.2f}%' for s in surge.steps)} | {surge.direction} signal sent")
        if len(surges) > MAX_SIGNALS_PER_POLL:
            rest = [s.ticker for s in surges[MAX_SIGNALS_PER_POLL:]]
            self.log.info(f"...and {len(rest)} more surging on this poll, not sent (at most {MAX_SIGNALS_PER_POLL} a "
                          f"poll): {', '.join(rest[:20])}{' ...' if len(rest) > 20 else ''}")

    def heartbeat(self, now: float, watching: bool) -> None:
        """Tells the followers the scanner is running."""
        path = heartbeat_path()
        try:
            os.makedirs(runner.STATE_DIR, exist_ok=True)
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"at": now, "watching": watching, "next_open": self.session[1]}, f)
            os.replace(path + ".tmp", path)
        except OSError:
            pass  # a follower reading it at that moment on Windows - the next one will do


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    log = logging.getLogger("surge-scanner")
    try:
        params = surge_scanner_params()
        data = AlpacaData(params["feed"])
        # Off unless DASHBOARD_URL and DASHBOARD_TOKEN are set (see shared/dashboard_reporter.py).
        dashboard = DashboardReporter("surge-scanner", "Surge scanner", broker="Alpaca",
                                      strategy="Opening surge scanner")
        SurgeScanner(params, data, dashboard, log).run()
    except SettingsError as e:
        log.error(f"Can't start: {e}.")
        sys.exit(1)
    except DataError as e:
        log.critical(f"Can't carry on: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("Scanner stopped manually (Ctrl+C). Goodbye.")
