"""
Which bots can run at the same time without getting in each other's way,
for the launcher's "Tick safe set" button and its warnings on Start.

Two bots clash when they're on the same broker account, trade a market in
common, and one of them would act on the other's positions there:

- The original scanners and EMA bots (not Pepperstone's) treat every
  position in their markets as their own: they count it, close it on a
  reversal, and the Alpaca scanner puts its stop order on it.
- The portfolio bots hold their markets all the time, so a bot that only
  trades where nobody else has a position would never get to trade them.
- The strategy bots, surge followers and Pepperstone bots only touch their
  own trades and keep out of markets where another position is open, so
  they're fine next to each other.
- A surge follower trades whatever US share surges, so it may land in any
  market of a bot on its account.

IG adds one more limit: about 30 requests a minute for the whole account,
which each IG bot paces itself to with IG_REQUESTS_PER_MINUTE.

Standard library only, so the tests can import it without the launcher's
Windows-only packages.
"""

import itertools
from dataclasses import dataclass

ANY_SHARE = "*"                  # a surge follower's markets: whatever US share surges
IG_REQUEST_LIMIT = 30            # IG's non-trading requests a minute, for the whole account
IG_REQUEST_BUDGET = 28           # what the IG bots may use between them - a little under IG's limit
IG_DEFAULT_REQUESTS = 28         # each IG bot's own pace when IG_REQUESTS_PER_MINUTE is empty

# The original bots that treat every position in their markets as their own:
# (setting that overrides the markets, the bot's default markets). Kept in
# step with each bot's DEFAULT_POOL / DEFAULT_WATCHLIST by the tests.
CLAIMING_BOTS = {
    "oanda-momentum-scanner": ("OANDA_POOL", "UK100_GBP,SPX500_USD,US30_USD,NAS100_USD,DE30_EUR,JP225_USD,"
                                             "XAU_USD,XAG_USD,BCO_USD,WTICO_USD,"
                                             "EUR_USD,GBP_USD,USD_JPY,EUR_GBP,AUD_USD"),
    "oanda-ema-bot": ("OANDA_WATCHLIST", "EUR_USD,GBP_USD,USD_JPY,AUD_USD,EUR_GBP,USD_CAD,USD_CHF,NZD_USD"),
    "capital-momentum-scanner": ("CAPITAL_POOL", "UK100,US500,US30,US100,DE40,J225,GOLD,SILVER,OIL_BRENT,OIL_CRUDE,"
                                                 "EURUSD,GBPUSD,USDJPY,EURGBP,AUDUSD"),
    "capital-ema-bot": ("CAPITAL_WATCHLIST", "AAPL,MSFT,AMZN,GOOGL,TSLA,NVDA,META,NFLX"),
    "alpaca-momentum-scanner": ("BOT_POOL", "AAPL,MSFT,AMZN,GOOGL,META,NVDA,TSLA,NFLX,AMD,AVGO,"
                                            "ORCL,CRM,ADBE,INTC,JPM,BAC,V,MA,WMT,COST,"
                                            "KO,PEP,MCD,NKE,DIS,XOM,CVX,UNH,JNJ,PFE"),
    "alpaca-ema-bot": ("BOT_SYMBOLS", "AAPL,MSFT,AMZN,GOOGL,TSLA"),
    "ig-momentum-scanner": ("IG_POOL", "FTSE 100,US 500,Wall Street,US Tech 100,Germany 40,Japan 225,"
                                       "Spot Gold,Spot Silver,Oil - Brent Crude,Oil - US Crude,"
                                       "EUR/USD,GBP/USD,USD/JPY,GBP/EUR,AUD/USD"),
    "ig-ema-bot": ("IG_WATCHLIST", "Apple,Microsoft,Amazon,Google:UB.D.GOOGL.CASH.IP,Tesla,"
                                   "BP,HSBC,Tesco,Vodafone,AstraZeneca"),
}
# Original bots that can't work with their default markets: (setting, why).
UNWORKABLE_DEFAULTS = {
    "ig-ema-bot": ("IG_WATCHLIST", "IG gives no share prices over its API, so it can't trade its default share "
                                   "watchlist - set IG_WATCHLIST to other markets to use it"),
}
HOLDING_FAMILIES = ("slow-trend", "etf-rotation", "etf-trend")  # the portfolio bots
# When several sets fit the same number of bots: keep the portfolio bots
# (a tested edge), then the scanners, intraday momentum (tested too, but
# newer than the scanners it would push out), and the EMA bots last.
PRIORITY = ("slow-trend", "etf-rotation", "etf-trend", "momentum", "intraday-momentum", "surge", "session-breakout",
            "index-reversion", "commodity-trend", "scalper", "ema")
