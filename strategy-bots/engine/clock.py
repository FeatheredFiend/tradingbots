"""
The market clock: the handful of time zones the strategies need, with
their daylight-saving rules written out. Windows Python has no time zone
database of its own and the Pepperstone environment doesn't install
tzdata, so this needs nothing but the standard library.

All times passed in and out are Unix seconds (UTC); "local" datetimes are
naive, in the zone named.
"""

from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone


def _nth_sunday(year: int, month: int, n: int) -> date:
    """The n-th Sunday of a month (n=-1 for the last)."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(6 - first.weekday()) % 7, weeks=n - 1)
    last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - 6) % 7)


def _eu_summer_time(utc: datetime) -> bool:
    """UK and EU clocks go forward at 01:00 UTC on the last Sunday of March
    and back at 01:00 UTC on the last Sunday of October."""
    start = datetime.combine(_nth_sunday(utc.year, 3, -1), dtime(1), timezone.utc)
    end = datetime.combine(_nth_sunday(utc.year, 10, -1), dtime(1), timezone.utc)
    return start <= utc < end


def _us_summer_time(utc: datetime) -> bool:
    """US clocks go forward at 02:00 local (07:00 UTC) on the second Sunday
    of March and back at 02:00 local (06:00 UTC) on the first Sunday of November."""
    start = datetime.combine(_nth_sunday(utc.year, 3, 2), dtime(7), timezone.utc)
    end = datetime.combine(_nth_sunday(utc.year, 11, 1), dtime(6), timezone.utc)
    return start <= utc < end


_OFFSETS = {  # zone -> function(UTC datetime) -> hours ahead of UTC
    "london": lambda utc: 1 if _eu_summer_time(utc) else 0,
    "frankfurt": lambda utc: 2 if _eu_summer_time(utc) else 1,
    "new_york": lambda utc: -4 if _us_summer_time(utc) else -5,
    "tokyo": lambda utc: 9,
}
ZONE_NAMES = {"london": "London", "frankfurt": "Frankfurt", "new_york": "New York", "tokyo": "Tokyo"}


def utc_offset_hours(zone: str, ts: float) -> int:
    return _OFFSETS[zone](datetime.fromtimestamp(ts, timezone.utc))


def local(zone: str, ts: float) -> datetime:
    """Unix seconds as a naive local datetime in `zone`."""
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None) + timedelta(hours=utc_offset_hours(zone, ts))


def to_utc(zone: str, local_dt: datetime) -> float:
    """A naive local datetime in `zone` as Unix seconds. Around a clock
    change the offset is taken from just after it, which is right for every
    time the strategies use (none falls in the skipped or repeated hour)."""
    naive_utc = local_dt.replace(tzinfo=timezone.utc).timestamp()
    guess = naive_utc - utc_offset_hours(zone, naive_utc) * 3600
    return naive_utc - utc_offset_hours(zone, guess) * 3600


def parse_hhmm(text: str) -> dtime:
    """'07:00' -> time(7, 0). Raises ValueError for anything else."""
    hours, minutes = str(text).strip().split(":")
    return dtime(int(hours), int(minutes))


def at(zone: str, day: date, clock: dtime) -> float:
    """Unix seconds of `clock` on `day` in `zone`."""
    return to_utc(zone, datetime.combine(day, clock))


def local_date(zone: str, ts: float) -> date:
    return local(zone, ts).date()


# ---------------------------------------------------------------------------
# Daily rollover - when CFD positions held past it are charged a night's
# financing (swap). Brokers roll at 17:00 New York time, the FX market's
# day boundary: 22:00 UK time for most of the year.
# ---------------------------------------------------------------------------
ROLLOVER = dtime(17, 0)


def next_rollover(ts: float) -> float:
    """The next 17:00 New York at or after `ts`."""
    day = local_date("new_york", ts)
    rollover = at("new_york", day, ROLLOVER)
    return rollover if rollover >= ts else at("new_york", day + timedelta(days=1), ROLLOVER)


def near_rollover(ts: float, before_minutes: int = 15, after_minutes: int = 45) -> bool:
    """Inside the window around the rollover where spreads widen and
    liquidity thins - no new trades then."""
    upcoming = next_rollover(ts)
    previous = upcoming - 86400  # an hour out on the two clock-change days, which is fine here
    return (upcoming - ts) <= before_minutes * 60 or (ts - previous) <= after_minutes * 60


# ---------------------------------------------------------------------------
# Index cash sessions - the hours the underlying exchange is open, which is
# when an index's intraday mean (its session VWAP) means anything.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Session:
    key: str
    name: str
    zone: str
    opens: dtime
    closes: dtime

    def bounds(self, day: date) -> tuple:
        """(open, close) as Unix seconds for a weekday; None at weekends."""
        if day.weekday() >= 5:
            return None
        return at(self.zone, day, self.opens), at(self.zone, day, self.closes)

    def bounds_at(self, ts: float):
        """The session on the local date of `ts` in this session's zone."""
        return self.bounds(local_date(self.zone, ts))


INDEX_SESSIONS = {
    "us": Session("us", "US cash session (New York 09:30-16:00)", "new_york", dtime(9, 30), dtime(16, 0)),
    "uk": Session("uk", "UK cash session (London 08:00-16:30)", "london", dtime(8, 0), dtime(16, 30)),
    "eu": Session("eu", "German cash session (Frankfurt 09:00-17:30)", "frankfurt", dtime(9, 0), dtime(17, 30)),
    "jp": Session("jp", "Japan cash session (Tokyo 09:00-15:00)", "tokyo", dtime(9, 0), dtime(15, 0)),
}
