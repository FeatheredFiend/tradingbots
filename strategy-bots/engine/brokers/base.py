"""
What the runner needs from a broker, and the shapes it's given in. Each
adapter wraps the same broker calls as that broker's existing bots.
"""

import math
from dataclasses import dataclass, field


class BrokerError(Exception):
    """The broker refused a request, or couldn't be reached; the message says why."""


@dataclass
class Market:
    symbol: str                 # the broker's id: OANDA instrument, Capital.com / IG epic, MT5 symbol, ticker
    name: str                   # a readable name
    requested: str              # the name in the market list it was resolved from
    min_size: float             # smallest trade, in the broker's size units
    size_step: float            # sizes are whole multiples of this
    digits: int = None          # decimals the broker accepts in a price
    raw: object = None          # the broker's own details
    session_hint: str = None    # "@us" etc. from the market list (index bot)
    session: object = None      # clock.Session, set by the index strategy


@dataclass
class Quote:
    bid: float
    ask: float
    tradeable: bool
    unit_value: float = None    # what one size unit is worth in the account's currency (None: unknown)
    why_not: str = ""           # why it isn't tradeable, when it isn't

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass
class Position:
    symbol: str
    direction: str              # "long" / "short"
    size: float
    entry: float
    opened_at: float = None     # Unix seconds, if the broker says
    pnl: float = None
    stop: float = None
    take_profit: float = None
    raw: object = None
    own: bool = None            # opened by this bot? (None: the broker can't tell - the runner's notes decide)


@dataclass
class Account:
    id: str
    currency: str
    balance: float
    equity: float
    description: str = ""       # one line for the startup log
    warnings: list = field(default_factory=list)


class Broker:
    key = ""
    name = ""
    native_stops = True         # stop-loss / take-profit ride on the order and the broker enforces them
    can_short = True
    fixed_min_size = False      # every trade is the market's minimum (IG)
    max_leverage = None         # a cap the broker imposes on this kind of account (Alpaca cash: 1)
    metered_history = False     # price history is rationed per bar (IG: 10,000 a week)
    dashboard_every = None      # seconds between dashboard snapshots; None = the reporter's 15

    def __init__(self, settings, dashboard, log):
        self.settings = settings
        self.dashboard = dashboard
        self.log = log
        self.close_reasons = {}  # broker trade/deal id -> why this bot closed it, for the dashboard
        # The broker's ids for the trades this bot opened (saved by the runner),
        # so it only ever manages - and reports - its own, never another bot's
        # or a manual trade in the same market.
        self.own_ids = set()

    # -- the runner calls these ---------------------------------------------
    def connect(self) -> Account:
        raise NotImplementedError

    def resolve(self, names: list) -> dict:
        """{symbol: Market} for the names the broker has; logs and skips the rest."""
        raise NotImplementedError

    def bars(self, market: Market, timeframe: str, count: int) -> list:
        """Up to `count` of the market's latest CLOSED bars, oldest first, as
        indicators.Bar with UTC start times and mid prices."""
        raise NotImplementedError

    def quotes(self, markets: list) -> dict:
        """{symbol: Quote} for these markets, right now."""
        raise NotImplementedError

    def positions(self, markets: dict) -> dict:
        """{symbol: Position} for the open positions in the bot's markets: the
        bot's own (own=True, built from its own trades only) where it has any,
        else anyone else's (own=False), so the runner can keep out of them."""
        raise NotImplementedError

    def open(self, market: Market, direction: str, size: float, stop: float, take_profit, quote: Quote) -> bool:
        """A market order with the stop-loss (and take-profit, unless None)
        attached. True if it filled; the new trade's ids go into own_ids."""
        raise NotImplementedError

    def close(self, market: Market, position: Position, reason: str) -> bool:
        """Close the bot's own position at market - only its own trades. True if it did."""
        raise NotImplementedError

    def swap_percent_per_night(self, market: Market, direction: str):
        """Overnight financing for holding `direction` one night, as a percent
        of the position's value - negative is a cost. None if unknown."""
        return None

    def report(self, markets: dict, notes: dict) -> None:
        """Account, positions and recent trades for the dashboard. `notes` is
        the runner's saved notes per symbol (stop/take-profit for brokers
        that don't hold them). A failure only skips a report."""

    def shutdown(self) -> None:
        pass

    # -- helpers --------------------------------------------------------------
    def round_size(self, market: Market, size: float) -> float:
        """Rounded down to the market's step; 0 if below its minimum."""
        if size <= 0:
            return 0.0
        step = market.size_step or market.min_size or 1.0
        rounded = round(math.floor(size / step + 1e-9) * step, 10)
        return rounded if rounded >= market.min_size - 1e-12 else 0.0

    def round_price(self, market: Market, price: float) -> float:
        return round(price, market.digits) if market.digits is not None else price
