"""
Hlavní denní běh: stáhne data -> zeptá se AI na rozhodnutí -> zvaliduje proti
mantinelům -> provede obchody -> vygeneruje a uloží report.

Spouští se přes GitHub Actions (viz .github/workflows/daily_trading.yml).

PILOTNÍ VERZE NA TRADING 212 (2026-08): nahrazuje dřívější Alpaca verzi (viz
git historie pro původní data_fetch.py/execute.py, pokud by bylo někdy potřeba
se vrátit). Vyžaduje proměnné prostředí: T212_API_ID, T212_API_KEY (Trading 212
API - viz broker_t212.py), ANTHROPIC_API_KEY. Volitelně T212_BASE_URL (default
demo/paper - viz broker_t212.py), EODHD_API_KEY (záložní zdroj dat - viz
market_data.py), FRED_API_KEY.

Krypto (BTC/ETH) v této verzi appka NEOBCHODUJE - Trading 212 Crypto je
samostatný účet mimo Invest/ISA, na který se beta obchodovací API nevztahuje.
"""
import os
from datetime import datetime, timezone

from instruments import INSTRUMENTS
import market_data
import broker_t212
from risk_rules import load_risk_limits, allowed_symbols, validate_decision
from decision import get_decision
from report import build_report
from history import load_history, update_history
from fred_data import get_macro_context
from webpush_notify import send_web_push, build_short_summary


def compute_realized_pl_delta(account_before, trade_results):
    """
    Odhad realizovaného zisku/ztráty z dnešních prodejů, pro dashboard (karta
    "Výkonnost"). Jako realizační cenu použije cenu pozice v okamžiku
    rozhodování (account_before) - u tržních příkazů je rozdíl oproti přesné
    fill ceně zanedbatelný, ale nejde o stoprocentně přesné číslo.
    """
    positions_by_symbol = {p["symbol"]: p for p in account_before.get("positions", [])}
    delta = 0.0
    for t in trade_results:
        if t.get("status") != "submitted" or t.get("side") != "sell":
            continue
        pos = positions_by_symbol.get(t.get("symbol"))
        if not pos:
            continue
        delta += t.get("qty", 0) * (pos["current_price"] - pos["avg_entry_price"])
    return delta


def main():
    limits = load_risk_limits()
    stocks, crypto = allowed_symbols(limits)  # crypto bude vždy [] v této verzi

    # Jen ty nástroje z instruments.py, které jsou zároveň povolené v risk_limits.yaml -
    # kdyby se risk_limits.yaml zúžilo, appka si o data řekne jen pro to, co smí obchodovat.
    active_instruments = {s: INSTRUMENTS[s] for s in stocks if s in INSTRUMENTS}
    missing = [s for s in stocks if s not in INSTRUMENTS]
    if missing:
        print(f"POZOR: symboly {missing} jsou v risk_limits.yaml, ale chybí v instruments.py "
              f"(nemají ISIN/datové tickery) - appka je bude ignorovat.")

    account_before = broker_t212.get_account_snapshot(INSTRUMENTS)
    # account_currency: appka ceny nástrojů převádí do měny účtu (viz fx.py) -
    # bez tohohle by risk_rules.py porovnávala cenu v GBP/USD přímo proti
    # mantinelu v CZK (viz POZOR o měnách v instruments.py).
    bars = market_data.get_recent_bars(active_instruments, account_currency=account_before.get("currency"))

    # Zprávy (dřív Alpaca News API) v pilotní verzi zatím nejsou - decision.py
    # umí fungovat i bez nich (viz news_section fallback v build_prompt).
    news = None

    # FRED je volitelný a nezávislý na brokerovi - pokud FRED_API_KEY není
    # nastavený, macro bude None a appka pokračuje úplně stejně jako dřív.
    macro = get_macro_context()

    decision = get_decision(account_before, bars, limits, news=news, macro=macro)

    # Aktuální ceny z nezávislého zdroje (tržní data, ne to, co si spočítala AI) -
    # slouží k přepočtu qty * cena při validaci, viz risk_rules.validate_decision.
    prices = {symbol: series[-1]["c"] for symbol, series in bars.items() if series}
    ok, reasons = validate_decision(decision, limits, account_before, prices=prices)

    trade_results = []
    if ok and decision.get("trades"):
        trade_results = broker_t212.execute_trades(decision["trades"], INSTRUMENTS)
    elif not ok:
        print("Rozhodnutí porušilo mantinely, obchody se neprovedou:", reasons)

    # Vždy zjistíme aktuální stav účtu (i beze dnů bez obchodu se mohla změnit
    # hodnota otevřených pozic vlivem pohybu trhu) - používá se pro report i dashboard.
    # get_settled_account_snapshot (ne obyčejný get_account_snapshot) počká, dokud
    # se dnešní nákupy reálně nepropíšou do pozic - viz POZOR v broker_t212.py.
    account_after = broker_t212.get_settled_account_snapshot(INSTRUMENTS, trade_results)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_md = build_report(
        date_str, account_before,
        account_after if trade_results else None,
        decision, trade_results, reasons if not ok else [],
    )

    os.makedirs("reports", exist_ok=True)
    report_path = f"reports/{date_str}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_md)

    # CSPX cena pro dashboard (srovnání "drž a čekej") - nahrazuje dřívější SPY,
    # ze stejného důvodu (PRIIPs) jako všude jinde v této appce.
    spy_price = None
    if "CSPX" in bars and bars["CSPX"]:
        spy_price = bars["CSPX"][-1]["c"]

    prev_history = load_history()
    prev_realized_pl_cum = (
        prev_history["entries"][-1].get("realized_pl_cum") if prev_history["entries"] else None
    )
    realized_pl_delta = compute_realized_pl_delta(account_before, trade_results)
    realized_pl_cum = (prev_realized_pl_cum or 0.0) + realized_pl_delta

    update_history(
        date_str, account_after, decision, trade_results, reasons if not ok else [],
        spy_price=spy_price, realized_pl_cum=realized_pl_cum,
    )

    print(report_md)

    # Push notifikace na telefon přímo z ikony dashboardu (volitelné - viz
    # webpush_notify.py). Posílá se vždy account_after (aktuální stav po
    # případných obchodech).
    blocked = reasons if not ok else []
    currency = account_after.get("currency", "GBP")
    send_web_push(
        "AI Trading Bot (dlouhodobý)",
        f"{build_short_summary(trade_results, blocked)} — {account_after['portfolio_value']:,.2f} {currency}",
    )

    # Pro GitHub Actions step summary (pěkně vidět report přímo v UI běhu)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(report_md)


if __name__ == "__main__":
    main()
