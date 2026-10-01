"""
One name for each instrument, and each broker's code for it - so a
broadcast trade from the dashboard ("buy US500") reaches every bot in its
broker's own spelling: SPX500_USD on OANDA, US500 on Pepperstone and
Capital.com, IG's US 500 epic. The map starts from the momentum scanners'
15-market pools, which already line up across the brokers, plus the
strategy bots' other markets.

What's typed on the dashboard can be one of these names, a common alias
("gold", "S&P 500", "DAX"), any broker's code for it, or something not in
the map at all - then each bot tries it as its broker's own code, and as a
currency pair or a US share ticker spelt the broker's way. Whatever comes
out, the bot still checks it against its broker's list of instruments
before it trades it.

Alpaca has no CFDs, so a US fund stands in for an index or a commodity
there (US500 -> SPY): a "stand-in", which the dashboard marks.

Standard library only, so it works in every bot's venv.
"""

import re
from dataclasses import dataclass

# canonical name -> {broker: its code}. IG's are "search term:EPIC" (the
# epics the IG scanner resolved), so looking one up costs no search.
MARKETS = {
    # indices
    "US500": {"oanda": "SPX500_USD", "pepperstone": "US500", "capital": "US500",
              "ig": "US 500:IX.D.SPTRD.IFS.IP", "alpaca": "SPY"},
    "US30": {"oanda": "US30_USD", "pepperstone": "US30", "capital": "US30",
             "ig": "Wall Street:IX.D.DOW.IFS.IP", "alpaca": "DIA"},
    "US100": {"oanda": "NAS100_USD", "pepperstone": "NAS100", "capital": "US100",
              "ig": "US Tech 100:IX.D.NASDAQ.IFS.IP", "alpaca": "QQQ"},
    "UK100": {"oanda": "UK100_GBP", "pepperstone": "UK100", "capital": "UK100", "ig": "FTSE 100:IX.D.FTSE.IFM.IP"},
    "DE40": {"oanda": "DE30_EUR", "pepperstone": "GER40", "capital": "DE40", "ig": "Germany 40:IX.D.DAX.IFS.IP"},
    "JP225": {"oanda": "JP225_USD", "pepperstone": "JPN225", "capital": "J225", "ig": "Japan 225:IX.D.NIKKEI.IFM.IP"},
    # commodities
    "XAUUSD": {"oanda": "XAU_USD", "pepperstone": "XAUUSD", "capital": "GOLD",
               "ig": "Spot Gold:CS.D.CFPGOLD.CFP.IP", "alpaca": "GLD"},
    "XAGUSD": {"oanda": "XAG_USD", "pepperstone": "XAGUSD", "capital": "SILVER",
               "ig": "Spot Silver:CS.D.CFDSILVER.CFM.IP", "alpaca": "SLV"},
    "BRENT": {"oanda": "BCO_USD", "pepperstone": "SpotBrent", "capital": "OIL_BRENT",
              "ig": "Oil - Brent Crude:CC.D.LCO.UMP.IP", "alpaca": "BNO"},
    "WTI": {"oanda": "WTICO_USD", "pepperstone": "SpotCrude", "capital": "OIL_CRUDE",
            "ig": "Oil - US Crude:CC.D.CL.UMP.IP", "alpaca": "USO"},
    # currencies (IG only quotes the pound against the euro as GBP/EUR - the inverse - so it has no EURGBP)
    "EURUSD": {"oanda": "EUR_USD", "pepperstone": "EURUSD", "capital": "EURUSD", "ig": "EUR/USD:CS.D.EURUSD.MINI.IP"},
    "GBPUSD": {"oanda": "GBP_USD", "pepperstone": "GBPUSD", "capital": "GBPUSD", "ig": "GBP/USD:CS.D.GBPUSD.MINI.IP"},
    "USDJPY": {"oanda": "USD_JPY", "pepperstone": "USDJPY", "capital": "USDJPY", "ig": "USD/JPY:CS.D.USDJPY.MINI.IP"},
    "AUDUSD": {"oanda": "AUD_USD", "pepperstone": "AUDUSD", "capital": "AUDUSD", "ig": "AUD/USD:CS.D.AUDUSD.MINI.IP"},
    "EURGBP": {"oanda": "EUR_GBP", "pepperstone": "EURGBP", "capital": "EURGBP"},
    "GBPJPY": {"oanda": "GBP_JPY", "pepperstone": "GBPJPY", "capital": "GBPJPY", "ig": "GBP/JPY"},
    "EURJPY": {"oanda": "EUR_JPY", "pepperstone": "EURJPY", "capital": "EURJPY", "ig": "EUR/JPY"},
}

