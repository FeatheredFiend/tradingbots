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

Closing positions from the dashboard
------------------------------------
The dashboard can't reach the bots, so its commands come back in the reply
to a report. A bot that registers a handler with accept_closes() - and runs
with DASHBOARD_COMMANDS=1 - carries them out on its own thread, inside
sleep() or run_commands(), never on the reporting thread, and the answer
goes out straight away. Commands only ever close (part of) the bot's own
positions; each one is carried out once at most.

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
# Off unless set: lets whoever can sign in to the dashboard as an admin close this bot's positions.
COMMANDS_ON = os.environ.get("DASHBOARD_COMMANDS", "").strip().lower() in ("1", "true", "yes", "on")

REPORT_EVERY_SECONDS = 10     # the dashboard calls a bot silent after 3 minutes without a report
SNAPSHOT_EVERY_SECONDS = 15   # how often due() asks the bot for account/positions/trades
TIMEOUT_SECONDS = 10
MAX_LOG_BACKLOG = 2000        # lines kept while the dashboard is unreachable
MAX_LOGS_PER_REPORT = 500
COMPLAIN_EVERY_SECONDS = 600  # how often a failing dashboard is mentioned on the console


class CommandError(Exception):
    """A dashboard command the bot won't or can't carry out. The message is
    the answer shown on the dashboard, so say why in plain words."""


