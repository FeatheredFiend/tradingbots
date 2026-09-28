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

    @property
    def script_name(self) -> str:
        return self.script.rsplit("\\", 1)[-1]


BOTS = [
    Bot("oanda-momentum-scanner", "OANDA momentum scanner", "oanda", "Momentum streak",
        r"oanda-momentum-scanner-bot\oanda_momentum_scanner_bot.py"),
    Bot("oanda-ema-bot", "OANDA EMA crossover", "oanda", "EMA 9/21 crossover", r"oanda-ema-bot\oanda_ema_bot.py"),
    Bot("pepperstone-momentum-scanner", "Pepperstone momentum scanner", "pepperstone", "Momentum streak",
        r"pepperstone-momentum-scanner-bot\pepperstone_momentum_scanner_bot.py"),
    Bot("pepperstone-ema-bot", "Pepperstone EMA crossover", "pepperstone", "EMA 9/21 crossover",
        r"pepperstone-ema-bot\pepperstone_ema_bot.py"),
    Bot("capital-momentum-scanner", "Capital.com momentum scanner", "capital", "Momentum streak",
        r"capital-momentum-scanner-bot\capital_momentum_scanner_bot.py"),
    Bot("capital-ema-bot", "Capital.com EMA crossover", "capital", "EMA 9/21 crossover",
        r"capital-ema-bot\capital_ema_bot.py"),
    Bot("alpaca-momentum-scanner", "Alpaca momentum scanner", "alpaca", "Momentum streak (buys only)",
        r"alpaca-momentum-scanner-bot\alpaca_momentum_scanner_bot.py"),
    Bot("alpaca-ema-bot", "Alpaca EMA crossover", "alpaca", "EMA 9/21 crossover", r"alpaca-ema-bot\alpaca_ema_bot.py"),
    Bot("ig-momentum-scanner", "IG momentum scanner", "ig", "Momentum streak",
        r"ig-momentum-scanner-bot\ig_momentum_scanner_bot.py"),
    Bot("ig-ema-bot", "IG EMA crossover", "ig", "EMA 9/21 crossover", r"ig-cfd-ema-bot\ig_cfd_ema_bot.py"),
]
BROKERS = {"oanda": "OANDA", "pepperstone": "Pepperstone", "capital": "Capital.com", "alpaca": "Alpaca", "ig": "IG"}


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
    }


def load_config() -> dict:
    config = default_config()
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        config["repo"] = saved.get("repo") or config["repo"]
        config["venvs"].update({k: v for k, v in (saved.get("venvs") or {}).items() if v})
        if isinstance(saved.get("start_all"), list):
            config["start_all"] = saved["start_all"]
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


