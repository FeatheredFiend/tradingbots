"""
Broadcast trades from the dashboard - the part every bot that takes them
shares (the strategy bots' engine and the momentum scanners).

An admin picks an instrument and Buy or Sell on the dashboard, and every
bot that takes broadcasts is asked, through the dashboard command channel
(shared/dashboard_reporter.py), in two rounds:

- broadcast-preview: the bot works out what it would do with the trade, as
  if its own strategy had signalled it - its broker's code for the
  instrument (shared/symbols.py), its size, stop-loss and take-profit, from
  live prices - and answers with those figures, or with why it won't take
  it. Nothing goes to the broker.
- broadcast-open, once the admin confirms: the bot checks everything again
  on fresh prices - its own limits always win - and opens it, never bigger
  than it previewed. It notes the broadcast before the order goes, so it
  never opens the same one twice, even across a restart.

Every limit the bot applies to its own trades applies here too; only the
signal is the admin's. A quantity, when given, is the exposure wanted in the
bot's account currency, and can only make a trade smaller than the bot's
own limits allow, never bigger.

The trades a broadcast opened are tagged entrySource MANUAL_BROADCAST and
their broadcastId in every position and trade row the bot sends the
dashboard, so benchmarks can leave them out (Book.tags()).

The switch: DASHBOARD_BROADCAST=1 (off unless set), separate from
DASHBOARD_COMMANDS - opening trades is riskier than closing them.

Standard library only, so it works in every bot's venv.
"""

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from dashboard_reporter import CommandError

ENTRY_SOURCE = "MANUAL_BROADCAST"

# The dashboard's kinds of "why not", which it groups and labels.
UNAVAILABLE = "unavailable"   # the broker hasn't got the instrument, or not for this side
NO_SLOT = "no-slot"           # no free position slot, or the market's already held
CLOSED = "closed"             # the market's shut, or it's outside the bot's trading hours
RISK = "risk"                 # one of the bot's limits: size, spread, swap, trades a day, cooldown...
PAUSED = "paused"             # the bot isn't trading: a dry run, its daily loss limit...
OTHER = "other"

# The dashboard takes a confirm for 90 s after asking, and the open command
# waits up to 30 s for the bot's next report: an open later than this after
# the bot's own preview is refused - prices have moved too far to trust it.
OPEN_WITHIN_SECONDS = 150
# A dashboard row opened this close to a broadcast trade, in the same market, is that trade.
MATCH_SECONDS = 120
# A trade just opened may take this long to show up at the broker (Alpaca fills a moment after accepting).
FILL_GRACE_SECONDS = 180
KEEP_SECONDS = 8 * 86400      # as long as the bots look back for closed trades


class Declined(CommandError):
    """A broadcast the bot won't take, and why, in plain words."""

    def __init__(self, kind: str, message: str):
        super().__init__(message, data={"reason": kind})
        self.kind = kind


