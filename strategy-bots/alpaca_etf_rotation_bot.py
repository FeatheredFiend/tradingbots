#!/usr/bin/env python3
"""
Monthly ETF rotation on the Alpaca paper account: splits its budget
between five funds (US shares, the rest of the world's, Treasuries,
commodities, property) and, once a month, parks any fund below its
10-month average in short-dated Treasuries (SHY). Buys only, no leverage.

The strategy, its settings and what the backtest says to expect are in
engine/rebalancer.py and in the README under "Portfolio bots". Run it in
the alpaca-bot-env Python environment:

    python strategy-bots/alpaca_etf_rotation_bot.py
"""

from engine.runner import main

if __name__ == "__main__":
    main("alpaca", "etf-rotation")
