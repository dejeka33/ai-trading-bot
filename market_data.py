"""
Modul pro stahování tržních dat z volně dostupných zdrojů (Stooq, záložně EODHD).

Nahrazuje Alpaca StockHistoricalDataClient/CryptoHistoricalDataClient v pilotní
verzi appky přepsané na Trading 212 (Trading 212 API zatím nemá endpoint na
tržní/historická data - viz docs.trading212.com, sekce Account Data/Positions/
Orders, žádná sekce "Market Data").

Zdroje:
- EODHD (https://eodhd.com) - PRIMÁRNÍ zdroj. Oficiální API, free tier 20
  dotazů/den (appka běžně potřebuje 5-8 tickerů/den, vejde se s rezervou).
  Vyžaduje EODHD_API_KEY - samoobslužná bezplatná registrace na
  eodhd.com/register, token hned po registraci, žádná karta netřeba.
- Stooq (https://stooq.com) - záložní, použije se jen když EODHD selže/chybí
  klíč. PŮVODNĚ byl plánovaný jako primární (zdarma, bez klíče, CSV přes HTTP),
  ale od dubna 2026 Stooq vyžaduje VLASTNÍ apikey i pro obyčejné CSV stažení
  (ověřeno živě na prvním testu appky - místo dat vrátil jen text "Get your
  apikey: 1. Open https://stooq.com/q/d/?s=...&get_apikey", appka to tehdy
  tiše vyhodnotila jako "žádná data"). Bez Stooq apikey (získává se emailem na
  www@stooq.com, nejistá rychlost) tenhle zdroj dnes reálně nefunguje - je tu
  jen pro případ, že by se klíč v budoucnu získal.

POZOR: tento modul, stejně jako Alpaca verze, spoléhá na síťový přístup, který
v cloudovém sandboxu Cowork není na allowlistu (ověřeno) - reálně poběží až
v GitHub Actions.
"""
import os
import csv
import io
from datetime import datetime, timedelta, timezone

import urllib.request
import urllib.error

import fx


STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}&d1={d1}&d2={d2}&i=d"
EODHD_URL = "https://eodhd.com/api/eod/{symbol}?from={d1}&to={d2}&api_token={token}&fmt=json&period=d"


def _fetch_url(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _fetch_stooq_bars(stooq_symbol, start, end):
    """
    Stáhne denní OHLCV z veřejného Stooq CSV exportu.
    Vrací seznam barů seřazený od nejstaršího, nebo [] při chybě/prázdné odpovědi.
    Stooq při neznámém symbolu nebo chybě vrací buď HTTP chybu, nebo text
    "N/D" místo CSV - obojí ošetřujeme jako "žádná data".
    """
    url = STOOQ_URL.format(
        symbol=stooq_symbol,
        d1=start.strftime("%Y%m%d"),
        d2=end.strftime("%Y%m%d"),
    )
    try:
        raw = _fetch_url(url)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"Stooq: chyba při stahování {stooq_symbol}: {e}")
        return []

    if not raw or raw.strip().upper().startswith("N/D") or "<html" in raw.lower():
        return []

    bars = []
    reader = csv.DictReader(io.StringIO(raw))
    for row in reader:
        try:
            bars.append({
                "t": datetime.strptime(row["Date"], "%Y-%m-%d").replace(tzinfo=timezone.utc).isoformat(),
                "o": float(row["Open"]),
                "h": float(row["High"]),
                "l": float(row["Low"]),
                "c": float(row["Close"]),
                "v": float(row["Volume"]) if row.get("Volume") else 0.0,
            })
        except (KeyError, ValueError):
            continue  # přeskočit poškozený/neúplný řádek, ne shodit celý fetch
    return bars


