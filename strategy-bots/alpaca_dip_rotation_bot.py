#!/usr/bin/env python3
"""
60/40 dip rotation on the Alpaca paper account: 60% in a share fund (VTI)
and 40% in a bond fund (GOVT), all in shares during a short-term dip.

The strategy and its settings are described in engine/rebalancer.py and
in the README under "Portfolio bots". Run it in the alpaca-bot-env Python
environment:

    python strategy-bots/alpaca_dip_rotation_bot.py
"""

from engine.runner import main

if __name__ == "__main__":
    main("alpaca", "dip-rotation")
