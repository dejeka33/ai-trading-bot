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

POZOR - bug nalezený a opravený 21.8.2026 (živě otestováno přímo v prohlížeči
- appka na to nemá síťový přístup, viz historie chatu): appka PŮVODNĚ posílala
všechny sledované tickery najednou v jednom parametru "tickers=AAPL,MSFT,..."
- přesně jako u cenových dat (market_data.fetch_all_bars). U NEWS_SENTIMENT to
ale funguje JINAK, než appka čekala - Alpha Vantage dokumentace popisuje
víc tickerů v jednom volání jako "articles that SIMULTANEOUSLY mention"
všechny zadané tickery (AND), ne "kterýkoliv z nich" (OR). Živý test tohle
potvrdil: dotaz jen na "AAPL" vrátil desítky reálných zpráv, dotaz na všech
7 tickerů appky najednou vrátil "feed": [] (protože žádný jeden článek
nezmiňuje AAPL+MSFT+GOOGL+AMZN+JPM+JNJ+NVDA naráz - to je matematicky skoro
nemožné). Appka teď volá NEWS_SENTIMENT ZVLÁŠŤ pro každý ticker (viz
_fetch_feed_for_ticker/_fetch_feed_for_tickers níže) a syrové výsledky slučuje
- o něco pomalejší kvůli limitu 5 volání/minutu na free tieru appka mezi
jednotlivými voláními čeká (REQUEST_DELAY_SECONDS), ale funguje správně.

POZOR - pokrytí: Alpha Vantage je primárně US trh. Appka proto zprávy zkouší
stahovat jen pro "obyčejné" US akciové tickery (AAPL, MSFT, GOOGL, AMZN,
JPM, JNJ, NVDA) - CSPX a EQQQ (evropské LSE UCITS ETF, viz instruments.py)
NEMAJÍ potvrzené pokrytí (nejsou to US-listované tickery, jak je AV čeká),
takže se pro ně zprávy vůbec nezkouší - appka i tak dostane užitečný
kontext přes zprávy o jednotlivých US akciích, ze kterých se S&P 500/
Nasdaq-100 (které CSPX/EQQQ sledují) skládají.

