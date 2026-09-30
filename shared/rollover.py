"""
The daily rollover, for the CFD momentum scanners (OANDA, Pepperstone,
Capital.com, IG). A CFD position still open at 17:00 New York - 22:00 UK
most of the year, 21:00 in the weeks the UK and US change their clocks on
different dates - is charged a night's financing (swap). A 3-bar streak is
long over by then, so by default a scanner:

- closes the trades it opened SCANNER_FLAT_MINUTES (15) before the
  rollover - 21:45 UK - and
- opens nothing from SCANNER_LAST_ENTRY_MINUTES (60) before it until
  RESUME_MINUTES (45) after it, when spreads are back from their widest.

0 switches either part off; both 0 holds overnight, as the scanners used to.

Only trades the scanner opened are closed, never everything in its
markets: it shares accounts with other bots (the commodity-trend bot holds
overnight on purpose) and with trades made by hand. The OANDA, Capital.com
and IG scanners keep the broker's ID for each trade they open in an
OwnTrades file; Pepperstone's knows its own by magic number.

The clock is the strategy bots' (strategy-bots/engine/clock.py): standard
library only, with the UK and US clock changes written out.
"""

import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "strategy-bots"))
from engine import clock  # noqa: E402 - needs the path above

log = logging.getLogger("rollover")


def _minutes(name: str, default: int) -> int:
    text = os.environ.get(name, "").strip()
    value = round(float(text)) if text else default  # the launcher accepts "15.0" too
    if value < 0:
        raise ValueError(f"{name} can't be negative")
    return value


FLAT_MINUTES = _minutes("SCANNER_FLAT_MINUTES", 15)
LAST_ENTRY_MINUTES = _minutes("SCANNER_LAST_ENTRY_MINUTES", 60)
RESUME_MINUTES = 45  # after the rollover, as the strategy bots wait
REASON = "before the rollover"  # the close reason the dashboard shows


def flat_due(now: float = None) -> bool:
    """In the last FLAT_MINUTES before the rollover: close the scanner's trades."""
    now = time.time() if now is None else now
    return FLAT_MINUTES > 0 and clock.next_rollover(now) - now <= FLAT_MINUTES * 60


def entries_paused(now: float = None) -> bool:
    """Too near the rollover to open a trade: from LAST_ENTRY_MINUTES (at
    least FLAT_MINUTES) before it until RESUME_MINUTES after it."""
    before = max(LAST_ENTRY_MINUTES, FLAT_MINUTES)
    if before == 0:
        return False
    return clock.near_rollover(time.time() if now is None else now, before, RESUME_MINUTES)


def trading_day_start(now: float = None) -> float:
    """When the current trading day began: the last rollover at or before `now`."""
    now = time.time() if now is None else now
    upcoming = clock.next_rollover(now)
    # The one before is 23 to 25 hours earlier (the clocks change in between).
    return now if upcoming == now else clock.next_rollover(upcoming - 26 * 3600)


def next_rollover_uk(now: float = None) -> str:
    """The next rollover in UK time, e.g. "22:00"."""
    upcoming = clock.next_rollover(time.time() if now is None else now)
    return clock.local("london", upcoming).strftime("%H:%M")


def describe(now: float = None) -> str:
    """One line for the startup log, with tonight's times in UK time."""
    now = time.time() if now is None else now
    rollover = clock.next_rollover(now)

    def uk(ts: float) -> str:
        return clock.local("london", ts).strftime("%H:%M")

    if FLAT_MINUTES == 0 and LAST_ENTRY_MINUTES == 0:
        return f"Rollover: holds overnight (SCANNER_FLAT_MINUTES=0), next rollover {uk(rollover)} UK"
    parts = [f"Rollover {uk(rollover)} UK"]
    if FLAT_MINUTES:
        parts.append(f"closes its trades at {uk(rollover - FLAT_MINUTES * 60)}")
    else:
        parts.append("holds its trades overnight (SCANNER_FLAT_MINUTES=0)")
    before = max(LAST_ENTRY_MINUTES, FLAT_MINUTES)
    parts.append(f"no new trades {uk(rollover - before * 60)}-{uk(rollover + RESUME_MINUTES * 60)}")
    return " | ".join(parts)


class OwnTrades:
    """The broker's IDs for the trades a scanner opened, in a JSON file
    beside the bot so a restart still knows them."""

    def __init__(self, path: str):
        self.path = path
        self.ids = set()
        try:
            with open(path, encoding="utf-8") as f:
                self.ids = {str(i) for i in json.load(f)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as e:
            # Starting empty only means older trades aren't closed before the rollover.
            log.warning(f"Couldn't read {path} ({e}); trades opened before this start won't be "
                        f"closed before the rollover.")

    def __contains__(self, trade_id) -> bool:
        return str(trade_id) in self.ids

    def add(self, *trade_ids) -> None:
        new = {str(i) for i in trade_ids if i} - self.ids
        if new:
            self.ids |= new
            self._save()

    def keep_open(self, open_ids) -> None:
        """Forget the trades that have closed, given every open trade's ID."""
        kept = self.ids & {str(i) for i in open_ids}
        if kept != self.ids:
            self.ids = kept
            self._save()

    def _save(self) -> None:
        temp = self.path + ".tmp"
        try:
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(sorted(self.ids), f)
            os.replace(temp, self.path)
        except OSError as e:
            log.error(f"Couldn't save {self.path} ({e}); a restart would forget which trades are this bot's.")
