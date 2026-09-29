"""
Reports a bot's console log, account, positions and trades to the trading
dashboard (github.com/FeatheredFiend/tradingdashboard).

Off unless both of these are set in the environment the bot runs in:

    DASHBOARD_URL    e.g. https://tradingdashboard.example.com
    DASHBOARD_TOKEN  the dashboard's INGEST_TOKEN

It can never stall or crash trading: reports go from a background thread
every REPORT_EVERY_SECONDS (which doubles as the "still running" heartbeat),
a failed send just keeps the data for the next try, and the backlog is
capped. Every log line the bot prints is forwarded; the bot itself calls
describe() once at startup and update() with account, positions and recent
trades whenever due() says so - including while it waits between passes,
if it waits with sleep() instead of time.sleep().

Standard library only, so it works in every bot's venv.
"""

import atexit
import collections
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

URL = os.environ.get("DASHBOARD_URL", "").strip().rstrip("/")
TOKEN = os.environ.get("DASHBOARD_TOKEN", "").strip()

REPORT_EVERY_SECONDS = 10     # the dashboard calls a bot silent after 3 minutes without a report
SNAPSHOT_EVERY_SECONDS = 15   # how often due() asks the bot for account/positions/trades
TIMEOUT_SECONDS = 10
MAX_LOG_BACKLOG = 2000        # lines kept while the dashboard is unreachable
MAX_LOGS_PER_REPORT = 500
COMPLAIN_EVERY_SECONDS = 600  # how often a failing dashboard is mentioned on the console


class DashboardReporter:
    def __init__(self, slug: str, name: str, broker: str, strategy: str):
        self.enabled = bool(URL and TOKEN)
        self._bot = {
            "slug": slug,
            "name": name,
            "broker": broker,
            "strategy": strategy,
            "startedAt": datetime.now(timezone.utc).isoformat(),
        }
        self._lock = threading.Lock()
        self._logs = collections.deque(maxlen=MAX_LOG_BACKLOG)
        self._account = None          # latest, unsent
        self._positions = None        # latest, unsent
        self._trades = {}             # ref -> trade, as last reported by the bot
        self._sent_trades = {}        # ref -> trade, as the dashboard last received it
        self._status = "running"
        self._last_snapshot = 0.0
        self._last_complaint = 0.0
        self._stop = threading.Event()
        self._thread = None

        if not self.enabled:
            return
        handler = _ForwardingHandler(self)
        handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(handler)
        self._thread = threading.Thread(target=self._run, name="dashboard-reporter", daemon=True)
        self._thread.start()
        atexit.register(self.stop)
        logging.getLogger(__name__).info(f"Reporting to the dashboard at {URL} as '{slug}'.")

    # -- called by the bot -------------------------------------------------
    def describe(self, account=None, currency=None, config=None) -> None:
        """What the dashboard shows about the bot itself; sent with every report."""
        with self._lock:
            if account is not None:
                self._bot["account"] = str(account)
            if currency is not None:
                self._bot["currency"] = currency
            if config is not None:
                self._bot["config"] = config

    def due(self, every: float = None) -> bool:
        """True every SNAPSHOT_EVERY_SECONDS (or `every`, for a broker that
        rations requests) - time to gather a snapshot for update(). Always
        False when reporting is off, so the bot makes no extra calls."""
        if not self.enabled or time.monotonic() - self._last_snapshot < (every or SNAPSHOT_EVERY_SECONDS):
            return False
        self._last_snapshot = time.monotonic()
        return True

    def sleep(self, seconds: float, report, every: float = None) -> None:
        """time.sleep(seconds), but runs report() whenever due(every) comes
        round meanwhile, so a bot whose loop sleeps longer than
        SNAPSHOT_EVERY_SECONDS still keeps the dashboard fresh. A report()
        that fails is logged, never raised - trading comes first."""
        if not self.enabled:
            time.sleep(seconds)
            return
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            time.sleep(min(left, 1.0))
            if self.due(every):
                try:
                    report()
                except Exception as e:
                    logging.getLogger(__name__).warning(f"Couldn't gather the dashboard report: {e}")

    def update(self, account=None, positions=None, trades=None) -> None:
        """account: {"balance", "equity", "unrealizedPl"}; positions: the full
        current list (an empty list means none); trades: recent trades, each
        with the broker's "ref" - only new or changed ones are sent."""
        if not self.enabled:
            return
        with self._lock:
            if account is not None:
                self._account = account
            if positions is not None:
                self._positions = positions
            for trade in trades or ():
                self._trades[str(trade["ref"])] = trade

    def trade(self, trade: dict) -> None:
        """Record one trade as it happens (e.g. a close the bot made itself)."""
        self.update(trades=[trade])

    def stop(self, status: str = "stopped") -> None:
        """Say goodbye: one last report marking the bot stopped. Called at exit."""
        if not self.enabled or self._stop.is_set():
            return
        self._stop.set()
        with self._lock:
            self._status = status
        self._send_once()

    # -- background thread ---------------------------------------------------
    def _run(self) -> None:
        while not self._stop.wait(REPORT_EVERY_SECONDS):
            self._send_once()

    def _send_once(self) -> None:
        with self._lock:
            logs = [self._logs.popleft() for _ in range(min(len(self._logs), MAX_LOGS_PER_REPORT))]
            account, positions = self._account, self._positions
            trades = {ref: t for ref, t in self._trades.items() if self._sent_trades.get(ref) != t}
            report = {"bot": dict(self._bot), "status": self._status, "logs": logs}
            if account is not None:
                report["account"] = account
            if positions is not None:
                report["positions"] = positions
            if trades:
                report["trades"] = list(trades.values())

        if self._post(report):
            with self._lock:
                if self._account is account:
                    self._account = None
                if self._positions is positions:
                    self._positions = None
                self._sent_trades.update(trades)
        else:
            with self._lock:
                self._logs.extendleft(reversed(logs))  # try them again next time, oldest first

    def _post(self, report: dict) -> bool:
        request = urllib.request.Request(
            URL + "/api/ingest",
            data=json.dumps(report, default=_json_default).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "X-Dashboard-Token": TOKEN,
                     "User-Agent": "tradingbots-dashboard-reporter/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return 200 <= response.status < 300
        except urllib.error.HTTPError as e:
            self._complain(f"HTTP {e.code}: {e.read(300).decode(errors='replace')}")
        except Exception as e:
            self._complain(str(e))
        return False

    def _complain(self, problem: str) -> None:
        # Straight to stderr, not logging - a logged complaint would itself be
        # forwarded, and pile up while the dashboard is down.
        now = time.monotonic()
        if now - self._last_complaint >= COMPLAIN_EVERY_SECONDS:
            self._last_complaint = now
            print(f"[dashboard] couldn't report to {URL} ({problem}); trading carries on, will keep trying.",
                  file=sys.stderr)


class _ForwardingHandler(logging.Handler):
    def __init__(self, reporter: DashboardReporter):
        super().__init__()
        self._reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
            with self._reporter._lock:
                self._reporter._logs.append({"time": record.created, "level": record.levelname, "message": message})
        except Exception:
            self.handleError(record)


def _json_default(value):
    """numpy numbers (MetaTrader 5) and anything else odd."""
    if hasattr(value, "item"):
        return value.item()
    return str(value)
