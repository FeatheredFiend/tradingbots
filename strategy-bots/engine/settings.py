"""
Every bot's settings, read from environment variables - the ones the
TradingBots launcher's Settings tab edits. Empty or unset means the
default below.

Two kinds:
- Strategy settings, shared by that strategy on every broker (like the
  scanners' STREAK_LENGTH): BREAKOUT_*, REVERSION_*, TREND_*, SCALPER_*,
  SURGE_* (the opening surge scanner and its followers).
- Per-bot settings, one set per broker and strategy:
  <BROKER>_<BREAKOUT|REVERSION|TREND|SCALPER>_MARKETS / _BUDGET / _MAX_POSITIONS,
  plus _ACCOUNT_ID on OANDA and Capital.com (falling back to the broker's
  OANDA_ACCOUNT_ID / CAPITAL_ACCOUNT_ID). The surge followers have no
  market list - they trade whatever the scanner finds - but a
  <BROKER>_SURGE_SYMBOL naming how the broker writes a US share's ticker.

STRATEGY_DRY_RUN=1 makes every strategy bot log the trades it would make
without sending them.
"""

import os
from dataclasses import dataclass, field

BROKER_NAMES = {"oanda": "OANDA", "pepperstone": "Pepperstone", "capital": "Capital.com", "ig": "IG", "alpaca": "Alpaca"}

STRATEGY_NAMES = {
    "session-breakout": "Forex session breakout",
    "index-reversion": "Index mean reversion",
    "commodity-trend": "Commodity trend (4H/15M)",
    "scalper": "Tick scalper (HFT-style)",
    "surge-follower": "Opening surge follower",
}
STRATEGY_PREFIX = {"session-breakout": "BREAKOUT", "index-reversion": "REVERSION", "commodity-trend": "TREND",
                   "scalper": "SCALPER", "surge-follower": "SURGE"}

# The surge followers trade the US shares the surge scanner finds, on the
# brokers that offer them: how each writes a ticker ("{}" is the ticker).
# IG doesn't give shares' prices over its API and OANDA has no shares.
SURGE_SYMBOLS = {"alpaca": "{}", "capital": "{}", "pepperstone": "{}.US"}

# Default markets, in each broker's own names. IG's are search terms
# ("term" or "term:EPIC"), resolved at startup like the IG scanner's pool.
# Index markets can carry "@us", "@uk", "@eu" or "@jp" to name their cash
# session when it can't be told from the name.
DEFAULT_MARKETS = {
    ("oanda", "session-breakout"): "GBP_USD,EUR_USD,GBP_JPY,EUR_JPY",
    ("oanda", "index-reversion"): "SPX500_USD,UK100_GBP,DE30_EUR",
    ("oanda", "commodity-trend"): "XAU_USD,BCO_USD",
    ("pepperstone", "session-breakout"): "GBPUSD,EURUSD,GBPJPY,EURJPY",
    ("pepperstone", "index-reversion"): "US500,UK100,GER40",
    ("pepperstone", "commodity-trend"): "XAUUSD,SpotBrent",
    ("capital", "session-breakout"): "GBPUSD,EURUSD,GBPJPY,EURJPY",
    ("capital", "index-reversion"): "US500,UK100,DE40",
    ("capital", "commodity-trend"): "GOLD,OIL_BRENT",
    ("ig", "session-breakout"): "GBP/USD,EUR/USD,GBP/JPY,EUR/JPY",
    ("ig", "index-reversion"): "US 500,FTSE 100,Germany 40",
    ("ig", "commodity-trend"): "Spot Gold,Oil - Brent Crude",
    # Alpaca has no forex or CFDs: US-listed funds stand in for the index
    # and the commodities, bought only (no fractional short selling).
    ("alpaca", "index-reversion"): "SPY,QQQ,DIA,IWM",
    ("alpaca", "commodity-trend"): "GLD,SLV,USO",
    # The scalper wants the tightest spreads: FX majors and the S&P 500. IG's
    # are named with their epics, so resolving them costs no searches.
    ("oanda", "scalper"): "EUR_USD,GBP_USD,USD_JPY,SPX500_USD",
    ("pepperstone", "scalper"): "EURUSD,GBPUSD,USDJPY,US500",
    ("capital", "scalper"): "EURUSD,GBPUSD,USDJPY,US500",
    ("ig", "scalper"): "EUR/USD:CS.D.EURUSD.MINI.IP,US 500:IX.D.SPTRD.IFS.IP",
    ("alpaca", "scalper"): "SPY,QQQ",
}

