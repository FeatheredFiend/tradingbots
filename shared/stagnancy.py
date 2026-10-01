"""
Stagnancy timeout - closes a trade that has gone nowhere for a while, so it
stops holding one of its bot's few position slots (and stops padding the
results with trades worth pennies). Shared by the strategy bots
(strategy-bots/engine/runner.py) and the original EMA bots and momentum
scanners, like rollover.py. It never changes how a bot enters a trade.

The rule
--------
A trade is stagnant when ALL of these hold:
- it's older than MIN_AGE;
- over the most recent WINDOW - a rolling window, not since the entry -
  the high-low range of the mid price is at most RANGE: a % of the price
  ("0.05%") or a multiple of ATR(14) on the bot's own bars ("0.25atr");
- its unrealised P/L - at the price it would close at, so net of the
  spread, and of the fees the bot knows about - is within PNL either way:
  an amount in the account's currency ("1.50") or a fraction of what the
  trade stood to lose at its stop ("0.2R").
Mid prices, so a spread that widens doesn't count as the price moving.
Nothing is checked while the prices are stale or have a gap in them, or
the market is closed: the window starts again on fresh prices.

Durations are written "90s", "15m", "2h" or "4bars" (bars of the bot's
timeframe). The strategy bots that trade on bars measure a "bars" window
on those bars (high and low); every other window is the mid prices the bot
reads while it holds the trade.

Modes
-----
- shadow (the default): logs TIMEOUT_STAGNANT_SHADOW when a trade turns
  stagnant (and again if it moves and stalls again) and leaves it open.
  When the trade closes, its dashboard record carries what the timeout saw
  the first time, so the archive can compare that with what happened.
- enforce: closes it at market through the bot's own close, reason
  TIMEOUT_STAGNANT. The trade is marked "closing" first, so nothing closes
  it twice, and its slot frees once the broker confirms the close - once.
  A refused close is tried again after 15, 30, 60, 120 and 240 seconds,
  then every 5 minutes, with one ALERT in the log after the 5th failure.
  A part-filled close has the rest closed straight away. If the market
  shuts before the close goes through, the timeout is called off and the
  rule starts again on fresh prices. If the stop-loss or take-profit gets
  there first, the broker refuses the close and the trade keeps the
  broker's reason.
- off.

Settings
--------
Per bot type, in the launcher (Windows user environment variables), each
empty one falling back to the defaults in TYPES below:
    STAGNANT_MODE                   every bot type's mode - default shadow
    <TYPE>_STAGNANT_MODE            off / shadow / enforce, for that type
    <TYPE>_STAGNANT_MIN_AGE, _WINDOW, _RANGE, _PNL
    <TYPE>_STAGNANT_COOLDOWN        no new trade in that market for this
                                    long after a TIMEOUT_STAGNANT close
                                    (0, the default, = none)
<TYPE> is SCALPER, SURGE, REVERSION, BREAKOUT, TREND (the strategy bots),
EMA or SCANNER (the original EMA bots and momentum scanners).

Exceptions for one broker, or one market on it, go in stagnancy.json at
the top of the repo (STAGNANT_FILE names another) and win over the
launcher's - see stagnancy.example.json:
    {"scalper": {"ig": {"mode": "off"},
                 "oanda": {"range": "0.008%", "symbols": {"SPX500_USD": {"window": "90s"}}}}}
Like every other setting, they're read when a bot starts.

Standard library only, so it works in every bot's venv.
"""

import json
import math
import os
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

TIMEOUT_REASON = "TIMEOUT_STAGNANT"
SHADOW_REASON = "TIMEOUT_STAGNANT_SHADOW"
MODES = ("off", "shadow", "enforce")
DEFAULT_MODE = "shadow"
FIELDS = ("mode", "min_age", "window", "range", "pnl", "cooldown")
BROKERS = ("oanda", "pepperstone", "capital", "ig", "alpaca")