ACCOUNT_BROKERS = ("oanda", "capital")  # the brokers where a bot can have an account of its own


@dataclass(frozen=True)
class Footprint:
    """Where a bot trades, as far as clashes go."""
    key: str
    name: str
    broker: str
    family: str
    account: str             # the account id it's set to use, "" for the broker's only/preferred one
    markets: frozenset       # upper-case names, or {ANY_SHARE}
    claims: bool = False     # treats every position in its markets as its own
    holds: bool = False      # holds its markets all the time
    trades: bool = True      # False for the surge scanner, which only reads prices
    unusable: str = ""       # why it can't work with these settings, if it can't


def footprints(bots, env: dict, strategy_bots: dict) -> dict:
    """{bot key: Footprint} for the launcher's bots under the settings in
    `env` (upper-case names). `strategy_bots` is the launcher's STRATEGY_BOTS:
    {family: (setting prefix, {broker: default markets})}."""
    def setting(name: str) -> str:
        return (env.get(name) or "").strip()

    result = {}
    for bot in bots:
        broker_prefix = bot.broker.upper() + "_"
        account = setting(broker_prefix + "ACCOUNT_ID") if bot.broker in ACCOUNT_BROKERS else ""
        common = dict(key=bot.key, name=bot.name, broker=bot.broker, family=bot.family)
        if bot.key in CLAIMING_BOTS:
            name, default = CLAIMING_BOTS[bot.key]
            needs, why = UNWORKABLE_DEFAULTS.get(bot.key, ("", ""))
            result[bot.key] = Footprint(**common, account=account, markets=_markets(setting(name) or default),
                                        claims=True, unusable=why if needs and not setting(needs) else "")
        elif bot.family in strategy_bots:
            prefix, defaults = strategy_bots[bot.family]
            own = f"{broker_prefix}{prefix}_"
            if bot.broker in ACCOUNT_BROKERS:
                account = setting(own + "ACCOUNT_ID") or account
            result[bot.key] = Footprint(**common, account=account,
                                        markets=_markets(setting(own + "MARKETS") or defaults[bot.broker]),
                                        holds=bot.family in HOLDING_FAMILIES)
        elif bot.family == "surge":
            follower = bot.key.endswith("-follower")
            if follower and bot.broker in ACCOUNT_BROKERS:
                account = setting(broker_prefix + "SURGE_ACCOUNT_ID") or account
            result[bot.key] = Footprint(**common, account=account,
                                        markets=frozenset({ANY_SHARE}) if follower else frozenset(), trades=follower)
        else:
            # The Pepperstone scanner and EMA bot: magic numbers keep them to their own trades.
            result[bot.key] = Footprint(**common, account=account, markets=frozenset())
    return result


def _markets(text: str) -> frozenset:
    """Comparable market names: "Google:UB.D.GOOGL.CASH.IP" -> GOOGLE, "US 500@us" -> US 500."""
    names = (" ".join(m.split(":")[0].split("@")[0].split()).upper() for m in text.split(","))
    return frozenset(n for n in names if n)


def ig_requests(env: dict) -> int:
    """Each IG bot's requests a minute."""
    try:
        value = int(float((env.get("IG_REQUESTS_PER_MINUTE") or "").strip()))
    except ValueError:
        value = 0
    return value if value > 0 else IG_DEFAULT_REQUESTS


