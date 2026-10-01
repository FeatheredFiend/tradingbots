"""
Pepperstone MetaTrader 5 demo account through MetaQuotes' MetaTrader5
package, which drives the MT5 terminal on this PC - the same calls as
pepperstone-momentum-scanner-bot/. Windows only; exits unless the account
is a demo account.

Each strategy tags its positions with its own magic number and ignores
every other position, so on a hedging account the bots can share one
account with each other, the other Pepperstone bots and manual trades.
"""

import os
import time

import MetaTrader5 as mt5

from .. import clock
from ..indicators import Bar
from ..settings import MT5_MAGIC
from .base import Account, Broker, BrokerError, Market, Position, Quote

TIMEFRAME = {"M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
             "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4}
DEVIATION_POINTS = 20          # accepted slippage, where the symbol's execution mode honours it
STALE_TICK_SECONDS = 300       # no price for this long = market closed
DEAL_REASONS = {
    mt5.DEAL_REASON_SL: "stop-loss", mt5.DEAL_REASON_TP: "take-profit", mt5.DEAL_REASON_EXPERT: "closed by the bot",
    mt5.DEAL_REASON_CLIENT: "closed by hand", mt5.DEAL_REASON_MOBILE: "closed by hand (phone)",
    mt5.DEAL_REASON_WEB: "closed by hand (web)", mt5.DEAL_REASON_SO: "stop-out",
}


class PepperstoneBroker(Broker):
    key = "pepperstone"
    name = "Pepperstone"
    size_unit = "lots"

    def __init__(self, settings, dashboard, log):
        super().__init__(settings, dashboard, log)
        self.magic = MT5_MAGIC[settings.strategy]
        self.comment = settings.strategy[:31]
        self.currency = None
        self._correction = 0   # seconds the server clock differs from Pepperstone's usual New York close + 7h

    # -- server time --------------------------------------------------------------
    @staticmethod
    def _usual_offset(ts: float) -> int:
        """Pepperstone's server runs at New York time + 7 hours (UTC+2 in
        winter, UTC+3 in summer), so its midnight is the 17:00 NY rollover."""
        return (clock.utc_offset_hours("new_york", ts) + 7) * 3600

    def _to_utc(self, server_time: float) -> float:
        return server_time - self._usual_offset(server_time - 3 * 3600) - self._correction

    def _check_server_clock(self, symbols) -> None:
        """Compare the usual offset with a tick from the last two minutes, and
        go by the tick if they differ. Nothing to compare at weekends."""
        now = time.time()
        for symbol in symbols:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                continue
            ahead = tick.time - now
            rounded = round(ahead / 1800) * 1800
            if abs(ahead - rounded) < 120 and abs(rounded) <= 14 * 3600:
                self._correction = int(rounded - self._usual_offset(now))
                if self._correction:
                    self.log.warning(f"The MT5 server clock is UTC{rounded / 3600:+g}, not the usual "
                                     f"UTC{self._usual_offset(now) / 3600:+g}; going by the server.")
                return

    # -- account / markets ----------------------------------------------------
    def connect(self) -> Account:
        login, password, server = (os.environ.get(n, "") for n in ("PEPPERSTONE_LOGIN", "PEPPERSTONE_PASSWORD",
                                                                     "PEPPERSTONE_SERVER"))
        path = os.environ.get("MT5_TERMINAL_PATH", "")
        kwargs = {"path": path} if path else {}
        if login:
            if not password or not server:
                raise BrokerError("PEPPERSTONE_LOGIN is set, so PEPPERSTONE_PASSWORD and PEPPERSTONE_SERVER must be too.")
            kwargs.update(login=int(login), password=password, server=server)
        if not mt5.initialize(**kwargs):
            raise BrokerError(f"couldn't connect to MetaTrader 5: {mt5.last_error()}. Check Pepperstone's MT5 terminal "
                              f"is installed (or set MT5_TERMINAL_PATH to its terminal64.exe) and logged into the demo "
                              f"account, or set PEPPERSTONE_LOGIN / PEPPERSTONE_PASSWORD / PEPPERSTONE_SERVER.")
        account = mt5.account_info()
        if account is None:
            raise BrokerError(f"MetaTrader 5 isn't logged into an account: {mt5.last_error()}")
        if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            raise BrokerError(f"account {account.login} on {account.server} is not a demo account - this bot only "
                              f"trades demo accounts")
        self.account_mode = "demo"  # as MT5 itself says, just checked
        terminal = mt5.terminal_info()
        if terminal is not None and (not terminal.trade_allowed or terminal.tradeapi_disabled):
            raise BrokerError("the MT5 terminal isn't allowing automated trading. Switch on Algo Trading on its "
                              "toolbar (and don't disable trading via the Python API under Tools > Options > Expert "
                              "Advisors), then restart.")
        self.currency = account.currency
        margin_mode = {mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING: "hedging",
                       mt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING: "netting"}.get(account.margin_mode, "exchange")
        warnings = []
        if margin_mode != "hedging":
            warnings.append("This is not a hedging account: positions in one symbol net together, so bots trading "
                            "the same markets on it would close each other's trades.")
        return Account(
            id=f"{account.login} on {account.server}", currency=account.currency, balance=account.balance,
            equity=account.equity, warnings=warnings,
            description=f"Pepperstone MT5 demo {account.login} on {account.server} ({margin_mode}) | balance "
                        f"{account.balance:,.2f} {account.currency} | equity {account.equity:,.2f} | free margin "
                        f"{account.margin_free:,.2f} | leverage 1:{account.leverage} | magic number {self.magic}",
        )

    def resolve(self, names: list) -> dict:
        all_names = [s.name for s in (mt5.symbols_get() or ())]
        markets = {}
        for requested in names:
            exact = [n for n in all_names if n.upper() == requested.upper()]
            matches = exact or [n for n in all_names if n.upper().startswith(requested.upper())]
            if len(matches) != 1:
                why = (f"several symbols start with it ({', '.join(matches[:8])}) - use the exact one" if matches
                       else "no such symbol on this account")
                self.log.warning(f"Skipping {requested}: {why}.")
                continue
            symbol = matches[0]
            if not mt5.symbol_select(symbol, True):  # Market Watch, which prices and bars need
                self.log.warning(f"Skipping {symbol}: couldn't add it to Market Watch ({mt5.last_error()}).")
                continue
            info = mt5.symbol_info(symbol)
            if info is None or info.trade_mode in (mt5.SYMBOL_TRADE_MODE_DISABLED, mt5.SYMBOL_TRADE_MODE_CLOSEONLY):
                self.log.warning(f"Skipping {symbol}: not open for new trades on this account.")
                continue
            markets[symbol] = Market(symbol=symbol, name=info.description or symbol, requested=requested,
                                     min_size=info.volume_min, size_step=info.volume_step, digits=info.digits, raw=info)
        self._check_server_clock(list(markets))
        return markets

    # -- prices -----------------------------------------------------------------
    def bars(self, market: Market, timeframe: str, count: int) -> list:
        """MT5's own bars (built from bid prices), position 0 being the one still forming."""
        rates = mt5.copy_rates_from_pos(market.symbol, TIMEFRAME[timeframe], 1, count)
        if rates is None:
            raise BrokerError(f"no {timeframe} bars for {market.symbol}: {mt5.last_error()}")
        return [Bar(self._to_utc(float(r["time"])), float(r["open"]), float(r["high"]), float(r["low"]),
                    float(r["close"]), float(r["tick_volume"])) for r in rates]

    def _lot_value(self, symbol: str, price: float):
        """One lot's worth in the account's currency: MT5's profit on a 1% move, x 100."""
        profit = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 1.0, price, price * 1.01)
        return profit * 100 if profit else None

    def quotes(self, markets: list) -> dict:
        quotes = {}
        now = time.time()
        for market in markets:
            tick = mt5.symbol_info_tick(market.symbol)
            if tick is None or not tick.bid or not tick.ask:
                continue
            age = now - self._to_utc(tick.time)
            fresh = age <= STALE_TICK_SECONDS
            quotes[market.symbol] = Quote(
                tick.bid, tick.ask, fresh, unit_value=self._lot_value(market.symbol, (tick.bid + tick.ask) / 2),
                why_not="" if fresh else f"no price for {age / 60:.0f} min - market closed?",
            )
        return quotes

    def min_stop_distance(self, market: Market, quote: Quote) -> float:
        info = mt5.symbol_info(market.symbol) or market.raw
        return info.trade_stops_level * info.point  # 0 on Pepperstone's FX and indices (29 September 2026)

    def swap_percent_per_night(self, market: Market, direction: str):
        info = mt5.symbol_info(market.symbol) or market.raw
        swap = info.swap_long if direction == "long" else info.swap_short
        tick = mt5.symbol_info_tick(market.symbol)
        price = (tick.bid + tick.ask) / 2 if tick and tick.bid else None
        mode = info.swap_mode
        if mode == mt5.SYMBOL_SWAP_MODE_DISABLED:
            return 0.0
        if mode == mt5.SYMBOL_SWAP_MODE_POINTS and price:
            return swap * info.point / price * 100
        if mode in (mt5.SYMBOL_SWAP_MODE_INTEREST_CURRENT, mt5.SYMBOL_SWAP_MODE_INTEREST_OPEN):
            return swap / 365
        if mode in (mt5.SYMBOL_SWAP_MODE_CURRENCY_DEPOSIT, mt5.SYMBOL_SWAP_MODE_CURRENCY_SYMBOL,
                    mt5.SYMBOL_SWAP_MODE_CURRENCY_MARGIN) and price:
            value = self._lot_value(market.symbol, price)  # money per lot per night, roughly in the account's currency
            return swap / value * 100 if value else None
        return None

    # -- positions / orders -------------------------------------------------------
    def _own_positions(self) -> list:
        positions = mt5.positions_get()
        if positions is None:
            raise BrokerError(f"couldn't read positions: {mt5.last_error()}")
        return [p for p in positions if p.magic == self.magic]

    def positions(self, markets: dict) -> dict:
        positions = {}
        for p in self._own_positions():
            if p.symbol not in markets:
                continue
            held = positions.get(p.symbol)
            if held is None:  # its magic number makes it this bot's
                held = positions[p.symbol] = Position(
                    p.symbol, "long" if p.type == mt5.POSITION_TYPE_BUY else "short", 0.0, 0.0,
                    opened_at=self._to_utc(p.time), pnl=0.0, stop=p.sl or None, take_profit=p.tp or None, raw=[],
                    own=True)
            held.entry = (held.entry * held.size + p.price_open * p.volume) / (held.size + p.volume)
            held.size += p.volume
            held.pnl += p.profit + p.swap
            held.raw.append(p)
        return positions

    @staticmethod
    def _filling(info) -> int:
        if info.filling_mode & 1:
            return mt5.ORDER_FILLING_FOK
        if info.filling_mode & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    @staticmethod
    def _to_tick(info, price: float) -> float:
        tick = info.trade_tick_size or info.point
        return round(round(price / tick) * tick, info.digits)

    @staticmethod
    def _send(request: dict) -> tuple:
        """(done?, what happened, MT5's result or None). A part-filled order
        (TRADE_RETCODE_DONE_PARTIAL) isn't done: the rest is still open."""
        result = mt5.order_send(request)
        if result is None:
            return False, f"NOT SENT ({mt5.last_error()})", None
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            return True, f"DONE {result.volume:g} @ {result.price}", result
        return False, f"REJECTED ({result.retcode}: {result.comment})", result

    def round_size(self, market: Market, size: float) -> float:
        return min(super().round_size(market, size), market.raw.volume_max)

    def open(self, market: Market, direction: str, size: float, stop: float, take_profit, quote: Quote) -> bool:
        info = market.raw
        buy = direction == "long"
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": market.symbol, "volume": size,
            "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, "price": quote.ask if buy else quote.bid,
            "sl": self._to_tick(info, stop), "tp": self._to_tick(info, take_profit) if take_profit is not None else 0.0,
            "deviation": DEVIATION_POINTS, "magic": self.magic, "comment": (self.order_tag or self.comment)[:31],
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(info),
        }
        done, outcome, _ = self._send(request)
        if not done:
            self.open_problem = outcome
        self.log.info(f"{direction.upper()} {market.symbol} volume={size:g} stop={request['sl']} "
                      f"take-profit={request['tp'] or 'none'} -> {outcome}")
        return done

    def close(self, market: Market, position: Position, reason: str, size: float = None) -> bool:
        all_closed = True
        left = size
        volume_filled = value_filled = pnl = 0.0
        for p in position.raw or ():
            if left is not None and left <= 0:
                break
            volume = p.volume if left is None else round(min(left, p.volume), 8)
            tick = mt5.symbol_info_tick(p.symbol)
            if tick is None:
                self.log.error(f"No live price for {p.symbol}; can't close it ({reason}).")
                self.close_problem = f"no live price for {p.symbol} (is the market open?)"
                return False
            closing_long = p.type == mt5.POSITION_TYPE_BUY
            # A position its stop-loss or take-profit closed a moment before is
            # refused here (TRADE_RETCODE_POSITION_CLOSED): the next read shows it gone.
            done, outcome, result = self._send({
                "action": mt5.TRADE_ACTION_DEAL, "position": p.ticket, "symbol": p.symbol, "volume": volume,
                "type": mt5.ORDER_TYPE_SELL if closing_long else mt5.ORDER_TYPE_BUY,
                "price": tick.bid if closing_long else tick.ask, "deviation": DEVIATION_POINTS, "magic": self.magic,
                "comment": self.comment, "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(market.raw),
            })
            self.log.info(f"CLOSE {p.symbol} {'long' if closing_long else 'short'} {volume:g} lots ({reason}) -> {outcome}")
            if done and volume >= p.volume:
                self.close_reasons[p.ticket] = reason
                # What the whole position made, costs in: its deals, now that it's closed.
                deals = mt5.history_deals_get(position=p.ticket) or ()
                pnl += sum(d.profit + d.swap + d.commission + d.fee for d in deals)
            if done and result is not None and result.volume:
                volume_filled += result.volume
                value_filled += result.volume * result.price
            if not done:
                self.close_problem = outcome
            if left is not None:
                left -= volume
            all_closed = all_closed and done
        if volume_filled:
            self.closing_fill = {"price": value_filled / volume_filled, "pnl": round(pnl, 2)}
        return all_closed

    def net_pnl(self, market: Market, position: Position, quote) -> float:
        """MT5's profit is at the closing price (so net of the spread) with the
        swap, but not the commission: its opening deal's (share CFDs:
        $0.02 a share each way) is counted, and the same again to close."""
        pnl = super().net_pnl(market, position, quote)
        if pnl is None:
            return None
        for p in position.raw or ():
            for d in mt5.history_deals_get(position=p.ticket) or ():
                if d.entry == mt5.DEAL_ENTRY_IN:
                    pnl += 2 * d.commission + d.fee
        return pnl

    def refs(self, position: Position) -> set:
        return {str(p.ticket) for p in position.raw or ()}

    # -- dashboard ------------------------------------------------------------------
    def report(self, markets: dict, notes: dict) -> None:
        try:
            account = mt5.account_info()
            positions = self._own_positions()
            now = int(time.time())
            deals = mt5.history_deals_get(now - 8 * 86400, now + 3 * 86400) or ()  # wide: the window is in server time
        except Exception:
            return
        if account is None:
            return
        opened = {d.position_id: d for d in deals if d.entry == mt5.DEAL_ENTRY_IN and d.magic == self.magic}
        closing = {}
        for deal in deals:
            if deal.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY) and deal.position_id in opened:
                closing.setdefault(deal.position_id, []).append(deal)
        trades = []
        for position_id, outs in closing.items():
            entry = opened[position_id]
            volume = sum(d.volume for d in outs)
            last_reason = outs[-1].reason
            trades.append({
                "ref": position_id, "symbol": entry.symbol,
                "direction": "long" if entry.type == mt5.DEAL_TYPE_BUY else "short", "size": entry.volume,
                "entryPrice": entry.price, "exitPrice": sum(d.price * d.volume for d in outs) / volume if volume else None,
                "openedAt": self._to_utc(entry.time), "closedAt": self._to_utc(outs[-1].time),
                "pnl": sum(d.profit + d.swap + d.commission + d.fee for d in outs) + entry.commission + entry.fee,
                "closeReason": (self.close_reasons.get(position_id) if last_reason == mt5.DEAL_REASON_EXPERT else None)
                or DEAL_REASONS.get(last_reason, "closed"),
                **self.exit_fields(position_id),  # the stagnancy timeout's, if it closed or watched it
            })
        self.dashboard.update(
            account={"balance": account.balance, "equity": account.equity, "unrealizedPl": account.profit},
            positions=[{
                "ref": p.ticket, "symbol": p.symbol, "direction": "long" if p.type == mt5.POSITION_TYPE_BUY else "short",
                "size": p.volume, "entryPrice": p.price_open, "currentPrice": p.price_current, "pnl": p.profit + p.swap,
                "stopLoss": p.sl or None, "takeProfit": p.tp or None, "openedAt": self._to_utc(p.time),
            } for p in positions if p.symbol in markets],
            trades=trades,
        )

    def shutdown(self) -> None:
        mt5.shutdown()
