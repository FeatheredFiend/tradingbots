"""
Trading Bots launcher - a small Windows app for the bots in this repo.

- Bots tab: which bots are running (including ones started by hand), with
  Start / Stop / Restart per bot and for all of them. Starting goes through
  launcher/start_bot.bat, so each bot gets its own console window exactly
  as if started by hand. Stopping sends the bot Ctrl+C, so it shuts down
  cleanly and tells the dashboard it stopped.
- Settings tab: every environment variable the bots read (API keys,
  budgets, market lists, stop-loss / take-profit, dashboard), saved as
  Windows user environment variables - the same place set by
  [Environment]::SetEnvironmentVariable(..., "User").
- Paths tab: where the repo and each broker's Python environment are.

Build the .exe with launcher/build.bat. Windows only.
"""

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
import winreg
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import psutil

import bot_clashes

APP_NAME = "Trading Bots"
CONFIG_PATH = Path(os.environ.get("APPDATA", Path.home())) / "TradingBots" / "launcher.json"
DEFAULT_REPO = r"\\wsl.localhost\Ubuntu\home\martyn\projects\tradingbots"
REFRESH_MS = 2500
STOP_TIMEOUT_SECONDS = 20


# ---------------------------------------------------------------------------
# BOTS
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Bot:
    key: str          # also its name on the dashboard and in start_bot.bat
    name: str
    broker: str       # alpaca / oanda / pepperstone / capital / ig - picks the Python environment
    strategy: str
    script: str       # relative to the repo
    family: str       # which kind of bot, for the Bots tab's filter - a FAMILIES key

    @property
    def script_name(self) -> str:
        return self.script.rsplit("\\", 1)[-1]


BROKERS = {"oanda": "OANDA", "pepperstone": "Pepperstone", "capital": "Capital.com", "alpaca": "Alpaca", "ig": "IG"}
FAMILIES = {
    "momentum": "Momentum scanner",
    "ema": "EMA crossover",
    "session-breakout": "Forex session breakout",
    "index-reversion": "Index mean reversion",
    "commodity-trend": "Commodity trend (4H/15M)",
    "scalper": "Tick scalper (HFT-style)",
    "surge": "Opening surge (scanner + followers)",
    "slow-trend": "Slow trend (daily, commodities)",
    "etf-rotation": "Monthly ETF rotation",
}
# Portfolio bots (strategy-bots/engine/rebalancer.py) hold all their markets
# at once, so they have no max positions.
PORTFOLIO_FAMILIES = ("slow-trend", "etf-rotation")
# The strategy bots in strategy-bots/: each of these on every broker, except
# forex on Alpaca, which has none. (key, env prefix, default markets per broker).
STRATEGY_BOTS = {
    "session-breakout": ("BREAKOUT", {
        "oanda": "GBP_USD, EUR_USD, GBP_JPY, EUR_JPY", "pepperstone": "GBPUSD, EURUSD, GBPJPY, EURJPY",
        "capital": "GBPUSD, EURUSD, GBPJPY, EURJPY", "ig": "GBP/USD, EUR/USD, GBP/JPY, EUR/JPY"}),
    "index-reversion": ("REVERSION", {
        "oanda": "SPX500_USD, UK100_GBP, DE30_EUR", "pepperstone": "US500, UK100, GER40",
        "capital": "US500, UK100, DE40", "ig": "US 500, FTSE 100, Germany 40", "alpaca": "SPY, QQQ, DIA, IWM"}),
    "commodity-trend": ("TREND", {
        "oanda": "XAU_USD, BCO_USD", "pepperstone": "XAUUSD, SpotBrent", "capital": "GOLD, OIL_BRENT",
        "ig": "Spot Gold, Oil - Brent Crude", "alpaca": "GLD, SLV, USO"}),
    "scalper": ("SCALPER", {
        "oanda": "EUR_USD, GBP_USD, USD_JPY, SPX500_USD", "pepperstone": "EURUSD, GBPUSD, USDJPY, US500",
        "capital": "EURUSD, GBPUSD, USDJPY, US500", "ig": "EUR/USD:CS.D.EURUSD.MINI.IP, US 500:IX.D.SPTRD.IFS.IP",
        "alpaca": "SPY, QQQ"}),
    "slow-trend": ("SLOW_TREND", {
        "oanda": "BCO_USD, WTICO_USD, NATGAS_USD, XAU_USD, XAG_USD, XCU_USD, CORN_USD, WHEAT_USD, SOYBN_USD, SUGAR_USD"}),
    "etf-rotation": ("ROTATION", {"alpaca": "VOO, EFA, IEF, DBC, VNQ"}),
}
# Each strategy bot's own budget, when <BROKER>_<PREFIX>_BUDGET is empty (IG bots have none).
STRATEGY_BOT_BUDGETS = {
    ("oanda", "session-breakout"): 100, ("oanda", "index-reversion"): 1000, ("oanda", "commodity-trend"): 300,
    ("pepperstone", "session-breakout"): 1000, ("pepperstone", "index-reversion"): 2000,
    ("pepperstone", "commodity-trend"): 2000, ("capital", "session-breakout"): 100,
    ("capital", "index-reversion"): 200, ("capital", "commodity-trend"): 100,
    ("alpaca", "index-reversion"): 100, ("alpaca", "commodity-trend"): 100,
    ("oanda", "scalper"): 100, ("pepperstone", "scalper"): 1000, ("capital", "scalper"): 100, ("alpaca", "scalper"): 100,
    ("oanda", "slow-trend"): 5000, ("alpaca", "etf-rotation"): 100,
}

CLASSIC_BOTS = [
    Bot("oanda-momentum-scanner", "OANDA momentum scanner", "oanda", "Momentum streak",
        r"oanda-momentum-scanner-bot\oanda_momentum_scanner_bot.py", "momentum"),
    Bot("oanda-ema-bot", "OANDA EMA crossover", "oanda", "EMA 9/21 crossover", r"oanda-ema-bot\oanda_ema_bot.py", "ema"),
    Bot("pepperstone-momentum-scanner", "Pepperstone momentum scanner", "pepperstone", "Momentum streak",
        r"pepperstone-momentum-scanner-bot\pepperstone_momentum_scanner_bot.py", "momentum"),
    Bot("pepperstone-ema-bot", "Pepperstone EMA crossover", "pepperstone", "EMA 9/21 crossover",
        r"pepperstone-ema-bot\pepperstone_ema_bot.py", "ema"),
    Bot("capital-momentum-scanner", "Capital.com momentum scanner", "capital", "Momentum streak",
        r"capital-momentum-scanner-bot\capital_momentum_scanner_bot.py", "momentum"),
    Bot("capital-ema-bot", "Capital.com EMA crossover", "capital", "EMA 9/21 crossover",
        r"capital-ema-bot\capital_ema_bot.py", "ema"),
    Bot("alpaca-momentum-scanner", "Alpaca momentum scanner", "alpaca", "Momentum streak (buys only)",
        r"alpaca-momentum-scanner-bot\alpaca_momentum_scanner_bot.py", "momentum"),
    Bot("alpaca-ema-bot", "Alpaca EMA crossover", "alpaca", "EMA 9/21 crossover", r"alpaca-ema-bot\alpaca_ema_bot.py",
        "ema"),
    Bot("ig-momentum-scanner", "IG momentum scanner", "ig", "Momentum streak",
        r"ig-momentum-scanner-bot\ig_momentum_scanner_bot.py", "momentum"),
    Bot("ig-ema-bot", "IG EMA crossover", "ig", "EMA 9/21 crossover", r"ig-cfd-ema-bot\ig_cfd_ema_bot.py", "ema"),
]


def _strategy_bot(broker: str, family: str) -> Bot:
    label = FAMILIES[family]
    short = label.split(" (")[0]
    return Bot(f"{broker}-{family}", f"{BROKERS[broker]} {short[0].lower()}{short[1:]}", broker, label,
               rf"strategy-bots\{broker}_{family.replace('-', '_')}_bot.py", family)