# bot type -> (its settings' prefix, what it's called, its defaults). At the
# default 15-minute bars the bar counts are: reversion 1h, breakout 2h /
# 1.5h, trend 4h, EMA bots 2h, scanners 45m.
TYPES = {
    # A scalp lives on a burst: no move of about a spread (FX majors, US 500)
    # for a minute means it's over. Its 300s time stop stays as the backstop.
    "scalper": ("SCALPER", "tick scalper", {"min_age": "90s", "window": "60s", "range": "0.01%", "pnl": "0.25R"}),
    # A surge should carry on within minutes; three flat ones means it's done.
    "surge-follower": ("SURGE", "surge follower",
                       {"min_age": "4m", "window": "3m", "range": "0.15%", "pnl": "0.25R"}),
    # A fade should snap back to the VWAP inside the hour; a tight range with
    # ~0 P/L means the stretch has gone without the reversion paying.
    "index-reversion": ("REVERSION", "index mean reversion",
                        {"min_age": "4bars", "window": "4bars", "range": "1atr", "pnl": "0.2R"}),
    # A real breakout follows through within a couple of hours; a tight range
    # after it means it failed.
    "session-breakout": ("BREAKOUT", "session breakout",
                         {"min_age": "8bars", "window": "6bars", "range": "1atr", "pnl": "0.25R"}),
    # Trend trades must sit through noise: only after 4 hours in a range
    # tighter than its own 2-ATR stop.
    "commodity-trend": ("TREND", "commodity trend",
                        {"min_age": "16bars", "window": "16bars", "range": "1.5atr", "pnl": "0.3R"}),
    # Against a 2% stop, under 0.3% of movement in 2 hours means the
    # crossover had no follow-through.
    "ema": ("EMA", "EMA crossover", {"min_age": "8bars", "window": "8bars", "range": "0.3%", "pnl": "0.15R"}),
    # A 3-bar streak should keep going within three bars; on 30 Sep the
    # OANDA scanner's FX trades never reached their stop or target - they sat
    # until a reversal or the rollover closed them.
    "scanner": ("SCANNER", "momentum scanner",
                {"min_age": "3bars", "window": "3bars", "range": "0.15%", "pnl": "0.2R"}),
}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RETRY_SECONDS = (15, 30, 60, 120, 240)   # a refused close is tried again after these...
RETRY_EVERY_SECONDS = 300                # ...then this often
ALERT_AFTER = 5                          # failed tries before the ALERT line
CONFIRM_SECONDS = 60                     # an accepted close (Alpaca) not filled after this long is sent again
KEEP_EXITS_SECONDS = 8 * 86400           # dashboard extras are kept as long as the bots look back for trades


class ConfigError(ValueError):
    """A stagnancy setting that can't be used; the message names it."""


def overrides_path() -> str:
    return os.environ.get("STAGNANT_FILE", "").strip() or os.path.join(ROOT, "stagnancy.json")


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Span:
    value: float
    unit: str                    # "s" or "bars"
    text: str

    def seconds(self, bar_seconds=None) -> float:
        return self.value * bar_seconds if self.unit == "bars" else self.value


@dataclass(frozen=True)
class Limit:
    value: float
    unit: str                    # "%" or "atr" (range); "money" or "R" (P/L)
    text: str


@dataclass(frozen=True)
class Rule:
    mode: str
    min_age: Span
    window: Span
    range: Limit
    pnl: Limit
    cooldown: Span

    def summary(self, currency: str = "") -> str:
        """e.g. "older than 4bars, range over the last 4bars at most 1 ATR, P/L within +/-0.2R"."""
        rng = f"{self.range.value:g}% of the price" if self.range.unit == "%" else f"{self.range.value:g} ATR"
        pnl = f"{self.pnl.value:g}R" if self.pnl.unit == "R" else f"{self.pnl.value:g} {currency}".strip()
        text = (f"older than {self.min_age.text}, range over the last {self.window.text} at most {rng}, "
                f"P/L within +/-{pnl}")
        if self.cooldown.value:
            text += f", then {self.cooldown.text} before re-entering"
        return text


_SPAN = re.compile(r"^(\d+(?:\.\d+)?)\s*(s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?|b|bars?)$", re.I)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}


def parse_span(text: str, where: str) -> Span:
    clean = str(text).strip().lower()
    if clean in ("0", "0s", "off", "none"):
        return Span(0.0, "s", "0")
    match = _SPAN.match(clean)
    if not match:
        raise ConfigError(f"{where}={text!r} must be a time like 90s, 15m, 2h or 4bars")
    value, unit = float(match.group(1)), match.group(2)[0]
    if unit == "b":
        if value != int(value) or value < 1:
            raise ConfigError(f"{where}={text!r} must be a whole number of bars")
        return Span(value, "bars", f"{int(value)}bars")
    return Span(value * _UNIT_SECONDS[unit], "s", clean.replace(" ", ""))


def parse_range(text: str, where: str) -> Limit:
    clean = str(text).strip().lower().replace(" ", "")
    match = re.match(r"^(\d+(?:\.\d+)?)%$", clean)
    if match:
        return Limit(float(match.group(1)), "%", clean)
    match = re.match(r"^(\d+(?:\.\d+)?)(?:x|\*)?atr(?:\(14\))?$", clean)
    if match:
        return Limit(float(match.group(1)), "atr", f"{match.group(1)}atr")
    raise ConfigError(f"{where}={text!r} must be a % of the price (e.g. 0.05%) or ATRs (e.g. 0.25atr)")


def parse_pnl(text: str, where: str) -> Limit:
    clean = str(text).strip().lower().replace(" ", "")
    match = re.match(r"^(\d+(?:\.\d+)?)r$", clean)
    if match:
        return Limit(float(match.group(1)), "R", f"{match.group(1)}R")
    try:
        value = float(clean)
    except ValueError:
        raise ConfigError(f"{where}={text!r} must be an amount in the account's currency (e.g. 1.50) or a "
                          f"fraction of the trade's risk (e.g. 0.2R)") from None
    if value < 0 or not math.isfinite(value):
        raise ConfigError(f"{where}={text!r} can't be negative")
    return Limit(value, "money", f"{value:g}")


def parse_mode(text: str, where: str) -> str:
    clean = str(text).strip().lower()
    if clean not in MODES:
        raise ConfigError(f"{where}={text!r} must be off, shadow or enforce")
    return clean


