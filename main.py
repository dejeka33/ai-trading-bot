"""
Hlavní denní běh: stáhne data -> zeptá se AI na rozhodnutí -> zvaliduje proti
mantinelům -> provede obchody -> vygeneruje a uloží report.

Spouští se přes GitHub Actions (viz .github/workflows/daily_trading.yml).
Vyžaduje proměnné prostředí: ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY,
ALPACA_API_BASE_URL, ALPACA_PAPER, ANTHROPIC_API_KEY.
"""
import os
from datetime import datetime, timezone

from data_fetch import get_clients, get_account_snapshot, get_recent_bars, get_recent_news
from risk_rules import load_risk_limits, allowed_symbols, validate_decision
from decision import get_decision
from execute import execute_trades
from report import build_report
from history import load_history, update_history
from fred_data import get_macro_context
from webpush_notify import send_web_push, build_short_summary


def compute_realized_pl_delta(account_before, trade_results):
    """
    Odhad realizovaného zisku/ztráty z dnešních prodejů, pro dashboard (karta
    "Výkonnost"). Jako realizační cenu použije cenu pozice v okamžiku
    rozhodování (account_before) - u tržních příkazů na paper účtu je rozdíl
    oproti přesné fill ceně zanedbatelný, ale nejde o stoprocentně přesné
    číslo (Alpaca vrací fill cenu tržního příkazu až asynchronně po
    vykonání, tady bychom na ni museli čekat/pollovat).
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
    stocks, crypto = allowed_symbols(limits)

    trading_client, stock_data_client, crypto_data_client, news_client = get_clients()

    account_before = get_account_snapshot(trading_client)
    bars = get_recent_bars(stock_data_client, crypto_data_client, stocks, crypto)
    news = get_recent_news(news_client, stocks)
    # FRED je volitelný - pokud FRED_API_KEY není nastavený, macro bude None
    # a bot pokračuje úplně stejně jako dřív (viz fred_data.get_macro_context).
    macro = get_macro_context()

    decision = get_decision(account_before, bars, limits, news=news, macro=macro)

    ok, reasons = validate_decision(decision, limits, account_before)

    trade_results = []
    if ok and decision.get("trades"):
        trade_results = execute_trades(trading_client, decision["trades"])
    elif not ok:
        print("Rozhodnutí porušilo mantinely, obchody se neprovedou:", reasons)

    # Vždy zjistíme aktuální stav účtu (i beze dnů bez obchodu se mohla změnit
    # hodnota otevřených pozic vlivem pohybu trhu) - používá se pro report i dashboard.
    account_after = get_account_snapshot(trading_client)

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

    # SPY cena pro dashboard (srovnání "drž a čekej SPY") - SPY je vždy mezi
    # povolenými nástroji v risk_limits.yaml, takže bars["SPY"] existuje bez
    # ohledu na to, jestli bot SPY zrovna drží.
    spy_price = None
    if "SPY" in bars and bars["SPY"]:
        spy_price = bars["SPY"][-1]["c"]

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
    send_web_push(
        "AI Trading Bot (dlouhodobý)",
        f"{build_short_summary(trade_results, blocked)} — ${account_after['portfolio_value']:,.2f}",
    )

    # Pro GitHub Actions step summary (pěkně vidět report přímo v UI běhu)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(report_md)


if __name__ == "__main__":
    main()