# The codes that are a fund standing in for the instrument, not the instrument.
STAND_INS = {"alpaca": {"US500", "US30", "US100", "XAUUSD", "XAGUSD", "BRENT", "WTI"}}

# Other ways of writing them (compared without spaces, punctuation or case).
ALIASES = {
    "SPX": "US500", "SP500": "US500", "S&P500": "US500", "SPX500": "US500",
    "DOW": "US30", "DJ30": "US30", "DJIA": "US30", "WALLST": "US30", "WALLSTREET": "US30",
    "NASDAQ": "US100", "NAS100": "US100", "NDX": "US100", "USTEC": "US100", "USTECH100": "US100",
    "FTSE": "UK100", "FTSE100": "UK100",
    "DAX": "DE40", "GER40": "DE40", "GERMANY40": "DE40", "DE30": "DE40",
    "NIKKEI": "JP225", "NIKKEI225": "JP225", "JPN225": "JP225", "J225": "JP225", "JAPAN225": "JP225",
    "GOLD": "XAUUSD", "SPOTGOLD": "XAUUSD", "SILVER": "XAGUSD", "SPOTSILVER": "XAGUSD",
    "BRENTCRUDE": "BRENT", "UKOIL": "BRENT", "BCO": "BRENT",
    "CRUDE": "WTI", "USOIL": "WTI", "USCRUDE": "WTI", "WTICO": "WTI",
}

CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "SEK", "NOK", "DKK", "SGD", "HKD", "ZAR",
              "MXN", "PLN", "CZK", "HUF", "TRY", "CNH"}
TICKER = re.compile(r"^([A-Z]{1,5})(?:[.-]US)?$")
# How each broker writes a currency pair and a US share ("{}" is the
# ticker); None: it has none (OANDA no shares, IG none over its API, Alpaca no FX).
FX_FORMATS = {"oanda": "{}_{}", "pepperstone": "{}{}", "capital": "{}{}", "ig": "{}/{}", "alpaca": None}
SHARE_FORMATS = {"oanda": None, "pepperstone": "{}.US", "capital": "{}", "ig": None, "alpaca": "{}"}


@dataclass(frozen=True)
class Candidate:
    """A broker code worth looking up for what was typed."""
    code: str                    # what to hand the broker's lookup (IG: "term" or "term:EPIC")
    canonical: str = None        # the map's name for it, if it's in the map
    stand_in: bool = False       # a fund standing in for `canonical` (Alpaca)


def squash(text: str) -> str:
    """For comparing names: upper case, letters, digits and & only."""
    return re.sub(r"[^A-Z0-9&]", "", (text or "").upper())


def _index() -> dict:
    index = {squash(alias): name for alias, name in ALIASES.items()}
    for name, codes in MARKETS.items():
        index[squash(name)] = name
        for code in codes.values():
            for part in code.split(":"):  # IG's search term and its epic
                index.setdefault(squash(part), name)
    return index


_INDEX = _index()


def canonical(text: str):
    """The map's name for `text`, or None if it isn't in the map."""
    return _INDEX.get(squash(text))


def names(broker: str) -> list:
    """The map's names this broker has a code for - the dashboard's list."""
    return sorted(name for name, codes in MARKETS.items() if broker in codes)


def code_for(broker: str, name: str):
    """The broker's code for one of the map's names, or None."""
    return MARKETS.get(name, {}).get(broker)


def candidates(broker: str, text: str) -> list:
    """The codes to look up on `broker` for `text`, best first. For one of
    the map's instruments, just the broker's code for it (none if the
    broker hasn't got it). Anything else: the text as a currency pair or a
    US share ticker in the broker's spelling, then as typed."""
    text = (text or "").strip()
    if not text:
        return []
    name = canonical(text)
    if name is not None:
        code = code_for(broker, name)
        return [Candidate(code, name, name in STAND_INS.get(broker, ()))] if code else []
    found, seen = [], set()

    def add(code):
        if code.upper() not in seen:
            seen.add(code.upper())
            found.append(Candidate(code))

    letters = squash(text)
    fx = FX_FORMATS.get(broker)
    if fx and len(letters) == 6 and letters[:3] in CURRENCIES and letters[3:] in CURRENCIES:
        add(fx.format(letters[:3], letters[3:]))
    share = SHARE_FORMATS.get(broker)
    ticker = TICKER.match(text.upper().replace(" ", ""))
    if share and ticker:
        add(share.format(ticker.group(1)))
    add(text)
    return found