SETTING_GROUPS = [
    ("Dashboard", "Where every bot reports to. Leave empty to switch reporting off.", [
        Setting("DASHBOARD_URL", "Dashboard address", "e.g. https://tradingdashboard.proprietary-data.com"),
        Setting("DASHBOARD_TOKEN", "Dashboard token", "The dashboard's INGEST_TOKEN", secret=True),
    ]),
    ("All momentum scanners", "Shared by the Alpaca, OANDA, Pepperstone, Capital.com and IG scanners.", [
        Setting("STREAK_LENGTH", "Streak length", "Bars in a row that trigger a trade - default 3", number=True),
        Setting("STOP_LOSS_PERCENT", "Stop-loss %", "Of the entry price - default 2", number=True),
        Setting("TAKE_PROFIT_PERCENT", "Take-profit %", "Of the entry price - default 5", number=True),
    ]),
    ("OANDA", "Practice account: hub > Tools > API > Generate.", [
        Setting("OANDA_API_TOKEN", "API token", secret=True),
        Setting("OANDA_ACCOUNT_ID", "Account ID", "Only needed if the token sees several accounts"),
        Setting("OANDA_BUDGET", "Budget", "Total exposure in the account currency - default 100", number=True),
        Setting("OANDA_MAX_POSITIONS", "Max positions", "Budget slices - default 5", number=True),
        Setting("OANDA_POOL", "Scanner markets", "Comma-separated, e.g. EUR_USD,GBP_USD - empty for the default 15"),
        Setting("OANDA_WATCHLIST", "EMA bot markets", "Comma-separated - empty for the default 8 currency pairs"),
    ]),
    ("Pepperstone (MetaTrader 5)", "Only needed if MT5 isn't left logged in to the demo account.", [
        Setting("PEPPERSTONE_LOGIN", "Login", "Demo account number"),
        Setting("PEPPERSTONE_PASSWORD", "Password", secret=True),
        Setting("PEPPERSTONE_SERVER", "Server", "e.g. PepperstoneUK-Demo"),
        Setting("PEPPERSTONE_BUDGET", "Budget", "Total exposure in the account currency - default 10000", number=True),
        Setting("PEPPERSTONE_MAX_POSITIONS", "Max positions", "Budget slices - default 5", number=True),
        Setting("PEPPERSTONE_POOL", "Scanner markets", "Comma-separated MT5 symbols - empty for the default 15"),
        Setting("PEPPERSTONE_WATCHLIST", "EMA bot markets", "Comma-separated - empty for AAPL.US, MSFT.US and co."),
        Setting("MT5_TERMINAL_PATH", "MT5 terminal", "Path to terminal64.exe, if it isn't found on its own"),
    ]),
    ("Capital.com", "Demo account: Settings > API integrations (needs two-factor login turned on).", [
        Setting("CAPITAL_API_KEY", "API key", secret=True),
        Setting("CAPITAL_EMAIL", "Login email"),
        Setting("CAPITAL_EMAIL_PASSWORD", "API key password", "The password set when the key was generated", secret=True),
        Setting("CAPITAL_ACCOUNT_ID", "Account ID", "Only needed to trade other than the preferred account"),
        Setting("CAPITAL_BUDGET", "Budget", "Total exposure in the account currency - default 600", number=True),
        Setting("CAPITAL_MAX_POSITIONS", "Max positions", "Budget slices - default 5", number=True),
        Setting("CAPITAL_POOL", "Scanner markets", "Comma-separated epics, e.g. US500,GOLD - empty for the default 15"),
        Setting("CAPITAL_WATCHLIST", "EMA bot markets", "Comma-separated epics - empty for AAPL, MSFT and co."),
    ]),
    ("Alpaca", "Paper account keys.", [
        Setting("APCA_API_KEY_ID", "API key ID"),
        Setting("APCA_API_SECRET_KEY", "API secret key", secret=True),
        Setting("BOT_BUDGET_USD", "Scanner budget ($)", "Default 100", number=True),
        Setting("BOT_MAX_POSITIONS", "Scanner max positions", "Default 5", number=True),
        Setting("BOT_POOL", "Scanner shares", "Comma-separated tickers - empty for the default 30"),
        Setting("BOT_SYMBOLS", "EMA bot shares", "Comma-separated - default AAPL,MSFT,AMZN,GOOGL,TSLA"),
    ]),
    ("IG", "Demo account login and API key.", [
        Setting("IG_USERNAME", "Username"),
        Setting("IG_PASSWORD", "Password", secret=True),
        Setting("IG_API_KEY", "API key", secret=True),
        Setting("IG_CURRENCY_CODE", "Currency", "Default GBP"),
        Setting("IG_POOL", "Scanner markets", "Comma-separated - empty for the default 15"),
        Setting("IG_WATCHLIST", "EMA bot markets", "Comma-separated names, or Name:EPIC"),
        Setting("IG_REQUESTS_PER_MINUTE", "Requests per minute", "Default 28 - split it if both IG bots run", number=True),
    ]),
]
SETTINGS = [s for _, _, group in SETTING_GROUPS for s in group]
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


class LauncherApp:
    def __init__(self, root: tk.Tk):
        global _scale
        self.root = root
        self.config = load_config()
        self.running = {}
        self.busy = {}          # bot key -> "Starting…" / "Stopping…"
        _scale = float(root.tk.call("tk", "scaling")) / (96 / 72)

        root.title(APP_NAME)
        root.geometry(f"{px(980)}x{px(700)}")
        root.minsize(px(800), px(520))
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
    def __init__(self, notebook, app: LauncherApp):
        self.app = app
        self.frame = ttk.Frame(notebook, padding=px(16))

        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill="x", pady=(0, px(10)))
        self.summary = ttk.Label(toolbar, text="", style="Muted.TLabel")
        self.summary.pack(side="left")
        ttk.Button(toolbar, text="Stop all", command=lambda: app.stop(BOTS)).pack(side="right")
        ttk.Button(toolbar, text="Start all ticked", style="Primary.TButton", command=self.start_all
                   ).pack(side="right", padx=(0, px(8)))

        self.rows = {}
        list_frame = ttk.Frame(self.frame)
        list_frame.pack(fill="both", expand=True)
        list_frame.columnconfigure(2, weight=1)
        for column, heading in ((0, "Start all"), (2, "Bot"), (3, "Status")):
            ttk.Label(list_frame, text=heading, style="Hint.TLabel").grid(row=0, column=column, sticky="w", pady=(0, px(4)))
        ttk.Separator(list_frame).grid(row=1, column=0, columnspan=5, sticky="ew")
        for i, bot in enumerate(BOTS):
            self.rows[bot.key] = BotRow(list_frame, 2 + i * 2, bot, app)
            ttk.Separator(list_frame).grid(row=3 + i * 2, column=0, columnspan=5, sticky="ew")

        ttk.Label(self.frame, style="Hint.TLabel", wraplength=px(900), justify="left", text=(
            "Tick the bots that \"Start all ticked\" starts - not an EMA bot and a scanner on the same OANDA or Alpaca "
            "account, they'd close each other's trades. Bots started by hand show up here too. Each bot opens as a tab "
            "in the \"TradingBots\" terminal window, and Stop presses Ctrl+C in its tab, so it shuts down cleanly. "
            "Closing that window stops every bot; closing this app leaves them running."
        )).pack(fill="x", pady=(px(12), 0))

    def start_all(self) -> None:
        self.app.start([b for b in BOTS if b.key in self.app.config["start_all"]])

    def update(self) -> None:
        running = sum(1 for b in BOTS if b.key in self.app.running)
        self.summary.configure(text=f"{running} of {len(BOTS)} bots running")
        for bot in BOTS:
            self.rows[bot.key].update(self.app.running.get(bot.key), self.app.busy.get(bot.key))


