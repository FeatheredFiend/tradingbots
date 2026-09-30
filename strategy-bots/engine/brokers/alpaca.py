"""
Alpaca paper account through alpaca-trade-api - the same calls as the
Alpaca bots. Paper endpoint only.

Alpaca has no forex and no CFDs, so only the index and commodity bots run
here, on US-listed funds (SPY, QQQ, GLD, ...), with these differences:
- Buys only: fractional shares can't be sold short, so short signals are
  skipped (logged).
- No leverage: fractional buys can't use margin (max leverage 1).
- Stop-loss and take-profit are enforced by the bot, not the broker
  (Alpaca can't attach them to fractional orders), so they only work while
  the bot runs and the market is open.
- No overnight financing on shares bought outright, so no swap.
- Regular US market hours only (09:30-16:00 New York). Bars longer than 15
  minutes are built from the day's 15-minute bars, counted from the 09:30
  open: a "4-hour" bar is 09:30-13:30 or 13:30-16:00.
- Alpaca holds one position per fund, so the bot manages a fund's position
  only if it bought it (its saved notes say so), and doesn't buy a fund
  someone else holds.
"""

import math
import os
import time
from datetime import datetime, timezone

import alpaca_trade_api as tradeapi
import pandas as pd
import requests
from alpaca_trade_api.rest import APIError, TimeFrame, TimeFrameUnit

from .base import Account, Broker, BrokerError, Market, Position, Quote
from ..indicators import Bar