# The opening surge: one scanner (on Alpaca's market data, no trades) and a
# follower on each broker with US shares - (budget default, how it writes a ticker).
SURGE_FOLLOWERS = {"alpaca": (100, "{}"), "capital": (200, "{}"), "pepperstone": (1000, "{}.US")}
SURGE_BOTS = [Bot("surge-scanner", "Surge scanner", "alpaca", "Opening surge scanner (finds, doesn't trade)",
                  r"strategy-bots\surge_scanner_bot.py", "surge")] + [
    Bot(f"{broker}-surge-follower", f"{BROKERS[broker]} surge follower", broker, "Opening surge follower",
        rf"strategy-bots\{broker}_surge_follower_bot.py", "surge") for broker in SURGE_FOLLOWERS]

# Every bot, listed by name on the Bots tab.
BOTS = sorted(
    CLASSIC_BOTS
    + [_strategy_bot(broker, family) for broker in BROKERS
       for family, (_, markets) in STRATEGY_BOTS.items() if broker in markets]
    + SURGE_BOTS,
    key=lambda bot: bot.name.lower())


def default_config() -> dict:
    home = Path.home()
    return {
        "repo": DEFAULT_REPO,
        "venvs": {
            "oanda": str(home / "ig-bot-env"),  # OANDA only needs requests, which ig-bot-env has
            "pepperstone": str(home / "pepperstone-bot-env"),
            "capital": str(home / "ig-bot-env"),  # Capital.com only needs requests too
            "alpaca": str(home / "alpaca-bot-env"),
            "ig": str(home / "ig-bot-env"),
        },
        # What "Start all" starts. Not every bot at once: an EMA bot and a scanner on
        # the same OANDA or Alpaca account would close each other's trades.
        "start_all": [b.key for b in BOTS if "momentum" in b.key],
        "filter": {"broker": "", "family": "", "running": False},  # the Bots tab's filter ("" = all)
    }


def load_config() -> dict:
    config = default_config()
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        config["repo"] = saved.get("repo") or config["repo"]
        config["venvs"].update({k: v for k, v in (saved.get("venvs") or {}).items() if v})
        if isinstance(saved.get("start_all"), list):
            config["start_all"] = saved["start_all"]
        if isinstance(saved.get("filter"), dict):
            config["filter"].update({k: v for k, v in saved["filter"].items() if k in config["filter"]})
    except (OSError, ValueError):
        pass
    return config


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


@dataclass
class BotProcess:
    pids: list          # every python process of the bot (a venv's python.exe starts a second one)
    instances: int      # separately started copies - more than 1 is a mistake worth showing
    started: float      # earliest start time


def find_running_bots() -> dict:
    """{bot key: BotProcess} for every bot with a python process whose
    command line names its script - however it was started."""
    by_script = {bot.script_name.lower(): bot for bot in BOTS}
    found = {}
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline", "create_time"]):
        info = proc.info
        if not (info["name"] or "").lower().startswith("python"):
            continue
        for arg in info["cmdline"] or ():
            bot = by_script.get(os.path.basename(arg).lower())
            if bot:
                found.setdefault(bot.key, []).append(info)
                break

    running = {}
    for key, procs in found.items():
        pids = {p["pid"] for p in procs}
        roots = [p for p in procs if p["ppid"] not in pids]
        running[key] = BotProcess(sorted(pids), max(1, len(roots)), min(p["create_time"] for p in procs))
    return running


def start_bot(bot: Bot, config: dict) -> None:
    """Runs start_bot.bat, which opens the bot in its own console window."""
    batch = Path(config["repo"]) / "launcher" / "start_bot.bat"
    if not batch.exists():
        raise RuntimeError(f"Can't find {batch} - check the repo folder on the Paths tab.")
    python = Path(config["venvs"][bot.broker]) / "Scripts" / "python.exe"
    if not python.exists():
        raise RuntimeError(f"No Python at {python} - check the {BROKERS[bot.broker]} environment on the Paths tab.")
    # Output to a file, not a pipe: the bot's window inherits the batch's
    # handles, so a pipe wouldn't reach end-of-file until the bot exits.
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            ["cmd.exe", "/c", str(batch), bot.key],
            env=bot_environment(config), cwd=str(Path.home()), stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30,
        )
        output.seek(0)
        message = output.read().decode(errors="replace").strip()
    if result.returncode != 0:
        raise RuntimeError(message or f"start_bot.bat exited with {result.returncode}")


def bot_environment(config: dict) -> dict:
    """This app's environment with the bot settings re-read from the
    registry - they may have changed since the app started - plus the
    Python environment paths start_bot.bat looks for."""
    env = dict(os.environ)
    for name in SETTING_NAMES:
        env.pop(name, None)
    env.update({name: value for name, value in read_user_env().items() if name.upper() in SETTING_NAMES})
    for broker, path in config["venvs"].items():
        env[f"TRADINGBOTS_{broker.upper()}_ENV"] = path
    return env


def stop_bot(bot: Bot, process: BotProcess) -> str:
    """Ctrl+C first, so the bot says goodbye to the dashboard; a hard kill
    only if it's still running STOP_TIMEOUT_SECONDS later. Returns how it went."""
    send_ctrl_c(process.pids)
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not any(psutil.pid_exists(pid) for pid in process.pids):
            return "stopped"
        time.sleep(0.5)

    for pid in process.pids:
        try:
            psutil.Process(pid).kill()
        except psutil.Error:
            pass
    report_stopped_to_dashboard(bot)
    return "force-stopped (it didn't answer Ctrl+C)"


def send_ctrl_c(pids) -> None:
    """Press Ctrl+C in the bot's console window. A GUI app has no console of
    its own, so borrow the bot's for a moment - with Ctrl+C ignored here,
    or this app would be the one to quit."""
    kernel32 = ctypes.windll.kernel32
    with _console_lock:
        for pid in pids:
            kernel32.FreeConsole()
            if kernel32.AttachConsole(pid):
                kernel32.SetConsoleCtrlHandler(None, True)
                kernel32.GenerateConsoleCtrlEvent(0, 0)  # CTRL_C_EVENT to every process on that console
                time.sleep(0.3)
                kernel32.FreeConsole()
                return


_console_lock = threading.Lock()


def report_stopped_to_dashboard(bot: Bot) -> None:
    """A killed bot can't say goodbye; say it for it, so the dashboard
    shows it stopped rather than silent."""
    env = read_user_env()
    url, token = env.get("DASHBOARD_URL", "").rstrip("/"), env.get("DASHBOARD_TOKEN", "")
    if not url or not token:
        return
    report = {"bot": {"slug": bot.key}, "status": "stopped",
              "logs": [{"level": "WARNING", "message": "Stopped by the launcher (it didn't answer Ctrl+C)."}]}
    request = urllib.request.Request(url + "/api/ingest", data=json.dumps(report).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Dashboard-Token": token,
                                              "User-Agent": "tradingbots-launcher/1"})
    try:
        urllib.request.urlopen(request, timeout=10).read()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# SETTINGS - Windows user environment variables
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Setting:
    name: str
    label: str
    hint: str = ""
    secret: bool = False
    number: bool = False


@dataclass(frozen=True)
class Sub:
    """A sub-heading inside a settings section."""
    title: str


def _strategy_bot_settings(broker: str) -> list:
    """Each strategy bot's own settings, for its broker's section."""
    rows = []
    for family, (prefix, markets) in STRATEGY_BOTS.items():
        if broker not in markets:
            continue
        p = f"{broker.upper()}_{prefix}_"
        rows.append(Sub(f"{FAMILIES[family]} bot"))
        rows.append(Setting(p + "MARKETS", "Markets", f"Comma-separated - empty for {markets[broker]}"))
        if broker != "ig":  # IG's always trade the minimum size
            rows.append(Setting(p + "BUDGET", "Budget", "Its own money, in the account's currency - default "
                                f"{STRATEGY_BOT_BUDGETS[(broker, family)]:,}", number=True))
        if family not in PORTFOLIO_FAMILIES:
            rows.append(Setting(p + "MAX_POSITIONS", "Max positions", "Open at once - default 2", number=True))
        if broker in ("oanda", "capital"):
            rows.append(Setting(p + "ACCOUNT_ID", "Account ID", f"Its own sub-account - empty for the "
                                                                f"{broker.upper()}_ACCOUNT_ID one"))
    return rows


