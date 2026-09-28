# tradingbots

Small, paper/demo-only trading bots against different brokers' APIs.
**Nothing in this repo trades real money.** The same two strategies run on
each broker; what differs is how small a trade can be:

| Broker | API access | Smallest trade | Runs on |
|---|---|---|---|
| Alpaca | free paper account | $1 of a US share | anywhere |
| OANDA | free practice account | 1 unit — about £1 of a currency pair | anywhere |
| Pepperstone | demo account on MetaTrader 5 | 0.01 lots — about £1,000 of a currency pair | Windows only |
| IG | needs an approved live account for the API key | IG's minimum — thousands, see [IG position sizing](#ig-position-sizing-both-ig-bots) | anywhere |

## EMA crossover strategy (`alpaca-ema-bot/`, `ig-cfd-ema-bot/`, `oanda-ema-bot/`, `pepperstone-ema-bot/`)

- Timeframe: 15-minute bars
- Buy: 9-period EMA crosses **above** the 21-period EMA
- Sell / close: 9-period EMA crosses **below** the 21-period EMA
- Risk management: 2% stop-loss / 5% take-profit from the position's entry price

The `*-momentum-scanner-bot/` folders run a different, higher-risk momentum
strategy — see the IG scanner's section below.

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
and is hardcoded to the demo account. Optional env vars: `STREAK_LENGTH`
(default `3`), `IG_POOL` (comma-separated override of the whole scanning
pool), and `STOP_LOSS_PERCENT` / `TAKE_PROFIT_PERCENT` (defaults `2` and
`5`, in percent of the entry price — e.g. `0.5` for 0.5%). IG has a minimum
stop/limit distance per market, so a very tight value can get a trade
rejected; the reason is logged.

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
  take-profit (adjustable with `STOP_LOSS_PERCENT` / `TAKE_PROFIT_PERCENT`,
  in percent — e.g. `0.5` for 0.5%)

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

### OANDA bots (`oanda-momentum-scanner-bot/`, `oanda-ema-bot/`)

The IG bots' two strategies on an [OANDA](https://www.oanda.com)
**practice** (demo) account, through OANDA's v20 REST API. Compared with IG:

- **No live account needed for the API.** Log into the practice account in
  the OANDA hub, then Tools > API > Generate.
- **Trades as small as one unit** (one unit of EUR/USD is one euro), so
  trades are sized from a small budget, not a broker minimum.
- **Free price history for every market**, so both bots use OANDA's own
  closed 15-minute bars: no warm-up, no bar file.
- Stop-loss/take-profit are attached to the order, as on IG.

```bash
pip install -r oanda-momentum-scanner-bot/requirements.txt   # just requests; oanda-ema-bot/ has the same file
export OANDA_API_TOKEN="your_practice_token"
export OANDA_ACCOUNT_ID="101-004-1234567-001"   # optional if the token can only see one account
python oanda-momentum-scanner-bot/oanda_momentum_scanner_bot.py
python oanda-ema-bot/oanda_ema_bot.py
```

**Sizing (both bots):** `OANDA_BUDGET` (default `100`, in the account's
currency) is split into `OANDA_MAX_POSITIONS` (default `5`) slices, and
each trade is worth one slice — so a 2% stop-loss on a £20 trade costs about
£0.40. The budget is **exposure** (what the positions are worth), not
margin: the default uses no leverage, and setting it above the balance uses
some. Each bot has its own budget. Sizes come from the budget, not the
balance, so the bots trade the same on OANDA's 100,000 practice balance as
on a real £100. A market whose smallest trade is worth more than a slice is
skipped at startup, with the reason logged: **at the default £100 only
currency pairs trade** — one unit of silver or oil is worth about £35–50,
and one unit of an index or of gold is worth thousands.

**Scanner:** the IG scanner's rules (3-bar streak, long and short, a reversal
closes) and the same `STREAK_LENGTH` / `STOP_LOSS_PERCENT` /
`TAKE_PROFIT_PERCENT` env vars — if those are set for the other scanners,
this one uses them too. `OANDA_POOL` overrides its pool: the IG scanner's 15
markets in OANDA's names (`EUR_USD`, `SPX500_USD`, `XAU_USD`, ...). A name
the account doesn't offer is skipped, with similar names it does offer.
Once `OANDA_MAX_POSITIONS` are open further signals are skipped; when
several markets signal at once, the biggest streak gets the slot first.

**EMA bot:** long only, fixed 2%/5% like the IG EMA bot. `OANDA_WATCHLIST`
defaults to eight currency pairs, since OANDA has no single-company shares.
A name the account doesn't offer stops it at startup.

