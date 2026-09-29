# tradingbots

Small, paper/demo-only trading bots against different brokers' APIs.
**Nothing in this repo trades real money.** The same strategies run on
each broker - a momentum scanner and an EMA crossover bot, plus four
[strategy bots](#strategy-bots-strategy-bots) (forex session breakout,
index mean reversion, commodity trend, and an "HFT-style" tick scalper) -
29 bots in all. What differs
between brokers is how small a trade can be:

| Broker | API access | Smallest trade | Runs on |
|---|---|---|---|
| Alpaca | free paper account | $1 of a US share | anywhere |
| OANDA | free practice account | 1 unit — about £1 of a currency pair | anywhere |
| Pepperstone | demo account on MetaTrader 5 | 0.01 lots — about £1,000 of a currency pair | Windows only |
| Capital.com | free demo account | a tenth of a share, a hundredth of an index — about £5–£110 | anywhere |
| IG | needs an approved live account for the API key | IG's minimum — thousands, see [IG position sizing](#ig-position-sizing-both-ig-bots) | anywhere |

## EMA crossover strategy (`alpaca-ema-bot/`, `ig-cfd-ema-bot/`, `oanda-ema-bot/`, `pepperstone-ema-bot/`, `capital-ema-bot/`)

- Timeframe: 15-minute bars by default. `EMA_TIMEFRAME` sets `M1`, `M5`,
  `M15` or `M30` (1, 5, 15 or 30 minutes) for every EMA bot
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

**It builds its own bars instead of fetching them.** IG's
price-history endpoint refuses every share with a 403
(`unauthorised.access.to.equity.exception` — IG's data vendors don't license
equity prices over the API) and caps everything else at 10,000 data points a
week, which a scanner polling ~15 markets would burn through in about half
an hour. So the bot samples each market's live bid/offer from the market
details it already reads every pass, and buckets those into one close per
bar. **Shares are left out entirely:** IG's market details carry no
bid/offer for them either, so the API gives no way to price a share at all.
The cost is a **4-bar warm-up** (45 minutes on 15-minute bars, logged as
`warming up (n/4 bars)`) before a market can signal. Bars are saved to
`ig-momentum-scanner-bot/momentum_bars.json` as they change and reloaded at
startup, so a restart picks up where it left off — only the first start,
or one after a stop of more than 16 hours or a change of bar length, has
to warm up again. Delete
that file to force a fresh warm-up.

```bash
pip install -r ig-momentum-scanner-bot/requirements.txt
export IG_USERNAME="your_ig_username"
export IG_PASSWORD="your_ig_password"
export IG_API_KEY="your_ig_api_key"
python ig-momentum-scanner-bot/ig_momentum_scanner_bot.py
```

Requires the same live-IG-account-for-an-API-key step as `ig-cfd-ema-bot/`,
and is hardcoded to the demo account. Optional env vars: `SCANNER_TIMEFRAME`
(bar length: `M1`, `M5`, `M15` or `M30`, default `M15`; shared by every
scanner), `STREAK_LENGTH` (default `3`), `IG_POOL` (comma-separated override of the whole scanning
pool), and `STOP_LOSS_PERCENT` / `TAKE_PROFIT_PERCENT` (defaults `2` and
`5`, in percent of the entry price — e.g. `0.5` for 0.5%). IG has a minimum
stop/limit distance per market, so a very tight value can get a trade
rejected; the reason is logged. A pass samples each market once and IG's
request limit stretches a pass to about 35 seconds for the default pool,
so a 1-minute bar gets a sample or two, and one a slow pass misses is skipped. Use 5 minutes or
longer here; the bot warns at startup when the bars are too short.

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
caps (override with `BOT_POOL`) on 15-minute bars (`SCANNER_TIMEFRAME`)
during regular market hours (9:30–16:00 New York, 14:30–21:00 UK):

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
  whole pool's bars in one request, and shares work. IEX is a small slice
  of US trading, so on 1-minute bars some minutes have no trade and no bar;
  a streak then runs across the gap.
- **Closed bars only.** The signal is judged on completed bars, so it can't
  flicker on and off within a bar, and a share is bought at most once per
  bar — a stop-loss doesn't immediately re-buy on the same streak.
- **Stop-loss and take-profit are checked by the bot** (every minute, or
  every 15 seconds on 1-minute bars), not
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
  closed bars: no warm-up, no bar file.
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
currency pairs trade.** OANDA sells fractions of a unit of indices and gold,
but even their smallest trades are bigger than a £20 slice — in September
2026 about £60 for the US 500, £45–80 for silver and oil, £200–500 for the
other indices and gold, and £1,100 for the UK 100. An `OANDA_BUDGET` of
`5500` (slices of £1,100) takes in the scanner's whole pool.