# The budget each bot treats as its whole account, in the account's
# currency - big enough that every default market's smallest trade fits.
# Not used on IG, whose bots always trade each market's minimum size.
DEFAULT_BUDGET = {
    ("oanda", "session-breakout"): 100, ("oanda", "index-reversion"): 1000, ("oanda", "commodity-trend"): 300,
    ("pepperstone", "session-breakout"): 1000, ("pepperstone", "index-reversion"): 2000,
    ("pepperstone", "commodity-trend"): 2000,
    ("capital", "session-breakout"): 100, ("capital", "index-reversion"): 200, ("capital", "commodity-trend"): 100,
    ("ig", "session-breakout"): 10000, ("ig", "index-reversion"): 10000, ("ig", "commodity-trend"): 10000,
    ("alpaca", "index-reversion"): 100, ("alpaca", "commodity-trend"): 100,
    ("oanda", "scalper"): 100, ("pepperstone", "scalper"): 1000, ("capital", "scalper"): 100,
    ("ig", "scalper"): 10000, ("alpaca", "scalper"): 100,
    ("alpaca", "surge-follower"): 100, ("capital", "surge-follower"): 200, ("pepperstone", "surge-follower"): 1000,
}
DEFAULT_MAX_POSITIONS = {"session-breakout": 2, "index-reversion": 2, "commodity-trend": 2, "scalper": 2,
                         "surge-follower": 3}

# MetaTrader 5 tags each bot's positions with its own number; the
# Pepperstone scanner and EMA bot use 928001 and 928002.
MT5_MAGIC = {"session-breakout": 928003, "index-reversion": 928004, "commodity-trend": 928005, "scalper": 928006,
             "surge-follower": 928007}

