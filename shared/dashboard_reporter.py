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

One relay for every bot on the PC
---------------------------------
The dashboard's host hangs up a connection after 5 idle seconds, so a bot
reporting every 10 seconds needs a new HTTPS connection for every report.
With 25 bots that's ~150 a minute, and the host began leaving some of them
unanswered ("<urlopen error timed out>"). So the first bot to report also
runs a small relay on 127.0.0.1:DASHBOARD_RELAY_PORT (default 47817), and
every bot hands its reports to it; the relay passes them on over a few
connections that are kept busy enough to stay open. Each bot still gets its
own reply (and commands) back, and nothing gets slower. When the relaying
bot stops, the next bot to report takes over. DASHBOARD_RELAY_PORT=0 turns
it off - every bot posts straight to the dashboard - and a bot also does
that for a minute whenever the relay can't be used.

Standard library only, so it works in every bot's venv.
"""

import atexit
import collections
import http.client
import http.server
import json
import logging
import os
import socket
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
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


def _relay_port(value) -> int:
    value = (value or "").strip().lower()
    if value in ("0", "off", "no", "false"):
        return 0
    return int(value) if value.isdigit() and 0 < int(value) < 65536 else 47817


RELAY_PORT = _relay_port(os.environ.get("DASHBOARD_RELAY_PORT"))  # 0 = no relay
RELAY_PATH = "/tradingbots-dashboard-relay"
RELAY_HEADER = "X-Dashboard-Relay"   # the relay's answers carry it, naming the bot that runs it
RELAY_CONNECTIONS = 6         # reports the relay passes on at once; more wait their turn
RELAY_QUEUE_SECONDS = 5       # ...this long at most
RELAY_WAIT_SECONDS = TIMEOUT_SECONDS + RELAY_QUEUE_SECONDS + 5  # a bot's wait for the relay's answer
RELAY_MISSES = 3              # unanswered reports in a row before a bot stops using the relay...
RELAY_RETRY_SECONDS = 60      # ...for this long, posting straight to the dashboard instead


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
        self._relay_misses = 0        # reports in a row the relay didn't answer
        self._relay_off_until = 0.0   # monotonic time until which reports skip the relay
        self._last_relay_note = 0.0
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
        """The dashboard's reply (a dict) if it took the report, else None.
        Goes through the relay unless that's off or can't be used just now."""
        body = json.dumps(report, default=_json_default).encode()
        if RELAY_PORT and time.monotonic() >= self._relay_off_until:
            reply = self._post_via_relay(body)
            if reply is not _NO_RELAY:
                return reply
        return self._post_direct(body)

    def _post_direct(self, body: bytes):
        request = urllib.request.Request(
            URL + "/api/ingest",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "X-Dashboard-Token": TOKEN,
                     "User-Agent": "tradingbots-dashboard-reporter/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                if not 200 <= response.status < 300:
                    return None
                return _reply(response.read())
        except urllib.error.HTTPError as e:
            self._complain(f"HTTP {e.code}: {e.read(300).decode(errors='replace')}")
        except Exception as e:
            self._complain(str(e))
        return None

    def _post_via_relay(self, body: bytes):
        """Like _post_direct, but through the relay - which this bot starts
        if no other bot is running one. _NO_RELAY if it can't be used, and
        then the report goes straight to the dashboard instead."""
        for _ in range(3):
            try:
                status, relayed_by, data = _ask_relay(body)
                break
            except ConnectionRefusedError:
                # No relay running, or its bot just stopped: run it here, unless another bot just did.
                if not _host_relay(self._bot["slug"]):
                    time.sleep(0.2)
            except (ConnectionResetError, ConnectionAbortedError):  # the relaying bot is stopping
                time.sleep(0.2)
            except (_NotARelay, http.client.HTTPException) as e:
                return self._skip_relay(str(e) or type(e).__name__)
            except OSError as e:  # no answer in time
                self._relay_misses += 1
                if self._relay_misses >= RELAY_MISSES:
                    self._relay_off_until = time.monotonic() + RELAY_RETRY_SECONDS
                self._complain(f"no answer from the reports relay: {e}")
                return None
        else:
            return self._skip_relay("couldn't start it or reach it")

        self._relay_misses = 0
        if 200 <= status < 300:
            return _reply(data)
        try:
            problem = json.loads(data)["relayError"]
        except (ValueError, TypeError, KeyError):
            problem = f"HTTP {status}: {data[:300].decode(errors='replace')}"
        self._complain(f"{problem}; passed on by {relayed_by}")
        return None

    def _skip_relay(self, why: str):
        self._relay_off_until = time.monotonic() + RELAY_RETRY_SECONDS
        now = time.monotonic()
        if now - self._last_relay_note >= COMPLAIN_EVERY_SECONDS:
            self._last_relay_note = now
            print(f"[dashboard] not using the reports relay on 127.0.0.1:{RELAY_PORT} ({why}); "
                  f"reporting straight to the dashboard for now.", file=sys.stderr)
        return _NO_RELAY

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


def _reply(data: bytes) -> dict:
    try:
        reply = json.loads(data or b"{}")
    except ValueError:
        reply = {}
    return reply if isinstance(reply, dict) else {}


# -- the relay (see "One relay for every bot on the PC" at the top) ----------
_NO_RELAY = object()
_relay_server = None          # this process's relay, if it runs it
_relay_server_lock = threading.Lock()


class _NotARelay(Exception):
    """Something else answers on the relay's port, or a relay for another dashboard."""