def _surge_follower_settings(broker: str) -> list:
    """The broker's surge follower's own settings, for its broker's section."""
    budget, symbol = SURGE_FOLLOWERS[broker]
    p = f"{broker.upper()}_SURGE_"
    rows = [
        Sub("Opening surge follower"),
        Setting(p + "BUDGET", "Budget", f"Its own money, in the account's currency - default {budget:,}", number=True),
        Setting(p + "MAX_POSITIONS", "Max positions", "Open at once - default 3", number=True),
        Setting(p + "SYMBOL", "Share names", f"How {BROKERS[broker]} writes a US ticker, {{}} being the ticker - "
                                             f"default {symbol}"),
    ]
    if broker == "capital":
        rows.append(Setting(p + "ACCOUNT_ID", "Account ID", "Its own sub-account - empty for the CAPITAL_ACCOUNT_ID one"))
    return rows


def _risk_settings(prefix: str, risk: str, spread: str = "10") -> list:
    return [
        Sub("Risk"),
        Setting(prefix + "RISK_PERCENT", "Risk per trade %", f"Of the bot's budget, lost if the stop-loss is hit - default {risk}",
                number=True),
        Setting(prefix + "MAX_LEVERAGE", "Max leverage", "Open trades worth at most budget x this - default 5", number=True),
        Setting(prefix + "MAX_SPREAD_PERCENT", "Max spread", f"As a % of the stop distance - default {spread}", number=True),
    ]


# The stagnancy timeout's defaults per bot type, (min age, window, range, P/L),
# as in shared/stagnancy.py TYPES (strategy-bots/tests/test_stagnancy.py checks).
STAGNANCY_DEFAULTS = {
    "SCALPER": ("90s", "60s", "0.01%", "0.25R"),
    "SURGE": ("4m", "3m", "0.15%", "0.25R"),
    "REVERSION": ("4bars", "4bars", "1atr", "0.2R"),
    "BREAKOUT": ("8bars", "6bars", "1atr", "0.25R"),
    "TREND": ("16bars", "16bars", "1.5atr", "0.3R"),
    "EMA": ("8bars", "8bars", "0.3%", "0.15R"),
    "SCANNER": ("3bars", "3bars", "0.15%", "0.2R"),
}


def _stagnancy_settings(prefix: str) -> list:
    """One bot type's stagnancy timeout (shared/stagnancy.py): a trade older
    than the min age whose mid price stayed within the range over the
    window, with its P/L within the limit, is logged or closed."""
    min_age, window, rng, pnl = STAGNANCY_DEFAULTS[prefix]
    times = "90s, 15m, 2h" + ("" if prefix in ("SCALPER", "SURGE") else " or 4bars (of its bar length)")
    ranges = "a % of the price (0.05%)" + ("" if prefix in ("SCALPER", "SURGE", "EMA", "SCANNER") else
                                          " or ATRs of its bars (0.25atr)")
    p = f"{prefix}_STAGNANT_"
    return [
        Sub("Stagnancy timeout"),
        Setting(p + "MODE", "Mode", "off, shadow (log it, leave it open) or enforce (close it: TIMEOUT_STAGNANT) - "
                                    "empty for the General tab's"),
        Setting(p + "MIN_AGE", "Checked from", f"The trade's age: {times} - default {min_age}"),
        Setting(p + "WINDOW", "Window", f"The latest stretch looked at: {times} - default {window}"),
        Setting(p + "RANGE", "Max range", f"Of the mid price over the window: {ranges} - default {rng}"),
        Setting(p + "PNL", "Max P/L", f"Either way, net of costs: an amount (1.50) or of what the trade risks "
                                      f"(0.2R) - default {pnl}"),
        Setting(p + "COOLDOWN", "Cooldown", f"No new trade in that market for this long after a timeout close: "
                                            f"{times.split(' or ')[0]} - default 0 (none)"),
    ]


