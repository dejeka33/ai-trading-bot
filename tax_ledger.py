"""
Evidence pro daňové účely (kapitálové zisky/ztráty z prodeje akcií, hrubý
příjem z prodejů, dividendy) - viz docs/data/tax_ledger.json.

POZOR - přidáno 2.9.2026, DŮLEŽITÉ: tohle je jen EVIDENCE (přehledná data pro
tebe/účetního), NE daňové doporučení a NE vstup do rozhodování AI. Záměrně se
tenhle modul NIKDY neposílá do decision.py/build_prompt - AI dál obchoduje jen
podle tržních dat/zpráv/mantinelů, přesně jako dřív. Kdyby appka nechala AI
"vědět", že už má/nemá splněný časový test, riskovalo by to, že si appka začne
sama pro sebe zdůvodňovat "nesmím prodat, i když by to mantinely/data
doporučovaly, kvůli dani" - to by mísilo dva různé cíle (výkon vs. daňová
optimalizace) a dělalo z appky hůř předvídatelnou. Appka radši drží obchodní
rozhodování a daňovou evidenci jako dva zcela oddělené systémy.

Appka sleduje jednotlivé nákupní "dávky" (lot) po symbolech a při prodeji je
páruje metodou FIFO (první nakoupené se appkou první prodá) - běžná a zákonem
akceptovaná výchozí metoda pro vyhodnocení 3letého časového testu (§4 odst. 1
písm. w) zákona o daních z příjmů - akcie držené déle než 3 roky jsou od daně
z příjmu osvobozené). U KAŽDÉHO prodeje appka sama spočítá, jestli byl časový
test splněný - je to ale jen INFORMATIVNÍ odhad, poslední slovo má vždycky
účetní/daňový poradce (appka nezná tvůj kompletní daňový kontext - jiné
příjmy, případné obchody mimo appku, apod.).

Appka jako nákupní/prodejní cenu bere tržní cenu použitou appkou pro dané
rozhodnutí (`prices` v main.py, stejná cena jako u risk_rules.validate_decision) -
je to PŘIBLIŽNÁ, ne přesná fill cena z brokera (T212 API appce přesnou fill
cenu nevrací) - stejný princip appka už používá u main.annotate_realized_pl
(realizovaný P/L na dashboardu). Pro podklady k opravdovému daňovému přiznání
si vždycky over skutečné údaje přímo v Trading 212 (Historie/výpisy) - appka
tohle bere jen jako orientační pomůcku pro průběžný přehled, ne jako finální
zdroj pravdy.
"""
import json
import os
from datetime import datetime

LEDGER_PATH = "docs/data/tax_ledger.json"


