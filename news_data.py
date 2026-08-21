"""
Modul pro stahování zpráv k appkou obchodovaným akciím - Alpha Vantage
NEWS_SENTIMENT API. Používá se jako DOPLŇKOVÝ KONTEXT do promptu pro AI
(viz `news_section` v decision.py.build_prompt), appka bez něj funguje úplně
stejně jako dřív (news=None) - stejný "nice to have, nikdy neblokuje běh"
princip jako u fred_data.get_macro_context().

Vyžaduje volitelnou proměnnou prostředí ALPHAVANTAGE_API_KEY (zdarma na
https://www.alphavantage.co/support/#api-key - jen e-mail, žádná platební
karta). Pokud není nastavená nebo API selže/je vyčerpaný free limit, appka
pokračuje bez zpráv (news=None), stejně jako dosud.

POZOR - zavedeno 21.8.2026, ZATÍM NEOVĚŘENO ŽIVĚ (sandbox, ve kterém appka
tenhle modul psal, nemá síťový přístup na alphavantage.co, aby to šlo
otestovat rovnou) - při PRVNÍM ostrém běhu s nastaveným klíčem zkontrolovat:
1) že appka vůbec dostane data (ne prázdný feed/chybovou odpověď),
2) že se v `main.py`/`backtest.py` logu objeví smysluplné tituly zpráv,
3) že appka NEPŘEKRAČUJE free limit (25 volání/den) - při navržené
   architektuře (1 volání na celý den živě, 1 volání na celé období v
   backtestu) by k tomu nemělo dojít, ale stojí za to to sledovat.

POZOR - pokrytí: Alpha Vantage je primárně US trh. Appka proto zprávy zkouší
stahovat jen pro "obyčejné" US akciové tickery (AAPL, MSFT, GOOGL, AMZN,
JPM, JNJ, NVDA) - CSPX a EQQQ (evropské LSE UCITS ETF, viz instruments.py)
NEMAJÍ potvrzené pokrytí (nejsou to US-listované tickery, jak je AV čeká),
takže se pro ně zprávy vůbec nezkouší - appka i tak dostane užitečný
kontext přes zprávy o jednotlivých US akciích, ze kterých se S&P 500/
Nasdaq-100 (které CSPX/EQQQ sledují) skládají.
"""
import os
from datetime import datetime, timedelta, timezone

import requests

ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"

# Viz POZOR o pokrytí výš - jen symboly s "obyčejným" US tickerem.
NOT_COVERED_SYMBOLS = {"CSPX", "EQQQ"}


def _covered_symbols(symbols):
    return [s for s in symbols if s not in NOT_COVERED_SYMBOLS]