class DashboardReporter:
    def __init__(self, slug: str, name: str, broker: str, strategy: str):
        self.enabled = bool(URL and TOKEN)
        self._bot = {
            "slug": slug,
            "name": name,
            "broker": broker,
            "strategy": strategy,
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "acceptsCommands": [],
        }
        self._close = None            # the bot's close handler, if it takes commands
        self._inbox = collections.deque()   # commands from the dashboard, not yet carried out
        self._answers = []            # {"id", "ok", "message"} not yet sent
        self._seen_commands = set()   # IDs already taken in, so none runs twice
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._logs = collections.deque(maxlen=MAX_LOG_BACKLOG)
        self._account = None          # latest, unsent
        self._positions = None        # latest, unsent
        self._trades = {}             # ref -> trade, as last reported by the bot
        self._sent_trades = {}        # ref -> trade, as the dashboard last received it
        self._status = "running"
        self._last_snapshot = 0.0
        self._soon = []               # monotonic times due() comes round early - see report_soon()
        self._send_now = False        # the next update() goes out straight away, not with the next report
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

    def accept_closes(self, close) -> None:
        """Let the dashboard close this bot's positions, if DASHBOARD_COMMANDS
        is on. `close(symbol, ref, direction, size)` closes the bot's own
        position in `symbol` (the one with broker ref `ref`, when given): all
        of it when `size` is None, else that much. It must refuse if that
        position is no longer `direction` ("long"/"short") - the bot may have
        reversed it since the dashboard last saw it. It returns a short line
        saying what it did, or raises CommandError (or anything else) saying
        why not. It runs on the bot's thread, inside sleep() or run_commands()."""
        if not self.enabled:
            return
        if not COMMANDS_ON:
            logging.getLogger(__name__).info(
                "Closing positions from the dashboard is off (set DASHBOARD_COMMANDS=1 to allow it).")
            return
        self._close = close
        with self._lock:
            self._bot["acceptsCommands"] = ["close"]
        logging.getLogger(__name__).info("The dashboard can close this bot's positions (DASHBOARD_COMMANDS is on).")

    def run_commands(self) -> int:
        """Carry out the commands the dashboard has sent, one by one, and
        send the answers straight away. Returns how many ran. The bot calls
        this between its passes; sleep() calls it every second."""
        log = logging.getLogger(__name__)
        ran = 0
        while True:
            with self._lock:
                if not self._inbox:
                    break
                command = self._inbox.popleft()
            ran += 1
            symbol, ref, size = command.get("symbol"), command.get("ref"), command.get("size")
            direction = command.get("direction")
            what = f"close {symbol} {direction}" + (f" (ref {ref})" if ref else "") + (
                f", size {size}" if size else ", all of it")
            log.info(f"Dashboard asks: {what}.")
            try:
                if command.get("action") != "close" or not symbol or direction not in ("long", "short"):
                    raise CommandError(f"This bot doesn't know the command {command.get('action')!r}.")
                if self._close is None:
                    raise CommandError("Commands from the dashboard are switched off in this bot.")
                if size is not None and (not isinstance(size, (int, float)) or size <= 0):
                    raise CommandError(f"{size!r} isn't a size.")
                message, ok = str(self._close(symbol, None if ref is None else str(ref), direction, size) or "Done."), True
            except Exception as e:  # the bot carries on whatever happens
                message, ok = (str(e) if isinstance(e, CommandError) else f"{type(e).__name__}: {e}"), False
            (log.info if ok else log.warning)(f"Dashboard command {'done' if ok else 'not done'}: {message}")
            with self._lock:
                self._answers.append({"id": command["id"], "ok": ok, "message": message[:255]})
        if ran:
            # due() at once; the update() it brings sends the answer straight
            # away, with the positions as they are now.
            self._last_snapshot = 0.0
        return ran

    def report_soon(self, *delays: float) -> None:
        """Make due() come round after each of these delays in seconds
        (straight away with none), besides its usual beat, and send what it
        gathers at once - after the bot trades, so the dashboard shows it in
        seconds. A second, later delay catches a broker whose history takes
        a moment to list a trade that just closed."""
        if self.enabled:
            now = time.monotonic()
            self._soon.extend(now + delay for delay in delays or (0,))

    def due(self, every: float = None) -> bool:
        """True every SNAPSHOT_EVERY_SECONDS (or `every`, for a broker that
        rations requests), or when report_soon() asked - time to gather a
        snapshot for update(). Always False when reporting is off, so the bot
        makes no extra calls."""
        if not self.enabled:
            return False
        now = time.monotonic()
        if any(at <= now for at in self._soon):
            self._soon = [at for at in self._soon if at > now]
            self._send_now = True
        elif now - self._last_snapshot < (every or SNAPSHOT_EVERY_SECONDS):
            return False
        self._last_snapshot = now
        return True

    def sleep(self, seconds: float, report, every: float = None) -> None:
        """time.sleep(seconds), but runs report() whenever due(every) comes
        round meanwhile, so a bot whose loop sleeps longer than
        SNAPSHOT_EVERY_SECONDS still keeps the dashboard fresh - and carries
        out any commands from the dashboard as they arrive. A report()
        that fails is logged, never raised - trading comes first."""
        if not self.enabled:
            time.sleep(seconds)
            return
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            time.sleep(min(left, 1.0))
            self.run_commands()
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
            if self._answers or self._send_now:
                self._send_now = False
                self._wake.set()  # a command's answer, or a trade, goes out now, not in up to 10 s

    def trade(self, trade: dict) -> None:
        """Record one trade as it happens (e.g. a close the bot made itself)."""
        self.update(trades=[trade])

    def stop(self, status: str = "stopped") -> None:
        """Say goodbye: one last report marking the bot stopped. Called at exit."""
        if not self.enabled or self._stop.is_set():
            return
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._status = status
        self._send_once()

    # -- background thread ---------------------------------------------------
    def _run(self) -> None:
        while True:
            self._wake.wait(REPORT_EVERY_SECONDS)
            self._wake.clear()
            if self._stop.is_set():
                return
            self._send_once()

    def _send_once(self) -> None:
        with self._lock:
            logs = [self._logs.popleft() for _ in range(min(len(self._logs), MAX_LOGS_PER_REPORT))]
            account, positions = self._account, self._positions
            trades = {ref: t for ref, t in self._trades.items() if self._sent_trades.get(ref) != t}
            answers = list(self._answers)
            report = {"bot": dict(self._bot), "status": self._status, "logs": logs}
            if account is not None:
                report["account"] = account
            if positions is not None:
                report["positions"] = positions
            if trades:
                report["trades"] = list(trades.values())
            if answers:
                report["commandResults"] = answers

        reply = self._post(report)
        if reply is not None:
            with self._lock:
                if self._account is account:
                    self._account = None
                if self._positions is positions:
                    self._positions = None
                self._sent_trades.update(trades)
                del self._answers[:len(answers)]
            self._take_commands(reply.get("commands"))
        else:
            with self._lock:
                self._logs.extendleft(reversed(logs))  # try them again next time, oldest first

    def _take_commands(self, commands) -> None:
        """Queue the commands in the dashboard's reply for the bot's thread.
        One that's already been taken in is ignored, so none runs twice."""
        if not isinstance(commands, list) or not commands:
            return
        with self._lock:
            if self._status != "running":
                return  # shutting down; the dashboard shows them unanswered
            for command in commands:
                if isinstance(command, dict) and isinstance(command.get("id"), int) \
                        and command["id"] not in self._seen_commands:
                    self._seen_commands.add(command["id"])
                    self._inbox.append(command)

    def _post(self, report: dict):
        """The dashboard's reply (a dict) if it took the report, else None."""
        request = urllib.request.Request(
            URL + "/api/ingest",
            data=json.dumps(report, default=_json_default).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "X-Dashboard-Token": TOKEN,
                     "User-Agent": "tradingbots-dashboard-reporter/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                if not 200 <= response.status < 300:
                    return None
                try:
                    reply = json.loads(response.read() or b"{}")
                except ValueError:
                    reply = {}
                return reply if isinstance(reply, dict) else {}
        except urllib.error.HTTPError as e:
            self._complain(f"HTTP {e.code}: {e.read(300).decode(errors='replace')}")
        except Exception as e:
            self._complain(str(e))
        return None

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
