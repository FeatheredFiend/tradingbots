#!/usr/bin/env python3
"""
Opening surge follower on the Alpaca paper account: buys the US shares the
surge scanner (surge_scanner_bot.py) signals rising at the open. Buys only.

The strategy, its settings and its risk rules are described in
engine/surge.py and engine/runner.py, and in the README under "Opening
surge". Run it in the alpaca-bot-env Python environment:

    python strategy-bots/alpaca_surge_follower_bot.py
"""

from engine.runner import main

if __name__ == "__main__":
    main("alpaca", "surge-follower")