TIMEFRAME_HINT = "M5, M15, M30, H1 or H4"
SETTING_GROUPS = [
    ("General", "Where every bot reports to (leave empty to switch reporting off), and a dry run for the strategy bots.", [
        Setting("DASHBOARD_URL", "Dashboard address", "e.g. https://tradingdashboard.proprietary-data.com"),
        Setting("DASHBOARD_TOKEN", "Dashboard token", "The dashboard's INGEST_TOKEN", secret=True),
        Setting("DASHBOARD_COMMANDS", "Close from dashboard", "1 = a dashboard admin can close (part of) a bot's "
                                                              "positions; empty = off"),
        Setting("DASHBOARD_BROADCAST", "Broadcast trades", "1 = a dashboard admin can push one trade to every "
                                                           "strategy bot and momentum scanner at once, each with its "
                                                           "own size, stops and limits; empty = off"),
        Setting("STRATEGY_DRY_RUN", "Strategy bots: dry run", "1 = log the trades they'd make without sending them"),
        Setting("STAGNANT_MODE", "Stagnancy timeout", "Trades going nowhere: shadow = log them (default), enforce = "
                                                      "close them (TIMEOUT_STAGNANT), off. Each bot type's own setting "
                                                      "wins; exceptions per broker or market go in stagnancy.json"),
    ]),
    ("Momentum scanners", "Shared by the Alpaca, OANDA, Pepperstone, Capital.com and IG scanners.", [
        Setting("SCANNER_TIMEFRAME", "Bar length", "M1, M5, M15 or M30 - default M15 (M1 is too quick for IG's)"),
        Setting("STREAK_LENGTH", "Streak length", "Bars in a row that trigger a trade - default 3", number=True),
        Setting("STOP_LOSS_PERCENT", "Stop-loss %", "Of the entry price - default 2", number=True),
        Setting("TAKE_PROFIT_PERCENT", "Take-profit %", "Of the entry price - default 5", number=True),
        Setting("SCANNER_FLAT_MINUTES", "Close before rollover", "CFD scanners close their trades this many minutes "
                                                                "before the 22:00 UK rollover, so no overnight fee - "
                                                                "default 15; 0 = hold overnight", number=True),
        Setting("SCANNER_LAST_ENTRY_MINUTES", "No entries before rollover", "No new CFD trades from this many "
                                                                           "minutes before it to 45 after - "
                                                                           "default 60; 0 = off", number=True),
        *_stagnancy_settings("SCANNER"),
    ]),
    ("EMA crossover bots", "Shared by the Alpaca, OANDA, Pepperstone, Capital.com and IG EMA bots.", [
        Setting("EMA_TIMEFRAME", "Bar length", "M1, M5, M15 or M30 - default M15"),
        *_stagnancy_settings("EMA"),
    ]),
    ("Forex session breakout", "Shared by the breakout bot on every broker. Trades the break of the London "
                               "morning's range in the London / New York overlap; always flat before the rollover. "
                               "Times are UK (London) time.", [
        Setting("BREAKOUT_TIMEFRAME", "Bar length", f"{TIMEFRAME_HINT} - default M15"),
        Setting("BREAKOUT_RANGE_START", "Range starts", "Default 07:00"),
        Setting("BREAKOUT_RANGE_END", "Range ends, entries start", "Default 13:00 - when New York arrives"),
        Setting("BREAKOUT_ENTRY_END", "Last entry", "Default 16:00"),
        Setting("BREAKOUT_FLAT_TIME", "Close everything at", "Default 20:00 - before the 22:00 rollover, so no swap"),
        Sub("Entry"),
        Setting("BREAKOUT_MIN_RANGE_PERCENT", "Narrowest range %", "Of the price - default 0.15", number=True),
        Setting("BREAKOUT_MAX_RANGE_PERCENT", "Widest range %", "Of the price - default 1.0", number=True),
        Setting("BREAKOUT_BUFFER_ATR", "Breakout buffer", "A close this many ATRs past the range - default 0.2", number=True),
        Setting("BREAKOUT_MAX_EXTENSION", "Furthest to chase", "In range widths past the edge - default 0.5", number=True),
        Setting("BREAKOUT_TREND_EMA", "Trend filter EMA", "Bars; 0 = off - default 50", number=True),
        Setting("BREAKOUT_MAX_TRADES_PER_DAY", "Trades per pair per day", "Default 1", number=True),
        Sub("Exits"),
        Setting("BREAKOUT_STOP_RANGE_FRACTION", "Stop-loss", "Range widths back from the broken edge - default 0.5 "
                                                            "(the middle)", number=True),
        Setting("BREAKOUT_REWARD_RISK", "Take-profit", "Times the stop distance - default 1.5", number=True),
        *_risk_settings("BREAKOUT_", "1"),
        *_stagnancy_settings("BREAKOUT"),
    ]),
    ("Index mean reversion", "Shared by the index bot on every broker. Fades moves stretched far from the day's "
                             "VWAP, only in each index's cash session; always closed the same day.", [
        Setting("REVERSION_TIMEFRAME", "Bar length", f"{TIMEFRAME_HINT} - default M15"),
        Setting("REVERSION_BAND_STDEV", "Band width", "Standard deviations from the VWAP - default 2", number=True),
        Setting("REVERSION_RSI_PERIOD", "RSI period", "Default 14", number=True),
        Setting("REVERSION_RSI_OVERSOLD", "RSI oversold", "Buy at or below - default 30", number=True),
        Setting("REVERSION_RSI_OVERBOUGHT", "RSI overbought", "Sell at or above - default 70", number=True),
        Setting("REVERSION_MAX_ADX", "Max ADX", "Skip trending days above this; 0 = off - default 25", number=True),
        Setting("REVERSION_SKIP_OPEN_MINUTES", "Quiet after the open", "Minutes - default 60", number=True),
        Setting("REVERSION_LAST_ENTRY_MINUTES", "Quiet before the close", "Minutes - default 60", number=True),
        Setting("REVERSION_MAX_TRADES_PER_DAY", "Trades per index per day", "Default 2", number=True),
        Sub("Exits"),
        Setting("REVERSION_STOP_ATR", "Stop-loss", "ATRs from the entry - default 1.5", number=True),
        Setting("REVERSION_MIN_REWARD_RISK", "Min reward / risk", "The VWAP must be this many stop distances away - "
                                                                  "default 1", number=True),
        Setting("REVERSION_MAX_HOLD_BARS", "Time stop", "Bars without reverting; 0 = off - default 8", number=True),
        Setting("REVERSION_FLAT_MINUTES", "Close before the close", "Minutes before the cash close - default 15",
                number=True),
        *_risk_settings("REVERSION_", "0.5"),
        *_stagnancy_settings("REVERSION"),
    ]),
    ("Commodity trend", "Shared by the commodity bot on every broker. Takes the 4-hour trend's side on a "
                        "15-minute EMA crossover; holds overnight, so it watches the swap.", [
        Setting("TREND_TIMEFRAME", "Entry bar length", f"{TIMEFRAME_HINT} - default M15"),
        Setting("TREND_HIGHER_TIMEFRAME", "Trend bar length", f"{TIMEFRAME_HINT} - default H4"),
        Setting("TREND_HTF_FAST_EMA", "Trend fast EMA", "Default 50", number=True),
        Setting("TREND_HTF_SLOW_EMA", "Trend slow EMA", "Default 200", number=True),
        Setting("TREND_MIN_ADX", "Min trend ADX", "0 = off - default 20", number=True),
        Setting("TREND_FAST_EMA", "Entry fast EMA", "Default 9", number=True),
        Setting("TREND_SLOW_EMA", "Entry slow EMA", "Default 21", number=True),
        Setting("TREND_MAX_TRADES_PER_DAY", "Trades per market per day", "Default 2", number=True),
        Sub("Exits"),
        Setting("TREND_STOP_ATR", "Stop-loss", "Entry-bar ATRs from the entry - default 2", number=True),
        Setting("TREND_REWARD_RISK", "Take-profit", "Times the stop distance; 0 = none - default 3", number=True),
        Setting("TREND_TRAIL_ATR", "Trailing stop", "ATRs back from the best price; 0 = off - default 3", number=True),
        Setting("TREND_MAX_HOLD_DAYS", "Max days held", "0 = no limit - default 10", number=True),
        Setting("TREND_WEEKEND_FLAT", "Flat for the weekend", "1 = close Friday 20:00 UK (default), 0 = hold"),
        Setting("TREND_MAX_SWAP_PERCENT", "Max swap", "% of the trade's value per night - default 0.05", number=True),
        *_risk_settings("TREND_", "1"),
        *_stagnancy_settings("TREND"),
    ]),
    ("Tick scalper (HFT-style)", "Shared by the scalper on every broker. Reads live prices every few seconds and "
                                 "trades short bursts, measured in the market's usual spread; out within minutes, "
                                 "never overnight. Backtested, it loses about a spread a trade.", [
        Setting("SCALPER_POLL_SECONDS", "Read prices every", "Seconds - default 2 (IG: 10 at the least)", number=True),
        Setting("SCALPER_MODE", "Mode", "momentum = follow the burst (default), reversion = fade it"),
        Setting("SCALPER_WINDOW_SECONDS", "Burst window", "Seconds - default 60", number=True),
        Setting("SCALPER_TRIGGER_SPREADS", "Trigger", "A move of this many usual spreads in the window - default 4",
                number=True),
        Setting("SCALPER_MAX_SPREAD_RATIO", "Widest spread", "Times the usual spread - default 1.5", number=True),
        Setting("SCALPER_SESSION_START", "Trade from", "London time - default 07:00"),
        Setting("SCALPER_SESSION_END", "Trade until", "London time; anything still open closes then - default 21:00"),
        Setting("SCALPER_COOLDOWN_SECONDS", "Cooldown", "Seconds after a signal before the next in that market - "
                                                        "default 60", number=True),
        Setting("SCALPER_MAX_TRADES_PER_DAY", "Trades per market per day", "Default 30", number=True),
        Sub("Exits"),
        Setting("SCALPER_STOP_SPREADS", "Stop-loss", "Usual spreads from the fill - default 3", number=True),
        Setting("SCALPER_TAKE_PROFIT_SPREADS", "Take-profit", "Usual spreads from the fill - default 3", number=True),
        Setting("SCALPER_MAX_HOLD_SECONDS", "Time stop", "Seconds - default 300", number=True),
        *_risk_settings("SCALPER_", "0.5", spread="60"),
        *_stagnancy_settings("SCALPER"),
    ]),
    ("Opening surge", "The surge scanner watches every liquid US share for the first minutes after the 09:30 New "
                      "York open (14:30 UK) and passes each surge to the followers, which trade it. Start the "
                      "scanner and at least one follower.", [
        Sub("Scanner"),
        Setting("SURGE_POLL_SECONDS", "Read prices every", "Seconds - default 5", number=True),
        Setting("SURGE_CONFIRM_POLLS", "Polls in a row", "The jump must carry on this many polls - default 3",
                number=True),
        Setting("SURGE_JUMP_PERCENT", "Jump per poll", "% each poll must move, the same way - default 0.2", number=True),
        Setting("SURGE_WATCH_MINUTES", "Watch for", "Minutes after the open - default 15", number=True),
        Setting("SURGE_MIN_PRICE", "Min share price", "Dollars - default 5", number=True),
        Setting("SURGE_MIN_DOLLAR_VOLUME", "Min traded a day", "$ millions, median of the last week - default 20",
                number=True),
        Setting("SURGE_MAX_SHARES", "Shares watched", "The most traded that qualify - default 1500", number=True),
        Setting("SURGE_FEED", "Price feed", "iex (free plan, default) or sip (Alpaca's paid plan)"),
        Sub("Followers"),
        Setting("SURGE_MAX_SIGNAL_AGE", "Signal too old after", "Seconds - default 20. Until then, no price or too "
                                                                 "wide a spread is tried again every 2s", number=True),
        Setting("SURGE_FIRST_PRICE_WAIT", "Wait for a first price", "Seconds after the open a signal may wait for "
                "its share's first price (Pepperstone's start at 14:31 UK) - default 90, 0 = off", number=True),
        Setting("SURGE_MAX_TRADES_PER_DAY", "Trades per share per day", "Default 1", number=True),
        Setting("SURGE_REWARD_RISK", "Take-profit", "Times the stop distance (the stop is where the surge "
                                                    "started) - default 2", number=True),
        Setting("SURGE_MAX_HOLD_MINUTES", "Time stop", "Minutes - default 15", number=True),
        *_risk_settings("SURGE_", "0.5", spread="25"),
        *_stagnancy_settings("SURGE"),
    ]),
    ("Slow trend (OANDA)", "Daily trend following on commodity CFDs: each market long, flat or short by its 50/200-day "
                           "EMAs and its move on a year ago, sized by its volatility, rebalanced once a weekday. "
                           "Backtested 2008-2026 it has an edge before costs, mostly eaten by OANDA's financing - "
                           "expect little. Give it an OANDA sub-account of its own (OANDA section).", [
        Setting("SLOW_TREND_TRADE_TIME", "Rebalance at", "London time, weekdays - default 15:00, when all its "
                                                          "markets are open"),
        Setting("SLOW_TREND_TARGET_VOLATILITY", "Target volatility", "% of the budget a year, all its markets "
                                                                      "together - default 10", number=True),
        Setting("SLOW_TREND_REBALANCE_BAND", "Rebalance band", "Resize a position only when it's this % off its "
                                                                "target - default 25", number=True),
        Setting("SLOW_TREND_FAST_EMA", "Fast EMA", "Days - default 50", number=True),
        Setting("SLOW_TREND_SLOW_EMA", "Slow EMA", "Days - default 200", number=True),
        Setting("SLOW_TREND_MOMENTUM_DAYS", "Momentum look-back", "Calendar days - default 365", number=True),
        Setting("SLOW_TREND_VOLATILITY_BARS", "Volatility span", "Daily bars - default 60", number=True),
        Sub("Risk"),
        Setting("SLOW_TREND_MAX_MARKET_LEVERAGE", "Max a market", "Exposure, times the budget - default 1", number=True),
        Setting("SLOW_TREND_MAX_LEVERAGE", "Max in all", "Exposure, times the budget - default 3", number=True),
        Setting("SLOW_TREND_SAFETY_STOP", "Safety stop", "Months of usual movement from the entry, for when the bot "
                                                         "isn't running - default 3", number=True),
        Setting("SLOW_TREND_MAX_SPREAD_PERCENT", "Max spread", "% of the price - default 0.3", number=True),
    ]),
    ("Monthly ETF rotation (Alpaca)", "Splits its budget equally between funds. Once a month, each is held if its "
                                      "month-end close is above its 10-month average, else its share goes to a cash "
                                      "fund. Buys only, no leverage - an investment, not a trading edge.", [
        Setting("ROTATION_SMA_MONTHS", "Average of", "Month-end closes - default 10", number=True),
        Setting("ROTATION_CASH_FUND", "Cash fund", "Ticker - default SHY (1-3 year US Treasuries)"),
        Setting("ROTATION_MINUTES_AFTER_OPEN", "Trade at", "Minutes into the month's first session - default 30",
                number=True),
        Setting("ROTATION_REBALANCE_BAND", "Rebalance band", "Drift under this % of a fund's share is left alone - "
                                                             "default 5", number=True),
        Setting("ROTATION_MAX_SPREAD_PERCENT", "Max spread", "% of the price - default 0.5", number=True),
    ]),
    ("OANDA","Practice account: hub > Tools > API > Generate. Give bots that trade the same markets "
              "sub-accounts of their own - OANDA nets a market's trades together.", [
        Setting("OANDA_API_TOKEN", "API token", secret=True),
        Setting("OANDA_ACCOUNT_ID", "Account ID", "Only needed if the token sees several accounts"),
        Sub("Scanner and EMA bot"),
        Setting("OANDA_BUDGET", "Budget", "Total exposure in the account currency - default 100", number=True),
        Setting("OANDA_MAX_POSITIONS", "Max positions", "Budget slices - default 5", number=True),
        Setting("OANDA_POOL", "Scanner markets", "Comma-separated, e.g. EUR_USD,GBP_USD - empty for the default 15"),
        Setting("OANDA_WATCHLIST", "EMA bot markets", "Comma-separated - empty for the default 8 currency pairs"),
        *_strategy_bot_settings("oanda"),
    ]),
    ("Pepperstone (MetaTrader 5)", "Login only needed if MT5 isn't left logged in to the demo account. Each bot "
                                   "tags its trades, so they can share one hedging account.", [
        Setting("PEPPERSTONE_LOGIN", "Login", "Demo account number"),
        Setting("PEPPERSTONE_PASSWORD", "Password", secret=True),
        Setting("PEPPERSTONE_SERVER", "Server", "e.g. PepperstoneUK-Demo"),
        Setting("MT5_TERMINAL_PATH", "MT5 terminal", "Path to terminal64.exe, if it isn't found on its own"),
        Sub("Scanner and EMA bot"),
        Setting("PEPPERSTONE_BUDGET", "Budget", "Total exposure in the account currency - default 10000", number=True),
        Setting("PEPPERSTONE_MAX_POSITIONS", "Max positions", "Budget slices - default 5", number=True),
        Setting("PEPPERSTONE_POOL", "Scanner markets", "Comma-separated MT5 symbols - empty for the default 15"),
        Setting("PEPPERSTONE_WATCHLIST", "EMA bot markets", "Comma-separated - empty for AAPL.US, MSFT.US and co."),
        *_strategy_bot_settings("pepperstone"),
        *_surge_follower_settings("pepperstone"),
    ]),
    ("Capital.com", "Demo account: Settings > API integrations (needs two-factor login turned on).", [
        Setting("CAPITAL_API_KEY", "API key", secret=True),
        Setting("CAPITAL_EMAIL", "Login email"),
        Setting("CAPITAL_EMAIL_PASSWORD", "API key password", "The password set when the key was generated", secret=True),
        Setting("CAPITAL_ACCOUNT_ID", "Account ID", "Only needed to trade other than the preferred account"),
        Sub("Scanner and EMA bot"),
        Setting("CAPITAL_BUDGET", "Budget", "Total exposure in the account currency - default 600", number=True),
        Setting("CAPITAL_MAX_POSITIONS", "Max positions", "Budget slices - default 5", number=True),
        Setting("CAPITAL_POOL", "Scanner markets", "Comma-separated epics, e.g. US500,GOLD - empty for the default 15"),
        Setting("CAPITAL_WATCHLIST", "EMA bot markets", "Comma-separated epics - empty for AAPL, MSFT and co."),
        *_strategy_bot_settings("capital"),
        *_surge_follower_settings("capital"),
    ]),
    ("Alpaca", "Paper account keys. The strategy bots here trade US funds (SPY, GLD, ...), buying only.", [
        Setting("APCA_API_KEY_ID", "API key ID"),
        Setting("APCA_API_SECRET_KEY", "API secret key", secret=True),
        Sub("Scanner and EMA bot"),
        Setting("BOT_BUDGET_USD", "Scanner budget ($)", "Default 100", number=True),
        Setting("BOT_MAX_POSITIONS", "Scanner max positions", "Default 5", number=True),
        Setting("BOT_POOL", "Scanner shares", "Comma-separated tickers - empty for the default 30"),
        Setting("ALPACA_FLAT_MINUTES", "Scanner: sell before close", "Minutes before the market closes - default 10, "
                "0 = hold overnight", number=True),
        Setting("ALPACA_LAST_ENTRY_MINUTES", "Scanner: no buys before close", "Minutes before the market closes - "
                "default 30, 0 = off", number=True),
        Setting("BOT_SYMBOLS", "EMA bot shares", "Comma-separated - default AAPL,MSFT,AMZN,GOOGL,TSLA"),
        *_strategy_bot_settings("alpaca"),
        *_surge_follower_settings("alpaca"),
    ]),
    ("IG", "Demo account login and API key. The strategy bots trade each market's minimum size and share "
           "IG's 10,000 price-history points a week.", [
        Setting("IG_USERNAME", "Username"),
        Setting("IG_PASSWORD", "Password", secret=True),
        Setting("IG_API_KEY", "API key", secret=True),
        Setting("IG_CURRENCY_CODE", "Currency", "Default GBP"),
        Setting("IG_REQUESTS_PER_MINUTE", "Requests per minute", "Default 28 - split it between IG bots running at once",
                number=True),
        Sub("Scanner and EMA bot"),
        Setting("IG_POOL", "Scanner markets", "Comma-separated - empty for the default 15"),
        Setting("IG_TAKE_PROFIT_PERCENT", "Scanner take-profit %", "Of the entry price, for the IG scanner alone - "
                                                                   "empty = the scanners' Take-profit %", number=True),
        Setting("IG_WATCHLIST", "EMA bot markets", "Comma-separated names, or Name:EPIC"),
        Sub("Scanner loss limits"),
        Setting("IG_MAX_TRADE_LOSS", "Max loss a trade", "Account currency; pulls the stop in so a stop-out loses at "
                                                         "most this - default 25, 0 = off", number=True),
        Setting("IG_MAX_POSITIONS", "Max positions", "Open at once - default 5, 0 = no cap", number=True),
        Setting("IG_DAILY_LOSS_LIMIT", "Daily loss limit", "Account currency; once the day is this far down it closes "
                                                           "its trades until the 22:00 UK rollover - default 250, "
                                                           "0 = off", number=True),
        Sub("Scanner profit and time limits"),
        Setting("IG_MAX_TRADE_PROFIT", "Max profit a trade", "Account currency; pulls the take-profit in so it makes "
                                                             "at most this - default 0 = off", number=True),
        Setting("IG_DAILY_GIVEBACK", "Daily giveback", "Account currency; once the day is this far up, falling this "
                                                       "far below its best stops it until the rollover - default "
                                                       "0 = off", number=True),
        Setting("IG_PAUSE_TIMES", "Pause new trades", "UK time, e.g. 13:00-17:00 (commas for more) - streaks open "
                                                      "nothing then; empty = never"),
        *_strategy_bot_settings("ig"),
    ]),
]
SETTINGS = [s for _, _, group in SETTING_GROUPS for s in group if isinstance(s, Setting)]
SETTING_NAMES = {s.name for s in SETTINGS}


