#!/usr/bin/env python3
"""
Intraday momentum on the Nasdaq 100, on the Capital.com demo account.

The strategy, its settings and its risk rules are described in
engine/strategies.py and engine/runner.py, and in the README under
"Strategy bots". Run it in the ig-bot-env Python environment:

    python strategy-bots/capital_intraday_momentum_bot.py
"""

from engine.runner import main

if __name__ == "__main__":
    main("capital", "intraday-momentum")