class RuleBook:
    """One bot's stagnancy rules: the defaults, the launcher's settings for
    its type, then stagnancy.json for its broker and each of its markets."""

    def __init__(self, bot_type: str, broker: str, bar_seconds: float = None, has_bars: bool = False,
                 environ=None, path: str = None):
        """bar_seconds: the bot's bar length, which a "4bars" setting counts in
        (None: the bot has no bars - the scalper, the surge followers).
        has_bars: the bot hands check() its own bars and ATR (the strategy
        bots that trade on bars); without, ATR can't be used."""
        if bot_type not in TYPES:
            raise ConfigError(f"no stagnancy timeout for {bot_type!r} bots")
        environ = os.environ if environ is None else environ
        self.bot_type, self.broker, self.bar_seconds, self.has_bars = bot_type, broker, bar_seconds, has_bars
        prefix, self.name, defaults = TYPES[bot_type]
        values = {**defaults, "cooldown": "0", "mode": DEFAULT_MODE}
        where = {f: f"the default {f}" for f in FIELDS}
        if (environ.get("STAGNANT_MODE") or "").strip():
            values["mode"], where["mode"] = environ["STAGNANT_MODE"].strip(), "STAGNANT_MODE"
        for f in FIELDS:
            name = f"{prefix}_STAGNANT_{f.upper()}"
            if (environ.get(name) or "").strip():
                values[f], where[f] = environ[name].strip(), name
        self.path = path or overrides_path()
        file = os.path.basename(self.path)
        layer, symbols = self._overrides(self.path)
        for f, v in layer.items():
            values[f], where[f] = v, f"{file} {bot_type} > {broker} > {f}"
        self.base = self._build(values, where)
        self.symbols = {}            # upper-case symbol -> Rule
        for symbol, extra in symbols.items():
            here = dict(where)
            here.update({f: f"{file} {bot_type} > {broker} > symbols > {symbol} > {f}" for f in extra})
            self.symbols[symbol.upper()] = self._build({**values, **extra}, here)
        self.broker_overrides = sorted(layer)

    @classmethod
    def for_bot(cls, bot_type: str, broker: str, **kwargs):
        """The bot's RuleBook, or None for bots the timeout doesn't cover (the portfolio bots)."""
        return cls(bot_type, broker, **kwargs) if bot_type in TYPES else None

    def _overrides(self, path: str) -> tuple:
        """({setting: value} for this bot's broker, {symbol: {setting: value}}) from stagnancy.json."""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}, {}
        except (OSError, ValueError) as e:
            raise ConfigError(f"can't read {path}: {e}") from None
        name = os.path.basename(path)

        def settings(block, at):
            if not isinstance(block, dict):
                raise ConfigError(f"{name} {at} must be an object")
            clean = {}
            for key, value in block.items():
                if key.startswith("_"):
                    continue  # "_comment" and the like
                if key not in FIELDS:
                    raise ConfigError(f"{name} {at} > {key}: unknown setting - use {', '.join(FIELDS)}")
                if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                    raise ConfigError(f"{name} {at} > {key} must be a string like \"15m\" or \"0.05%\"")
                clean[key] = str(value)
            return clean

        if not isinstance(data, dict):
            raise ConfigError(f"{name} must hold an object of bot types")
        mine, symbols = {}, {}
        for bot_type, brokers in data.items():
            if bot_type.startswith("_"):
                continue
            if bot_type not in TYPES:
                raise ConfigError(f"{name}: unknown bot type {bot_type!r} - use {', '.join(TYPES)}")
            if not isinstance(brokers, dict):
                raise ConfigError(f"{name} {bot_type} must be an object of brokers")
            for broker, block in brokers.items():
                if broker.startswith("_"):
                    continue
                if broker not in BROKERS:
                    raise ConfigError(f"{name} {bot_type}: unknown broker {broker!r} - use {', '.join(BROKERS)}")
                at = f"{bot_type} > {broker}"
                symbol_blocks = block.get("symbols", {}) if isinstance(block, dict) else None
                if not isinstance(symbol_blocks, dict):
                    raise ConfigError(f"{name} {at} > symbols must be an object of markets")
                layer = settings({k: v for k, v in block.items() if k != "symbols"}, at)
                parsed = {s: settings(b, f"{at} > symbols > {s}") for s, b in symbol_blocks.items()}
                if bot_type == self.bot_type and broker == self.broker:
                    mine, symbols = layer, parsed
        return mine, symbols

    def _build(self, values: dict, where: dict) -> Rule:
        rule = Rule(
            mode=parse_mode(values["mode"], where["mode"]),
            min_age=parse_span(values["min_age"], where["min_age"]),
            window=parse_span(values["window"], where["window"]),
            range=parse_range(values["range"], where["range"]),
            pnl=parse_pnl(values["pnl"], where["pnl"]),
            cooldown=parse_span(values["cooldown"], where["cooldown"]),
        )
        for f in ("min_age", "window", "cooldown"):
            span = getattr(rule, f)
            if span.unit == "bars" and not self.bar_seconds:
                raise ConfigError(f"{where[f]}={span.text!r}: {self.name} bots have no bars - give a time like "
                                  f"90s or 15m")
        if rule.window.value <= 0:
            raise ConfigError(f"{where['window']} must be longer than 0")
        if rule.range.unit == "atr" and not self.has_bars:
            raise ConfigError(f"{where['range']}={rule.range.text!r}: ATR needs the bot's own bars, which "
                              f"{self.name} bots don't keep - give a % of the price (e.g. 0.05%)")
        return rule

    def rule(self, symbol: str, aliases=()) -> Rule:
        """The rule for one market, by any of its names."""
        for name in (symbol, *aliases):
            if name and str(name).upper() in self.symbols:
                return self.symbols[str(name).upper()]
        return self.base

    def rules(self) -> list:
        return [self.base, *self.symbols.values()]

    @property
    def active(self) -> bool:
        return any(r.mode != "off" for r in self.rules())

    def uses_bars(self, rule: Rule) -> bool:
        """The window is measured on the bot's own bars (else on its price reads)."""
        return rule.window.unit == "bars" and self.has_bars

    @property
    def needs_reads(self) -> bool:
        """Some window is measured on price reads, so the bot must read prices while it holds a trade."""
        return any(r.mode != "off" and not self.uses_bars(r) for r in self.rules())

    def longest_read_window(self) -> float:
        spans = [r.window.seconds(self.bar_seconds) for r in self.rules() if not self.uses_bars(r)]
        return max(spans, default=0.0)

    def describe(self, currency: str = "") -> str:
        """One line for the startup log."""
        base = self.base
        text = f"Stagnancy timeout ({TIMEOUT_REASON}): {base.mode}"
        if base.mode != "off":
            text += f" - {base.summary(currency)}"
        extras = []
        if self.broker_overrides:
            extras.append(f"{', '.join(self.broker_overrides)} set for {self.broker} in {os.path.basename(self.path)}")
        if self.symbols:
            extras.append(f"own settings for {', '.join(sorted(self.symbols))}")
        return text + (f" ({'; '.join(extras)})" if extras else "")

    def config(self) -> dict:
        """The settings, for the dashboard's bot page."""
        base = self.base
        config = {"stagnantMode": base.mode, "stagnantMinAge": base.min_age.text, "stagnantWindow": base.window.text,
                  "stagnantRange": base.range.text, "stagnantPnl": base.pnl.text,
                  "stagnantCooldown": base.cooldown.text}
        if self.symbols:
            config["stagnantOwnSettings"] = ", ".join(sorted(self.symbols))
        return config