def read_user_env() -> dict:
    """HKCU\\Environment, i.e. the user environment variables."""
    values = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        index = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, index)
            except OSError:
                break
            values[name.upper()] = str(value)
            index += 1
    return values


def write_user_env(changes: dict) -> None:
    """Set (or, for an empty value, remove) user environment variables, then
    tell Windows so windows opened from now on see them."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        for name, value in changes.items():
            if value:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            else:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))


# ---------------------------------------------------------------------------
# WINDOW
# ---------------------------------------------------------------------------
# Windows' own control theme ("vista"): it's drawn by Windows, so it stays
# sharp at any display scaling (the window is DPI aware). Fonts are in points,
# which Tk scales itself; pixel distances go through px().
MUTED = "#5b6678"
GOOD, BUSY, OFF, BAD = "#0a7d0a", "#b7791f", "#98a2b3", "#c62828"
_scale = 1.0


def px(n: float) -> int:
    """A distance in 100%-scaling pixels, scaled for this display."""
    return int(round(n * _scale))


def resource(name: str) -> Path:
    """A file bundled next to this script, or inside the PyInstaller .exe."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / name


class ScrollArea:
    """A vertically scrolling area. Put widgets in `inner`, or build other
    frames as children of `canvas` and switch between them with show()."""

    def __init__(self, parent, background: str):
        self.canvas = tk.Canvas(parent, highlightthickness=0, bd=0, background=background)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.watch(self.inner)

    def watch(self, frame) -> None:
        frame.bind("<Configure>", lambda e: self._fit())

    def show(self, frame) -> None:
        self.canvas.itemconfigure(self._window, window=frame)
        self.canvas.yview_moveto(0)
        self.canvas.after_idle(self._fit)

    def _fit(self) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _wheel(self, event) -> None:
        if self.canvas.yview() != (0.0, 1.0):  # only when there's something to scroll
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


