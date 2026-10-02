#!/usr/bin/env python3
"""
Weekly ETF trend rotation on the Alpaca paper account: holds whichever of
35 funds (shares, bonds, property, commodities, currencies) are trending
up - EMA 50 above EMA 200 and up on a year ago - each sized by how much it
usually moves, and parks the rest in Treasury bills (BIL). Rebalanced once
a week. Buys only, no leverage.

The strategy, its settings and what the backtest says to expect are in
engine/rebalancer.py and in the README under "Portfolio bots". Run it in
the alpaca-bot-env Python environment:

    python strategy-bots/alpaca_etf_trend_bot.py
"""

from engine.runner import main

if __name__ == "__main__":
    main("alpaca", "etf-trend")