**Scanner:** the IG scanner's rules (3-bar streak, long and short, a reversal
closes) and the same `SCANNER_TIMEFRAME` / `STREAK_LENGTH` /
`STOP_LOSS_PERCENT` / `TAKE_PROFIT_PERCENT` env vars — if those are set for the other scanners,
this one uses them too. `OANDA_POOL` overrides its pool: the IG scanner's 15
markets in OANDA's names (`EUR_USD`, `SPX500_USD`, `XAU_USD`, ...). A name
the account doesn't offer is skipped, with similar names it does offer.
Once `OANDA_MAX_POSITIONS` are open further signals are skipped; when
several markets signal at once, the biggest streak gets the slot first.

**EMA bot:** long only, fixed 2%/5% like the IG EMA bot. `OANDA_WATCHLIST`
defaults to eight currency pairs, since OANDA has no single-company shares.
A name the account doesn't offer stops it at startup.

**Both** act on each bar as it closes, so after a start nothing happens
until the next bar closes. **Give each bot its own OANDA
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
server name in the login dialog, e.g. `PepperstoneUK-Demo`) are set. Set
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

### Capital.com bots (`capital-momentum-scanner-bot/`, `capital-ema-bot/`)

The IG bots' two strategies on a [Capital.com](https://capital.com)
**demo** account, through Capital.com's REST API. Compared with IG:

- **No live account needed for the API.** Turn on two-factor login, then
  Settings > API integrations > Generate new key, giving the key its own
  password.
- **Small trades:** a hundredth of the US 500, a tenth of a share, 100
  units of a currency pair — so trades are sized from a budget, not a
  broker minimum.
- **Free price history for everything, shares included**, so both bots use
  Capital.com's own closed bars (no warm-up, no bar file), and
  the EMA bot can trade the US shares the IG bot never could.
- Stop-loss/take-profit are attached to the order, as on IG.

```powershell
pip install -r capital-momentum-scanner-bot/requirements.txt   # just requests; capital-ema-bot/ has the same file
[Environment]::SetEnvironmentVariable("CAPITAL_API_KEY", "the key", "User")
[Environment]::SetEnvironmentVariable("CAPITAL_EMAIL", "your login email", "User")
[Environment]::SetEnvironmentVariable("CAPITAL_EMAIL_PASSWORD", "the key's password", "User")
python capital-momentum-scanner-bot/capital_momentum_scanner_bot.py
python capital-ema-bot/capital_ema_bot.py
```

`CAPITAL_ACCOUNT_ID` picks an account other than the login's preferred
one. **They only ever use the demo endpoint.**

**Sizing:** the same budget-slice scheme as the OANDA bots, with
`CAPITAL_BUDGET` / `CAPITAL_MAX_POSITIONS`. The default budget is **600**
(5 slices of £120), about the smallest that takes in the scanner's whole
pool: in September 2026 the smallest trades were about £107 for the UK 100
(0.01 contracts), £55–£100 for 100 units of a currency pair, £25–£70 for
the other indices, gold, silver and oil, and up to about £25 for a US share. The
budget is exposure, not margin — a £120 index trade ties up about £6.

**Scanner:** the same as the OANDA scanner. `CAPITAL_POOL` overrides the
pool, as Capital.com epics (`UK100`, `US500`, `DE40`, `GOLD`, `OIL_BRENT`,
`EURUSD`, ...); an epic it doesn't have is skipped, with similar markets.

**EMA bot:** long only, fixed 2%/5%. `CAPITAL_WATCHLIST` defaults to
`AAPL,MSFT,AMZN,GOOGL,TSLA,NVDA,META,NFLX` — US shares, which trade
14:30–21:00 UK time. An epic Capital.com doesn't have stops it at startup.

**Both** treat every position in their markets as their own. Their default
markets don't overlap, so they can share one account (the dashboard then
shows the same balance for both); for separate balances, add a second demo
account on Capital.com and set `CAPITAL_ACCOUNT_ID` per bot. Capital.com
has no list of closed trades, so for the dashboard they're pieced together
from the activity history (entry, exit, and whether a stop-loss or
take-profit closed it) and the transaction history (the realised profit).

### Overnight: the CFD scanners close before the rollover (`shared/rollover.py`)

A CFD position still open at the daily rollover (17:00 New York: 22:00 UK
most of the year, 21:00 in the weeks the UK and US change their clocks on
different dates) is charged a night's financing. In September 2026 an index
long cost about £1.70-£2.20 a night per £10,000 of exposure, and a Brent
short at OANDA over £13. That's about one or two spreads, and a 3-bar streak
is long over by then. So by default the OANDA, Pepperstone, Capital.com and
IG scanners:

