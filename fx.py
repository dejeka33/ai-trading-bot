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

POZOR - přidáno 9.9.2026 (viz diskuze v chatu o FX chybě 9.9.2026 a
"sanity check" pojistce, po retroaktivní opravě téhož dne): appka si kromě
kurzu pro TENHLE běh (in-memory _rate_cache níže) navíc TRVALE ukládá
poslední úspěšně stažený kurz do souboru CACHE_FILE (přežije mezi
jednotlivými denními běhy na GitHub Actions - proto v docs/data/, ten
adresář appka po každém běhu commitne zpět do repa, viz
.github/workflows/daily_trading.yml "git add ... docs/data/"). Slouží ke
dvěma věcem:
  1. SANITY CHECK - nový kurz appka porovná s posledním známým dobrým. Když
     se liší o víc než SANITY_THRESHOLD, appka mu nevěří (ochrana i proti
     situaci, kdy by Frankfurter vrátil nějaké číslo, ale ŠPATNÉ - ne jen
     chybu/None, na kterou appka reagovala už dřív).
  2. FALLBACK - když se dnešní stažení nepovede (nebo neprojde sanity
     check), appka použije poslední známý dobrý kurz (pokud není starší než
     FALLBACK_MAX_AGE_DAYS) místo toho, aby celý den kvůli výpadku appka
     neobchodovala (viz main.py/broker_t212.py) - kurz GBP/USD->CZK se
     typicky den ku dni hýbe o zlomky procenta, o pár dní starý je pro
     účely appky (risk-limit kontrola, ne HFT) stále v pořádku.
Jen když appka nemá ani čerstvý, ani použitelný záložní kurz, vrátí se pořád
None - volající kód (market_data.py, broker_t212.py) pak nástroj/den vynechá
stejně jako dřív.
"""
import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone

FRANKFURTER_URL = "https://api.frankfurter.app/latest?from={base}&to={quote}"

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data", "fx_rate_cache.json")
SANITY_THRESHOLD = 0.20        # 20 % - normální denní pohyb GBP/USD->CZK je řádově pod 1 %
FALLBACK_MAX_AGE_DAYS = 14     # starší uložený kurz appka nepoužije ani jako záložní

_rate_cache = {}


def _load_persisted_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_persisted_cache(cache):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"FX: nepodařilo se uložit trvalou zálohu kurzu (appka pokračuje dál, jen bez ní): {e}")


def _days_since(date_str):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - d).days


def _fetch_live_rate(base_currency, quote_currency):
    """Stáhne AKTUÁLNÍ kurz z Frankfurter API. Vrací float, nebo None při chybě."""
    url = FRANKFURTER_URL.format(base=base_currency, quote=quote_currency)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return float(data["rates"][quote_currency])
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError, json.JSONDecodeError) as e:
        print(f"FX: chyba při stahování kurzu {base_currency}->{quote_currency}: {e}")
        return None


def get_fx_rate(base_currency, quote_currency):
    """
    Vrátí kolik jednotek `quote_currency` dostaneš za 1 `base_currency`
    (např. get_fx_rate("USD", "CZK") -> ~23.5).

    Postup (viz POZOR výše u CACHE_FILE):
    1. Zkusí stáhnout AKTUÁLNÍ kurz.
    2. Pokud se to podaří A kurz dává smysl (sanity check proti poslednímu
       známému dobrému kurzu, pokud appka nějaký má), použije ho a uloží
       jako nový "poslední dobrý".
    3. Pokud stažení selže NEBO kurz neprojde sanity checkem, zkusí použít
       poslední známý dobrý kurz jako záložní (jen když není starší než
       FALLBACK_MAX_AGE_DAYS).
    4. Jen když ani tohle není k dispozici, appka vrátí None - volající kód
       (market_data.py, broker_t212.py) pak radši daný nástroj/den úplně
       vynechá, než aby počítal s nepřevedenou/špatnou cenou (viz POZOR
       9.9.2026 v market_data.py).
    """
    base_currency = (base_currency or "").upper()
    quote_currency = (quote_currency or "").upper()
    if not base_currency or not quote_currency or base_currency == quote_currency:
        return 1.0

    cache_key = (base_currency, quote_currency)
    if cache_key in _rate_cache:
        return _rate_cache[cache_key]

    persisted = _load_persisted_cache()
    persist_key = f"{base_currency}_{quote_currency}"
    last_good = persisted.get(persist_key)  # {"rate": ..., "date": "YYYY-MM-DD"} nebo None

    live_rate = _fetch_live_rate(base_currency, quote_currency)

    if live_rate is not None and last_good is not None:
        deviation = abs(live_rate - last_good["rate"]) / last_good["rate"]
        if deviation > SANITY_THRESHOLD:
            print(f"FX: nově stažený kurz {base_currency}->{quote_currency} ({live_rate}) se od "
                  f"posledního známého dobrého kurzu ({last_good['rate']} z {last_good['date']}) "
                  f"liší o {deviation * 100:.1f} % - appka mu nevěří, zkusí záložní kurz místo něj.")
            live_rate = None

    if live_rate is not None:
        _rate_cache[cache_key] = live_rate
        persisted[persist_key] = {
            "rate": live_rate,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        _save_persisted_cache(persisted)
        return live_rate

    if last_good is not None:
        age_days = _days_since(last_good["date"])
        if age_days is not None and age_days <= FALLBACK_MAX_AGE_DAYS:
            print(f"FX: appka pro {base_currency}->{quote_currency} použije záložní (poslední "
                  f"známý dobrý) kurz {last_good['rate']} z {last_good['date']} (stáří {age_days} "
                  f"dní) - dnešní živé stažení se nepodařilo nebo nedávalo smysl.")
            _rate_cache[cache_key] = last_good["rate"]
            return last_good["rate"]
        print(f"FX: záložní kurz pro {base_currency}->{quote_currency} appka má, ale je starý "
              f"{age_days if age_days is not None else '??'} dní (nad limit {FALLBACK_MAX_AGE_DAYS}) "
              f"- appka mu raději taky nevěří.")

    return None