def ig_room(env: dict) -> int:
    """How many IG bots fit in IG's requests a minute at once."""
    return max(1, IG_REQUEST_BUDGET // ig_requests(env))


def clash(a: Footprint, b: Footprint):
    """Why `a` shouldn't run next to `b`, from a's side - or None if it can."""
    if a.key == b.key or a.broker != b.broker or a.account != b.account or not (a.trades and b.trades):
        return None
    where = f"same account ({a.account})" if a.account else "same account"
    if a.claims and ANY_SHARE in b.markets:
        return (f"treats every position in its {len(a.markets)} markets as its own, and {b.name} could land on any "
                f"of them ({where})")
    if b.claims and ANY_SHARE in a.markets:
        return f"could land on any of the {len(b.markets)} markets {b.name} treats as its own ({where})"
    shared = a.markets & b.markets
    if not shared:
        return None
    if a.claims:
        return f"would treat {b.name}'s positions in {_listed(shared)} as its own ({where})"
    if b.claims:
        return f"{b.name} would treat its positions in {_listed(shared)} as its own ({where})"
    if a.holds:
        return f"holds {_listed(shared)} all the time, so {b.name} would never get to trade them ({where})"
    if b.holds:
        return f"{b.name} holds {_listed(shared)} all the time, so it would never get to trade them ({where})"
    return None


def _listed(markets) -> str:
    names = sorted(markets)
    return ", ".join(names) if len(names) <= 5 else ", ".join(names[:4]) + f" and {len(names) - 4} more"


def _ig_note(env: dict, kept) -> str:
    room, pace = ig_room(env), ig_requests(env)
    return (f"IG allows about {IG_REQUEST_LIMIT} requests a minute for the whole account and each IG bot is set to "
            f"{pace} (IG_REQUESTS_PER_MINUTE), so {room} fit{'s' if room == 1 else ''} at once"
            + (f" - kept {', '.join(sorted(kept))}" if kept else "")
            + f". Lower the setting to run more: {IG_REQUEST_BUDGET // (room + 1)} each fits {room + 1}.")


def pick(prints: dict, env: dict, running=(), ticked=()) -> tuple:
    """The biggest set of bots that can all run at once, broker by broker.
    Ties go to the set with more of the running bots, then more of the
    ticked ones, then PRIORITY. Returns (chosen keys, {left-out key: why})."""
    running, ticked, room = set(running), set(ticked), ig_room(env)
    chosen = set()
    for broker in sorted({f.broker for f in prints.values()}):
        group = [f for f in prints.values() if f.broker == broker and f.trades and not f.unusable]
        subsets = (s for size in range(len(group) + 1) for s in itertools.combinations(group, size)
                   if _fits(s, room))
        best = max(subsets, key=lambda s: (len(s), sum(f.key in running for f in s), sum(f.key in ticked for f in s),
                                           sorted((-_rank(f) for f in s), reverse=True)))
        chosen.update(f.key for f in best)

    left_out = {}
    for f in prints.values():
        if f.key in chosen:
            continue
        if f.unusable:
            left_out[f.key] = f.unusable
            continue
        if not f.trades:
            if any(prints[k].family == f.family for k in chosen):
                chosen.add(f.key)  # the surge scanner, for the followers in the set
            else:
                left_out[f.key] = "it only finds surges for the surge followers, and none of them is in the set"
            continue
        why = next((w for k in sorted(chosen) if (w := clash(f, prints[k]))), None)
        if why is None and f.broker == "ig":
            why = _ig_note(env, [prints[k].name for k in chosen if prints[k].broker == "ig" and prints[k].trades])
        left_out[f.key] = why or "it doesn't fit with the rest"
    return chosen, left_out


def _fits(group, room: int) -> bool:
    if sum(f.broker == "ig" for f in group) > room:
        return False
    return not any(clash(a, b) for a, b in itertools.combinations(group, 2))


def _rank(f: Footprint) -> int:
    return PRIORITY.index(f.family) if f.family in PRIORITY else len(PRIORITY)


def problems(prints: dict, env: dict, new, running) -> list:
    """What would get in the way if the `new` bots started next to the
    `running` ones: ["<bot name>: why", ...]. Clashes only among the running
    bots are left out - they're already running."""
    new = [k for k in new if k in prints]
    everyone = sorted(set(new) | {k for k in running if k in prints})
    notes, seen = [], set()
    for a in new:
        for b in everyone:
            pair = frozenset((a, b))
            if pair in seen:
                continue
            seen.add(pair)
            why = clash(prints[a], prints[b])
            if why:
                notes.append(f"{prints[a].name}: {why}")
    ig = [k for k in everyone if prints[k].broker == "ig" and prints[k].trades]
    if len(ig) > ig_room(env) and any(k in new for k in ig):
        notes.append(f"{len(ig)} IG bots: " + _ig_note(env, []))
    return notes