- **close the trades they opened 15 minutes before the rollover**
  (`SCANNER_FLAT_MINUTES`, default `15`: 21:45 UK), and
- **open nothing from an hour before it until 45 minutes after**
  (`SCANNER_LAST_ENTRY_MINUTES`, default `60`: 21:00-22:45 UK), when spreads are at their widest.

`0` switches either part off; both `0` holds overnight as before. The
startup log shows tonight's times. The Alpaca scanner buys shares with
cash, which carries no overnight charge, so it isn't affected.

**Only the scanner's own trades are closed.** Accounts are shared with other
bots (the commodity-trend bot holds overnight on purpose) and with trades
made by hand. Pepperstone's scanner knows its trades by magic number. The
OANDA, Capital.com and IG scanners save the broker's ID for each trade they
open to `own_trades.json` in their folder (gitignored). A trade opened
before that file existed, or with the file deleted, is left to its
stop-loss / take-profit as before. Reversal closes still treat every
position in the scanner's markets as its own, as described above.

## Strategy bots (`strategy-bots/`)

Four strategies, each on every broker - 19 bots, since Alpaca has no
forex. Unlike the bots above, the strategies are written once
(`strategy-bots/engine/strategies.py`, and `engine/scalper.py` for the
tick scalper), run by one loop (`engine/runner.py`) and reach each broker
through a small adapter (`engine/brokers/`) built from that broker's
existing bot. Each bot is a two-line script naming its broker and strategy:

| | OANDA | Pepperstone | Capital.com | IG | Alpaca |
|---|---|---|---|---|---|
| Forex session breakout | `oanda_session_breakout_bot.py` | `pepperstone_…` | `capital_…` | `ig_…` | - (no forex) |
| Index mean reversion | `oanda_index_reversion_bot.py` | `pepperstone_…` | `capital_…` | `ig_…` | SPY, QQQ, DIA, IWM |
| Commodity trend (4H/15M) | `oanda_commodity_trend_bot.py` | `pepperstone_…` | `capital_…` | `ig_…` | GLD, SLV, USO |
| Tick scalper (HFT-style) | `oanda_scalper_bot.py` | `pepperstone_…` | `capital_…` | `ig_…` | SPY, QQQ (buys only) |

Run them from the launcher, or in the broker's Python environment (the same
ones as the other bots - nothing new to install):

```powershell
python strategy-bots\oanda_session_breakout_bot.py        # ig-bot-env (OANDA, Capital.com and IG)
python strategy-bots\pepperstone_commodity_trend_bot.py   # pepperstone-bot-env
python strategy-bots\alpaca_index_reversion_bot.py        # alpaca-bot-env
```

They use the broker keys already set for the other bots. On the dashboard
they appear as e.g. `oanda-session-breakout`. **Set `STRATEGY_DRY_RUN=1`**
to have them log every trade they would make without sending it.

All times below are UK (London) time unless said otherwise, with summer
time handled; "ATR" is the 14-bar average true range, "R" the stop
distance. Every number in brackets is a setting's default (see [Settings](#settings)).

### 1. Forex session breakout (`BREAKOUT_*`)

**Concept.** High-volatility currency pairs (GBP/USD, EUR/USD, GBP/JPY,
EUR/JPY) tend to coil through the London morning and break out when New
York arrives. The bot trades that break during the London / New York
overlap only - never in the quiet Asian session - and is always flat
before the daily rollover.

**Indicators and setup.** 15-minute bars (`TIMEFRAME`, M15). Range = the
highest high (RH) and lowest low (RL) of the bars from `RANGE_START` to
`RANGE_END` (07:00-13:00); width W = RH - RL. ATR(14) on the same bars.
Trend filter EMA(`TREND_EMA`, 50) of the closes (0 turns it off).

**Entry and exit.**
- Filter: W is `MIN_RANGE_PERCENT`-`MAX_RANGE_PERCENT` of the price
  (0.15-1.0%); a long needs the close above the EMA, a short below it; at
  most `MAX_TRADES_PER_DAY` (1) per pair.
- Trigger: a bar that starts at or after 13:00 and closes by `ENTRY_END`
  (16:00) closes above RH + `BUFFER_ATR` x ATR (0.2) -> **long**, or below
  RL - 0.2 x ATR -> **short** - unless the close is already more than
  `MAX_EXTENSION` x W (0.5) past the edge (no chasing).