def load_ledger():
    if not os.path.exists(LEDGER_PATH):
        return {"open_lots": {}, "realized": [], "annual": {}}
    with open(LEDGER_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("open_lots", {})
    data.setdefault("realized", [])
    data.setdefault("annual", {})
    return data


def save_ledger(data):
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    with open(LEDGER_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _year(date_str):
    return (date_str or "")[:4] or None


def _time_test_met(buy_date_str, sell_date_str):
    """3letý časový test (§4 odst. 1 písm. w) zákona o daních z příjmů) -
    appka to vyhodnotí jako "prodáno aspoň 3 kalendářní roky po nákupu"
    (datum nákupu + 3 roky <= datum prodeje). Vrací None, pokud appka nemá
    spolehlivé datum nákupu (viz "odhadnuté" počáteční dávky u
    seed_initial_lots, nebo dávka, o které appka vůbec neví - viz
    record_sell)."""
    if not buy_date_str or not sell_date_str:
        return None
    try:
        buy = datetime.strptime(buy_date_str[:10], "%Y-%m-%d")
        sell = datetime.strptime(sell_date_str[:10], "%Y-%m-%d")
        threshold = buy.replace(year=buy.year + 3)
        return sell >= threshold
    except (ValueError, TypeError):
        return None


def _ensure_annual(data, year):
    if not year:
        return None
    data.setdefault("annual", {})
    data["annual"].setdefault(year, {"gross_sale_proceeds": 0.0, "dividends_total": 0.0})
    return data["annual"][year]


def seed_initial_lots(data, positions, date_str):
    """
    Appka evidenci nákupních dávek vede až od nasazení téhle featury (2.9.2026) -
    pozice, které appka drží už teď (nakoupené appkou dřív, appka si přesné
    datum nákupu nezaznamenala), appka "nasadí" jako jednu dávku k DNEŠNÍMU dni,
    ale označí ji "estimated": true - časový test u ní bude zkreslený (appka
    nezná SKUTEČNÉ datum nákupu), dokud se pozice celá neprodá a nenahradí
    novými, appkou už sledovanými dávkami. Appka funkci volá KAŽDÝ běh, ale je
    neškodná/idempotentní - symbol, který už appka sleduje, přeskočí, takže se
    nic nepřepíše.
    """
    for p in positions:
        symbol = p.get("symbol")
        qty = p.get("qty") or 0
        if not symbol or qty <= 0 or symbol in data["open_lots"]:
            continue
        data["open_lots"][symbol] = [{
            "date": date_str,
            "qty": float(qty),
            "price": float(p.get("avg_entry_price") or 0.0),
            "estimated": True,
        }]


def record_buy(data, symbol, date_str, qty, price):
    if not symbol or not qty or qty <= 0 or price is None:
        return
    data.setdefault("open_lots", {})
    data["open_lots"].setdefault(symbol, [])
    data["open_lots"][symbol].append({
        "date": date_str, "qty": float(qty), "price": float(price), "estimated": False,
    })


def record_sell(data, symbol, date_str, qty, price):
    """
    FIFO spáruje prodej s nejstaršími otevřenými dávkami daného symbolu a
    appka za každou spárovanou (část) dávky uloží jeden "realized" záznam
    (buy_date/sell_date/qty/ceny/zisk/časový test). Pokud appka nemá dost
    evidovaných otevřených kusů (např. appka o starším nákupu neví), zbytek
    appka spáruje jako "neznámou" dávku (buy_date None, time_test_met None) -
    hrubý příjem z prodeje (limit 100 000 Kč) appka i tak správně napočítá,
    jen bez spolehlivého data nákupu pro časový test.
    """
    if not symbol or not qty or qty <= 0 or price is None:
        return []
    remaining = float(qty)
    lots = data.setdefault("open_lots", {}).get(symbol, [])
    realized_entries = []

    while remaining > 1e-9 and lots:
        lot = lots[0]
        take = min(remaining, lot["qty"])
        buy_date = lot["date"]
        buy_price = lot["price"]
        cost_basis = take * buy_price
        proceeds = take * price
        realized_entries.append({
            "symbol": symbol,
            "buy_date": buy_date,
            "sell_date": date_str,
            "qty": take,
            "buy_price": buy_price,
            "sell_price": price,
            "cost_basis": cost_basis,
            "proceeds": proceeds,
            "gain": proceeds - cost_basis,
            "time_test_met": _time_test_met(buy_date, date_str),
            "estimated_buy_date": bool(lot.get("estimated")),
        })
        lot["qty"] -= take
        remaining -= take
        if lot["qty"] <= 1e-9:
            lots.pop(0)

    if remaining > 1e-9:
        realized_entries.append({
            "symbol": symbol,
            "buy_date": None,
            "sell_date": date_str,
            "qty": remaining,
            "buy_price": None,
            "sell_price": price,
            "cost_basis": None,
            "proceeds": remaining * price,
            "gain": None,
            "time_test_met": None,
            "estimated_buy_date": None,
        })

    data.setdefault("realized", [])
    data["realized"].extend(realized_entries)

    annual = _ensure_annual(data, _year(date_str))
    if annual is not None:
        annual["gross_sale_proceeds"] += sum(e["proceeds"] for e in realized_entries)

    return realized_entries


def record_dividend_for_tax(data, date_str, amount):
    """Přičte dividendu do ročního přehledu (`annual[rok].dividends_total`) -
    dividendy appka na rozdíl od kapitálových zisků/ztrát NEpáruje s dávkami
    (nesouvisí s konkrétním nákupem), jen je appka sčítá po kalendářních
    letech (dividendy appka zdaňuje vždy, bez limitu, viz main.py)."""
    if not amount:
        return
    annual = _ensure_annual(data, _year(date_str))
    if annual is not None:
        annual["dividends_total"] += float(amount)
