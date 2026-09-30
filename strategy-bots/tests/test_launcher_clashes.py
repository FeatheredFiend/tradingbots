"""
Tests for the launcher's "Tick safe set" (launcher/bot_clashes.py): which
bots clash on a shared account, the set it picks, the warnings on Start,
and that its copies of each bot's default markets match the bots' own. The
launcher's bot list is read from its source, so tkinter/psutil aren't needed.

    python -m unittest discover -s strategy-bots/tests
"""

import ast
import os
import sys
import unittest
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, "..", "..")
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "launcher"))

import bot_clashes  # noqa: E402
from engine import settings  # noqa: E402

Bot = namedtuple("Bot", "key name broker strategy script family")


def _assignments(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    return {node.targets[0].id: node.value for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}


LAUNCHER = _assignments(os.path.join(REPO, "launcher", "tradingbots_launcher.py"))
STRATEGY_BOTS = ast.literal_eval(LAUNCHER["STRATEGY_BOTS"])


def launcher_bots() -> list:
    """The launcher's BOTS, built the way it builds them."""
    brokers = ast.literal_eval(LAUNCHER["BROKERS"])
    bots = [Bot(*(ast.literal_eval(arg) for arg in call.args)) for call in LAUNCHER["CLASSIC_BOTS"].elts]
    bots += [Bot(f"{broker}-{family}", f"{broker}-{family}", broker, family, "", family)
             for broker in brokers for family, (_, markets) in STRATEGY_BOTS.items() if broker in markets]
    bots.append(Bot("surge-scanner", "surge-scanner", "alpaca", "", "", "surge"))
    bots += [Bot(f"{broker}-surge-follower", f"{broker}-surge-follower", broker, "", "", "surge")
             for broker in ast.literal_eval(LAUNCHER["SURGE_FOLLOWERS"])]
    return bots


BOTS = launcher_bots()
KEYS = {b.key for b in BOTS}

# The account layout worked out on 30 Sep 2026: OANDA strategy bots on the
# primary account with the slow trend on its own, Capital.com's four strategy
# bots and the surge follower each on an account of their own.
USER_ENV = {
    "OANDA_ACCOUNT_ID": "101-001", "OANDA_SLOW_TREND_ACCOUNT_ID": "101-002",
    "CAPITAL_ACCOUNT_ID": "cap-main", "CAPITAL_SURGE_ACCOUNT_ID": "cap-surge",
    **{f"CAPITAL_{p}_ACCOUNT_ID": "cap-strategy" for p in ("BREAKOUT", "REVERSION", "TREND", "SCALPER")},
}


def pick(env: dict, running=(), ticked=(), bots=BOTS):
    return bot_clashes.pick(bot_clashes.footprints(bots, env, STRATEGY_BOTS), env, running, ticked)


class TablesInStep(unittest.TestCase):
    FILES = {
        "oanda-momentum-scanner": ("oanda-momentum-scanner-bot/oanda_momentum_scanner_bot.py", "DEFAULT_POOL"),
        "oanda-ema-bot": ("oanda-ema-bot/oanda_ema_bot.py", "DEFAULT_WATCHLIST"),
        "capital-momentum-scanner": ("capital-momentum-scanner-bot/capital_momentum_scanner_bot.py", "DEFAULT_POOL"),
        "capital-ema-bot": ("capital-ema-bot/capital_ema_bot.py", "DEFAULT_WATCHLIST"),
        "alpaca-momentum-scanner": ("alpaca-momentum-scanner-bot/alpaca_momentum_scanner_bot.py", "DEFAULT_POOL"),
        "alpaca-ema-bot": ("alpaca-ema-bot/alpaca_ema_bot.py", "DEFAULT_WATCHLIST"),
        "ig-momentum-scanner": ("ig-momentum-scanner-bot/ig_momentum_scanner_bot.py", "DEFAULT_POOL"),
        "ig-ema-bot": ("ig-cfd-ema-bot/ig_cfd_ema_bot.py", "DEFAULT_WATCHLIST"),
    }

    def test_claiming_bots_defaults_match_the_bots(self):
        self.assertEqual(set(bot_clashes.CLAIMING_BOTS), set(self.FILES))
        for key, (path, name) in self.FILES.items():
            with self.subTest(key):
                full = os.path.join(REPO, path)
                default = ast.literal_eval(_assignments(full)[name])
                if isinstance(default, list):  # the IG scanner's (name, type) pairs
                    default = ",".join(term for term, _ in default)
                setting, ours = bot_clashes.CLAIMING_BOTS[key]
                self.assertEqual([m.strip() for m in ours.split(",")], [m.strip() for m in default.split(",")])
                with open(full, encoding="utf-8") as f:
                    self.assertIn(f'"{setting}"', f.read(), f"{path} doesn't read {setting}")

    def test_launcher_strategy_tables_match_the_engine(self):
        budgets = ast.literal_eval(LAUNCHER["STRATEGY_BOT_BUDGETS"])
        for family, (prefix, markets) in STRATEGY_BOTS.items():
            self.assertEqual(prefix, settings.STRATEGY_PREFIX[family])
            for broker, text in markets.items():
                with self.subTest(f"{broker}-{family}"):
                    self.assertEqual([m.strip() for m in text.split(",")],
                                     [m.strip() for m in settings.DEFAULT_MARKETS[(broker, family)].split(",")])
                    if broker != "ig":  # IG's always trade the minimum size, so the launcher has no budget for them
                        self.assertEqual(budgets.get((broker, family)), settings.DEFAULT_BUDGET[(broker, family)])

    def test_every_bot_has_a_footprint(self):
        self.assertEqual(set(bot_clashes.footprints(BOTS, {}, STRATEGY_BOTS)), KEYS)


class Clashes(unittest.TestCase):
    def prints(self, env=None):
        return bot_clashes.footprints(BOTS, env or USER_ENV, STRATEGY_BOTS)

    def test_market_names_compare_without_epics_or_sessions(self):
        self.assertEqual(bot_clashes._markets(" Google:UB.D.GOOGL.CASH.IP, US  500@us ,"), {"GOOGLE", "US 500"})

    def test_claiming_bot_and_strategy_bot_on_one_account(self):
        p = self.prints()
        why = bot_clashes.clash(p["oanda-scalper"], p["oanda-momentum-scanner"])
        self.assertIn("OANDA momentum scanner", why)
        self.assertIn("EUR_USD", why)
        self.assertIn("101-001", why)

    def test_own_account_ends_the_clash(self):
        p = self.prints({**USER_ENV, "OANDA_SCALPER_ACCOUNT_ID": "101-003"})
        self.assertIsNone(bot_clashes.clash(p["oanda-scalper"], p["oanda-momentum-scanner"]))

    def test_strategy_bots_share_happily(self):
        p = self.prints()
        self.assertIsNone(bot_clashes.clash(p["oanda-scalper"], p["oanda-session-breakout"]))

    def test_pepperstone_never_clashes(self):
        p = self.prints()
        pepperstone = [f for f in p.values() if f.broker == "pepperstone"]
        for a in pepperstone:
            for b in pepperstone:
                self.assertIsNone(bot_clashes.clash(a, b), (a.key, b.key))

    def test_surge_follower_and_share_bot(self):
        p = self.prints()
        self.assertIn("could land on any of the 30 markets Alpaca momentum scanner",
                      bot_clashes.clash(p["alpaca-surge-follower"], p["alpaca-momentum-scanner"]))
        self.assertIn("alpaca-surge-follower could land on any of them",
                      bot_clashes.clash(p["alpaca-ema-bot"], p["alpaca-surge-follower"]))
        # Only claiming bots mind a follower: the ETF rotation's funds are simply skipped by it.
        self.assertIsNone(bot_clashes.clash(p["alpaca-surge-follower"], p["alpaca-etf-rotation"]))

    def test_portfolio_bot_holding_a_shared_market(self):
        p = self.prints({**USER_ENV, "OANDA_SLOW_TREND_ACCOUNT_ID": ""})
        self.assertIn("all the time", bot_clashes.clash(p["oanda-commodity-trend"], p["oanda-slow-trend"]))
        self.assertIn("all the time", bot_clashes.clash(p["oanda-slow-trend"], p["oanda-commodity-trend"]))


class Pick(unittest.TestCase):
    def test_the_user_layout(self):
        chosen, left_out = pick(USER_ENV)
        self.assertEqual(set(left_out), {
            "oanda-momentum-scanner", "oanda-ema-bot",            # claim the OANDA strategy bots' markets
            "alpaca-ema-bot", "alpaca-surge-follower",            # the Alpaca scanner wins the tie
            "ig-ema-bot",                                         # no share prices on IG
            "ig-session-breakout", "ig-index-reversion", "ig-commodity-trend", "ig-scalper",  # the IG scanner's markets
        })
        self.assertEqual(chosen, KEYS - set(left_out))
        self.assertIn("surge-scanner", chosen)
        self.assertIn("IG momentum scanner", left_out["ig-scalper"])
        self.assertIn("share prices", left_out["ig-ema-bot"])
        self.assertIn("Alpaca momentum scanner", left_out["alpaca-ema-bot"])

    def test_ig_request_limit_named_when_markets_dont_clash(self):
        chosen, left_out = pick({"IG_POOL": "Japan 225"})
        self.assertIn("ig-momentum-scanner", chosen)
        self.assertIn("IG_REQUESTS_PER_MINUTE", left_out["ig-scalper"])
        self.assertIn("14 each fits 2", left_out["ig-scalper"])

    def test_chosen_bots_never_clash(self):
        for env in ({}, USER_ENV, {"IG_REQUESTS_PER_MINUTE": "9"}):
            p = bot_clashes.footprints(BOTS, env, STRATEGY_BOTS)
            chosen, _ = bot_clashes.pick(p, env)
            self.assertEqual(bot_clashes.problems(p, env, sorted(chosen), []), [], env)

    def test_no_accounts_of_their_own(self):
        chosen, left_out = pick({})
        # The four Capital.com strategy bots outnumber the scanner, so it's the one left out.
        self.assertIn("capital-momentum-scanner", left_out)
        self.assertTrue({"capital-session-breakout", "capital-scalper", "capital-surge-follower"} <= chosen)
        # The follower shares the EMA bot's account and could land on its shares; a tie, and PRIORITY keeps the follower.
        self.assertIn("capital-surge-follower", left_out["capital-ema-bot"])

    def test_the_ticked_bot_wins_a_tie(self):
        chosen, left_out = pick(USER_ENV, ticked=["alpaca-surge-follower"])
        self.assertIn("alpaca-surge-follower", chosen)
        self.assertIn("alpaca-momentum-scanner", left_out)

    def test_a_running_bot_beats_a_ticked_one(self):
        chosen, _ = pick(USER_ENV, running=["alpaca-ema-bot"], ticked=["alpaca-surge-follower"])
        self.assertIn("alpaca-ema-bot", chosen)
        self.assertFalse({"alpaca-surge-follower", "alpaca-momentum-scanner"} & chosen)

    def test_more_bots_beat_a_running_one(self):
        chosen, left_out = pick({}, running=["oanda-momentum-scanner"])
        self.assertIn("oanda-momentum-scanner", left_out)
        self.assertIn("oanda-session-breakout", chosen)

    def test_portfolio_bot_keeps_its_markets(self):
        chosen, left_out = pick({**USER_ENV, "OANDA_SLOW_TREND_ACCOUNT_ID": ""})
        self.assertIn("oanda-slow-trend", chosen)
        self.assertIn("all the time", left_out["oanda-commodity-trend"])

    def test_ig_room_follows_the_request_setting(self):
        for pace, room in (("", 1), ("28", 1), ("14", 2), ("9", 3), ("abc", 1)):
            chosen, _ = pick({"IG_REQUESTS_PER_MINUTE": pace})
            self.assertEqual(sum(k.startswith("ig-") for k in chosen), room, pace)

    def test_ig_ema_bot_with_markets_ig_can_price(self):
        chosen, left_out = pick({"IG_WATCHLIST": "EUR/GBP", "IG_REQUESTS_PER_MINUTE": "14"})
        self.assertNotIn("share prices", left_out.get("ig-ema-bot", ""))

    def test_surge_scanner_only_with_a_follower(self):
        bots = [b for b in BOTS if b.key in ("surge-scanner", "alpaca-momentum-scanner")]
        chosen, left_out = pick({}, bots=bots)
        self.assertEqual(chosen, {"alpaca-momentum-scanner"})
        self.assertIn("surge-scanner", left_out)


class Problems(unittest.TestCase):
    def problems(self, new, running, env=None):
        env = USER_ENV if env is None else env
        return bot_clashes.problems(bot_clashes.footprints(BOTS, env, STRATEGY_BOTS), env, new, running)

    def test_starting_a_clashing_bot(self):
        notes = self.problems(["oanda-momentum-scanner"], ["oanda-session-breakout"])
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("OANDA momentum scanner: "))
        self.assertIn("oanda-session-breakout", notes[0])

    def test_clashes_among_running_bots_alone_are_not_repeated(self):
        self.assertEqual(self.problems(["pepperstone-scalper"], ["oanda-momentum-scanner", "oanda-scalper"]), [])

    def test_ig_over_its_request_limit(self):
        notes = self.problems(["ig-scalper"], ["ig-session-breakout"])
        self.assertEqual(len(notes), 1)
        self.assertIn("IG_REQUESTS_PER_MINUTE", notes[0])
        self.assertEqual(self.problems(["ig-scalper"], ["ig-session-breakout"], {"IG_REQUESTS_PER_MINUTE": "14"}), [])


if __name__ == "__main__":
    unittest.main()