- Stop-loss: `STOP_RANGE_FRACTION` x W (0.5) back inside the range from
  the broken edge - its midpoint by default.
- Take-profit: `REWARD_RISK` (1.5) x the stop distance from the fill.
- Time exit: everything closes at `FLAT_TIME` (20:00).

**CFD risk controls.** Flat before the 17:00 New York rollover (22:00 UK,
21:00 for the few weeks a year the UK and US clocks differ), so no swap is
ever paid - the startup log shows each pair's rate anyway. Spread at most
`MAX_SPREAD_PERCENT` (10%) of the stop distance. Sizing: see below.

### 2. Index mean reversion (`REVERSION_*`)

**Concept.** Major indices (S&P 500, FTSE 100, DAX) overshoot intraday and
drift back to the day's volume-weighted mean. The bot fades stretched moves
during each index's own cash session, and closes every trade the same day.

**Indicators and setup.** 15-minute bars. Session VWAP from the cash open
(New York 09:30-16:00, London 08:00-16:30, Frankfurt 09:00-17:30, Tokyo
09:00-15:00, chosen from the market's name - or add `@us`, `@uk`, `@eu` or
`@jp` to it in the market list): VWAP = sum(typical price x volume) /
sum(volume), typical = (H + L + C) / 3, volume = the broker's tick count.
Sigma = the volume-weighted standard deviation of typical price around it.
Bands VWAP +/- `BAND_STDEV` (2) x sigma. RSI(`RSI_PERIOD`, 14), ADX(14),
ATR(14).

**Entry and exit.**
- Filter: no entries in the first `SKIP_OPEN_MINUTES` (60) or the last
  `LAST_ENTRY_MINUTES` (60) of the session; ADX at most `MAX_ADX` (25) -
  a ranging day, not a trending one (0 = off); at most
  `MAX_TRADES_PER_DAY` (2) per index.
- Trigger: a bar closes at or below the lower band with RSI at or below
  `RSI_OVERSOLD` (30) -> **long**; at or above the upper band with RSI at or
  above `RSI_OVERBOUGHT` (70) -> **short**.
- Stop-loss: `STOP_ATR` (1.5) x ATR from the close.
- Take-profit: the VWAP at entry; skipped unless that's at least
  `MIN_REWARD_RISK` (1) x the stop distance away.
- Exits: a bar closing back through the (moving) VWAP; `MAX_HOLD_BARS` (8)
  bars without reverting; and `FLAT_MINUTES` (15) before the cash close,
  whatever happens.

**CFD risk controls.** Never held overnight, so no swap. Spread at most 10%
of the stop distance. Risk 0.5% of the budget per trade (`RISK_PERCENT`).

### 3. Commodity multi-timeframe trend (`TREND_*`)

**Concept.** Gold and Brent crude trend for days. The bot confirms the
trend on 4-hour bars and enters on a 15-minute crossover in its direction,
then trails it - and, since it holds overnight, watches the swap.

**Indicators and setup.** Trend (`HIGHER_TIMEFRAME`, H4): EMA(`HTF_FAST_EMA`,
50), EMA(`HTF_SLOW_EMA`, 200), ADX(14). Entry (`TIMEFRAME`, M15):
EMA(`FAST_EMA`, 9), EMA(`SLOW_EMA`, 21), ATR(14).

**Entry and exit.**
- Filter: 4-hour uptrend = close above EMA 200, EMA 50 above EMA 200 and
  ADX at least `MIN_ADX` (20; 0 = off); a downtrend is the mirror. At most
  `MAX_TRADES_PER_DAY` (2) per market.
- Trigger: EMA 9 crosses above EMA 21 on the latest closed 15-minute bar
  in an uptrend -> **long**; below, in a downtrend -> **short**.
- Stop-loss: `STOP_ATR` (2) x the 15-minute ATR from the close.
- Take-profit: `REWARD_RISK` (3) x the stop distance (0 = none).
- Exits: a 15-minute close more than `TRAIL_ATR` (3) x ATR back from the
  best price since entry (a chandelier trailing stop; 0 = off); a 4-hour
  close back through EMA 200; `MAX_HOLD_DAYS` (10) days held.

