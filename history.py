"""
Udržuje strukturovanou historii běhů v docs/data/history.json - z tohoto
souboru čte dashboard (docs/index.html) publikovaný přes GitHub Pages.
"""
import json
import os

HISTORY_PATH = "docs/data/history.json"


def load_history():
    if not os.path.exists(HISTORY_PATH):
        return {"starting_value": None, "entries": []}
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def update_history(date_str, account_current, decision, trade_results, validation_reasons,
                    spy_price=None, realized_pl_delta=0.0,
                    cash_flow_net=0.0, cash_flow_items=None, cash_flow_check=None,
                    dividend_net=0.0, dividend_items=None, dividend_check=None):
    """
    `realized_pl_delta`: kolik appka dnes realizovala prodejem (viz
    main.compute_realized_pl_delta) - kumulativní součet (realized_pl_cum) si
    tahle funkce počítá SAMA z předchozího dne, aby to nemusel řešit main.py.

    `cash_flow_net`, `cash_flow_items`, `cash_flow_check`: POZOR - přidáno
    21.8.2026 kvůli rozeznání ručních vkladů/výběrů na T212 účtu mimo appku
    (viz main.compute_cash_flow_delta a broker_t212.get_cash_flows) - bez
    tohohle by dashboard vklad omylem vykazoval jako zisk appky. cash_flow_net
    je čistý součet dnešních vkladů/výběrů (kladné = vklad), cash_flow_items
    jsou syrové položky z T212 API (pro detail/audit), cash_flow_check je nové
    "poslední zkontrolované" razítko, které se uloží na kořenovou úroveň
    (mimo jednotlivé dny) - viz load_history().

    `dividend_net`, `dividend_items`, `dividend_check`: POZOR - přidáno
    2.9.2026 stejným principem jako cash_flow_* výše, ale opačný účel - viz
    main.compute_dividend_delta a broker_t212.get_dividends. Dividenda se na
    rozdíl od vkladu NEODEČÍTÁ z výkonu appky (je to skutečný investiční
    výnos), appka si jen dnešní součet ukládá pro zobrazení (samostatná
    kartička na dashboardu) a pro kumulativní součet dividends_cum.
    """
    data = load_history()

    if data["starting_value"] is None:
        data["starting_value"] = account_current["portfolio_value"]

    # Kumulativní pole (realized_pl_cum, net_deposits_cum) se počítají vůči
    # PŘEDCHOZÍMU dni, ne vůči poslednímu záznamu v poli jak byl uložený předtím -
    # kdyby dnešní datum v historii už existovalo (ruční re-run stejného dne),
    # bral by se jako "předchozí" omylem vlastní dřívější běh dneška a delta by
    # se sečetla dvakrát. Proto se dnešní záznam napřed z výpočtu vyřadí.
    entries_before_today = [e for e in data["entries"] if e["date"] != date_str]
    prev_entry = entries_before_today[-1] if entries_before_today else None
    prev_realized_pl_cum = prev_entry.get("realized_pl_cum", 0.0) if prev_entry else 0.0
    prev_net_deposits_cum = prev_entry.get("net_deposits_cum", 0.0) if prev_entry else 0.0
    prev_dividends_cum = prev_entry.get("dividends_cum", 0.0) if prev_entry else 0.0

    realized_pl_cum = prev_realized_pl_cum + (realized_pl_delta or 0.0)
    net_deposits_cum = prev_net_deposits_cum + (cash_flow_net or 0.0)
    dividends_cum = prev_dividends_cum + (dividend_net or 0.0)

    entry = {
        "date": date_str,
        "portfolio_value": account_current["portfolio_value"],
        "cash": account_current["cash"],
        "buying_power": account_current["buying_power"],
        # Měna účtu (viz broker_t212.get_account_snapshot) - appka je od pilota na
        # Trading 212 vedená v CZK, ne v USD jako dřív u Alpaky. Dashboard (docs/*.html)
        # tohle pole čte, aby nezobrazoval natvrdo "$" u částky v jiné měně.
        "currency": account_current.get("currency", "CZK"),
        "positions": account_current["positions"],
        "market_summary": decision.get("market_summary", ""),
        "trades": trade_results,
        "trade_count": len(trade_results),
        "blocked_reasons": validation_reasons,
        # Volitelná pole pro dashboard (benchmark "drž a čekej SPY" a kumulativní
        # realizovaný zisk/ztráta) - u dní před zavedením tohoto trackování chybí,
        # dashboard s tím počítá a benchmark/realizovaný graf zobrazí až od chvíle,
        # kdy jsou data k dispozici.
        "spy_price": spy_price,
        "realized_pl_cum": realized_pl_cum,
        # Vklady/výběry mimo appku (viz POZOR výše) - u dní před 21.8.2026 tahle
        # pole chybí/jsou 0, dashboard to bere jako "žádný vklad ten den".
        "cash_flow_net": cash_flow_net or 0.0,
        "cash_flow_items": cash_flow_items or [],
        "net_deposits_cum": net_deposits_cum,
        # Dividendy (viz POZOR výše) - u dní před 2.9.2026 tahle pole chybí/jsou 0,
        # dashboard to bere jako "žádná dividenda ten den".
        "dividend_net": dividend_net or 0.0,
        "dividend_items": dividend_items or [],
        "dividends_cum": dividends_cum,
    }

    # Pokud dnešní datum už v historii je (např. ruční re-run stejný den), přepiš ho
    data["entries"] = entries_before_today
    data["entries"].append(entry)
    data["entries"].sort(key=lambda e: e["date"])

    if cash_flow_check is not None:
        data["last_cash_flow_check"] = cash_flow_check
    if dividend_check is not None:
        data["last_dividend_check"] = dividend_check

    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    return data