def _ask_relay(body: bytes):
    """Hand one report to the relay: (HTTP status, the bot running the
    relay, the dashboard's answer). Raises ConnectionRefusedError when no
    relay is running."""
    connection = http.client.HTTPConnection("127.0.0.1", RELAY_PORT, timeout=RELAY_WAIT_SECONDS)
    try:
        # HTTP/1.1 with no "Connection: close": the relay waits for us to hang
        # up, so the closed connection lingers on this side, not the relay's port.
        connection.request("POST", RELAY_PATH, body, {
            "Content-Type": "application/json", "X-Dashboard-Token": TOKEN, "X-Dashboard-Url": URL})
        response = connection.getresponse()
        data = response.read()
    finally:
        connection.close()
    relayed_by = response.getheader(RELAY_HEADER)
    if relayed_by is None:
        raise _NotARelay(f"something else answers there (HTTP {response.status})")
    if response.status == 421:
        raise _NotARelay(_reply(data).get("relayError") or "it reports to another dashboard")
    return response.status, relayed_by, data


def _host_relay(slug: str) -> bool:
    """Start the relay in this process. False if the port is taken - most
    likely another bot has just started it."""
    global _relay_server
    with _relay_server_lock:
        if _relay_server is None:
            try:
                server = _RelayServer(("127.0.0.1", RELAY_PORT), _RelayHandler)
            except OSError:
                return False
            server.relay = _Relay(URL)
            server.slug = slug
            threading.Thread(target=server.serve_forever, name="dashboard-relay", daemon=True).start()
            _relay_server = server
            logging.getLogger(__name__).info(
                f"Passing every bot's dashboard reports on from here (relay on 127.0.0.1:{RELAY_PORT}).")
    return True


def _stop_relay() -> None:
    """Stop this process's relay, if it runs one (for tests)."""
    global _relay_server
    with _relay_server_lock:
        server, _relay_server = _relay_server, None
    if server is not None:
        server.shutdown()
        server.server_close()
        server.relay.close()


class _RelayServer(http.server.ThreadingHTTPServer):
    # Never share the port: on Windows SO_REUSEADDR would let a second bot
    # bind it too, so ask for it exclusively instead.
    allow_reuse_address = False
    allow_reuse_port = False
    request_queue_size = 64
    block_on_close = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)  # not HTTPServer's: its reverse DNS lookup can take seconds
        self.server_name, self.server_port = self.server_address[:2]


