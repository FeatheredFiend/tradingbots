# tradingbots

Small, paper/demo-only trading bots against different brokers' APIs.
**Nothing in this repo trades real money.** Only the Alpaca bots' trades are
genuinely small ($2 and $20 each); the IG bots trade IG's minimum size,
which is still big — see [IG position sizing](#ig-position-sizing-both-ig-bots).

## EMA crossover strategy (`alpaca-ema-bot/`, `ig-cfd-ema-bot/`)

- Timeframe: 15-minute bars
- Buy: 9-period EMA crosses **above** the 21-period EMA
- Sell / close: 9-period EMA crosses **below** the 21-period EMA
- Risk management: 2% stop-loss / 5% take-profit from the position's entry price

`ig-momentum-scanner-bot/` and `alpaca-momentum-scanner-bot/` run a
different, higher-risk momentum strategy — see their own sections below.

## Bots

### `alpaca-ema-bot/`

Trades a watchlist of symbols (equities and/or crypto, auto-detected) via
[Alpaca](https://alpaca.markets)'s **paper trading** API. Fixed $2.00
notional per trade using fractional shares/crypto. Stop-loss/take-profit are
enforced by the bot itself (Alpaca doesn't support attaching them to
fractional orders).

```bash
pip install -r alpaca-ema-bot/requirements.txt
export APCA_API_KEY_ID="your_paper_key_id"
export APCA_API_SECRET_KEY="your_paper_secret_key"
export BOT_SYMBOLS="AAPL,MSFT,AMZN,GOOGL,TSLA"   # optional, this is the default
python alpaca-ema-bot/alpaca_ema_bot.py
```

**Known issue:** `alpaca-trade-api` pins exact old versions of `PyYAML` and
`msgpack` that have no prebuilt wheel on very new Python versions (e.g. 3.14
on Windows), which makes a plain `pip install alpaca-trade-api` fail asking
for Microsoft C++ Build Tools. The pinned `requirements.txt` above uses
current, wheel-available versions instead (installed via `--no-deps` on the
package itself) and is known to work — if installing from scratch on a new
machine hits the same build error, install with:
```bash
pip install alpaca-trade-api --no-deps
pip install -r alpaca-ema-bot/requirements.txt
```

### `ig-cfd-ema-bot/`

Trades a watchlist of share CFDs via [IG Markets](https://www.ig.com)'
**demo** account (`trading-ig` library). Unlike Alpaca, IG's CFD orders
support native stop-loss/take-profit attached to the order itself, so risk
management is enforced by the broker, not by polling.

```bash
pip install -r ig-cfd-ema-bot/requirements.txt
export IG_USERNAME="your_ig_username"
export IG_PASSWORD="your_ig_password"
export IG_API_KEY="your_ig_api_key"
export IG_WATCHLIST="Apple,Microsoft,Amazon,Google:UB.D.GOOGL.CASH.IP,Tesla,BP,HSBC,Tesco,Vodafone,AstraZeneca"   # optional, this is the default
python ig-cfd-ema-bot/ig_cfd_ema_bot.py
```

**Requires a live IG account** to generate an API key (My IG > Settings >
API keys) — this is an IG platform requirement, not a choice made here. The
bot itself is hardcoded to `ACCOUNT_TYPE = "DEMO"` and never trades live.

**Every trade is IG's minimum size** for its market — the smallest trade IG
allows, but still thousands of pounds of exposure. See
[IG position sizing](#ig-position-sizing-both-ig-bots).

**It can't trade its default watchlist.** IG's API gives no prices for
shares at all — neither price history nor live prices (see the momentum
scanner below) — so every share in the watchlist fails. It would need a
watchlist of indices, commodities or FX instead.

**`IG_WATCHLIST` is a fixed list, not a screener** — the bot only ever
trades exactly what's named here (comma-separated company names, UK and US
freely mixed, each resolved to an IG "epic" at startup and then followed on
its own exchange's hours). When a name is ambiguous — multiple share
classes, leveraged ETPs, foreign cross-listings all matching the same
search — the bot refuses to guess and exits showing the candidates instead.
Pin the exact one with `Name:EPIC` syntax, e.g. `Google:UB.D.GOOGL.CASH.IP`
(used above because "Google" alone matches both Alphabet's Class A and
Class C shares as separate markets).

### `ig-momentum-scanner-bot/` — high-risk / experimental

A deliberately riskier third bot. Instead of a small named watchlist, it
scans a broad ~15-instrument pool (indices, commodities, FX majors — no
shares, see below) every cycle and trades whichever ones show a raw momentum
streak — no symbol is chosen in advance, the bot decides purely from the
data:

- Buy (open **long**): 3 consecutive higher closes in a row
- Sell (open **short**): 3 consecutive lower closes in a row
- A reversal streak closes an opposing position; same-direction streak while
  already positioned is a no-op

Unlike the other two bots, this one can go **short** — CFDs support it, and
that's a genuinely higher-risk capability than the long-only bots. It also
has no trend-confirmation smoothing (no EMA), so expect more false signals
and more round-trips hitting the stop-loss than the EMA bots. Same native
stop-loss/take-profit (2%/5%) as `ig-cfd-ema-bot/`, and the same catch on
trade size — see [IG position sizing](#ig-position-sizing-both-ig-bots).

**It builds its own 15-minute bars instead of fetching them.** IG's
price-history endpoint refuses every share with a 403
(`unauthorised.access.to.equity.exception` — IG's data vendors don't license
equity prices over the API) and caps everything else at 10,000 data points a
week, which a scanner polling ~15 markets would burn through in about half
an hour. So the bot samples each market's live bid/offer from the market
details it already reads every pass, and buckets those into 15-minute
closes. **Shares are left out entirely:** IG's market details carry no
bid/offer for them either, so the API gives no way to price a share at all.
The cost is a **~45-minute warm-up** (logged as
`warming up (n/4 bars)`) before a market can signal. Bars are saved to
`ig-momentum-scanner-bot/momentum_bars.json` as they change and reloaded at
startup, so a restart picks up where it left off — only the first start,
or one after a stop of more than 16 hours, has to warm up again. Delete
that file to force a fresh warm-up.

```bash
pip install -r ig-momentum-scanner-bot/requirements.txt
export IG_USERNAME="your_ig_username"
export IG_PASSWORD="your_ig_password"
export IG_API_KEY="your_ig_api_key"
python ig-momentum-scanner-bot/ig_momentum_scanner_bot.py
```

Requires the same live-IG-account-for-an-API-key step as `ig-cfd-ema-bot/`,
and is hardcoded to the demo account. `STREAK_LENGTH` (default `3`) and
`IG_POOL` (comma-separated override of the whole scanning pool) are
optional env vars.

**Resolution is intentionally looser here than `ig-cfd-ema-bot/`:** that bot
hard-exits on any ambiguous or unresolved name, because getting one specific
hand-picked symbol wrong matters. This bot's whole point is breadth, not
precision on any one name, so it *skips* (logs why, keeps going) any pool
entry that doesn't resolve cleanly instead of stopping the whole bot. When a
name has several plausible matches it prefers, in order: an exact name match
(so `GBP/EUR` doesn't become the inverse `EUR/GBP`), an undated market over
dated futures, the plainest name (`BP PLC` over `BP PLC - Pfd`), and the
**smallest contract size** — e.g. US 500 at £1 a point rather than $250, and
FX "Mini" contracts. It logs every candidate, so if a pick is wrong, change
that `DEFAULT_POOL` entry to `Name:EPIC`.

### `alpaca-momentum-scanner-bot/` — built for a $100 account, buys only

The IG momentum scanner's signal, moved to Alpaca so it can trade small
amounts: IG's smallest trade is thousands of pounds of exposure, while
Alpaca sells fractions of a US share from $1. It scans ~30 liquid US large
caps (override with `BOT_POOL`) on 15-minute bars during regular market
hours (9:30–16:00 New York, 14:30–21:00 UK):

- Buy: 3 consecutive higher closes in a row
- Sell (close): 3 consecutive lower closes in a row, a 2% stop-loss or a 5%
  take-profit

**Buys only.** Fractional shares can't be sold short on Alpaca, and short
selling needs a $2,000+ margin account, so a falling streak only ever closes
a position.

**Sizing:** a $100 budget (`BOT_BUDGET_USD`) split into 5 slices
(`BOT_MAX_POSITIONS`), so each buy is $20 and a stop-loss costs about $0.40.
Once 5 positions are open, further buy signals are skipped; when several
shares signal at once, the strongest rise gets the slot first. It sizes
from the budget, not the account, so it trades the same on Alpaca's default
$100,000 paper account as on a real $100 one — but there, losses don't
shrink the budget the way real ones would.

```bash
pip install -r alpaca-momentum-scanner-bot/requirements.txt   # same deps as alpaca-ema-bot/
export APCA_API_KEY_ID="your_paper_key_id"
export APCA_API_SECRET_KEY="your_paper_secret_key"
python alpaca-momentum-scanner-bot/alpaca_momentum_scanner_bot.py
```

Differences from the IG scanner worth knowing:

- **Real price history, no warm-up.** Alpaca's free IEX feed gives the
  whole pool's 15-minute bars in one request, and shares work.
- **Closed bars only.** The signal is judged on completed bars, so it can't
  flicker on and off within a bar, and a share is bought at most once per
  bar — a stop-loss doesn't immediately re-buy on the same streak.
- **Stop-loss and take-profit are checked by the bot** (every minute), not
  the broker: Alpaca can't attach them to fractional orders. They only work
  while the bot is running and the market is open, and positions are held
  overnight, so a gap at the next open can go well past 2%.

Don't run it alongside `alpaca-ema-bot/` on the same paper account — both
trade AAPL, MSFT and friends, and each would close the other's positions.

## IG position sizing (both IG bots)

Both IG bots trade **each market's minimum deal size** — the smallest trade
IG allows. An IG CFD trade is a number of contracts, each gaining or losing
a fixed amount per point the price moves, and every market has a minimum
number of contracts. (They used to aim for $2 of exposure, but that's far
below every minimum, and for currency pairs priced near 1 the formula
actually came out *above* the minimum — so they now just use it.)

That smallest trade is still big. For example, one £1-a-point FTSE 100
contract, with the index around 10,700, is roughly:

- **£10,700 of exposure** (10,700 points × £1)
- **£540 of margin** held while it's open (IG's 5% rate for major indices)
- **£215 lost** if the 2% stop-loss is hit (about 215 points × £1)

IG's minimum is typically in the region of one such contract; other
markets land in the same ballpark. Check a market's deal ticket on IG for
its exact minimum and margin.

**The bots never look at the account balance.** On a 10,000 demo balance
they work, but the scanner holding trades in many of its 15 markets at
once could tie up several thousand in margin. On 100 or less, IG would
reject trades for insufficient funds, or a single stop-loss could wipe the
account out. **These bots can't be scaled down to small amounts** — IG's
minimum trade size is the floor. For genuinely small trades, see
`alpaca-ema-bot/`, which buys fractional shares.

## IG rate limits (both IG bots)

IG allows only **~30 non-trading requests per minute, account-wide** —
shared by market search, market details, price bars and position reads. IG
answers an exceeded allowance with a 403, which `trading-ig`'s own pacing
doesn't catch, so an unpaced burst makes every later call fail too. Both IG
bots pace every non-trading call to stay under it, which means:

- The momentum bot takes about a minute to resolve its pool at startup, and
  a full pass over it takes well under a minute (fine for 15-minute bars).
- A startup lookup that fails is retried (after 20s, 40s, then 60s) rather
  than dropped, since it's nearly always a temporary 403 — e.g. from a bot
  you stopped less than a minute ago.
- **Each bot paces itself, but they don't coordinate.** Running both at once
  on the same account would overrun the limit together. Split it between
  them with `IG_REQUESTS_PER_MINUTE` (default `28`), set in each bot's own
  window, keeping the total under 30 — e.g. `18` for the scanner and `10`
  for the EMA bot.

## Repo layout

Each bot is self-contained in its own folder with its own `requirements.txt`.
Virtual environments (`*-bot-env/`) are gitignored — create your own per bot.