def _fetch_eodhd_bars(eodhd_symbol, start, end, api_token):
    url = EODHD_URL.format(
        symbol=eodhd_symbol,
        d1=start.strftime("%Y-%m-%d"),
        d2=end.strftime("%Y-%m-%d"),
        token=api_token,
    )
    try:
        raw = _fetch_url(url)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"EODHD: chyba při stahování {eodhd_symbol}: {e}")
        return []

    import json
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"EODHD: neočekávaná odpověď pro {eodhd_symbol}: {raw[:200]}")
        return []

    if not isinstance(data, list):
        return []

    bars = []
    for row in data:
        try:
            bars.append({
                "t": datetime.strptime(row["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc).isoformat(),
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
                "v": float(row["volume"]) if row.get("volume") else 0.0,
            })
        except (KeyError, ValueError, TypeError):
            continue
    return bars


def get_recent_bars(symbol_map, lookback_days=14, account_currency=None):
    """
    Stáhne denní bary pro zadané symboly. `symbol_map` je typicky přímo
    instruments.INSTRUMENTS (nebo jeho podmnožina) - slovník ve tvaru:
        {
            "CSPX": {"stooq": "cspx.uk", "eodhd": "CSPX.LSE", "price_divisor": 1, "currency": "GBP"},
            "AAPL": {"stooq": "aapl.us", "eodhd": "AAPL.US", "price_divisor": 1, "currency": "USD"},
            ...
        }
    "price_divisor" je volitelný (default 1) - viz normalizace GBX/GBP níže.
    "currency" je volitelná - měna ceny PO price_divisor úpravě (viz fx.py).

    `account_currency` (volitelné, typicky account_snapshot["currency"] z
    broker_t212.py) - pokud je zadaná a liší se od measy nástroje, appka ceny
    ještě převede přes fx.py (viz POZOR o měnách v instruments.py). Bez tohohle
    parametru appka ceny nepřevádí (zachová se dřívější chování) - důležité
    hlavně pro risk_rules.py, který porovnává qty * cena proti mantinelu
    v měně účtu.

    Vrací {symbol: [bary]} - stejný tvar jako dřívější Alpaca get_recent_bars,
    aby zbytek appky (decision.py, main.py, backtest.py) nemusel měnit, jak
    s daty pracuje. Symbol bez jakýchkoliv dat (oba zdroje selhaly) se do
    výsledku vůbec nezařadí - volající kód už dnes počítá s tím, že ne každý
    symbol musí mít bary (viz podmínky "if symbol in bars.data" v původním
    data_fetch.py).
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)

    result = {}
    for symbol, sources in symbol_map.items():
        bars = fetch_symbol_bars_raw(symbol, sources, start, end)

        # Převod do měny účtu (viz fx.py a POZOR o měnách v instruments.py) -
        # BEZ TOHOHLE appka porovnávala cenu v cizí měně (GBP/USD) přímo proti
        # mantinelu v měně účtu (CZK), což na živém testu 19.8.2026 vedlo k
        # obchodům, které vypadaly "pod limitem", ale ve skutečnosti stály
        # řádově víc (nebo je broker rovnou odmítl pro nedostatek prostředků).
        # Používá "aktuální" kurz (fx.get_fx_rate) - pro živý provoz správně,
        # pro historickou simulaci s kurzem PLATNÝM K DANÉMU DNI viz backtest.py.
        instrument_currency = sources.get("currency")
        if bars and account_currency and instrument_currency and instrument_currency.upper() != account_currency.upper():
            rate = fx.get_fx_rate(instrument_currency, account_currency)
            if rate is not None:
                for b in bars:
                    b["o"] *= rate
                    b["h"] *= rate
                    b["l"] *= rate
                    b["c"] *= rate
            else:
                print(f"{symbol}: kurz {instrument_currency}->{account_currency} se nepodařilo "
                      f"získat - ceny zůstávají v {instrument_currency}, risk-limit kontrola pro "
                      f"tenhle symbol dnes může být nespolehlivá.")

        if bars:
            result[symbol] = bars
        else:
            print(f"{symbol}: žádná data z žádného zdroje.")

    return result


def fetch_symbol_bars_raw(symbol, sources, start, end):
    """
    Stáhne denní bary pro JEDEN nástroj v DANÉM rozsahu dat (EODHD primárně,
    Stooq záložně) a normalizuje GBX/GBP (price_divisor) - ale BEZ převodu do
    měny účtu (to je na volajícím: get_recent_bars výše to dělá "aktuálním"
    kurzem pro živý provoz, backtest.py historickým kurzem PLATNÝM K DANÉMU DNI
    pro simulaci - jde o sdílenou "surovou" část, ať se logika stahování/
    normalizace cen neduplikuje na dvou místech a nerozjíždí se v čase).

    Vrací seznam barů (může být []), NIKDY nevyhazuje výjimku kvůli chybějícím
    datům - stejný "nice to have, ne blokující" princip jako zbytek modulu.
    """
    bars = []
    eodhd_token = os.environ.get("EODHD_API_KEY", "").strip() or None

    # EODHD je teď PRIMÁRNÍ zdroj (ne Stooq) - Stooq od dubna 2026 vyžaduje
    # vlastní apikey i pro obyčejné CSV stažení (ověřeno živě - appka místo
    # dat dostávala jen text "Get your apikey..."), zatímco EODHD free tier
    # (20 dotazů/den, samoobslužná registrace) funguje bez čekání na email.
    eodhd_symbol = sources.get("eodhd")
    if eodhd_symbol and eodhd_token:
        bars = _fetch_eodhd_bars(eodhd_symbol, start, end, eodhd_token)
    elif eodhd_symbol and not eodhd_token:
        print(f"{symbol}: EODHD_API_KEY není nastavený, zkouším Stooq (nemusí fungovat "
              f"bez vlastního Stooq apikey - viz poznámka v hlavičce modulu).")

    if not bars:
        stooq_symbol = sources.get("stooq")
        if stooq_symbol:
            if eodhd_token:
                print(f"{symbol}: EODHD nevrátil data, zkouším Stooq záložně "
                      f"(pravděpodobně taky selže bez Stooq apikey).")
            bars = _fetch_stooq_bars(stooq_symbol, start, end)

    # Normalizace GBX/pence na GBP u LSE nástrojů - viz POZOR v instruments.py
    # (price_divisor: 1 = beze změny, 100 = GBX -> GBP). Nutné pro konzistenci
    # s cenami z Trading 212 (a s risk_rules.py, který qty * cena počítá v
    # jedné jednotce pro celý účet).
    divisor = sources.get("price_divisor", 1)
    if bars and divisor != 1:
        for b in bars:
            b["o"] /= divisor
            b["h"] /= divisor
            b["l"] /= divisor
            b["c"] /= divisor

    return bars