TIMEFRAMES = {"M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400}


class SettingsError(Exception):
    """A setting that can't be used; the message names it."""


def _raw(name: str) -> str:
    return os.environ.get(name, "").strip()


def number(name: str, default: float, minimum: float = None, maximum: float = None, whole: bool = False):
    text = _raw(name)
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        raise SettingsError(f"{name}={text!r} isn't a number") from None
    if whole:
        if value != int(value):
            raise SettingsError(f"{name}={text!r} must be a whole number")
        value = int(value)
    if minimum is not None and value < minimum:
        raise SettingsError(f"{name}={text!r} must be at least {minimum:g}")
    if maximum is not None and value > maximum:
        raise SettingsError(f"{name}={text!r} must be at most {maximum:g}")
    return value


def clock_time(name: str, default: str) -> str:
    text = _raw(name) or default
    try:
        hours, minutes = text.split(":")
        if not (0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59):
            raise ValueError
    except ValueError:
        raise SettingsError(f"{name}={text!r} must be a time like 07:00") from None
    return f"{int(hours):02d}:{int(minutes):02d}"


def timeframe(name: str, default: str) -> str:
    text = (_raw(name) or default).upper()
    if text not in TIMEFRAMES:
        raise SettingsError(f"{name}={text!r} must be one of {', '.join(TIMEFRAMES)}")
    return text


def flag(name: str, default: bool) -> bool:
    text = _raw(name).lower()
    if not text:
        return default
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise SettingsError(f"{name}={text!r} must be 1 (on) or 0 (off)")


# ---------------------------------------------------------------------------
# Strategy settings - shared by the strategy on every broker
# ---------------------------------------------------------------------------
def strategy_params(strategy: str) -> dict:
    """The strategy's settings with defaults; see strategies.py for what each does."""
    if strategy == "session-breakout":
        p = "BREAKOUT_"
        return {
            "timeframe": timeframe(p + "TIMEFRAME", "M15"),
            "range_start": clock_time(p + "RANGE_START", "07:00"),
            "range_end": clock_time(p + "RANGE_END", "13:00"),
            "entry_end": clock_time(p + "ENTRY_END", "16:00"),
            "flat_time": clock_time(p + "FLAT_TIME", "20:00"),
            "min_range_percent": number(p + "MIN_RANGE_PERCENT", 0.15, minimum=0),
            "max_range_percent": number(p + "MAX_RANGE_PERCENT", 1.0, minimum=0),
            "buffer_atr": number(p + "BUFFER_ATR", 0.2, minimum=0),
            "max_extension": number(p + "MAX_EXTENSION", 0.5, minimum=0),
            "trend_ema": number(p + "TREND_EMA", 50, minimum=0, whole=True),
            "stop_range_fraction": number(p + "STOP_RANGE_FRACTION", 0.5, minimum=0, maximum=1),
            "reward_risk": number(p + "REWARD_RISK", 1.5, minimum=0.1),
            "max_trades_per_day": number(p + "MAX_TRADES_PER_DAY", 1, minimum=1, whole=True),
            **_risk(p, risk=1.0),
        }
    if strategy == "index-reversion":
        p = "REVERSION_"
        return {
            "timeframe": timeframe(p + "TIMEFRAME", "M15"),
            "band_stdev": number(p + "BAND_STDEV", 2.0, minimum=0.1),
            "rsi_period": number(p + "RSI_PERIOD", 14, minimum=2, whole=True),
            "rsi_oversold": number(p + "RSI_OVERSOLD", 30, minimum=0, maximum=50),
            "rsi_overbought": number(p + "RSI_OVERBOUGHT", 70, minimum=50, maximum=100),
            "max_adx": number(p + "MAX_ADX", 25, minimum=0),
            "stop_atr": number(p + "STOP_ATR", 1.5, minimum=0.1),
            "min_reward_risk": number(p + "MIN_REWARD_RISK", 1.0, minimum=0),
            "skip_open_minutes": number(p + "SKIP_OPEN_MINUTES", 60, minimum=0, whole=True),
            "last_entry_minutes": number(p + "LAST_ENTRY_MINUTES", 60, minimum=0, whole=True),
            "flat_minutes": number(p + "FLAT_MINUTES", 15, minimum=1, whole=True),
            "max_hold_bars": number(p + "MAX_HOLD_BARS", 8, minimum=0, whole=True),
            "max_trades_per_day": number(p + "MAX_TRADES_PER_DAY", 2, minimum=1, whole=True),
            **_risk(p, risk=0.5),
        }
    if strategy == "commodity-trend":
        p = "TREND_"
        params = {
            "timeframe": timeframe(p + "TIMEFRAME", "M15"),
            "higher_timeframe": timeframe(p + "HIGHER_TIMEFRAME", "H4"),
            "htf_fast_ema": number(p + "HTF_FAST_EMA", 50, minimum=2, whole=True),
            "htf_slow_ema": number(p + "HTF_SLOW_EMA", 200, minimum=3, whole=True),
            "min_adx": number(p + "MIN_ADX", 20, minimum=0),
            "fast_ema": number(p + "FAST_EMA", 9, minimum=2, whole=True),
            "slow_ema": number(p + "SLOW_EMA", 21, minimum=3, whole=True),
            "stop_atr": number(p + "STOP_ATR", 2.0, minimum=0.1),
            "trail_atr": number(p + "TRAIL_ATR", 3.0, minimum=0),
            "reward_risk": number(p + "REWARD_RISK", 3.0, minimum=0),
            "max_hold_days": number(p + "MAX_HOLD_DAYS", 10, minimum=0),
            "weekend_flat": flag(p + "WEEKEND_FLAT", True),
            "max_swap_percent": number(p + "MAX_SWAP_PERCENT", 0.05, minimum=0),
            "max_trades_per_day": number(p + "MAX_TRADES_PER_DAY", 2, minimum=1, whole=True),
            **_risk(p, risk=1.0),
        }
        if params["htf_fast_ema"] >= params["htf_slow_ema"] or params["fast_ema"] >= params["slow_ema"]:
            raise SettingsError("each fast EMA (TREND_HTF_FAST_EMA, TREND_FAST_EMA) must be shorter than its slow EMA")
        if TIMEFRAMES[params["higher_timeframe"]] <= TIMEFRAMES[params["timeframe"]]:
            raise SettingsError("TREND_HIGHER_TIMEFRAME must be longer than TREND_TIMEFRAME")
        return params
    if strategy == "scalper":
        p = "SCALPER_"
        params = {
            "poll_seconds": number(p + "POLL_SECONDS", 2, minimum=1, maximum=60),
            "mode": choice(p + "MODE", "momentum", ("momentum", "reversion")),
            "window_seconds": number(p + "WINDOW_SECONDS", 60, minimum=5, maximum=3600, whole=True),
            "trigger_spreads": number(p + "TRIGGER_SPREADS", 4.0, minimum=0.5),
            "stop_spreads": number(p + "STOP_SPREADS", 3.0, minimum=0.5),
            "take_profit_spreads": number(p + "TAKE_PROFIT_SPREADS", 3.0, minimum=0.5),
            "max_spread_ratio": number(p + "MAX_SPREAD_RATIO", 1.5, minimum=1),
            "max_hold_seconds": number(p + "MAX_HOLD_SECONDS", 300, minimum=10, whole=True),
            "cooldown_seconds": number(p + "COOLDOWN_SECONDS", 60, minimum=0, whole=True),
            "session_start": clock_time(p + "SESSION_START", "07:00"),
            "session_end": clock_time(p + "SESSION_END", "21:00"),
            "max_trades_per_day": number(p + "MAX_TRADES_PER_DAY", 30, minimum=1, whole=True),
            # The stop is only a few spreads away, so the spread is always a big
            # share of it; MAX_SPREAD_RATIO is the scalper's real spread filter.
            **_risk(p, risk=0.5, spread=60),
        }
        if params["session_start"] >= params["session_end"]:
            raise SettingsError("SCALPER_SESSION_START must be before SCALPER_SESSION_END (London time)")
        return params
    if strategy == "surge-follower":
        p = "SURGE_"
        return {
            "reward_risk": number(p + "REWARD_RISK", 2.0, minimum=0.1),
            "max_hold_minutes": number(p + "MAX_HOLD_MINUTES", 15, minimum=1, maximum=360, whole=True),
            "max_signal_age": number(p + "MAX_SIGNAL_AGE", 20, minimum=2, maximum=600, whole=True),
            # Pepperstone's US share CFDs start quoting at 09:31 New York, a minute after the open.
            "first_price_wait": number(p + "FIRST_PRICE_WAIT", 90, minimum=0, maximum=600, whole=True),
            "max_trades_per_day": number(p + "MAX_TRADES_PER_DAY", 1, minimum=1, whole=True),
            # The stop is only the surge's own size away, and spreads are wide
            # at the open, so this is looser than the bar strategies' 10%.
            **_risk(p, risk=0.5, spread=25),
        }
    raise SettingsError(f"unknown strategy {strategy!r}")


def surge_scanner_params() -> dict:
    """The opening surge scanner's settings (engine/surge_scanner.py)."""
    p = "SURGE_"
    return {
        "poll_seconds": number(p + "POLL_SECONDS", 5, minimum=2, maximum=60),
        "confirm_polls": number(p + "CONFIRM_POLLS", 3, minimum=1, maximum=10, whole=True),
        "jump_percent": number(p + "JUMP_PERCENT", 0.2, minimum=0.01, maximum=20),
        "watch_minutes": number(p + "WATCH_MINUTES", 15, minimum=1, maximum=390, whole=True),
        "min_price": number(p + "MIN_PRICE", 5, minimum=0),
        "min_dollar_volume": number(p + "MIN_DOLLAR_VOLUME", 20, minimum=0),
        "max_shares": number(p + "MAX_SHARES", 1500, minimum=1, maximum=12000, whole=True),
        "feed": choice(p + "FEED", "iex", ("iex", "sip")),
    }


def choice(name: str, default: str, options: tuple) -> str:
    text = (_raw(name) or default).lower()
    if text not in options:
        raise SettingsError(f"{name}={text!r} must be one of {', '.join(options)}")
    return text


def _risk(prefix: str, risk: float, spread: float = 10) -> dict:
    return {
        "risk_percent": number(prefix + "RISK_PERCENT", risk, minimum=0.01, maximum=10),
        "max_leverage": number(prefix + "MAX_LEVERAGE", 5, minimum=0.1, maximum=30),
        "max_spread_percent": number(prefix + "MAX_SPREAD_PERCENT", spread, minimum=0, maximum=100),
    }


# ---------------------------------------------------------------------------
# Per-bot settings
# ---------------------------------------------------------------------------
@dataclass
class BotSettings:
    broker: str
    strategy: str
    slug: str
    name: str
    markets: list
    budget: float
    max_positions: int
    account_id: str
    dry_run: bool
    params: dict = field(default_factory=dict)
    symbol_format: str = ""      # surge followers: how the broker writes a US ticker, "{}" being the ticker

    @property
    def env_prefix(self) -> str:
        """e.g. OANDA_BREAKOUT_ - what this bot's own settings start with."""
        return f"{self.broker.upper()}_{STRATEGY_PREFIX[self.strategy]}_"


def bot_settings(broker: str, strategy: str) -> BotSettings:
    prefix = f"{broker.upper()}_{STRATEGY_PREFIX.get(strategy, '')}_"
    symbol_format = ""
    if strategy == "surge-follower":
        if broker not in SURGE_SYMBOLS:
            raise SettingsError(f"there is no {STRATEGY_NAMES[strategy]} bot for {BROKER_NAMES.get(broker, broker)} - "
                                f"it doesn't offer US shares over its API")
        markets = []  # whatever the scanner signals
        symbol_format = _raw(prefix + "SYMBOL") or SURGE_SYMBOLS[broker]
        if "{}" not in symbol_format:
            raise SettingsError(f"{prefix}SYMBOL={symbol_format!r} must contain {{}} where the ticker goes, e.g. {{}}.US")
    elif (broker, strategy) not in DEFAULT_MARKETS:
        raise SettingsError(f"there is no {STRATEGY_NAMES.get(strategy, strategy)} bot for {BROKER_NAMES.get(broker, broker)}")
    else:
        markets = [m.strip() for m in (_raw(prefix + "MARKETS") or DEFAULT_MARKETS[(broker, strategy)]).split(",")
                   if m.strip()]
        if not markets:
            raise SettingsError(f"{prefix}MARKETS names no markets")
    account_id = ""
    if broker in ("oanda", "capital"):
        account_id = _raw(prefix + "ACCOUNT_ID") or _raw(f"{broker.upper()}_ACCOUNT_ID")
    return BotSettings(
        broker=broker,
        strategy=strategy,
        slug=f"{broker}-{strategy}",
        name=f"{BROKER_NAMES[broker]} {STRATEGY_NAMES[strategy].split(' (')[0].lower()}",
        markets=markets,
        budget=number(prefix + "BUDGET", DEFAULT_BUDGET[(broker, strategy)], minimum=1),
        max_positions=number(prefix + "MAX_POSITIONS", DEFAULT_MAX_POSITIONS[strategy], minimum=1, whole=True),
        account_id=account_id,
        dry_run=flag("STRATEGY_DRY_RUN", False),
        params=strategy_params(strategy),
        symbol_format=symbol_format,
    )
