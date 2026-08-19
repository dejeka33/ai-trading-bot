"""
Modul pro komunikaci s Trading 212 API (účet, pozice, obchody) - nahrazuje
alpaca.trading.client.TradingClient v pilotní appce přepsané z Alpaca.

Prostředí (viz docs.trading212.com/api/section/general-information/api-environments):
- demo.trading212.com/api/v0 - paper/practice účet, simulované peníze
- live.trading212.com/api/v0 - ostrý účet, reálné peníze

Přepínání paper/live řídí T212_BASE_URL (analogicky k ALPACA_PAPER u staré verze) -
DEFAULT je demo, na live se NIKDY nepřepíná automaticky.

Autentizace: Trading 212 při generování API klíče vydává DVĚ hodnoty - "Key"
(identifikátor) a "Secret" (funguje jako heslo). Appka je čte z GitHub Secrets
T212_API_ID (= "Key") a T212_API_KEY (= "Secret") a posílá je jako HTTP Basic
Auth hlavičku: `Authorization: Basic base64(T212_API_ID:T212_API_KEY)`.

POZOR - i tohle je zatím NEOVĚŘENO živým voláním (sandbox Cowork nemá
Trading 212 API na síťovém allowlistu - ověřeno, síťová volání odsud jdou jen
proti docs.claude.com/anthropic.com apod. - takže tenhle modul šlo jen
syntakticky zkontrolovat, ne reálně vyzkoušet). Pokud by při prvním běhu proti
demo účtu appka spadla na HTTP 401, chybová hláška obsahuje tělo odpovědi od
Trading 212 - podle ní se to doladí (např. kdyby to nakonec chtělo Bearer token
jen s jednou z těch dvou hodnot, nebo obrácené pořadí Key/Secret).

Tickery nástrojů: appka NEHÁDÁ formát T212 tickeru (v dokumentaci/komunitě jsme
narazili na tři různé konvence - "AAPL_US_EQ", "BPl_EQ", "VUSA_LSE_EQ"). Místo
toho si přesný ticker dohledá za běhu podle ISIN (jednoznačný, formátem T212
nezávislý identifikátor - viz instruments.py) přes
GET /equity/metadata/instruments. Výsledek se cachuje v paměti běhu (endpoint
má rate limit 1 dotaz/50s, appka ho proto volá nejvýš jednou za spuštění).
"""
import os
import json
import time
import base64
import http.cookiejar
import urllib.request
import urllib.error

import fx

# Sdílený cookie jar pro celý běh - Cloudflare (WAF před Trading 212 API) na
# některé endpointy (viz /equity/positions, ověřeno na živém běhu - HTTP 403
# s __cf_bm cookie a prázdným tělem, typický Cloudflare bot-management pattern)
# vyžaduje, aby si klient "zapamatoval" cookie z dřívější odpovědi a poslal ho
# zpátky - normální prohlížeč to dělá automaticky, urllib bez tohohle ne.
_cookie_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookie_jar))

_instruments_cache = None


def _get_instruments():
    global _instruments_cache
    if _instruments_cache is None:
        _instruments_cache = _request("GET", "/equity/metadata/instruments")
    return _instruments_cache


def resolve_ticker_by_isin(isin, preferred_currency=None):
    """
    Najde přesný T212 ticker pro daný ISIN. Vyhazuje výjimku, pokud nástroj
    není v nabídce (dobré selhat hlasitě, ne tiše obchodovat něco jiného).

    POZOR - zjištěno testem 19.8.2026 večer: jeden ISIN může mít VÍC T212
    tickerů najednou - ETF jako CSPX/EQQQ bývají kotované na víc burzách
    (Londýn, Xetra, Milán...) pod různými tickery a hlavně v RŮZNÝCH měnách
    (GBP na LSE, ale EUR na Xetra/Miláně). Appka dřív brala první nalezenou
    shodu v pořadí, v jakém je vrátí GET /equity/metadata/instruments - to
    NENÍ garantovaně stabilní napříč běhy. Živě se to projevilo tak, že se
    ISIN CSPX (IE00B5BMR087) jednou přeložil na "SXR8d_EQ" (vypadá jako
    Xetra/Frankfurt kotace) místo očekávané londýnské - což by rozbilo
    předpoklad GBP v instruments.py (currency/price_divisor), na kterém navíc
    stojí FX převod v get_account_snapshot(). Proto appka teď preferuje tu
    kotaci, jejíž currencyCode odpovídá měně, se kterou appka pro daný nástroj
    počítá (instruments.py "currency", předané jako preferred_currency) - a
    jen pokud žádná neodpovídá, spadne zpátky na první nalezenou shodu (lepší
    obchodovat něco, co appka umí spočítat, než nic).
    """
    matches = []
    for instr in _get_instruments():
        instr_isin = instr.get("isin") or instr.get("ISIN")
        if instr_isin and instr_isin.upper() == isin.upper():
            ticker = instr.get("ticker") or instr.get("symbol")
            if ticker:
                currency = (instr.get("currencyCode") or instr.get("currency") or "").upper()
                matches.append((ticker, currency))

    if not matches:
        raise RuntimeError(f"Nástroj s ISIN {isin} nebyl v Trading 212 nabídce (metadata/instruments) nalezen.")

    if preferred_currency:
        for ticker, currency in matches:
            if currency == preferred_currency.upper():
                return ticker
        print(f"POZOR: pro ISIN {isin} appka nenašla kotaci v očekávané měně "
              f"{preferred_currency} (nalezené kotace: {matches}) - používám první "
              f"nalezenou ({matches[0][0]}, {matches[0][1]}), hodnoty v reportu proto "
              f"můžou být zkreslené.")

    return matches[0][0]