@dataclass
class Request:
    """What a broadcast-preview or broadcast-open command carries."""
    id: int
    target: int
    symbol: str                  # as typed on the dashboard
    side: str                    # "buy" / "sell"
    quantity: float = None       # the exposure wanted, in the account's currency; None = the bot's own sizing
    preview: dict = None         # broadcast-open: what this bot previewed
    size: float = None           # broadcast-open: the most it may open (the previewed size)

    @property
    def direction(self) -> str:
        return "long" if self.side == "buy" else "short"

    @classmethod
    def parse(cls, command: dict) -> "Request":
        payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
        try:
            broadcast_id, target = int(payload["broadcastId"]), int(payload.get("target") or 0)
            symbol, side = str(payload["symbol"]).strip(), payload["side"]
        except (KeyError, TypeError, ValueError):
            raise Declined(OTHER, "That broadcast came without its details - nothing was done.") from None
        if side not in ("buy", "sell") or not symbol:
            raise Declined(OTHER, f"That broadcast asked to {side!r} {symbol!r} - nothing was done.")
        quantity = payload.get("quantity")
        if quantity is not None and (not isinstance(quantity, (int, float)) or quantity <= 0):
            raise Declined(OTHER, f"{quantity!r} isn't a quantity - nothing was done.")
        preview = payload.get("preview") if isinstance(payload.get("preview"), dict) else None
        size = command.get("size")
        if not isinstance(size, (int, float)) or size <= 0:
            size = (preview or {}).get("size") if isinstance((preview or {}).get("size"), (int, float)) else None
        return cls(broadcast_id, target, symbol, side, quantity, preview, size)

    def describe(self) -> str:
        return (f"broadcast #{self.id}: {self.side} {self.symbol}"
                + (f", {self.quantity:g} exposure" if self.quantity else ", the bot's own size"))

    def check_age(self, now: float) -> None:
        """For broadcast-open: refuse it if this bot's preview is too old, or missing."""
        at = (self.preview or {}).get("previewedAt")
        if not isinstance(at, (int, float)):
            raise Declined(OTHER, "The trade came without this bot's preview, so nothing was opened. Preview it again.")
        if now - at > OPEN_WITHIN_SECONDS:
            raise Declined(OTHER, f"This bot previewed it {now - at:.0f}s ago - prices have moved too far since "
                                  f"(at most {OPEN_WITHIN_SECONDS}s). Nothing was opened; preview it again.")

    def most(self, size: float) -> float:
        """`size`, but never more than this bot previewed."""
        return size if self.size is None else min(size, self.size)


def figures(**values) -> dict:
    """A preview's figures for the dashboard - symbol, name, size, sizeUnit,
    exposure, risk, currency, entry, stopLoss, takeProfit, accountMode,
    exits, capped, standIn, previewedAt - leaving out the ones that are None."""
    return {k: v for k, v in values.items() if v is not None}


def exposure_for(request: Request, normal: float, currency: str, limit: str = "per-trade limit") -> tuple:
    """(exposure to trade, "" or a note that it was capped) - for a bot that
    sizes by exposure: its own `normal` amount, or the quantity asked for if
    that's smaller. Never more."""
    if request.quantity is None:
        return normal, ""
    if request.quantity > normal * (1 + 1e-9):
        return normal, f"capped at {normal:,.2f} {currency} (the bot's {limit})"
    return request.quantity, ""


