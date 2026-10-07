#!/usr/bin/env python3
"""
Intraday momentum on the Nasdaq 100, on the Pepperstone demo account (MetaTrader 5).

The strategy, its settings and its risk rules are described in
engine/strategies.py and engine/runner.py, and in the README under
"Strategy bots". Run it in the pepperstone-bot-env Python environment:

    python strategy-bots/pepperstone_intraday_momentum_bot.py
"""

from engine.runner import main

if __name__ == "__main__":
    main("pepperstone", "intraday-momentum")
