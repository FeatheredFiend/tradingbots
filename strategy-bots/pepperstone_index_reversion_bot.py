#!/usr/bin/env python3
"""
Index mean reversion on the Pepperstone MetaTrader 5 demo account.

The strategy, its settings and its risk rules are described in
engine/strategies.py and engine/runner.py, and in the README under
"Strategy bots". Run it in the pepperstone-bot-env Python environment:

    python strategy-bots/pepperstone_index_reversion_bot.py
"""

from engine.runner import main

if __name__ == "__main__":
    main("pepperstone", "index-reversion")
