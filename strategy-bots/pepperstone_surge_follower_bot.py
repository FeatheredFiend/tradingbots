#!/usr/bin/env python3
"""
Opening surge follower on the Pepperstone demo account (MetaTrader 5):
trades the US shares the surge scanner (surge_scanner_bot.py) signals at
the open, as share CFDs - long after a rise, short after a fall.

The strategy, its settings and its risk rules are described in
engine/surge.py and engine/runner.py, and in the README under "Opening
surge". Run it in the pepperstone-bot-env Python environment:

    python strategy-bots/pepperstone_surge_follower_bot.py
"""

from engine.runner import main

if __name__ == "__main__":
    main("pepperstone", "surge-follower")