BASE_URL = "https://paper-api.alpaca.markets"  # Paper trading only - do not change.
DATA_FEED = "iex"                              # the free data plan's feed
MARKET_TZ = "America/New_York"
SESSION_MINUTES = 390                          # 09:30-16:00
TIMEFRAME_MINUTES = {"M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240}
MIN_NOTIONAL_USD = 1.00
CLOCK_SECONDS = 30                             # how long "is the market open?" is trusted


def _two_sided(quote) -> bool:
    bid, ask = float(quote.bp or 0), float(quote.ap or 0)
    return bid > 0 and ask >= bid


class AlpacaBroker(Broker):
    key = "alpaca"
    name = "Alpaca"
    native_stops = False
    can_short = False
    max_leverage = 1.0

    def __init__(self, settings, dashboard, log):
        super().__init__(settings, dashboard, log)
        self.api = None
        self._is_open = False
        self._clock_until = 0.0
        self._session = (None, None)  # (New York date, that day's opening time), from Alpaca's calendar

    def _call(self, what: str, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (APIError, requests.exceptions.RequestException) as e:
            raise BrokerError(f"{what}: {e}") from None

    # -- account / markets ----------------------------------------------------
    def connect(self) -> Account:
        key, secret = os.environ.get("APCA_API_KEY_ID", ""), os.environ.get("APCA_API_SECRET_KEY", "")
        if not key or not secret:
            raise BrokerError("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY (the paper account's keys).")
        self.api = tradeapi.REST(key, secret, BASE_URL, api_version="v2")
        account = self._call("account", self.api.get_account)
        equity = float(account.equity)
        return Account(
            id=str(account.account_number), currency="USD", balance=float(account.cash), equity=equity,
            description=f"Alpaca paper account {account.account_number} | status {account.status} | equity "
                        f"${equity:,.2f} | cash for fractional buys ${float(account.non_marginable_buying_power):,.2f}",
        )

    def resolve(self, names: list) -> dict:
        markets = {}
        for requested in names:
            symbol = requested.strip().upper()
            try:
                asset = self.api.get_asset(symbol)
            except (APIError, requests.exceptions.RequestException) as e:
                self.log.warning(f"Skipping {symbol}: couldn't look it up ({e}).")
                continue
            if not asset.tradable:
                self.log.warning(f"Skipping {symbol}: not tradable on Alpaca.")
            elif not asset.fractionable:
                self.log.warning(f"Skipping {symbol}: no fractional trading, which small budgets need.")
            else:
                markets[symbol] = Market(symbol=symbol, name=asset.name or symbol, requested=requested,
                                         min_size=0.0, size_step=1e-6, digits=2, raw=asset)
        return markets

    # -- prices -----------------------------------------------------------------
    def bars(self, market: Market, timeframe: str, count: int) -> list:
        minutes = TIMEFRAME_MINUTES[timeframe]
        base = minutes if minutes <= 30 else 15
        per_day = math.ceil(SESSION_MINUTES / minutes)
        days = math.ceil(count / per_day * 7 / 5) + 4  # calendar days that hold `count` bars, with room for holidays
        start = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)).isoformat()
        frame = self._call(f"bars for {market.symbol}", self.api.get_bars, [market.symbol],
                           TimeFrame(base, TimeFrameUnit.Minute), start=start, feed=DATA_FEED).df
        if frame is None or frame.empty:
            return []
        local = frame.tz_convert(MARKET_TZ)
        opens = local.index.normalize() + pd.Timedelta(hours=9, minutes=30)
        since_open = (local.index - opens).total_seconds() / 60
        local = local[(since_open >= 0) & (since_open < SESSION_MINUTES)]  # regular hours only
        since_open = since_open[(since_open >= 0) & (since_open < SESSION_MINUTES)]
        block = (since_open // minutes).astype(int)
        block_start = local.index.normalize() + pd.Timedelta(hours=9, minutes=30) + pd.to_timedelta(block * minutes, "min")
        grouped = local.groupby(block_start).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                                 close=("close", "last"), volume=("volume", "sum"))
        now = time.time()
        bars = []
        for start_time, row in grouped.iterrows():
            begins = start_time.timestamp()
            session_close = (start_time.normalize() + pd.Timedelta(hours=16)).timestamp()
            if min(begins + minutes * 60, session_close) > now:
                continue  # still forming
            bars.append(Bar(begins, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]),
                            float(row["volume"])))
        return bars[-count:]

    def daily_bars(self, market: Market, count: int) -> list:
        """Consolidated (SIP) daily bars, adjusted for dividends and splits,
        up to 20 minutes ago (the free plan's limit); IEX's if SIP is refused."""
        now = pd.Timestamp.now(tz="UTC")
        start = (now - pd.Timedelta(days=math.ceil(count * 7 / 5) + 10)).isoformat()
        end = (now - pd.Timedelta(minutes=20)).isoformat()
        try:
            frame = self._call(f"daily bars for {market.symbol}", self.api.get_bars, market.symbol, TimeFrame.Day,
                               start=start, end=end, adjustment="all", feed="sip").df
        except BrokerError:
            frame = self._call(f"daily bars for {market.symbol}", self.api.get_bars, market.symbol, TimeFrame.Day,
                               start=start, end=end, adjustment="all", feed=DATA_FEED).df
        if frame is None or frame.empty:
            return []
        return [Bar(t.timestamp(), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                    float(r["volume"])) for t, r in frame.iterrows()][-count:]

    def session_opened_at(self, now: float):
        self._check_clock()
        if not self._is_open:
            return None
        day = pd.Timestamp(now, unit="s", tz="UTC").tz_convert(MARKET_TZ).date()
        if self._session[0] != day:
            days = self._call("market calendar", self.api.get_calendar, start=day.isoformat(), end=day.isoformat())
            opens = str(days[0].open)[:5] if days else "09:30"  # "09:30", or a time on newer library versions
            opened = pd.Timestamp(f"{day.isoformat()} {opens}").tz_localize(MARKET_TZ).timestamp()
            self._session = (day, opened)
        return self._session[1]

    def _check_clock(self) -> None:
        """Whether the market's open, asked at most every CLOCK_SECONDS."""
        if time.monotonic() >= self._clock_until:
            self._is_open = self._call("market clock", self.api.get_clock).is_open
            self._clock_until = time.monotonic() + CLOCK_SECONDS

    def quotes(self, markets: list) -> dict:
        """Every fund's latest quote in one request (the scalper asks every few
        seconds); whether the market's open is asked at most every CLOCK_SECONDS."""
        self._check_clock()
        symbols = [m.symbol for m in markets]
        latest = self._call("quotes", self.api.get_latest_quotes, symbols, feed=DATA_FEED) if symbols else {}
        one_sided = [s for s in symbols if s not in latest or not _two_sided(latest[s])]
        trades = self._call("last trades", self.api.get_latest_trades, one_sided, feed=DATA_FEED) if one_sided else {}
        quotes = {}
        for symbol in symbols:
            if symbol in one_sided:  # IEX often shows one side only
                if symbol not in trades:
                    continue
                bid = ask = float(trades[symbol].p)
            else:
                bid, ask = float(latest[symbol].bp), float(latest[symbol].ap)
            quotes[symbol] = Quote(bid, ask, self._is_open, unit_value=(bid + ask) / 2,
                                   why_not="" if self._is_open else "the US market is closed")
        return quotes

    def swap_percent_per_night(self, market: Market, direction: str):
        return 0.0  # shares bought outright pay no overnight financing

    # -- positions / orders -------------------------------------------------------
    def positions(self, markets: dict) -> dict:
        return {
            p.symbol: Position(p.symbol, "long", float(p.qty), float(p.avg_entry_price),
                               pnl=float(p.unrealized_pl), raw=p)
            for p in self._call("positions", self.api.list_positions) if p.symbol in markets
        }

    def open(self, market: Market, direction: str, size: float, stop: float, take_profit, quote: Quote) -> bool:
        notional = math.floor(size * quote.ask * 100) / 100
        if notional < MIN_NOTIONAL_USD:
            self.log.info(f"{market.symbol}: a ${notional:.2f} buy is under Alpaca's $1 minimum; skipping.")
            return False
        try:
            order = self.api.submit_order(symbol=market.symbol, notional=notional, side="buy", type="market",
                                          time_in_force="day")
        except (APIError, requests.exceptions.RequestException) as e:
            self.log.error(f"{market.symbol}: buy refused: {e}")
            return False
        levels = "" if stop is None else (
            f" (stop {stop:.2f}, take-profit {f'{take_profit:.2f}' if take_profit is not None else 'none'}, "
            f"enforced by the bot)")
        self.log.info(f"BUY {market.symbol} ${notional:.2f}{levels} -> order {order.id} {order.status}")
        return True

    def close(self, market: Market, position: Position, reason: str, size: float = None) -> bool:
        try:
            order = self.api.close_position(market.symbol, qty=size)
        except (APIError, requests.exceptions.RequestException) as e:
            self.log.error(f"{market.symbol}: close refused ({reason}): {e}")
            self.close_problem = str(e)
            return False
        self.log.info(f"CLOSE {market.symbol} {size if size is not None else 'all'} ({reason}) -> order {order.id}")
        p = position.raw
        if p is not None:  # priced as Alpaca valued the position just before the sell
            share = 1.0 if size is None else size / float(p.qty)
            self.dashboard.trade({
                "ref": str(order.id), "symbol": p.symbol, "direction": "long",
                "size": float(p.qty) if size is None else size,
                "entryPrice": float(p.avg_entry_price), "exitPrice": float(p.current_price),
                "closedAt": datetime.now(timezone.utc).isoformat(), "pnl": float(p.unrealized_pl) * share,
                "closeReason": reason,
            })
        return True

    # -- dashboard ------------------------------------------------------------------
    def report(self, markets: dict, notes: dict) -> None:
        try:
            account = self.api.get_account()
            positions = [p for p in self.api.list_positions() if p.symbol in markets and p.symbol in notes]
        except Exception:
            return
        unrealized = sum(float(p.unrealized_pl) for p in positions)
        self.dashboard.update(
            account={"balance": float(account.equity) - unrealized, "equity": float(account.equity),
                     "unrealizedPl": unrealized},
            positions=[{
                "symbol": p.symbol, "direction": "long", "size": float(p.qty), "entryPrice": float(p.avg_entry_price),
                "currentPrice": float(p.current_price), "pnl": float(p.unrealized_pl),
                "stopLoss": (notes.get(p.symbol) or {}).get("stop"),
                "takeProfit": (notes.get(p.symbol) or {}).get("take_profit"),
                "openedAt": (notes.get(p.symbol) or {}).get("opened_at"),
            } for p in positions],
        )