class LauncherApp:
    def __init__(self, root: tk.Tk):
        global _scale
        self.root = root
        self.config = load_config()
        self.running = {}
        self.busy = {}          # bot key -> "Starting…" / "Stopping…"
        _scale = float(root.tk.call("tk", "scaling")) / (96 / 72)

        root.title(APP_NAME)
        # As big as it's designed for, but never taller or wider than the
        # screen - at 300% scaling 700 points is more than a 4K screen's height.
        width = min(px(1040), root.winfo_screenwidth() - px(40))
        height = min(px(700), root.winfo_screenheight() - px(100))
        root.geometry(f"{width}x{height}")
        root.minsize(min(px(800), width), min(px(480), height))
        try:
            root.iconbitmap(default=str(resource("icon.ico")))
        except tk.TclError:
            pass

        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        self.background = style.lookup("TFrame", "background") or root.cget("background")
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 16))
        style.configure("Name.TLabel", font=("Segoe UI Semibold", 10))
        style.configure("Group.TLabel", font=("Segoe UI Semibold", 11))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Hint.TLabel", foreground=MUTED, font=("Segoe UI", 8))
        style.configure("Status.TLabel", font=("Segoe UI", 9))
        style.configure("Primary.TButton", font=("Segoe UI Semibold", 9))
        style.configure("TNotebook.Tab", padding=(px(16), px(6)))

        header = ttk.Frame(root, padding=(px(20), px(14), px(20), px(8)))
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Button(header, text="Open dashboard ↗", command=self.open_dashboard).pack(side="right")

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=px(16), pady=(0, px(16)))
        self.bots_tab = BotsTab(notebook, self)
        self.settings_tab = SettingsTab(notebook, self)
        self.paths_tab = PathsTab(notebook, self)
        notebook.add(self.bots_tab.frame, text="Bots")
        notebook.add(self.settings_tab.frame, text="Settings")
        notebook.add(self.paths_tab.frame, text="Paths")

        self.refresh()

    def refresh(self) -> None:
        try:
            self.running = find_running_bots()
        except Exception:
            pass
        self.bots_tab.update()
        self.root.after(REFRESH_MS, self.refresh)

    def open_dashboard(self, bot: Bot = None) -> None:
        url = read_user_env().get("DASHBOARD_URL", "").rstrip("/")
        if not url:
            messagebox.showinfo(APP_NAME, "Set the dashboard address on the Settings tab first.")
            return
        webbrowser.open(url + (f"/bots/{bot.key}" if bot else "/"))

    # -- actions, run off the UI thread ------------------------------------
    def start(self, bots) -> None:
        bots = [b for b in bots if b.key not in self.running and b.key not in self.busy]
        for bot in bots:
            self.busy[bot.key] = "Starting…"
        self.bots_tab.update()

        def work():
            errors = []
            for bot in bots:
                try:
                    start_bot(bot, self.config)
                except Exception as e:
                    errors.append(f"{bot.name}: {e}")
                time.sleep(1)  # let each window open before the next; they take focus
            self.root.after(4000, lambda: [self.busy.pop(b.key, None) for b in bots])
            if errors:
                self.root.after(0, lambda: messagebox.showerror(APP_NAME, "Couldn't start:\n\n" + "\n\n".join(errors)))

        threading.Thread(target=work, daemon=True).start()

    def start_checked(self, bots) -> None:
        """start(), after a warning if the bots would get in each other's way
        or in the way of bots already running."""
        env = read_user_env()
        notes = bot_clashes.problems(bot_clashes.footprints(BOTS, env, STRATEGY_BOTS), env,
                                     [b.key for b in bots if b.key not in self.running], self.running)
        if notes and not messagebox.askyesno(
                APP_NAME, "These would get in each other's way:\n\n" + "\n\n".join("• " + n for n in notes)
                + "\n\nStart anyway?", icon="warning", default="no"):
            return
        self.start(bots)

    def stop(self, bots, then=None) -> None:
        targets = [(b, self.running[b.key]) for b in bots if b.key in self.running and b.key not in self.busy]
        for bot, _ in targets:
            self.busy[bot.key] = "Stopping…"
        self.bots_tab.update()

        def work():
            notes = []
            threads = []
            for bot, process in targets:
                def stop_one(bot=bot, process=process):
                    outcome = stop_bot(bot, process)
                    if outcome != "stopped":
                        notes.append(f"{bot.name}: {outcome}")
                threads.append(threading.Thread(target=stop_one, daemon=True))
                threads[-1].start()
            for thread in threads:
                thread.join()
            for bot, _ in targets:
                self.busy.pop(bot.key, None)
            if notes:
                self.root.after(0, lambda: messagebox.showwarning(APP_NAME, "\n".join(notes)))
            if then:
                self.root.after(500, then)

        threading.Thread(target=work, daemon=True).start()

    def restart(self, bots) -> None:
        bots = [b for b in bots if b.key in self.running]
        if bots:
            self.stop(bots, then=lambda: self.start(bots))


