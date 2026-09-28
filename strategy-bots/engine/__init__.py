"""
The strategy bots' shared engine: three CFD strategies written once
(strategies.py), run by one loop (runner.py) against any broker through a
small adapter per broker (brokers/). Each bot in strategy-bots/ is a
two-line script naming a broker and a strategy.
"""
