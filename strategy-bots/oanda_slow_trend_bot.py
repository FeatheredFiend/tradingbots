#!/usr/bin/env python3
"""
Slow trend (daily) on the OANDA practice account: holds commodity CFDs
long, flat or short by their 50/200-day EMA trend and their move on a year
ago, sized by volatility, rebalanced once a day.

The strategy, its settings and what the backtest says to expect are in
engine/rebalancer.py and in the README under "Portfolio bots". Run it in
the ig-bot-env Python environment:

    python strategy-bots/oanda_slow_trend_bot.py
"""

from engine.runner import main

if __name__ == "__main__":
    main("oanda", "slow-trend")
