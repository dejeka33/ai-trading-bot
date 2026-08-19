"""
Modul pro převod cen nástrojů do měny účtu (FX).

Ceny z market_data.py (Stooq/EODHD) přichází v PŮVODNÍ měně konkrétního
nástroje/burzy (viz instruments.py - "currency": GBP pro CSPX/EQQQ na LSE po
GBX/GBP normalizaci, USD pro AAPL/MSFT/GOOGL). Trading 212 účet v tomto
pilotu je ale veden v CZK (viz account_snapshot["currency"]).

Bez převodu appka porovnávala číslo v cizí měně (např. "494.40" u AAPL, což
je ve skutečnosti 494.40 USD) proti mantinelu v CZK (500 CZK) - vypadalo to,
že obchod je "pod limitem", ale ve skutečnosti šlo o cca 494 USD ~ 11 500 CZK,
tedy víc než 20x nad limitem. Živě to appku poprvé odhalilo 19.8.2026: CSPX/
EQQQ (ceny v GBP, řádově podobné číslo jako CZK limit) formálně prošly, ale
utratily ve skutečnosti ~25x víc, než appka počítala (reservedForOrders
2500.91 CZK místo předpokládaných ~98 CZK); AAPL/MSFT/GOOGL byly rovnou
odmítnuty brokerem "Insufficient funds", protože skutečná cena v CZK dalece
přesahovala zbývající hotovost.

Zdroj kurzů: Frankfurter API (https://api.frankfurter.app) - zdarma, bez
registrace/klíče, kurzy ECB (aktualizace 1x denně, referenční kurz - appka
není HFT, přesnost je pro risk-limit kontrolu dostatečná). Kurzy se v rámci
jednoho běhu appky cachují (max pár dotazů - jeden na měnu nástroje).
"""
import json
import urllib.request
import urllib.error

FRANKFURTER_URL = "https://api.frankfurter.app/latest?from={base}&to={quote}"

_rate_cache = {}


def get_fx_rate(base_currency, quote_currency):
    """
    Vrátí kolik jednotek `quote_currency` dostaneš za 1 `base_currency`
    (např. get_fx_rate("USD", "CZK") -> ~23.5). Vrací None při chybě - appka
    pak cenu radši ponechá nepřevedenou a nahlásí to do logu, než aby spadla
    nebo tiše počítala se špatným číslem.
    """
    base_currency = (base_currency or "").upper()
    quote_currency = (quote_currency or "").upper()
    if not base_currency or not quote_currency or base_currency == quote_currency:
        return 1.0

    cache_key = (base_currency, quote_currency)
    if cache_key in _rate_cache:
        return _rate_cache[cache_key]

    url = FRANKFURTER_URL.format(base=base_currency, quote=quote_currency)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rate = float(data["rates"][quote_currency])
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError, json.JSONDecodeError) as e:
        print(f"FX: chyba při stahování kurzu {base_currency}->{quote_currency}: {e}")
        return None

    _rate_cache[cache_key] = rate
    return rate
