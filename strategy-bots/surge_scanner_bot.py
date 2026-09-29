#!/usr/bin/env python3
"""
Opening surge scanner: watches every liquid US share for surges in the
first minutes after the 09:30 New York open, through Alpaca's market data,
and passes each one to the surge followers. It places no trades itself.

The strategy and its settings are described in engine/surge.py and in the
README under "Opening surge". Run it in the alpaca-bot-env Python
environment, alongside one or more followers:

    python strategy-bots/surge_scanner_bot.py
"""

from engine.surge_scanner import main

if __name__ == "__main__":
    main()
