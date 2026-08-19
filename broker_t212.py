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
import base64
import urllib.request
import urllib.error

_instruments_cache = None


def _get_instruments():
    global _instruments_cache
    if _instruments_cache is None:
        _instruments_cache = _request("GET", "/equity/metadata/instruments")
    return _instruments_cache


def resolve_ticker_by_isin(isin):
    """
    Najde přesný T212 ticker pro daný ISIN. Vyhazuje výjimku, pokud nástroj
    není v nabídce (dobré selhat hlasitě, ne tiše obchodovat něco jiného).
    """
    for instr in _get_instruments():
        instr_isin = instr.get("isin") or instr.get("ISIN")
        if instr_isin and instr_isin.upper() == isin.upper():
            ticker = instr.get("ticker") or instr.get("symbol")
            if ticker:
                return ticker
    raise RuntimeError(f"Nástroj s ISIN {isin} nebyl v Trading 212 nabídce (metadata/instruments) nalezen.")


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
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
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

    # POZOR: přesné názvy polí u jednotlivé pozice (ticker/quantity/averagePrice/
    # currentPrice/...) nejsou z dokumentace 100% jistě potvrzené - ošetřeno
    # pomocí .get() s více variantami názvu, aby appka při drobné nepřesnosti
    # nespadla, jen pozici případně vynechá. Při prvním běhu na demo účtu stojí
    # za to si vypsat "positions" surově do logu a podle skutečné odpovědi
    # tenhle mapping doladit.
    parsed_positions = []
    for p in positions:
        raw_ticker = p.get("ticker") or p.get("symbol")
        symbol = _ticker_to_our_symbol(raw_ticker, instruments_map) if raw_ticker else None
        qty = p.get("quantity") or p.get("qty")
        avg_price = p.get("averagePrice") or p.get("avgPrice") or p.get("averageEntryPrice")
        current_price = p.get("currentPrice") or p.get("marketPrice")
        if symbol is None or qty is None:
            continue
        avg_price = float(avg_price) if avg_price is not None else 0.0
        current_price = float(current_price) if current_price is not None else avg_price
        qty = float(qty)
        market_value = qty * current_price
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
        "currency": summary.get("currency", "GBP"),  # pro zobrazení v reportu/notifikaci
        "buying_power": float(cash),
        "positions": parsed_positions,
    }


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
            ticker = resolve_ticker_by_isin(info["isin"])

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
