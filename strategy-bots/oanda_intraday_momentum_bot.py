#!/usr/bin/env python3
"""
Intraday momentum on the Nasdaq 100, on the OANDA practice account.

The strategy, its settings and its risk rules are described in
engine/strategies.py and engine/runner.py, and in the README under
"Strategy bots". Run it in the ig-bot-env Python environment:

    python strategy-bots/oanda_intraday_momentum_bot.py
"""

from engine.runner import main

if __name__ == "__main__":
    main("oanda", "intraday-momentum")
