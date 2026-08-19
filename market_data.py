"""
Modul pro stahování tržních dat z volně dostupných zdrojů (Stooq, záložně EODHD).

Nahrazuje Alpaca StockHistoricalDataClient/CryptoHistoricalDataClient v pilotní
verzi appky přepsané na Trading 212 (Trading 212 API zatím nemá endpoint na
tržní/historická data - viz docs.trading212.com, sekce Account Data/Positions/
Orders, žádná sekce "Market Data").

Zdroje:
- Stooq (https://stooq.com) - primární. Zdarma, bez API klíče, CSV přes HTTP.
  Pro evropské (LSE) tickery používá příponu ".uk" (např. "eqqq.uk"), pro
  americké akcie příponu ".us" (např. "aapl.us"). Neoficiální zdroj (scraping
  veřejné CSV stránky) - může se kdykoliv bez varování rozbít nebo změnit formát.
- EODHD (https://eodhd.com) - záložní zdroj, použije se jen když Stooq selže
  nebo vrátí prázdná data. Oficiální API, ale free tier má limit 20 dotazů/den,
  proto se nepoužívá jako primární (appka běžně potřebuje ceny pro 5-8 tickerů
  denně, na free tier je malá rezerva). Vyžaduje EODHD_API_KEY (volitelné -
  pokud není nastavený, fallback se prostě přeskočí a symbol zůstane bez dat).

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


def get_recent_bars(symbol_map, lookback_days=14):
    """
    Stáhne denní bary pro zadané symboly. `symbol_map` je typicky přímo
    instruments.INSTRUMENTS (nebo jeho podmnožina) - slovník ve tvaru:
        {
            "CSPX": {"stooq": "cspx.uk", "eodhd": "CSPX.LSE", "price_divisor": 1},
            "AAPL": {"stooq": "aapl.us", "eodhd": "AAPL.US", "price_divisor": 1},
            ...
        }
    "price_divisor" je volitelný (default 1) - viz normalizace GBX/GBP níže.
    Vrací {symbol: [bary]} - stejný tvar jako dřívější Alpaca get_recent_bars,
    aby zbytek appky (decision.py, main.py, backtest.py) nemusel měnit, jak
    s daty pracuje. Symbol bez jakýchkoliv dat (oba zdroje selhaly) se do
    výsledku vůbec nezařadí - volající kód už dnes počítá s tím, že ne každý
    symbol musí mít bary (viz podmínky "if symbol in bars.data" v původním
    data_fetch.py).
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    eodhd_token = os.environ.get("EODHD_API_KEY", "").strip() or None

    result = {}
    for symbol, sources in symbol_map.items():
        bars = []
        stooq_symbol = sources.get("stooq")
        if stooq_symbol:
            bars = _fetch_stooq_bars(stooq_symbol, start, end)

        if not bars:
            eodhd_symbol = sources.get("eodhd")
            if eodhd_symbol and eodhd_token:
                print(f"{symbol}: Stooq nevrátil data, zkouším EODHD záložně.")
                bars = _fetch_eodhd_bars(eodhd_symbol, start, end, eodhd_token)
            elif eodhd_symbol and not eodhd_token:
                print(f"{symbol}: Stooq nevrátil data a EODHD_API_KEY není nastavený, "
                      f"symbol zůstane bez dat.")

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

        if bars:
            result[symbol] = bars
        else:
            print(f"{symbol}: žádná data z žádného zdroje.")

    return result