# ---------------------------------------------------------------------------
# THE RULE
# ---------------------------------------------------------------------------
class Window:
    """A market's recent mid prices, read while the bot holds a trade in it.
    A closed market, a broken price or a gap between reads starts it again."""

    def __init__(self, keep: float, gap: float):
        self.keep, self.gap = keep, gap
        self.samples = deque()       # (time, mid), oldest first

    def add(self, now: float, mid, tradeable: bool = True) -> None:
        if not tradeable or mid is None or not math.isfinite(mid) or mid <= 0:
            self.samples.clear()
            return
        if self.samples and now - self.samples[-1][0] > self.gap:
            self.samples.clear()
        self.samples.append((now, mid))
        # Keep one read from before the window too: it shows the window is covered.
        while len(self.samples) > 1 and self.samples[1][0] <= now - self.keep:
            self.samples.popleft()

    def clear(self) -> None:
        self.samples.clear()


@dataclass
class Verdict:
    stagnant: bool = False
    skipped: str = ""            # why the check couldn't be made (stale prices, market closed...), else ""
    why: str = ""                # why the trade isn't stagnant, when it isn't
    age: float = None
    mid: float = None
    range: float = None
    range_limit: float = None
    pnl: float = None
    pnl_limit: float = None


def check(rule: Rule, now: float, opened_at, pnl, *, risk=None, samples=None, bars=None, bar_seconds=None,
          atr=None, mid=None, stale_after: float = 60.0) -> Verdict:
    """Whether a trade is stagnant now. `samples` are (time, mid) price reads
    (a Window's); `bars` the bot's own closed bars (with .time, .high, .low,
    .close), used for a window counted in bars when given. `pnl` is the
    trade's unrealised P/L net of costs; `risk` what it stood to lose at its
    stop (for an R limit), both in the account's currency."""
    v = Verdict()
    if opened_at is None:
        v.skipped = "the broker gives no opening time"
        return v
    v.age = now - opened_at
    if v.age < rule.min_age.seconds(bar_seconds):
        v.why = f"{duration_text(v.age)} old (checked from {rule.min_age.text})"
        return v

    if rule.window.unit == "bars" and bars is not None:
        n = int(rule.window.value)
        recent = list(bars)[-n:]
        if len(recent) < n:
            v.skipped = f"only {len(recent)} of the {n} bars"
            return v
        last_end = recent[-1].time + bar_seconds
        if now - last_end > 2 * bar_seconds + 60:
            v.skipped = f"no new bar since {iso(last_end)} - market closed?"
            return v
        if recent[-1].time - recent[0].time > 2 * (n - 1) * bar_seconds:
            v.skipped = f"the last {n} bars span a gap (market closed in between)"
            return v
        high, low = max(b.high for b in recent), min(b.low for b in recent)
        v.mid = mid or recent[-1].close
    else:
        span = rule.window.seconds(bar_seconds)
        if not samples:
            v.skipped = "no prices - market closed?"
            return v
        last_time, last_mid = samples[-1]
        if now - last_time > stale_after:
            v.skipped = f"no fresh price for {now - last_time:.0f}s"
            return v
        if samples[0][0] > now - span:
            v.why = f"watching: {now - samples[0][0]:.0f}s of the {rule.window.text} window so far"
            return v
        points = [m for t, m in samples if t >= now - span] or [last_mid]
        high, low = max(points), min(points)
        v.mid = mid or last_mid

    v.range = high - low
    if rule.range.unit == "%":
        v.range_limit = rule.range.value / 100 * v.mid
    elif atr:
        v.range_limit = rule.range.value * atr
    else:
        v.skipped = "no ATR yet"
        return v
    if pnl is None:
        v.skipped = "the broker gives no P/L for it"
        return v
    v.pnl = pnl
    if rule.pnl.unit == "R":
        if not risk or risk <= 0:
            v.skipped = f"doesn't know what the trade stood to lose at its stop, so the {rule.pnl.text} P/L limit " \
                        f"can't be checked"
            return v
        v.pnl_limit = rule.pnl.value * risk
    else:
        v.pnl_limit = rule.pnl.value
    if v.range > v.range_limit * (1 + 1e-9):
        v.why = f"moving: range {num(v.range)} over {rule.window.text} > {num(v.range_limit)}"
    elif abs(pnl) > v.pnl_limit * (1 + 1e-9):
        v.why = f"P/L {pnl:+.2f} is outside +/-{v.pnl_limit:.2f}"
    else:
        v.stagnant = True
    return v