class BotsTab:
    ALL = "All"

    def __init__(self, notebook, app: LauncherApp):
        self.app = app
        self.frame = ttk.Frame(notebook, padding=px(16))

        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill="x", pady=(0, px(6)))
        ttk.Label(toolbar, text="Broker").pack(side="left")
        self.broker_choice = ttk.Combobox(toolbar, state="readonly", width=13, values=[self.ALL, *BROKERS.values()])
        self.broker_choice.pack(side="left", padx=(px(6), px(16)))
        ttk.Label(toolbar, text="Strategy").pack(side="left")
        self.family_choice = ttk.Combobox(toolbar, state="readonly", width=26, values=[self.ALL, *FAMILIES.values()])
        self.family_choice.pack(side="left", padx=(px(6), 0))
        saved = app.config["filter"]
        self.broker_choice.set(BROKERS.get(saved.get("broker"), self.ALL))
        self.family_choice.set(FAMILIES.get(saved.get("family"), self.ALL))
        for choice in (self.broker_choice, self.family_choice):
            choice.bind("<<ComboboxSelected>>", lambda e: self.apply_filter(save=True))
        self.running_only = tk.BooleanVar(value=bool(saved.get("running")))
        ttk.Checkbutton(toolbar, text="Running only", variable=self.running_only,
                        command=lambda: self.apply_filter(save=True)).pack(side="left", padx=(px(16), 0))
        ttk.Button(toolbar, text="Stop all", command=lambda: app.stop(BOTS)).pack(side="right")
        ttk.Button(toolbar, text="Start all ticked", style="Primary.TButton", command=self.start_all
                   ).pack(side="right", padx=(0, px(8)))
        ttk.Button(toolbar, text="Tick safe set", command=self.tick_safe_set).pack(side="right", padx=(0, px(8)))
        self.summary = ttk.Label(self.frame, text="", style="Muted.TLabel")
        self.summary.pack(fill="x", pady=(0, px(8)))

        hint = ttk.Label(self.frame, style="Hint.TLabel", wraplength=px(900), justify="left", text=(
            "Tick the bots that \"Start all ticked\" starts (whatever the filter shows). \"Tick safe set\" ticks the most "
            "bots that can run at once without touching each other's trades, going by the accounts and markets set on "
            "the Settings tab, and Start warns before starting a bot that would. The strategy bots only manage their "
            "own trades and keep out of markets where another position is open. Bots started by hand show up here too. "
            "Each bot opens as a tab in the \"TradingBots\" terminal window, and Stop presses Ctrl+C in its tab, so it "
            "shuts down cleanly. Closing that window stops every bot; closing this app leaves them running."
        ))
        hint.pack(side="bottom", fill="x", pady=(px(12), 0))

        area = ScrollArea(self.frame, app.background)
        list_frame = area.inner
        list_frame.columnconfigure(2, weight=1)
        for column, heading in ((0, "Start all"), (2, "Bot"), (3, "Status")):
            ttk.Label(list_frame, text=heading, style="Hint.TLabel").grid(row=0, column=column, sticky="w", pady=(0, px(4)))
        ttk.Separator(list_frame).grid(row=1, column=0, columnspan=5, sticky="ew")
        self.rows = {bot.key: BotRow(list_frame, 2 + i * 2, bot, app) for i, bot in enumerate(BOTS)}
        self.apply_filter()

    def chosen(self) -> tuple:
        """(broker key, family key) of the filter, "" for all."""
        broker = next((k for k, v in BROKERS.items() if v == self.broker_choice.get()), "")
        family = next((k for k, v in FAMILIES.items() if v == self.family_choice.get()), "")
        return broker, family

    def shown(self) -> list:
        """The bots the filter lets through. "Running only" keeps a bot that's
        starting or stopping, so its status stays in view until it's done."""
        broker, family = self.chosen()
        active = set(self.app.running) | set(self.app.busy) if self.running_only.get() else None
        return [b for b in BOTS if (not broker or b.broker == broker) and (not family or b.family == family)
                and (active is None or b.key in active)]

    def apply_filter(self, save: bool = False) -> None:
        if save:
            broker, family = self.chosen()
            self.app.config["filter"] = {"broker": broker, "family": family, "running": self.running_only.get()}
            save_config(self.app.config)
        self.update()

    def start_all(self) -> None:
        self.app.start_checked([b for b in BOTS if b.key in self.app.config["start_all"]])

    def tick_safe_set(self) -> None:
        """Tick the most bots that can all run at once under the current
        settings, and say what was left out and why."""
        env = read_user_env()
        chosen, left_out = bot_clashes.pick(bot_clashes.footprints(BOTS, env, STRATEGY_BOTS), env,
                                            running=self.app.running, ticked=self.app.config["start_all"])
        self.app.config["start_all"] = [b.key for b in BOTS if b.key in chosen]
        save_config(self.app.config)
        for bot in BOTS:
            self.rows[bot.key].in_start_all.set(bot.key in chosen)

        lines = [f"Ticked {len(chosen)} of {len(BOTS)} bots: with the accounts and markets set now, none of them "
                 "touches another's trades."]
        reasons = {}
        for bot in BOTS:
            if bot.key in left_out:
                reasons.setdefault(left_out[bot.key], []).append(bot.name)
        if reasons:
            lines += ["", "Left out:"] + [f"• {', '.join(names)}: {why}" for why, names in reasons.items()]
        stop_first = [b.name for b in BOTS if b.key in self.app.running and b.key not in chosen]
        if stop_first:
            lines += ["", "Running but not in the set - stop before \"Start all ticked\": " + ", ".join(stop_first)]
        messagebox.showinfo(APP_NAME, "\n".join(lines))

    def update(self) -> None:
        """Every refresh, since "Running only" changes as bots start and stop."""
        running = sum(1 for b in BOTS if b.key in self.app.running)
        shown = self.shown()
        text = f"{running} of {len(BOTS)} bots running"
        if len(shown) != len(BOTS):
            text += f" - showing {len(shown)} ({sum(1 for b in shown if b.key in self.app.running)} running)"
        self.summary.configure(text=text)
        shown_keys = {b.key for b in shown}
        for bot in BOTS:
            row = self.rows[bot.key]
            row.show(bot.key in shown_keys)
            row.update(self.app.running.get(bot.key), self.app.busy.get(bot.key))


class BotRow:
    def __init__(self, parent, row: int, bot: Bot, app: LauncherApp):
        self.bot, self.app = bot, app
        pad = {"pady": px(4)}  # rows have to stay compact on a 4K screen at 300%
        self.in_start_all = tk.BooleanVar(value=bot.key in app.config["start_all"])
        tick = ttk.Checkbutton(parent, variable=self.in_start_all, command=self.on_start_all_changed)
        tick.grid(row=row, column=0, padx=(px(14), px(20)), **pad)

        size = px(12)
        self.dot = tk.Canvas(parent, width=size, height=size, highlightthickness=0, bd=0, background=app.background)
        self.dot.grid(row=row, column=1, padx=(0, px(10)), **pad)
        self.dot_id = self.dot.create_oval(1, 1, size - 1, size - 1, fill=OFF, outline="")

        names = ttk.Frame(parent)
        names.grid(row=row, column=2, sticky="w", **pad)
        ttk.Label(names, text=bot.name, style="Name.TLabel").pack(anchor="w")
        ttk.Label(names, text=f"{BROKERS[bot.broker]} · {bot.strategy}", style="Muted.TLabel").pack(anchor="w")

        self.status = ttk.Label(parent, text="", style="Status.TLabel", width=24)
        self.status.grid(row=row, column=3, sticky="w", padx=px(12), **pad)

        buttons = ttk.Frame(parent)
        buttons.grid(row=row, column=4, sticky="e", padx=(0, px(8)), **pad)
        self.toggle = ttk.Button(buttons, text="Start", width=8, command=self.on_toggle)
        self.toggle.pack(side="left")
        self.restart = ttk.Button(buttons, text="Restart", width=8, command=lambda: app.restart([bot]))
        self.restart.pack(side="left", padx=px(6))
        ttk.Button(buttons, text="Dashboard ↗", command=lambda: app.open_dashboard(bot)).pack(side="left")

        separator = ttk.Separator(parent)
        separator.grid(row=row + 1, column=0, columnspan=5, sticky="ew")
        self.widgets = [tick, self.dot, names, self.status, buttons, separator]
        self.visible = True

    def show(self, visible: bool) -> None:
        if visible == self.visible:
            return
        self.visible = visible
        for widget in self.widgets:
            widget.grid() if visible else widget.grid_remove()

    def on_start_all_changed(self) -> None:
        chosen = set(self.app.config["start_all"])
        (chosen.add if self.in_start_all.get() else chosen.discard)(self.bot.key)
        self.app.config["start_all"] = [b.key for b in BOTS if b.key in chosen]
        save_config(self.app.config)

    def on_toggle(self) -> None:
        if self.bot.key in self.app.running:
            self.app.stop([self.bot])
        else:
            self.app.start_checked([self.bot])

    def update(self, process, busy) -> None:
        if busy:
            colour, text = BUSY, busy
        elif process:
            since = datetime.fromtimestamp(process.started)
            since_text = since.strftime("%H:%M" if time.time() - process.started < 86400 else "%d %b %H:%M")
            colour, text = GOOD, f"Running since {since_text}"
            if process.instances > 1:
                colour, text = BUSY, f"Running {process.instances} copies!"
        else:
            colour, text = OFF, "Stopped"
        self.dot.itemconfigure(self.dot_id, fill=colour)
        self.status.configure(text=text)
        self.toggle.configure(text="Stop" if process else "Start", state="disabled" if busy else "normal")
        self.restart.configure(state="normal" if process and not busy else "disabled")