**Both** act on each bar as it closes, so after a start nothing happens
until the next 15-minute bar closes. **Give each bot its own OANDA
sub-account** (add one in the hub; set `OANDA_ACCOUNT_ID` in each bot's
window): each treats every position in its markets as its own, and OANDA
nets buys and sells of one market into a single position, so two bots on
one account would close each other's trades.

### Pepperstone bots (`pepperstone-momentum-scanner-bot/`, `pepperstone-ema-bot/`) — Windows only

The IG bots' two strategies on a [Pepperstone](https://pepperstone.com)
**MetaTrader 5 demo** account. Pepperstone has no web API of its own; these
use MetaQuotes' `MetaTrader5` Python package, which drives the MT5 terminal
on the same PC. So they need **Windows**, Pepperstone's MT5 terminal, and a
demo account opened **on MetaTrader 5** — not MT4, cTrader or TradingView.

1. Open the demo account choosing MetaTrader 5, install Pepperstone's MT5
   terminal, and log into the demo account in it once.
2. Switch on **Algo Trading** on the terminal's toolbar. Without it the
   terminal refuses every order; the bots check this at startup.
3. Install and run (PowerShell):

```powershell
pip install -r pepperstone-momentum-scanner-bot/requirements.txt   # MetaTrader5 + numpy; pepperstone-ema-bot/ has the same file
python pepperstone-momentum-scanner-bot/pepperstone_momentum_scanner_bot.py
python pepperstone-ema-bot/pepperstone_ema_bot.py
```

The bots attach to whichever account the terminal is logged into (starting
the terminal if it's closed), or log in themselves when
`PEPPERSTONE_LOGIN`, `PEPPERSTONE_PASSWORD` and `PEPPERSTONE_SERVER` (the
server name in the login dialog, e.g. `Pepperstone-Demo`) are set. Set
`MT5_TERMINAL_PATH` to `terminal64.exe` if the package can't find the
terminal. **They exit unless the account is a demo account.**

**Sizing:** the same budget-slice scheme as the OANDA bots, with
`PEPPERSTONE_BUDGET` / `PEPPERSTONE_MAX_POSITIONS` — but the default budget
is **10,000** (5 slices of 2,000), because MT5's smallest trade is 0.01
lots: 1,000 units of a currency pair, roughly £750–£1,000 of exposure, and
more for gold. At £100 nothing would trade. Share CFDs trade in whole
shares (one Apple share is about £170).

**Scanner:** the same as the OANDA scanner. `PEPPERSTONE_POOL` overrides
the pool, in Pepperstone's MT5 names (`US500`, `GER40`, `XAUUSD`,
`SpotBrent`, `EURUSD`, ...).

**EMA bot:** here the IG bot's original watchlist idea — US shares — works,
since MT5 has price history for Pepperstone's share CFDs.
`PEPPERSTONE_WATCHLIST` defaults to
`AAPL.US,MSFT.US,AMZN.US,GOOGL.US,TSLA.US,NVDA.US,META.US` (the US-session
symbols; Pepperstone also lists 24-hour versions such as `AAPL.US-24`). A
name missing from the account, or matching several symbols, stops it at
startup, listing what it found.

**Both** accept a name that isn't exact if exactly one symbol starts with
it (brokers add suffixes). They tag their positions with their own magic
number and ignore all others, so they can share one account with each
other and with your manual trades — on a hedging account, that is. The
startup log shows the account type and warns if it isn't hedging.

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
minimum trade size is the floor. For genuinely small trades, see the
Alpaca bots (fractional shares) or the OANDA bots (single units).

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

## Backtest (`backtest/streak_backtest.py`)

Replays the scanners' 3-bar streak rule over past 15-minute bars for a grid
of stop-loss / take-profit percentages, to pick `STOP_LOSS_PERCENT` /
`TAKE_PROFIT_PERCENT` on evidence rather than guesswork:

```bash
python backtest/streak_backtest.py alpaca   # in alpaca-bot-env: ~90 days of the Alpaca scanner's US shares
python backtest/streak_backtest.py ig       # in ig-bot-env: last 500 bars of the IG scanner's 15 markets
```

Bars are cached in `backtest/data/` (gitignored), so reruns are free. The IG
run uses 7,500 of IG's 10,000-points-a-week price-history allowance and ~16
requests — stop the IG scanner first so the two don't overrun IG's
30-a-minute limit. First Alpaca run (30 Jun – 25 Sep 2026): the default 2% /
5% roughly broke even per trade; a 0.25–0.5% stop with a 2.5–5% take-profit
did best, and a 0.5% take-profit was worst at every stop. The edge was
tiny (~0.05% a trade) and assumes stops fill exactly at their level.

## Repo layout

Each bot is self-contained in its own folder with its own `requirements.txt`.
Virtual environments (`*-bot-env/`) are gitignored — create your own per
broker (both bots of a broker share the same requirements).