def seconds(value):
    """A row's time as Unix seconds: from seconds (or a string of them) or an
    ISO 8601 string; None if it's neither."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


class Book:
    """The broadcasts a bot has acted on, and the trades they opened:
    {id: {"status": "opening" | "opened" | "failed", "symbol", "aliases",
    "at", "openedAt", "closedAt", "refs"}}. Saved with every change - the
    engine keeps it in its state file, a scanner in a file of its own - so a
    broadcast is never opened twice, and its trade's rows are still tagged
    after a restart."""

    def __init__(self, records: dict = None, save=None, path: str = None, log=None):
        self.path, self.log = path, log
        self.records = records if records is not None else {}
        self._save = save
        if path is not None:
            try:
                with open(path, encoding="utf-8") as f:
                    self.records.update(json.load(f).get("broadcasts") or {})
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as e:
                if log is not None:
                    log.warning(f"Couldn't read {path} ({e}); starting without its broadcast notes.")

    def save(self) -> None:
        if self._save is not None:
            self._save()
        elif self.path is not None:
            try:
                with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump({"broadcasts": self.records}, f, indent=1)
                os.replace(self.path + ".tmp", self.path)
            except OSError as e:
                if self.log is not None:
                    self.log.warning(f"Couldn't save {self.path}: {e}")

    # -- acting on one ------------------------------------------------------------
    def check_new(self, request: Request) -> None:
        if str(request.id) in self.records:
            raise Declined(OTHER, f"This bot has already acted on broadcast #{request.id} - it never opens one twice.")

    def opening(self, request: Request, symbol: str, aliases=(), now: float = None) -> None:
        """Noted - and saved - before the order goes, so a crash or a repeat
        can never send it twice."""
        self.records[str(request.id)] = {
            "status": "opening", "symbol": symbol, "aliases": sorted({symbol, *(a for a in aliases if a)}),
            "at": now or time.time(), "openedAt": None, "closedAt": None, "refs": [],
        }
        self.save()

    def opened(self, request: Request, refs=(), now: float = None) -> None:
        record = self.records[str(request.id)]
        record.update(status="opened", openedAt=now or time.time(), refs=sorted(str(r) for r in refs if r))
        self.save()

    def failed(self, request: Request) -> None:
        self.records[str(request.id)]["status"] = "failed"
        self.save()

    def closed(self, symbol: str, now: float) -> None:
        """The bot's trade in `symbol` has gone (the engine says so as it frees its slot)."""
        changed = False
        for record in self._open():
            if symbol == record["symbol"] or symbol in record.get("aliases", ()):
                record["closedAt"] = now
                changed = True
        if changed:
            self.save()

    def sync(self, open_symbols, now: float) -> None:
        """For a bot with no one place a slot frees (the scanners): the open
        broadcast trades whose market no longer shows a position have closed."""
        open_symbols = set(open_symbols)
        changed = False
        for record in self._open():
            if record["symbol"] not in open_symbols and now - record["openedAt"] > FILL_GRACE_SECONDS:
                record["closedAt"] = now
                changed = True
        if changed:
            self.save()

    def open_symbols(self) -> set:
        """The markets of the broadcast trades still open."""
        return {record["symbol"] for record in self._open()}

    def symbols(self) -> set:
        """The markets of every broadcast trade the book still holds, open or
        closed - their closed trades are still reported to the dashboard."""
        return {r["symbol"] for r in self.records.values() if r.get("status") == "opened"}

    def aliases(self, symbol: str) -> list:
        """The other names a broadcast trade's market was noted under (its IG name, say)."""
        for record in self.records.values():
            if record.get("symbol") == symbol:
                return [a for a in record.get("aliases", ()) if a != symbol]
        return []

    def prune(self, now: float) -> None:
        old = [key for key, r in self.records.items() if now - (r.get("closedAt") or r.get("at") or now) > KEEP_SECONDS]
        for key in old:
            del self.records[key]
        if old:
            self.save()

    def _open(self) -> list:
        return [r for r in self.records.values() if r.get("status") == "opened" and r.get("closedAt") is None]

    # -- tagging the dashboard's rows ------------------------------------------------
    def tags(self, row: dict, now: float = None) -> dict:
        """{"entrySource", "broadcastId"} for a position or trade row about a
        trade a broadcast opened, else {}. Matched by the broker's ref where
        the bot knows it, else by market and opening time - brokers name a
        trade differently in their history (IG) or not at all (Alpaca) - or,
        for a row with no opening time, by its closing time."""
        now = now or time.time()
        ref = row.get("ref")
        ref = None if ref in (None, "") else str(ref)
        symbol = row.get("symbol")
        opened, closed = seconds(row.get("openedAt")), seconds(row.get("closedAt"))
        for key, record in self.records.items():
            if record.get("status") != "opened":
                continue
            if ref is not None and ref in record.get("refs", ()):
                return {"entrySource": ENTRY_SOURCE, "broadcastId": int(key)}
            if symbol not in record.get("aliases", ()):
                continue
            if opened is not None:
                match = abs(opened - record["openedAt"]) <= MATCH_SECONDS
            elif closed is not None:
                match = record["openedAt"] - MATCH_SECONDS <= closed <= (record["closedAt"] or now) + MATCH_SECONDS
            else:  # an open position with no times at all (the Alpaca scanner's)
                match = record["closedAt"] is None
            if match:
                return {"entrySource": ENTRY_SOURCE, "broadcastId": int(key)}
        return {}