class _RelayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60  # a bot that never hangs up

    def do_POST(self):
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        except ValueError:
            return self._answer(400, {"relayError": "no Content-Length"})
        relay = self.server.relay
        if self.path != RELAY_PATH:
            self._answer(404, {"relayError": f"the relay takes reports at {RELAY_PATH}"})
        elif self.headers.get("X-Dashboard-Url", "") != relay.url:
            self._answer(421, {"relayError": f"the relay on this port reports to {relay.url}"})
        else:
            self._answer(*relay.forward(body, self.headers.get("X-Dashboard-Token", "")))

    def _answer(self, status: int, data) -> None:
        if isinstance(data, dict):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(RELAY_HEADER, self.server.slug)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass  # else a line per report on the relaying bot's console


class _Relay:
    """Passes reports on to the dashboard, reusing connections while the
    dashboard's server would still keep them open, and opening a new one
    only when none is free."""

    def __init__(self, url: str):
        self.url = url
        parts = urllib.parse.urlsplit(url)
        self._connection_class = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        self._address = (parts.hostname, parts.port)
        self._path = parts.path + "/api/ingest"
        self._lock = threading.Lock()
        self._idle = []           # (connection, reusable until), most recently used last
        self._slots = threading.BoundedSemaphore(RELAY_CONNECTIONS)
        self.opened = 0           # connections opened so far

    def forward(self, body: bytes, token: str):
        """(HTTP status, answer) - the dashboard's, or a "relayError" one."""
        if not self._slots.acquire(timeout=RELAY_QUEUE_SECONDS):
            return 503, {"relayError": f"the relay is busy ({RELAY_CONNECTIONS} reports already on their way)"}
        try:
            for attempt in range(2):
                connection, reused = self._take(new=attempt > 0)
                try:
                    connection.request("POST", self._path, body, {
                        "Content-Type": "application/json", "X-Dashboard-Token": token,
                        "User-Agent": "tradingbots-dashboard-relay/1"})
                    response = connection.getresponse()
                    data = response.read()
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
                    connection.close()
                    if reused:
                        continue  # the server hung up the kept connection first; once more on a new one
                    return 502, {"relayError": str(e) or type(e).__name__}
                except Exception as e:
                    connection.close()
                    return 502, {"relayError": str(e) or type(e).__name__}
                self._keep(connection, response)
                return response.status, data
        finally:
            self._slots.release()

    def _take(self, new: bool):
        now = time.monotonic()
        with self._lock:
            stale = [connection for connection, until in self._idle if until <= now]
            self._idle = [(connection, until) for connection, until in self._idle if until > now]
            connection = self._idle.pop()[0] if self._idle and not new else None
            if connection is None:
                self.opened += 1
        for old in stale:
            old.close()
        if connection is not None:
            return connection, True
        return self._connection_class(*self._address, timeout=TIMEOUT_SECONDS), False

    def _keep(self, connection, response) -> None:
        if connection.sock is None:
            return  # the server closed it (e.g. after its 100th request)
        until = time.monotonic() + _reusable_for(response.getheader("Keep-Alive"))
        with self._lock:
            self._idle.append((connection, until))

    def close(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, []
        for connection, _ in idle:
            connection.close()


def _reusable_for(keep_alive) -> float:
    """How long a connection may sit idle and still be used: a second less
    than the server's Keep-Alive timeout (Hostinger's LiteSpeed says
    "timeout=5, max=100"), or 4 seconds if it doesn't say."""
    for part in (keep_alive or "").split(","):
        name, _, value = part.strip().partition("=")
        if name.lower() == "timeout" and value.strip().isdigit():
            return max(0, min(int(value) - 1, 60))
    return 4