class BotRow:
    def __init__(self, parent, row: int, bot: Bot, app: LauncherApp):
        self.bot, self.app = bot, app
        pad = {"pady": px(4)}  # 10 bots have to fit a 4K screen at 300%
        self.in_start_all = tk.BooleanVar(value=bot.key in app.config["start_all"])
        ttk.Checkbutton(parent, variable=self.in_start_all, command=self.on_start_all_changed
                        ).grid(row=row, column=0, padx=(px(14), px(20)), **pad)

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
        buttons.grid(row=row, column=4, sticky="e", **pad)
        self.toggle = ttk.Button(buttons, text="Start", width=8, command=self.on_toggle)
        self.toggle.pack(side="left")
        self.restart = ttk.Button(buttons, text="Restart", width=8, command=lambda: app.restart([bot]))
        self.restart.pack(side="left", padx=px(6))
        ttk.Button(buttons, text="Dashboard ↗", command=lambda: app.open_dashboard(bot)).pack(side="left")

    def on_start_all_changed(self) -> None:
        chosen = set(self.app.config["start_all"])
        (chosen.add if self.in_start_all.get() else chosen.discard)(self.bot.key)
        self.app.config["start_all"] = [b.key for b in BOTS if b.key in chosen]
        save_config(self.app.config)

    def on_toggle(self) -> None:
        if self.bot.key in self.app.running:
            self.app.stop([self.bot])
        else:
            self.app.start([self.bot])

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

        # Scrollable form
        canvas = tk.Canvas(self.frame, highlightthickness=0, bd=0, background=app.background)
        scrollbar = ttk.Scrollbar(self.frame, orient="vertical", command=canvas.yview)
        form = ttk.Frame(canvas, padding=(px(16), px(12), px(24), px(12)))
        form.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=form, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", lambda w: canvas.yview_scroll(int(-w.delta / 120), "units")))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        form.columnconfigure(1, weight=1)
        row = 0
        for title, blurb, group in SETTING_GROUPS:
            ttk.Label(form, text=title, style="Group.TLabel").grid(row=row, column=0, columnspan=3, sticky="w",
                                                                   pady=(px(16) if row else 0, 0))
            ttk.Label(form, text=blurb, style="Muted.TLabel").grid(row=row + 1, column=0, columnspan=3, sticky="w", pady=(0, px(4)))
            row += 2
            for setting in group:
                ttk.Label(form, text=setting.label).grid(row=row, column=0, sticky="w", padx=(0, px(16)), pady=(px(6), 0))
                var = tk.StringVar()
                entry = ttk.Entry(form, textvariable=var, show="•" if setting.secret else "")
                entry.grid(row=row, column=1, sticky="ew", pady=(px(6), 0))
                self.vars[setting.name] = var
                if setting.secret:
                    shown = tk.BooleanVar(value=False)
                    ttk.Checkbutton(form, text="Show", variable=shown,
                                    command=lambda e=entry, v=shown: e.configure(show="" if v.get() else "•")
                                    ).grid(row=row, column=2, sticky="w", padx=(px(8), 0), pady=(px(6), 0))
                    self.show_vars.append(shown)  # Tk forgets a variable nothing refers to
                ttk.Label(form, text=f"{setting.name}    {setting.hint}".rstrip(), style="Hint.TLabel"
                          ).grid(row=row + 1, column=1, sticky="w")
                row += 2

        ttk.Button(bar, text="Save", style="Primary.TButton", command=self.save).pack(side="right")
        ttk.Button(bar, text="Undo changes", command=self.load).pack(side="right", padx=px(8))
        ttk.Label(bar, style="Hint.TLabel", text=(
            "Saved as your Windows user environment variables. Empty = the bot's default. "
            "Running bots keep their old values until restarted.")).pack(side="left")
        self.load()

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