**CFD risk controls.** An entry is skipped when that direction's overnight
financing costs more than `MAX_SWAP_PERCENT` (0.05%) of the trade's value a
night - read from the broker (OANDA's yearly rates, Capital.com's daily
fee, MT5's swap settings; IG's isn't available). With `WEEKEND_FLAT` on
(1, the default) there are no entries after Friday 16:00 and everything
closes Friday 20:00 - no weekend swap or Monday gap. Spread at most 10% of
the stop distance.

### 4. Tick scalper - "HFT-style" (`SCALPER_*`)

**What it is, and isn't.** Real high-frequency trading means reacting in
microseconds from servers inside the exchange, reading the order book and
being paid rebates for providing liquidity. None of that is possible through
a retail broker's API: every price read and every order here is a web
request taking a tenth of a second or more, and the broker sets its own
spread. This bot is the nearest thing that is possible. It reads live
bid/ask prices every few seconds instead of waiting for bars, trades short
bursts, and is out again within minutes. **Backtested, it loses about one
spread a trade** - see [What to expect](#what-to-expect). It's here to find
out whether the demo accounts agree.

**Concept.** A sharp move over the last minute sometimes keeps going for a
few more seconds. Everything is measured in spreads, because the spread is
what a scalper has to beat: "U", the usual spread, is the median bid/ask
spread of the last five minutes' reads (at least one price step).

**Reads and setup.** One read of every market's bid and ask every
`POLL_SECONDS` (2; IG at least 10, because of its request allowance). No
bars and no price history, so nothing is fetched at startup, and on IG none
of the 10,000-points-a-week allowance is used. A gap in the reads (a slow
broker, a sleeping PC) starts the history again. Trading starts once each
market has `WINDOW_SECONDS` (60) of reads.

**Entry and exit.**
- Filter: weekdays from `SESSION_START` to `SESSION_END` (07:00-21:00
  London - London and New York, never the thin Asian hours); no entry while
  the spread is over `MAX_SPREAD_RATIO` (1.5) x U (spreads widen on news,
  just when bursts show up); `COOLDOWN_SECONDS` (60) after a signal before
  the next one in that market; at most `MAX_TRADES_PER_DAY` (30) per market.
- Trigger: the mid price has moved at least `TRIGGER_SPREADS` (4) x U over
  the last `WINDOW_SECONDS` (60) and is at the window's high (after a rise)
  or low (after a fall) right now. `MODE` `momentum` (the default) goes
  with the burst -> **long** after a rise, **short** after a fall;
  `reversion` fades it.
- Stop-loss `STOP_SPREADS` (3) x U and take-profit `TAKE_PROFIT_SPREADS` (3)
  x U from the fill. The bot checks both on every read, and they also go on
  the order as the broker's own stop-loss and take-profit, pushed out to
  the broker's minimum distance where it has one (IG: 2 pips on EUR/USD,
  1 point on the US 500; Capital.com: 0.01% of the price). Those keep the
  trade protected if the bot stops.
- Time exit: `MAX_HOLD_SECONDS` (300) after the entry, and anything still
  open at `SESSION_END`. Never held overnight, so no swap.

**CFD risk controls.** Sizing, slots and "own trades only" as below (MT5
magic number 928006), with `RISK_PERCENT` 0.5. `MAX_SPREAD_PERCENT`
defaults to 60 here, not 10: with the stop only 3 spreads away, the spread
is always a third of it, and `MAX_SPREAD_RATIO` is the real spread filter.
Markets: EUR/USD, GBP/USD, USD/JPY and the S&P 500 (IG: EUR/USD and the US
500; Alpaca: SPY and QQQ, buys only, stops checked by the bot, US market
hours only).

### Risk rules for all four

- **Sizing.** Each bot trades a budget (`<BROKER>_<STRATEGY>_BUDGET`) as if
  it were its whole account. A trade risks `RISK_PERCENT` of it between the
  fill and the stop: `size = budget x RISK_PERCENT / (stop distance x value
  of one unit per point)`, capped so it's worth no more than `budget x
  MAX_LEVERAGE / max positions` (leverage 5), then rounded **down** to a
  size the broker accepts. A market whose smallest trade is over that cap
  is skipped at startup; one where it would risk too much is skipped when
  it signals, with the sums in the log. The defaults are sized so every
  default market fits: OANDA 100 / 1,000 / 300 / 100 (breakout / index /
  commodity / scalper - OANDA's UK 100 minimum is ~£1,100 of exposure),
  Pepperstone 1,000 / 2,000 / 2,000 / 1,000 (MT5's 0.01-lot minimum),
  Capital.com 100 / 200 / 100 / 100, Alpaca $100 each (no leverage -
  fractional buys can't use margin).
  **IG always trades each market's minimum size**, as its other bots do.
- **Rollover.** No new trades from 15 minutes before to 45 minutes after
  the 17:00 New York rollover, when spreads blow out.
- **Slots.** At most `<BROKER>_<STRATEGY>_MAX_POSITIONS` (2) open; when
  several markets signal on the same bar, the strongest goes first.
- **Only its own trades.** A bot manages only the trades it opened - it
  remembers the broker's trade/deal ids (on MT5, its magic number:
  928003 / 928004 / 928005 / 928006). Any other position in one of its markets -
  another bot's, or yours - is left alone, and the bot doesn't trade that
  market while it's open, because OANDA, Capital.com (unless in hedging
  mode) and Alpaca would net the two together. To run a strategy bot
  alongside another bot in the same markets, give it a sub-account:
  `OANDA_<STRATEGY>_ACCOUNT_ID` / `CAPITAL_<STRATEGY>_ACCOUNT_ID`.
- **New bars only.** Nothing is traded on a bar that closed before the bot
  started, so after a start nothing opens until the next bar closes (the
  scalper: until it has a full window of its own reads).
- **Notes survive restarts.** Each bot keeps its trade ids, the entry /
  stop / best price of its open positions and the day's trade counts in
  `strategy-bots/state/<bot>.json` (not in git). Delete a bot's file and it
  forgets which open trades are its own - they still have their broker
  stop-loss and take-profit.

### Settings

Strategy settings are shared by that strategy on every broker - the
names in the sections above, prefixed `BREAKOUT_`, `REVERSION_`, `TREND_`
or `SCALPER_`, plus `RISK_PERCENT`, `MAX_LEVERAGE` and `MAX_SPREAD_PERCENT` for
each. Each bot also has its own:

| Setting | Meaning |
|---|---|
| `<BROKER>_<STRATEGY>_MARKETS` | Comma-separated, in the broker's names - `OANDA_BREAKOUT_MARKETS=GBP_USD,EUR_USD` |
| `<BROKER>_<STRATEGY>_BUDGET` | The bot's money, in the account's currency (not IG) |
| `<BROKER>_<STRATEGY>_MAX_POSITIONS` | Default 2 |
| `<BROKER>_<STRATEGY>_ACCOUNT_ID` | OANDA and Capital.com: a sub-account of its own |

`<BROKER>` is `OANDA`, `PEPPERSTONE`, `CAPITAL`, `IG` or `ALPACA`;
`<STRATEGY>` is `BREAKOUT`, `REVERSION`, `TREND` or `SCALPER`. Every one of them is
on the launcher's Settings tab, with its default. A setting that doesn't
make sense (a letter in a number, times out of order) stops the bot at
startup, naming it.

### Broker notes

- **OANDA / Capital.com:** trade sizes small enough for the default
  budgets; give each bot its own sub-account if it shares markets with
  another bot.
- **Pepperstone:** needs the MT5 terminal, like the other Pepperstone bots;
  its bars are MT5's (bid prices, converted from the server's clock - New
  York time + 7 hours - to UTC).
- **IG:** minimum-size trades, and **price history is rationed to 10,000
  data points a week** for the whole account. A strategy bot uses ~120-450
  points to start (per market: its history once) and then ~2 per market per
  bar - about 2,000-4,000 a week each - so run one or two at a time, not all
  three alongside the backtest. The bot warns in its log when fewer than
  1,500 are left. (On 29 September 2026 only ~300 were left until 5 October.)
  Untested beyond logging in and reading markets, prices and positions.
  The scalper uses no price history, but it does use IG's ~30 requests a
  minute: two per read (prices, positions), so ~12 a minute at its 10-second
  floor - leave that much of `IG_REQUESTS_PER_MINUTE` for it.
- **Alpaca:** US-listed funds stand in for the indices and commodities;
  buys only (a short signal is logged and skipped); stop-loss and
  take-profit checked by the bot every 30 seconds, only while it runs and
  the market is open; regular hours only, with "4-hour" bars built from the
  day's 15-minute bars from the 09:30 open (09:30-13:30 and 13:30-16:00).

### What to expect

A replay of OANDA's last 5,000 15-minute bars (about ten weeks to 28
September 2026) through the three strategies with their default settings,
bar by bar, checking only whether the stop or the target was hit first -
no spread, and none of the trailing, VWAP or trend exits:

| Strategy | Trades | Result |
|---|---|---|
| Breakout: GBP/USD, EUR/USD, GBP/JPY, EUR/JPY | 89 (19-27 per pair) | -6.2R in all (-4.9R to +4.0R per pair), about -0.07R a trade |
| Index reversion: S&P 500, FTSE 100, DAX | 34 (4-18 per index) | -4.0R in all (-5.3R to +2.3R per index) |
| Commodity trend: gold, Brent | 101 (40 and 61) | -5.0R in all (0R and -5.0R) |

In other words: they trade at sensible rates and do what they say, but
there's no evidence of an edge yet - the same finding as the momentum
scanner's backtest. Treat the demo accounts as the experiment.

**The tick scalper** has a backtest of its own on real bid/ask prices,
`backtest/scalper_backtest.py` (see [Scalper backtest](#scalper-backtest-backtestscalper_backtestpy)).
For the week of 22-29 September 2026, 07:00-21:00 London, on EUR/USD,
GBP/USD, USD/JPY and the S&P 500, with each broker's history adjusted to
the spread its demo account quotes:

| | OANDA (5-second prices) | Pepperstone (every tick, read every 2s) |
|---|---|---|
| Default settings (momentum, trigger 4, stop 3 / take-profit 3) | 432 trades, 33% won, -0.33R a trade | 506 trades, 32% won, -0.35R a trade |
| ... in spreads per trade: after / before the spread | -1.00 / +0.03 | -1.04 / -0.01 |
| All 24 settings tried (momentum or reversion, trigger 2-6, stop / take-profit 2-6 spreads) | every one lost: -0.85 to -1.20 spreads a trade | every one lost: -0.46 to -1.18 spreads a trade |
| ... before the spread | -0.15 to +0.16 spreads | -0.16 to +0.55 spreads |

The "before the spread" row says the signal predicts nothing: valued at
mid prices, the trades come out at about zero, so each one loses what it
paid to cross the spread. Fading bursts (`SCALPER_MODE=reversion`) came out
a little less badly than following them on both brokers, but not by enough
to matter. This fills every trade at the price the bot saw, with no delay,
so the live bot should do slightly worse. IG and Capital.com have no fine
enough price history to test.

### Tests

```powershell
python -m unittest discover -s strategy-bots\tests
```

Clock and DST rules, indicators, each strategy's signals on made-up bars,
and the runner's sizing, spread, rollover, slots, dry run and
other-people's-positions rules against a fake broker. `test_rollover.py`
covers the CFD scanners' close before the rollover: its timing, and that
each closes only its own trades. No broker is contacted.

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
  a full pass over it takes about 35 seconds: fine for 5-minute bars or
  longer, too slow for 1-minute ones.
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

## Scalper backtest (`backtest/scalper_backtest.py`)

Replays the tick scalper - the bot's own code from
`strategy-bots/engine/scalper.py` - over real bid/ask prices, for 24
combinations of mode, trigger and stop / take-profit, and prints each one's
trades, win rate, R and spreads per trade, before and after the spread:

```powershell
python backtest\scalper_backtest.py oanda         # ig-bot-env: OANDA's 5-second bid/ask candles
python backtest\scalper_backtest.py pepperstone   # pepperstone-bot-env: every MT5 tick (the terminal must be running)
```

Options: `--days 7`, `--markets EUR_USD,GBP_USD`, `--windows 30,60,120`
(other `SCALPER_WINDOW_SECONDS`), `--delay 1` (fill a read late). The
other `SCALPER_*` settings come from the environment, as for the bot.
Prices are cached in `backtest/data/`. Neither broker's history quite
matches what its demo account pays: MT5's tick history is Pepperstone's
raw feed (EUR/USD 0.0-0.1 pips, while the Standard demo account quotes
1.0), and OANDA's candles show EUR/USD at 1.6 pips against 0.8 live. So
the fetch compares each market's live spread with the history's last few
minutes and widens or narrows every price by the difference, printing it.
**Fetch while the markets are open**, or the history is used as it is.
Results: [What to expect](#what-to-expect).

## Dashboard (`shared/dashboard_reporter.py`)

Every bot can report to the
[trading dashboard](https://github.com/FeatheredFiend/tradingdashboard), a
web app that shows whether each bot is running, its account, open
positions, trades, profit/loss and console log - from any browser or phone.
The bots push to it, so it works with them running at home; no broker key
ever leaves this PC.

It's off until both of these are set where the bots run (PowerShell, saved
for new windows):

```powershell
[Environment]::SetEnvironmentVariable("DASHBOARD_URL", "https://your-dashboard-subdomain", "User")
[Environment]::SetEnvironmentVariable("DASHBOARD_TOKEN", "the dashboard's INGEST_TOKEN", "User")
```

then restart the bots. Each sends a heartbeat with its new log lines every
10 seconds and, every 15 seconds, its account, positions and recent trades
(the IG bots once a minute, since IG's request limit is tight). If the dashboard is down, the bot keeps trading and holds back up
to 2,000 log lines to send later. What each broker can report:

| Bots | Account, positions, trades | Extra broker calls |
|---|---|---|
| OANDA | yes - trades with why they closed (stop-loss, take-profit, reversal) | 12 a minute |
| Pepperstone | yes - trades matched from MT5's deal history by the bot's magic number | none (local terminal) |
| Capital.com | yes - trades pieced together from the activity and transaction history, with why they closed | 16 a minute |
| Alpaca | yes - trades as the bot closes them, priced at Alpaca's value just before the sell | 8 a minute |
| IG scanner | yes - each open position's profit is worked out by the bot (IG's REST API doesn't give one: move x size x contract size, converted at IG's own rate); trades from the transaction history every 5 minutes | about 1.2 a minute, inside its pacing |
| IG EMA bot | log and running status only | none |
| Strategy bots | as their broker's bots above, but only the bot's own positions and trades; its Settings tab lists every setting it started with | as their broker's bots |

### Closing positions from the dashboard (`DASHBOARD_COMMANDS`)

With `DASHBOARD_COMMANDS=1` as well (launcher: Settings > General > "Close
from dashboard"), a dashboard admin gets a **Close** button on each of the
bot's open positions - all of it, or part. The dashboard can't reach this
PC, so the request rides back on the reply to the bot's next report
(within about 10 seconds); the bot carries it out between its passes and
its answer shows on the bot page. It's off by default: with it on, anyone
who can sign in to the dashboard as an admin can close trades.

- Only ever the bot's own position, and only if it's still the one the
  dashboard showed - same side and same broker ref. If the bot reversed or
  closed it in the meantime, nothing happens and the answer says why.
- A request the bot doesn't collect within a minute (it's stopped, or the
  PC is off) is dropped, so a close never fires late. Each goes out once.
- Part closes are rounded down to the market's size step. Capital.com's
  API only closes whole positions, and IG's minimum-size positions can't be
  split.
- The IG EMA bot doesn't report positions, so it has nothing to close.
- A strategy bot on a dry run answers that it sent nothing.

## Launcher (`launcher/`) - a Windows app for all of this

`TradingBots.exe` starts and stops the bots and edits their settings:

- **Bots:** all 29 bots with their status - including ones started by hand -
  filtered by broker and strategy (and "Running only" to hide stopped ones), and Start / Stop / Restart per bot,
  "Start all ticked" and "Stop all", plus a link to each bot's dashboard
  page. Starting runs
  `launcher/start_bot.bat`, which opens the bot as a new tab in one
  "TradingBots" Windows Terminal window (a console window of its own if
  Windows Terminal isn't installed), on the right Python environment.
  Stopping presses Ctrl+C in that tab, so the bot shuts down cleanly and
  tells the dashboard it stopped (after 20 seconds without an answer it's
  force-stopped instead). Closing a tab stops that bot, and closing the
  whole window stops every bot at once - without the dashboard goodbye, so
  they show as "not reporting" there. Only the ticked
  bots start with "Start all": by default the scanners, since an EMA
  bot and a scanner on the same OANDA or Alpaca account would close each
  other's trades. Closing the app leaves the bots running.
- **Settings:** a form for every environment variable the bots read -
  keys, budgets, market lists, stop-loss / take-profit, each strategy's
  parameters, the dashboard - in sections (General, the momentum scanners,
  the EMA bots, each strategy, then each broker with its bots' own markets and budgets),
  saved as Windows user environment variables (the same place
  `[Environment]::SetEnvironmentVariable(..., "User")` writes). Empty means
  the bot's default. It offers to restart running bots so they pick changes up.
- **Paths:** the repo folder and each broker's Python environment (kept in
  `%APPDATA%\TradingBots\launcher.json`).

Build or rebuild it (close the app first) by running, from PowerShell or a
double-click:

```powershell
\\wsl.localhost\Ubuntu\home\martyn\projects\tradingbots\launcher\build.bat
```

It builds with PyInstaller in its own environment
(`%LOCALAPPDATA%\TradingBots\build-env`), installs the app to
`%LOCALAPPDATA%\Programs\TradingBots\TradingBots.exe` and puts a "Trading
Bots" shortcut on the desktop. `start_bot.bat <bot>` also works on its own,
e.g. `start_bot.bat oanda-momentum-scanner`.

## Repo layout

Each momentum scanner and EMA bot lives in its own folder with its own
`requirements.txt`; the shared pieces are `shared/dashboard_reporter.py`
and `shared/rollover.py` (standard library only; the latter uses the
strategy engine's `clock.py`). The strategy bots share `strategy-bots/engine/`
and need nothing beyond their broker's existing requirements.
Virtual environments (`*-bot-env/`) are gitignored — create your own per
broker (all of a broker's bots share the same requirements).