class SettingsTab:
    def __init__(self, notebook, app: LauncherApp):
        self.app = app
        self.frame = ttk.Frame(notebook)
        self.vars = {}
        self.show_vars = []
        self.saved = {}

        bar = ttk.Frame(self.frame, padding=(px(16), px(10)))
        bar.pack(side="bottom", fill="x")
        ttk.Separator(self.frame).pack(side="bottom", fill="x")

        # Sections down the left; the chosen one's form, scrolling, on the right.
        body = ttk.Frame(self.frame)
        body.pack(fill="both", expand=True)
        ttk.Style().configure("Sections.Treeview", rowheight=px(30))
        self.nav = ttk.Treeview(body, show="tree", selectmode="browse", style="Sections.Treeview")
        self.nav.column("#0", width=px(220), stretch=False)
        self.nav.pack(side="left", fill="y", padx=(px(12), 0), pady=px(12))
        ttk.Separator(body, orient="vertical").pack(side="left", fill="y", padx=(px(12), 0))
        self.area = ScrollArea(body, app.background)
        self.forms = []
        for index, (title, blurb, group) in enumerate(SETTING_GROUPS):
            self.nav.insert("", "end", iid=str(index), text=title)
            form = ttk.Frame(self.area.canvas, padding=(px(16), px(12), px(24), px(16)))
            self.area.watch(form)
            self.build_form(form, title, blurb, group)
            self.forms.append(form)
        self.area.show(self.forms[0])
        self.area.inner.destroy()  # the sections' forms take its place
        self.nav.selection_set("0")
        self.nav.bind("<<TreeviewSelect>>", lambda e: self.area.show(self.forms[int(self.nav.selection()[0])]))

        ttk.Button(bar, text="Save", style="Primary.TButton", command=self.save).pack(side="right")
        ttk.Button(bar, text="Undo changes", command=self.load).pack(side="right", padx=px(8))
        ttk.Label(bar, style="Hint.TLabel", text=(
            "Saved as your Windows user environment variables. Empty = the bot's default. "
            "Running bots keep their old values until restarted.")).pack(side="left")
        self.load()

    def build_form(self, form, title: str, blurb: str, group: list) -> None:
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text=title, style="Group.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(form, text=blurb, style="Muted.TLabel", wraplength=px(620), justify="left"
                  ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, px(4)))
        row = 2
        for item in group:
            if isinstance(item, Sub):
                ttk.Label(form, text=item.title, style="Name.TLabel").grid(row=row, column=0, columnspan=3, sticky="w",
                                                                          pady=(px(16), 0))
                row += 1
                continue
            ttk.Label(form, text=item.label).grid(row=row, column=0, sticky="w", padx=(0, px(16)), pady=(px(6), 0))
            var = tk.StringVar()
            entry = ttk.Entry(form, textvariable=var, show="•" if item.secret else "")
            entry.grid(row=row, column=1, sticky="ew", pady=(px(6), 0))
            self.vars[item.name] = var
            if item.secret:
                shown = tk.BooleanVar(value=False)
                ttk.Checkbutton(form, text="Show", variable=shown,
                                command=lambda e=entry, v=shown: e.configure(show="" if v.get() else "•")
                                ).grid(row=row, column=2, sticky="w", padx=(px(8), 0), pady=(px(6), 0))
                self.show_vars.append(shown)  # Tk forgets a variable nothing refers to
            ttk.Label(form, text=f"{item.name}    {item.hint}".rstrip(), style="Hint.TLabel", wraplength=px(560),
                      justify="left").grid(row=row + 1, column=1, sticky="w")
            row += 2

    def load(self) -> None:
        env = read_user_env()
        self.saved = {s.name: env.get(s.name, "") for s in SETTINGS}
        for name, value in self.saved.items():
            self.vars[name].set(value)

    def save(self) -> None:
        values = {name: var.get().strip() for name, var in self.vars.items()}
        bad = [s.label for s in SETTINGS if s.number and values[s.name] and not _is_number(values[s.name])]
        if bad:
            messagebox.showerror(APP_NAME, "These need a number:\n\n" + "\n".join(bad))
            return
        changes = {name: value for name, value in values.items() if value != self.saved.get(name, "")}
        if not changes:
            messagebox.showinfo(APP_NAME, "Nothing has changed.")
            return
        write_user_env(changes)
        self.load()

        running = [b for b in BOTS if b.key in self.app.running]
        message = f"Saved {len(changes)} setting{'s' if len(changes) != 1 else ''}."
        if running and messagebox.askyesno(APP_NAME, message + "\n\nRunning bots only pick up changes when they restart. "
                                                                f"Restart the {len(running)} running bot(s) now?"):
            self.app.restart(running)
        elif not running:
            messagebox.showinfo(APP_NAME, message)


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


class PathsTab:
    def __init__(self, notebook, app: LauncherApp):
        self.app = app
        self.frame = ttk.Frame(notebook, padding=px(16))
        self.frame.columnconfigure(1, weight=1)
        self.vars = {}
        self.checks = {}

        rows = [("repo", "tradingbots repo", "The folder with the bots and launcher\\start_bot.bat")] + [
            (broker, f"{label} Python environment", "A venv with that broker's requirements installed")
            for broker, label in BROKERS.items()
        ]
        for i, (key, label, hint) in enumerate(rows):
            ttk.Label(self.frame, text=label).grid(row=i * 2, column=0, sticky="w", padx=(0, px(16)), pady=(px(10), 0))
            var = tk.StringVar(value=app.config["repo"] if key == "repo" else app.config["venvs"][key])
            var.trace_add("write", lambda *_: self.check())
            self.vars[key] = var
            ttk.Entry(self.frame, textvariable=var).grid(row=i * 2, column=1, sticky="ew", pady=(px(10), 0))
            ttk.Button(self.frame, text="Browse…", command=lambda v=var: self.browse(v)).grid(row=i * 2, column=2, padx=px(8), pady=(px(10), 0))
            self.checks[key] = ttk.Label(self.frame, text="", width=10)
            self.checks[key].grid(row=i * 2, column=3, sticky="w", pady=(px(10), 0))
            ttk.Label(self.frame, text=hint, style="Hint.TLabel").grid(row=i * 2 + 1, column=1, sticky="w")

        bar = ttk.Frame(self.frame)
        bar.grid(row=len(rows) * 2, column=0, columnspan=4, sticky="ew", pady=(px(24), 0))
        ttk.Button(bar, text="Save", style="Primary.TButton", command=self.save).pack(side="right")
        ttk.Label(bar, style="Hint.TLabel", text=f"Kept in {CONFIG_PATH}").pack(side="left")
        self.check()

    def browse(self, var: tk.StringVar) -> None:
        folder = filedialog.askdirectory(initialdir=var.get() or str(Path.home()))
        if folder:
            var.set(str(Path(folder)))

    def check(self) -> None:
        for key, var in self.vars.items():
            path = Path(var.get())
            ok = (path / "launcher" / "start_bot.bat").exists() if key == "repo" else (path / "Scripts" / "python.exe").exists()
            self.checks[key].configure(text="✓ found" if ok else "✗ missing", foreground=GOOD if ok else BAD)

    def save(self) -> None:
        self.app.config["repo"] = self.vars["repo"].get().strip()
        for broker in BROKERS:
            self.app.config["venvs"][broker] = self.vars[broker].get().strip()
        save_config(self.app.config)
        messagebox.showinfo(APP_NAME, "Paths saved.")


def main() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # sharp at any display scaling
    except Exception:
        pass
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