POZOR - počet volání API: appka dělá 1 volání PRO KAŽDÝ sledovaný US ticker
(ne 1 volání celkem, viz POZOR o bugu výš) - při 7 tickerech je to 7 volání.
Živý provoz (main.py) tak dělá 7 volání/den (free tier limit je 25/den),
backtest dělá 7 volání CELKEM za celé testované období (ne za simulovaný
den, viz fetch_all_news - stejná "1x na celé období" architektura jako
dřív, jen rozpadlá na víc dílčích volání) - v obou případech s bezpečnou
rezervou pod denním limitem. Kvůli limitu 5 volání/minutu appka mezi
jednotlivými voláními čeká (viz REQUEST_DELAY_SECONDS) - u 7 tickerů to
přidá cca 1,5 minuty k běhu, což appce nevadí (živý běh i backtest na to
mají dost času).
"""
import os
import time
from datetime import datetime, timedelta, timezone

import requests

ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"

# Viz POZOR o pokrytí výš - jen symboly s "obyčejným" US tickerem.
NOT_COVERED_SYMBOLS = {"CSPX", "EQQQ"}

# Free tier Alpha Vantage: max 5 volání/minutu - appka mezi jednotlivými
# voláními (1 na ticker, viz POZOR v docstringu modulu) čeká, ať se do
# limitu nikdy netrefí. 60/5=12s je teoretické minimum, appka dává malou
# rezervu navíc.
REQUEST_DELAY_SECONDS = 13


def _covered_symbols(symbols):
    return [s for s in symbols if s not in NOT_COVERED_SYMBOLS]


def _fetch_feed_for_ticker(ticker, time_from=None, time_to=None, limit=200, api_key=None):
    """Jedno volání NEWS_SENTIMENT pro JEDEN ticker - viz POZOR v docstringu
    modulu, proč appka NEPOSÍLÁ víc tickerů najednou (AV by to vzalo jako
    "zmiňuje VŠECHNY najednou", ne "kterýkoliv z nich", a appka by dostávala
    prázdné odpovědi, jak se skutečně stalo). Vrací syrový seznam "feed"
    položek z odpovědi API, nebo [] při chybě/prázdné odpovědi."""
    params = {
        "function": "NEWS_SENTIMENT",
        "tickers": ticker,
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
    # bere jako "žádné zprávy pro tenhle ticker", ne jako pád (stejný princip
    # jako jinde v tomhle souboru - zprávy appku nikdy nesmí zablokovat).
    if "feed" not in data:
        msg = data.get("Information") or data.get("Note") or data.get("Error Message") or data
        print(f"Alpha Vantage ({ticker}): nepodařilo se stáhnout zprávy (pokračuji bez nich): {msg}")
        return []

    print(f"Alpha Vantage ({ticker}): staženo {len(data['feed'])} zpráv"
          f"{' (' + time_from + ' -> ' + (time_to or 'teď') + ')' if time_from else ''}.")
    return data["feed"]


def _fetch_feed_for_tickers(tickers, time_from=None, time_to=None, limit=200, api_key=None):
    """Zavolá _fetch_feed_for_ticker POSTUPNĚ pro každý ticker (viz POZOR v
    docstringu modulu o bugu s AND sémantikou u víc tickerů najednou) a
    syrové výsledky spojí do jednoho seznamu bez duplicit podle URL článku
    (stejný článek se může objevit ve výsledku pro víc tickerů, pokud
    zmiňuje víc z nich najednou - týž článek by appce jinak zbytečně
    "spotřeboval" místo v limitu limit_per_symbol dvakrát). Mezi
    jednotlivými voláními čeká REQUEST_DELAY_SECONDS kvůli limitu 5
    volání/minutu na free tieru. Chyba u JEDNOHO tickeru appku nezastaví -
    pokračuje se zbylými tickery (viz _fetch_feed_for_ticker/try-except).
    """
    combined = []
    seen_urls = set()
    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(REQUEST_DELAY_SECONDS)
        try:
            feed = _fetch_feed_for_ticker(
                ticker, time_from=time_from, time_to=time_to, limit=limit, api_key=api_key
            )
        except Exception as e:
            print(f"Alpha Vantage ({ticker}): chyba při stahování zpráv (pokračuji bez nich): {e}")
            continue
        for article in feed:
            url = article.get("url")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            combined.append(article)
    return combined


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
        feed = _fetch_feed_for_tickers(covered, time_from=time_from, limit=200, api_key=api_key)
    except Exception as e:
        print(f"Alpha Vantage: chyba při stahování zpráv (pokračuji bez nich): {e}")
        return None

    articles = _simplify_articles(feed, covered, limit_per_symbol=limit_per_symbol)
    print(f"Alpha Vantage: celkem {len(feed)} unikátních zpráv staženo (za {len(covered)} tickerů) -> "
          f"{len(articles)} po výběru nejrelevantnějších pro appku.")
    return articles or None


def fetch_all_news(symbols, start, end, api_key=None):
    """
    BACKTEST (backtest.py): appka pro CELÉ testované období zavolá API 1x na
    KAŽDÝ ticker (viz _fetch_feed_for_tickers a POZOR v docstringu modulu) -
    appka pak slice-uje syrový feed po dnech přes news_as_of() níže, ať se
    nemusí volat API znovu pro každý simulovaný den (šetří free limit
    25 volání/den - podobný princip jako fetch_all_bars/fetch_all_fred v
    backtest.py, jen s víc dílčími voláními kvůli AND-sémantice u víc
    tickerů najednou).

    POZOR - historická hloubka: NEOVĚŘENO, jak daleko zpátky AV free tier
    zprávy reálně vrací (dokumentace to explicitně neuvádí) - u dlouhých
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
        return _fetch_feed_for_tickers(covered, time_from=time_from, time_to=time_to, limit=1000, api_key=api_key)
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
    # POZOR - přidáno 21.8.2026, viz stejný důvod jako u výpisu v _fetch_feed_for_ticker.
    print(f"  news_as_of({day_str}): {len(all_feed)} zpráv v celém staženém feedu -> "
          f"{len(windowed)} v okně {lookback_days} dní -> {len(simplified)} po výběru "
          f"nejrelevantnějších pro appku.")
    return simplified
