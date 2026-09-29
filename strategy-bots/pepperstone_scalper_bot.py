#!/usr/bin/env python3
"""
Tick scalper (HFT-style) on the Pepperstone MT5 demo account.

The strategy, its settings and its risk rules are described in
engine/scalper.py and engine/runner.py, and in the README under
"Strategy bots". Run it in the pepperstone-bot-env Python environment:

    python strategy-bots/pepperstone_scalper_bot.py
"""

from engine.runner import main

if __name__ == "__main__":
    main("pepperstone", "scalper")
