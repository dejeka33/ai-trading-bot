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
                    spy_price=None, realized_pl_cum=None):
    data = load_history()

    if data["starting_value"] is None:
        data["starting_value"] = account_current["portfolio_value"]

    entry = {
        "date": date_str,
        "portfolio_value": account_current["portfolio_value"],
        "cash": account_current["cash"],
        "buying_power": account_current["buying_power"],
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
    }

    # Pokud dnešní datum už v historii je (např. ruční re-run stejný den), přepiš ho
    data["entries"] = [e for e in data["entries"] if e["date"] != date_str]
    data["entries"].append(entry)
    data["entries"].sort(key=lambda e: e["date"])

    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    return data