def _isin_to_symbol_map(instruments_map):
    return {v["isin"].upper(): k for k, v in instruments_map.items()}


def _ticker_to_our_symbol(ticker, instruments_map):
    """Zpětné mapování T212 ticker -> náš interní symbol (pro pozice v účtu)."""
    isin_map = _isin_to_symbol_map(instruments_map)
    for instr in _get_instruments():
        if (instr.get("ticker") or instr.get("symbol")) == ticker:
            instr_isin = (instr.get("isin") or instr.get("ISIN") or "").upper()
            if instr_isin in isin_map:
                return isin_map[instr_isin]
            break
    return ticker  # neznámý/nenamapovaný nástroj - vrátit surový ticker, ne spadnout


def _base_url():
    # Bezpečný výchozí stav = demo/paper, přesně jako u Alpaca (ALPACA_PAPER default true).
    return os.environ.get("T212_BASE_URL", "https://demo.trading212.com/api/v0").strip().rstrip("/")


def _auth_header_value():
    api_id = os.environ["T212_API_ID"].strip()      # "Key" z Trading 212
    api_key = os.environ["T212_API_KEY"].strip()     # "Secret" z Trading 212
    token = base64.b64encode(f"{api_id}:{api_key}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _request(method, path, body=None):
    url = f"{_base_url()}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", _auth_header_value())  # viz POZOR výše - ověřit při prvním běhu
    # Bez vlastního User-Agent posílá urllib "Python-urllib/3.x", což firewall/WAF
    # Trading 212 API blokuje jako podezřelý provoz (403 s prázdným tělem, dřív
    # ověřeno na prvním živém testu) - komunitní diskuze potvrzuje stejný problém.
    req.add_header("User-Agent", "Mozilla/5.0 (ai-trading-bot)")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with _opener.open(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            # DOČASNÝ diagnostický log (viz ladění prvního živého běhu v chatu) -
            # ukazuje, že tenhle konkrétní požadavek prošel, i s náhledem těla.
            print(f"[T212] {method} {path} -> {resp.status}, tělo (prvních 200 znaků): {raw[:200]!r}")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        # DOČASNÝ diagnostický log - i hlavičky odpovědi, pro případ, že tělo je
        # prázdné (např. blokace na úrovni WAF/proxy, ne appky samotné).
        print(f"[T212] {method} {path} -> HTTP {e.code}, hlavičky: {dict(e.headers)!r}")
        raise RuntimeError(
            f"Trading 212 API chyba {e.code} na {method} {path}: {error_body}"
        ) from e


def get_account_snapshot(instruments_map):
    """
    Vrací stejný tvar jako dřívější Alpaca get_account_snapshot(), aby zbytek
    appky (risk_rules.py, report.py, main.py) nemusel měnit svou logiku:
        {"cash", "portfolio_value", "buying_power", "positions": [...]}

    `instruments_map` = instruments.INSTRUMENTS - potřeba pro zpětné namapování
    T212 tickeru z pozice na náš interní symbol (CSPX/EQQQ/AAPL/...), se kterým
    pracuje zbytek appky.

    Pozn.: Trading 212 nemá koncept "buying_power" s pákou jako Alpaca (appka
    ani nikdy leverage nepoužívá - allow_leverage: false), takže buying_power
    nastavujeme rovno dostupné hotovosti.
    """
    summary = _request("GET", "/equity/account/summary")
    positions = _request("GET", "/equity/positions")

    cash = summary.get("cash", {}).get("availableToTrade", 0.0)
    total_value = summary.get("totalValue", cash)
    account_currency = (summary.get("currency") or "CZK").upper()

    # POZOR - zjištěno živým testem 19.8.2026: /equity/positions NEVRACÍ ticker/
    # isin/currency na horní úrovni objektu pozice, ale VNOŘENÉ v "instrument"
    # (a P&L vnořené ve "walletImpact") - potvrzeno oficiální dokumentací
    # (docs.trading212.com/api/positions/getpositions):
    #   {"instrument": {"ticker", "isin", "name", "currency"}, "quantity",
    #    "averagePricePaid", "currentPrice", "walletImpact": {"unrealizedProfitLoss", ...}, ...}
    # Appka dřív hledala "ticker"/"symbol" jen na horní úrovni (p.get("ticker")),
    # což NIKDY nenašlo shodu - každá pozice tak byla tiše přeskočena jako
    # "neznámý nástroj", i těsně po úspěšném provedení obchodu. To je SKUTEČNÁ
    # příčina toho, proč appka dlouho ukazovala "jen hotovost, žádné pozice" i
    # po opravě zpoždění vyplnění (get_settled_account_snapshot) - to zpoždění
    # samo o sobě nikdy nepomůže, protože parsing pozici nenajde ani po sebedelším
    # čekání. Mapujeme primárně přes ISIN (jednoznačný, appka ho má přímo
    # v odpovědi) - ticker jen jako záložní cesta, kdyby v budoucí verzi API ISIN
    # chyběl.
    parsed_positions = []
    for p in positions:
        instrument = p.get("instrument") or {}
        isin = (instrument.get("isin") or instrument.get("ISIN") or p.get("isin") or p.get("ISIN") or "").upper()
        symbol = _isin_to_symbol_map(instruments_map).get(isin) if isin else None
        if symbol is None:
            raw_ticker = instrument.get("ticker") or instrument.get("symbol") or p.get("ticker") or p.get("symbol")
            symbol = _ticker_to_our_symbol(raw_ticker, instruments_map) if raw_ticker else None

        qty = p.get("quantity") or p.get("qty")
        avg_price = p.get("averagePricePaid") or p.get("averagePrice") or p.get("avgPrice") or p.get("averageEntryPrice")
        current_price = p.get("currentPrice") or p.get("marketPrice")
        if symbol is None or qty is None:
            continue
        avg_price = float(avg_price) if avg_price is not None else 0.0
        current_price = float(current_price) if current_price is not None else avg_price
        qty = float(qty)

        # POZOR - zjištěno testem 19.8.2026 večer: avg_price/current_price z T212
        # API jsou v PŮVODNÍ měně nástroje (USD u AAPL/MSFT/GOOGL, GBP/GBX u
        # CSPX/EQQQ na LSE), ne v měně účtu (CZK) - appka je dřív ukládala
        # nepřevedené, takže report/dashboard u AAPL ukazoval "315.71" místo
        # částky v Kč (a odvozená market_value/unrealized_pl byly tím pádem taky
        # řádově mimo - třeba "22 Kč" místo skutečných stovek Kč). Používáme
        # STEJNOU normalizaci jako market_data.py/fx.py (price_divisor pro
        # GBX->GBP, pak fx.get_fx_rate do měny účtu), aby pozice v reportu/
        # dashboardu byly ve stejných jednotkách jako všude jinde v appce.
        instr_info = instruments_map.get(symbol) if symbol else None
        if instr_info:
            divisor = instr_info.get("price_divisor", 1) or 1
            native_currency = instr_info.get("currency", account_currency)
            avg_price = avg_price / divisor
            current_price = current_price / divisor
            fx_rate = fx.get_fx_rate(native_currency, account_currency)
            if fx_rate is not None:
                avg_price *= fx_rate
                current_price *= fx_rate
            else:
                print(f"POZOR: kurz {native_currency}->{account_currency} se nepodařilo stáhnout, "
                      f"pozice {symbol} zůstává v původní měně {native_currency} - hodnoty v reportu "
                      f"proto můžou být zkreslené.")

        market_value = qty * current_price
        wallet_impact = p.get("walletImpact") or {}
        unrealized_pl = wallet_impact.get("unrealizedProfitLoss")
        if unrealized_pl is None:
            unrealized_pl = p.get("ppl")
        unrealized_pl = float(unrealized_pl) if unrealized_pl is not None else (qty * (current_price - avg_price))
        parsed_positions.append({
            "symbol": symbol,
            "qty": qty,
            "avg_entry_price": avg_price,
            "current_price": current_price,
            "market_value": market_value,
            "unrealized_pl": unrealized_pl,
            "unrealized_plpc": (unrealized_pl / (avg_price * qty) * 100) if avg_price and qty else 0.0,
        })

    return {
        "cash": float(cash),
        "portfolio_value": float(total_value),
        "currency": account_currency,  # pro zobrazení v reportu/notifikaci
        "buying_power": float(cash),
        "positions": parsed_positions,
    }


def get_settled_account_snapshot(instruments_map, trade_results, max_attempts=8, delay_seconds=5):
    """
    Stejné jako get_account_snapshot(), ale počká, dokud se nově koupené
    symboly reálně neobjeví v /equity/positions.

    POZOR (zjištěno živým testem 19.8.2026): Trading 212 tržní příkaz se
    nevyplní okamžitě - GET /equity/positions volané hned po
    POST /equity/orders/market ještě vrací [] (a totalValue/investments
    v /equity/account/summary taky neodráží nově koupené akcie). Appka si tak
    dřív do reportu/dashboardu uložila "jen hotovost, žádné pozice", i když se
    nákup ve skutečnosti provedl - o pár hodin později bylo v T212 appce vidět
    reálné pozice v hodnotě stovek Kč, o kterých dashboard nevěděl.

    Použije se místo get_account_snapshot() jen tam, kde appka bere finální
    stav účtu PO obchodech (main.py) - snapshot PŘED obchody (account_before)
    tenhle problém logicky mít nemůže.
    """
    bought_symbols = {
        t["symbol"] for t in trade_results
        if t.get("status") == "submitted" and t.get("side") == "buy"
    }
    if not bought_symbols:
        return get_account_snapshot(instruments_map)

    snapshot = None
    for attempt in range(max_attempts):
        snapshot = get_account_snapshot(instruments_map)
        have_symbols = {p["symbol"] for p in snapshot["positions"]}
        missing = bought_symbols - have_symbols
        if not missing:
            return snapshot
        if attempt < max_attempts - 1:
            print(f"Pozice {missing} se po nákupu v účtu ještě neobjevily (obchod se teprve "
                  f"vyplňuje), čekám {delay_seconds}s a zkusím to znovu "
                  f"(pokus {attempt + 1}/{max_attempts})...")
            time.sleep(delay_seconds)

    print(f"POZOR: po {max_attempts} pokusech se v účtu pořád neobjevily pozice pro {missing} - "
          f"ukládám poslední dostupný snapshot i tak, ať appka nespadne. Report/dashboard pro "
          f"dnešek proto může dočasně ukazovat jen hotovost, než se to příště samo dorovná.")
    return snapshot


def execute_trades(trades, instruments_map):
    """
    Provede seznam obchodů přes Trading 212 equity orders API (market order).
    Stejné rozhraní jako dřívější Alpaca execute.execute_trades() (jen navíc
    potřebuje instruments_map = instruments.INSTRUMENTS, aby uměl náš interní
    symbol přeložit na skutečný T212 ticker přes ISIN) - vrací seznam výsledků,
    jeden neúspěšný obchod nezastaví ostatní.

    Trading 212 quantity konvence: kladné číslo = nákup, záporné = prodej
    (na rozdíl od Alpaca, kde se strana určuje samostatným polem "side" a qty
    je vždy kladné) - proto se tady znaménko počítá z t["side"].
    """
    results = []
    for t in trades:
        symbol = t.get("symbol")
        qty = t.get("qty")
        side = t.get("side")
        try:
            info = instruments_map.get(symbol)
            if not info:
                raise RuntimeError(f"Symbol {symbol} není v instruments.py namapovaný na ISIN.")
            ticker = resolve_ticker_by_isin(info["isin"], preferred_currency=info.get("currency"))

            signed_qty = float(qty) if side == "buy" else -float(qty)
            order = _request("POST", "/equity/orders/market", body={
                "ticker": ticker,
                "quantity": signed_qty,
            })
            results.append({
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "status": "submitted",
                "order_id": str(order.get("id", "")),
                "reasoning": t.get("reasoning", ""),
            })
        except Exception as e:
            results.append({
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "status": "failed",
                "error": str(e),
                "reasoning": t.get("reasoning", ""),
            })
    return results