def _fetch_feed(tickers, time_from=None, time_to=None, limit=200, api_key=None):
    """Jedno volání NEWS_SENTIMENT pro VŠECHNY zadané tickery najednou (čárkou
    oddělený seznam v parametru "tickers") - stejná úspora volání jako
    fetch_all_bars v backtest.py u cenových dat. Vrací syrový seznam "feed"
    položek z odpovědi API, nebo [] při chybě/prázdné odpovědi."""
    params = {
        "function": "NEWS_SENTIMENT",
        "tickers": ",".join(tickers),
        "sort": "LATEST",
        "limit": limit,
        "apikey": api_key,
    }
    if time_from:
        params["time_from"] = time_from
    if time_to:
        params["time_to"] = time_to

    resp = requests.get(ALPHA_VANTAGE_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    # Alpha Vantage při chybě/vyčerpaném limitu nevrací HTTP chybu, ale JSON
    # s klíčem "Information"/"Error Message"/"Note" místo "feed" - appka to
    # bere jako "žádné zprávy", ne jako pád (stejný princip jako jinde v tomhle
    # souboru - zprávy appku nikdy nesmí zablokovat).
    if "feed" not in data:
        msg = data.get("Information") or data.get("Note") or data.get("Error Message") or data
        print(f"Alpha Vantage: nepodařilo se stáhnout zprávy (pokračuji bez nich): {msg}")
        return []

    # POZOR - přidáno 21.8.2026 po prvním živém testu napojení: appka sice
    # zprávy stáhla bez chyby, ale v reasoningu AI se vůbec neobjevily - bez
    # tohohle výpisu nebylo poznat, jestli feed přišel prázdný (AV pro dané
    # okno/tickery nic nenašel), nebo se něco ztratilo až při dalším
    # zpracování (_simplify_articles/news_as_of). Teď je to vidět přímo v logu.
    print(f"Alpha Vantage: staženo {len(data['feed'])} zpráv pro tickery {', '.join(tickers)}"
          f"{' (' + time_from + ' -> ' + (time_to or 'teď') + ')' if time_from else ''}.")

    return data["feed"]


def _simplify_articles(feed, symbols, limit_per_symbol=3):
    """Z syrového AV feedu vybere pro KAŽDÝ symbol nejrelevantnější zprávy
    (podle ticker_sentiment[].relevance_score, ne jen podle data) a zjednoduší
    je na pár polí - appka do promptu appky (viz decision.py) posílá jen
    title/date/source/sentiment, ne celé shrnutí/URL, ať zbytečně nenafukuje
    prompt (a tím náklady na Anthropic API) o věci, které appka k rozhodnutí
    nepotřebuje."""
    result = []
    for symbol in symbols:
        scored = []
        for article in feed:
            for ts in article.get("ticker_sentiment", []):
                if ts.get("ticker") != symbol:
                    continue
                scored.append((float(ts.get("relevance_score", 0) or 0), article, ts))
        scored.sort(key=lambda x: x[0], reverse=True)
        for _, article, ts in scored[:limit_per_symbol]:
            published = article.get("time_published", "")  # tvar YYYYMMDDTHHMMSS
            result.append({
                "symbol": symbol,
                "date": f"{published[:4]}-{published[4:6]}-{published[6:8]}" if len(published) >= 8 else None,
                "title": article.get("title"),
                "source": article.get("source"),
                "sentiment": ts.get("ticker_sentiment_label"),
            })
    return result


def get_recent_news(symbols, lookback_days=3, limit_per_symbol=3):
    """
    ŽIVÝ PROVOZ (main.py): vrátí zjednodušené nedávné zprávy (posledních
    `lookback_days` dní) pro `symbols` (typicky allowed_symbols z risk_rules.py),
    nebo None, pokud ALPHAVANTAGE_API_KEY není nastavený nebo se nepodařilo
    stáhnout žádné zprávy - appka pak pokračuje jako dosud (news=None).
    """
    api_key = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
    if not api_key:
        return None

    covered = _covered_symbols(symbols)
    if not covered:
        return None

    time_from = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y%m%dT%H%M")

    try:
        feed = _fetch_feed(covered, time_from=time_from, limit=200, api_key=api_key)
    except Exception as e:
        print(f"Alpha Vantage: chyba při stahování zpráv (pokračuji bez nich): {e}")
        return None

    articles = _simplify_articles(feed, covered, limit_per_symbol=limit_per_symbol)
    print(f"Alpha Vantage: {len(feed)} zpráv staženo -> {len(articles)} po výběru "
          f"nejrelevantnějších pro appku.")
    return articles or None


def fetch_all_news(symbols, start, end, api_key=None):
    """
    BACKTEST (backtest.py): JEDNO volání pro CELÉ testované období (stejný
    princip jako fetch_all_bars/fetch_all_fred v backtest.py) - appka pak
    slice-uje syrový feed po dnech přes news_as_of() níže, ať se nemusí volat
    API znovu pro každý simulovaný den (šetří free limit 25 volání/den).

    POZOR - historická hloubka: NEOVĚŘENO, jak daleko zpátky AV free tier
    zprávy reálně vrací (dokumentace to explicitně needávala) - u dlouhých
    backtestů (měsíce/roky zpátky) se může stát, že appka pro starší dny
    žádné zprávy nedostane, i když by v realitě existovaly. To appku
    neshodí (news_as_of() vrátí prázdný seznam), jen backtest pro tyhle dny
    nebude mít news kontext k dispozici - při prvním delším backtestu
    zkontrolovat, od kterého data appka reálně nějaké zprávy dostává.
    """
    api_key = (api_key or os.environ.get("ALPHAVANTAGE_API_KEY", "")).strip()
    if not api_key:
        return []

    covered = _covered_symbols(symbols)
    if not covered:
        return []

    time_from = datetime(start.year, start.month, start.day, tzinfo=timezone.utc).strftime("%Y%m%dT%H%M")
    time_to = datetime(end.year, end.month, end.day, 23, 59, tzinfo=timezone.utc).strftime("%Y%m%dT%H%M")

    try:
        return _fetch_feed(covered, time_from=time_from, time_to=time_to, limit=1000, api_key=api_key)
    except Exception as e:
        print(f"Alpha Vantage: chyba při stahování zpráv pro backtest (pokračuji bez nich): {e}")
        return []


def news_as_of(all_feed, symbols, day_str, lookback_days=3, limit_per_symbol=3):
    """
    Vyřízne z `all_feed` (výstup fetch_all_news) jen zprávy publikované
    NEJVÝŠ do konce dne `day_str` (žádný pohled do budoucnosti - stejný
    princip jako bars_as_of/macro_as_of v backtest.py) a ne starší než
    `lookback_days` dní před ním. Vrací zjednodušený seznam přes
    _simplify_articles(), nebo [] když se pro dané okno nic nenajde.
    """
    if not all_feed:
        return []

    day = datetime.strptime(day_str, "%Y-%m-%d").date()
    window_start = day - timedelta(days=lookback_days)

    windowed = []
    for article in all_feed:
        published = article.get("time_published", "")
        if len(published) < 8:
            continue
        pub_date = datetime.strptime(published[:8], "%Y%m%d").date()
        if window_start <= pub_date <= day:
            windowed.append(article)

    covered = _covered_symbols(symbols)
    simplified = _simplify_articles(windowed, covered, limit_per_symbol=limit_per_symbol)
    # POZOR - přidáno 21.8.2026, viz stejný důvod jako u výpisu v _fetch_feed.
    print(f"  news_as_of({day_str}): {len(all_feed)} zpráv v celém staženém feedu -> "
          f"{len(windowed)} v okně {lookback_days} dní -> {len(simplified)} po výběru "
          f"nejrelevantnějších pro appku.")
    return simplified