# ---------------------------------------------------------------------------
# WATCHING A BOT'S TRADES
# ---------------------------------------------------------------------------
@dataclass
class Held:
    """One of the bot's open trades (or positions), as the timeout sees it."""
    key: str                     # one per trade the bot manages: a strategy bot's symbol, a scanner's trade ID...
    symbol: str
    direction: str               # "long" / "short"
    size: float
    entry: float
    opened_at: float             # Unix seconds
    pnl: float = None            # unrealised, account currency, net of the spread and known fees
    risk: float = None           # account currency lost at the stop
    refs: tuple = ()             # the broker refs its dashboard trade record(s) go by
    aliases: tuple = ()          # other names of the market, for stagnancy.json


class Watch:
    """The timeout for one bot. Each pass the bot calls observe() with its
    price reads, assess() for each trade it holds - True means send a
    TIMEOUT_STAGNANT close now - and closed() after sending one; when a
    position it held has gone, gone(). It frees the slot when closed() or
    gone() hands back the trade's record fields (closed() only once the
    broker has confirmed the close)."""

    def __init__(self, book: RuleBook, bot: str, broker: str, log, *, currency: str = "", gap: float = 60.0,
                 dry_run: bool = False, store: dict = None):
        """store: a dict the bot saves between runs (the strategy bots' state
        file), so a restart still knows what's closing; None = memory only."""
        self.book, self.bot, self.broker, self.log = book, bot, broker, log
        self.currency, self.gap, self.dry_run = currency, gap, dry_run
        store = {} if store is None else store
        self.marks = store.setdefault("marks", {})          # key -> what the timeout knows about the trade
        self.exits = store.setdefault("exits", {})          # ref -> extra fields for its dashboard trade record
        self.cooldowns = store.setdefault("cooldowns", {})  # symbol -> no new trade there before this time
        self.windows = {}                                   # symbol -> Window
        self.noted = {}                                     # key -> the pause last logged
        self.changed = False                                # something to save

    # -- each pass -------------------------------------------------------------------
    def observe(self, symbol: str, now: float, bid, ask, tradeable: bool = True) -> None:
        """One price read of a market the bot holds."""
        window = self.windows.get(symbol)
        if window is None:
            window = self.windows[symbol] = Window(self.book.longest_read_window() + self.gap, self.gap)
        good = bid is not None and ask is not None and 0 < bid <= ask
        window.add(now, (bid + ask) / 2 if good else None, tradeable and good)

    def assess(self, held: Held, now: float, *, mid=None, tradeable: bool = True, bars=None, atr=None) -> bool:
        """True: send (or send again) a TIMEOUT_STAGNANT close for `held` now."""
        rule = self.book.rule(held.symbol, held.aliases)
        mark = self._mark(held)
        closing = mark.get("closing")
        if closing:
            return self._closing_step(held, mark, closing, now, tradeable)
        if rule.mode == "off":
            return False
        window = self.windows.get(held.symbol)
        if tradeable:
            v = check(rule, now, held.opened_at, held.pnl, risk=held.risk,
                      samples=window.samples if window else None, bars=bars if self.book.uses_bars(rule) else None,
                      bar_seconds=self.book.bar_seconds, atr=atr, mid=mid, stale_after=self.gap)
        else:
            v = Verdict(skipped="market closed")
        self._note(held, v.skipped)
        if not v.stagnant:
            if mark.get("stagnant"):
                mark["stagnant"] = False
                self.changed = True
            return False

        trigger = self._trigger(rule, v, now)
        if rule.mode == "shadow" or self.dry_run:
            if not mark.get("stagnant"):
                mark["stagnant"] = True
                mark["episodes"] = mark.get("episodes", 0) + 1
                mark.setdefault("shadow", trigger)  # the first time is what the archive compares with
                self.changed = True
                self.log.info(self._shadow_line(held, v, trigger, now, dry_run=rule.mode == "enforce"))
            return False
        mark["stagnant"] = True
        mark["closing"] = {"since": now, "attempts": 0, "next_try": now, "size": held.size, "trigger": trigger,
                           "mid": v.mid, "cooldown": rule.cooldown.seconds(self.book.bar_seconds)}
        self.changed = True
        self.log.info(f"{TIMEOUT_REASON}: closing {held.symbol} {held.direction} {held.size:g} "
                      f"(trade {self._trade_ids(held.refs, held.symbol)}) - {self._trigger_text(trigger)}")
        return True

    def closed(self, held: Held, now: float, ok: bool, *, filled: bool = True, price=None, pnl=None, refs=(),
               mid=None, problem: str = ""):
        """After the bot sent the TIMEOUT_STAGNANT close for `held`: ok = the
        broker took it; filled = and filled it (Alpaca only accepts it - the
        fill is confirmed when the position has gone). Returns the trade's
        record fields once the close is done - free the slot then - else None."""
        mark = self.marks.get(held.key) or {}
        closing = mark.get("closing")
        if not closing:
            return None
        if mid:
            closing["mid"] = mid
        self.changed = True
        if ok and filled:
            return self._finish(held.key, now, price, pnl, refs)
        if ok:
            closing["sent"] = now
            self.log.info(f"{held.symbol}: {self.broker} accepted the {TIMEOUT_REASON} close - the slot frees once "
                          f"the position has gone.")
            return None
        closing["attempts"] += 1
        tries = closing["attempts"]
        delay = RETRY_SECONDS[tries - 1] if tries <= len(RETRY_SECONDS) else RETRY_EVERY_SECONDS
        closing["next_try"] = now + delay
        self.log.warning(f"{held.symbol}: the {TIMEOUT_REASON} close didn't go through ({problem or 'see above'}) - "
                         f"try {tries}; trying again in {delay}s.")
        if tries >= ALERT_AFTER and not closing.get("alerted"):
            closing["alerted"] = True
            self.log.error(f"ALERT: {self.bot} couldn't close {held.symbol} {held.direction} ({TIMEOUT_REASON}) in "
                           f"{tries} tries since {iso(closing['since'])} - last: {problem or 'see above'}. It keeps "
                           f"trying every {RETRY_EVERY_SECONDS // 60} min; the trade still holds a slot.")
        return None

    def gone(self, key: str, now: float, *, price=None, pnl=None, refs=()):
        """The bot no longer sees the trade `key` - something closed it.
        Returns its record fields if the timeout has any: a TIMEOUT_STAGNANT
        close the broker only accepted until now, or what shadow mode saw."""
        mark = self.marks.get(key)
        if mark is None:
            return None
        closing = mark.get("closing")
        if closing and "sent" in closing:
            return self._finish(key, now, price, pnl, refs)
        del self.marks[key]
        self.noted.pop(key, None)
        self.changed = True
        if closing:
            self.log.info(f"{mark.get('symbol', key)}: closed before the {TIMEOUT_REASON} close went through - by its "
                          f"stop-loss or take-profit at {self.broker}, or by hand. Not a timeout exit: the trade "
                          f"record keeps the broker's reason.")
            return None
        if "shadow" in mark:
            fields = {"stagnancy": {**mark["shadow"], "mode": "shadow", "episodes": mark.get("episodes", 1)}}
            self._store_exit(mark, fields, refs, now)
            return fields
        return None

    def forget_missing(self, present_keys, now: float, fill_of=None) -> list:
        """gone() for every trade the timeout knows that isn't in `present_keys`
        (for bots that read all their trades each pass). fill_of(key) gives a
        close the broker only accepted what it filled at ({"price", "pnl",
        "refs"} or None). Returns [(key, fields)] for those with fields."""
        present = {str(k) for k in present_keys}
        done = []
        for key in [k for k in self.marks if k not in present]:
            fill = fill_of(key) if fill_of is not None and self.sent(key) else None
            fields = self.gone(key, now, **(fill or {}))
            if fields:
                done.append((key, fields))
        return done

    def run(self, trades: list, now: float, close, fill_of=None) -> list:
        """One pass over an original bot's own open trades. `trades` is
        [(Held, tradeable)] for EVERY trade it holds now - any the timeout knew
        that aren't there have gone. close(held) sends a TIMEOUT_STAGNANT close
        and returns {"done": bool, "filled": bool (default True), "price",
        "pnl", "refs", "problem"}. Returns [(key, fields)] for the trades whose
        dashboard record got fields this pass."""
        done = self.forget_missing([h.key for h, _ in trades], now, fill_of)
        for held, tradeable in trades:
            if not self.assess(held, now, tradeable=tradeable):
                continue
            mid = self.last_mid(held.symbol)
            result = close(held) or {}
            fields = self.closed(held, now, bool(result.get("done")), filled=result.get("filled", True),
                                 price=result.get("price"), pnl=result.get("pnl"), refs=result.get("refs", ()),
                                 mid=mid, problem=result.get("problem", ""))
            if fields:
                done.append((held.key, fields))
        self.prune(now)
        return done

    def last_mid(self, symbol: str):
        window = self.windows.get(symbol)
        return window.samples[-1][1] if window is not None and window.samples else None

    # -- questions ---------------------------------------------------------------------
    def closing(self, key: str) -> bool:
        return "closing" in (self.marks.get(key) or {})

    def closing_in(self, symbol: str) -> bool:
        """A TIMEOUT_STAGNANT close is under way for one of the bot's trades in
        `symbol` - the bot's other exits leave it alone meanwhile."""
        return any("closing" in m and m.get("symbol") == symbol for m in self.marks.values())

    def sent(self, key: str) -> bool:
        return "sent" in ((self.marks.get(key) or {}).get("closing") or {})

    def cooling(self, symbol: str, now: float) -> float:
        """Until when `symbol` mustn't be re-entered after a TIMEOUT_STAGNANT close (0 = it may)."""
        until = self.cooldowns.get(symbol, 0)
        if until and until <= now:
            del self.cooldowns[symbol]
            self.changed = True
            return 0
        return until

    def shadow_fields(self, key: str) -> dict:
        """What shadow mode saw of a trade still open - for a bot that sends a
        trade's record itself as it closes it (Alpaca's), before gone()."""
        mark = self.marks.get(key) or {}
        if "shadow" not in mark:
            return {}
        return {"stagnancy": {**mark["shadow"], "mode": "shadow", "episodes": mark.get("episodes", 1)}}

    def fields_for(self, ref) -> dict:
        """Extra fields for the dashboard record of the trade with broker ref `ref`."""
        entry = self.exits.get(str(ref))
        return {k: v for k, v in entry.items() if not k.startswith("_")} if entry else {}

    def fields_matching(self, symbol: str, opened_at, tolerance: float = 5.0) -> dict:
        """The same, found by market and opening time - for IG, whose trade
        history doesn't name the deal that opened a trade."""
        if opened_at is None:
            return {}
        for entry in self.exits.values():
            if entry.get("_symbol") == symbol and entry.get("_openedAt") is not None \
                    and abs(entry["_openedAt"] - opened_at) <= tolerance:
                return {k: v for k, v in entry.items() if not k.startswith("_")}
        return {}

    def prune(self, now: float) -> None:
        for ref in [r for r, e in self.exits.items() if now - e.get("_at", now) > KEEP_EXITS_SECONDS]:
            del self.exits[ref]
            self.changed = True
        for symbol in [s for s, until in self.cooldowns.items() if until <= now]:
            del self.cooldowns[symbol]
            self.changed = True

    # -- inside ------------------------------------------------------------------------
    def _mark(self, held: Held) -> dict:
        info = {"symbol": held.symbol, "direction": held.direction, "entry": held.entry,
                "opened_at": held.opened_at, "refs": sorted(str(r) for r in held.refs)}
        mark = self.marks.get(held.key)
        if mark is None:
            mark = self.marks[held.key] = {**info, "size": held.size}
            self.changed = True
        elif any(mark.get(k) != v for k, v in info.items()):
            mark.update(info)
            self.changed = True
        mark["size"] = held.size
        return mark

    def _closing_step(self, held: Held, mark: dict, closing: dict, now: float, tradeable: bool) -> bool:
        if not tradeable:
            del mark["closing"]
            mark["stagnant"] = False
            if held.symbol in self.windows:
                self.windows[held.symbol].clear()
            self.changed = True
            self.log.warning(f"{held.symbol}: the market closed before the {TIMEOUT_REASON} close went through - "
                             f"called off. The timeout looks at it again on fresh prices once it reopens.")
            return False
        if held.size < closing["size"] - 1e-9:
            self.log.info(f"{held.symbol}: partly closed - {held.size:g} of {closing['size']:g} left; closing the "
                          f"rest ({TIMEOUT_REASON}).")
            closing["size"] = held.size
            closing["next_try"] = now
            closing.pop("sent", None)
            self.changed = True
        if "sent" in closing:
            if now - closing["sent"] < CONFIRM_SECONDS:
                return False
            self.log.warning(f"{held.symbol}: still open {now - closing['sent']:.0f}s after {self.broker} accepted "
                             f"the {TIMEOUT_REASON} close - sending it again.")
            closing.pop("sent")
            closing["next_try"] = now
            self.changed = True
        return now >= closing["next_try"]

    def _finish(self, key: str, now: float, price, pnl, refs) -> dict:
        """A TIMEOUT_STAGNANT close is done: log the record, keep the fields for
        the dashboard, start the cooldown."""
        mark = self.marks.pop(key)
        self.noted.pop(key, None)
        self.changed = True
        closing, direction = mark["closing"], mark.get("direction")
        mid = closing.get("mid")
        slippage = None
        if price is not None and mid:
            slippage = (price - mid) if direction == "long" else (mid - price)  # negative = worse than the mid
        held_for = now - mark["opened_at"] if mark.get("opened_at") is not None else None
        fields = {
            "closeReason": TIMEOUT_REASON,
            "exitMid": mid,
            "slippage": None if slippage is None else _round(slippage),
            "durationSeconds": None if held_for is None else round(held_for),
            "stagnancy": {**closing["trigger"], "mode": "enforce", "tries": closing["attempts"] + 1},
        }
        fields = {k: v for k, v in fields.items() if v is not None}
        self._store_exit(mark, fields, refs, now)
        if closing.get("cooldown"):
            self.cooldowns[mark["symbol"]] = now + closing["cooldown"]
        all_refs = sorted({str(r) for r in (*refs, *mark.get("refs", ()))})
        pnl_text = (f"{pnl:+.2f} {self.currency}".rstrip() + " net" if pnl is not None
                    else "not given at the close - the trade record gets it from the broker's history")
        parts = [
            TIMEOUT_REASON, f"trade {self._trade_ids(all_refs, mark['symbol'])}", self.bot, self.broker,
            f"{mark['symbol']} {direction} {mark.get('size', 0):g}",
            f"entry {iso(mark.get('opened_at'))} @ {num(mark.get('entry'))}",
            f"exit {iso(now)} @ {num(price) if price is not None else 'price not given'}",
            f"held {duration_text(held_for)}", f"P/L {pnl_text}",
            (f"slippage {num(slippage)} vs mid {num(mid)}" if slippage is not None else f"last mid {num(mid)}"),
            self._trigger_text(closing["trigger"]),
        ]
        self.log.info(" | ".join(parts))
        return fields

    def _store_exit(self, mark: dict, fields: dict, refs, now: float) -> None:
        keys = {str(r) for r in (*refs, *mark.get("refs", ()))} or {f"{mark.get('symbol')}@{mark.get('opened_at')}"}
        for ref in keys:
            self.exits[ref] = {**fields, "_symbol": mark.get("symbol"), "_openedAt": mark.get("opened_at"),
                               "_entry": mark.get("entry"), "_at": now}
        self.changed = True

    def _note(self, held: Held, skipped: str) -> None:
        """Logs why the check is paused - once, not every pass."""
        if skipped == self.noted.get(held.key, ""):
            return
        self.noted[held.key] = skipped
        if skipped:
            self.log.info(f"{held.symbol}: stagnancy check paused - {skipped}.")

    def _trigger(self, rule: Rule, v: Verdict, now: float) -> dict:
        return {"at": iso(now), "window": rule.window.text, "range": _round(v.range),
                "rangeLimit": _round(v.range_limit), "rangeRule": rule.range.text, "pnl": round(v.pnl, 2),
                "pnlLimit": round(v.pnl_limit, 2), "pnlRule": rule.pnl.text, "mid": _round(v.mid),
                "ageSeconds": round(v.age)}

    def _trigger_text(self, t: dict) -> str:
        return (f"range {num(t['range'])} <= {num(t['rangeLimit'])} ({t['rangeRule']}) over {t['window']} | "
                f"P/L {t['pnl']:+.2f} within +/-{t['pnlLimit']:.2f} ({t['pnlRule']}) | "
                f"{duration_text(t['ageSeconds'])} old")

    def _shadow_line(self, held: Held, v: Verdict, trigger: dict, now: float, dry_run: bool) -> str:
        parts = [
            SHADOW_REASON, f"trade {self._trade_ids(held.refs, held.symbol)}", self.bot, self.broker,
            f"{held.symbol} {held.direction} {held.size:g}", f"entry {iso(held.opened_at)} @ {num(held.entry)}",
            f"would exit {iso(now)} near mid {num(v.mid)}", f"held {duration_text(v.age)}",
            f"P/L {v.pnl:+.2f} {self.currency}".rstrip() + " net, unrealised", self._trigger_text(trigger),
            "DRY RUN: would close it (enforce), but sends no orders" if dry_run else "left open (shadow mode)",
        ]
        return " | ".join(parts)

    @staticmethod
    def _trade_ids(refs, symbol: str) -> str:
        return ",".join(str(r) for r in refs) or symbol


def start_watch(bot_type: str, broker: str, bot: str, broker_name: str, log, *, bar_seconds: float,
                loop_seconds: float, currency: str = "") -> Watch:
    """The timeout for one of the original EMA bots or momentum scanners,
    with its startup line logged. A setting it can't use stops the bot."""
    try:
        book = RuleBook(bot_type, broker, bar_seconds=bar_seconds)
    except ConfigError as e:
        log.error(f"Can't start: {e}.")
        raise SystemExit(1) from None
    log.info(book.describe(currency))
    return Watch(book, bot, broker_name, log, currency=currency, gap=max(3 * loop_seconds, 60))


# ---------------------------------------------------------------------------
# FORMATTING
# ---------------------------------------------------------------------------
def iso(ts) -> str:
    """Unix seconds as ISO 8601 UTC, e.g. 2026-10-01T09:15:02Z."""
    if ts is None:
        return "?"
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def duration_text(seconds) -> str:
    if seconds is None:
        return "?"
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def num(x) -> str:
    """A price or price distance, without scientific notation."""
    if x is None:
        return "?"
    text = f"{x:.6g}"
    return f"{x:.10f}".rstrip("0").rstrip(".") if "e" in text else text


def _round(x):
    return None if x is None else float(f"{x:.8g}")
