"""
Hlavní denní běh: stáhne data -> zeptá se AI na rozhodnutí -> zvaliduje proti
mantinelům -> provede obchody -> vygeneruje a uloží report.

Spouští se přes GitHub Actions (viz .github/workflows/daily_trading.yml).
Vyžaduje proměnné prostředí: ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY,
ALPACA_API_BASE_URL, ALPACA_PAPER, ANTHROPIC_API_KEY.
"""
import os
from datetime import datetime, timezone

from data_fetch import get_clients, get_account_snapshot, get_recent_bars
from risk_rules import load_risk_limits, allowed_symbols, validate_decision
from decision import get_decision
from execute import execute_trades
from report import build_report


def main():
    limits = load_risk_limits()
    stocks, crypto = allowed_symbols(limits)

    trading_client, stock_data_client, crypto_data_client = get_clients()

    account_before = get_account_snapshot(trading_client)
    bars = get_recent_bars(stock_data_client, crypto_data_client, stocks, crypto)

    decision = get_decision(account_before, bars, limits)

    ok, reasons = validate_decision(decision, limits, account_before)

    trade_results = []
    if ok and decision.get("trades"):
        trade_results = execute_trades(trading_client, decision["trades"])
    elif not ok:
        print("Rozhodnutí porušilo mantinely, obchody se neprovedou:", reasons)

    account_after = get_account_snapshot(trading_client) if trade_results else None

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_md = build_report(date_str, account_before, account_after, decision, trade_results, reasons if not ok else [])

    os.makedirs("reports", exist_ok=True)
    report_path = f"reports/{date_str}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_md)

    print(report_md)

    # Pro GitHub Actions step summary (pěkně vidět report přímo v UI běhu)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(report_md)


if __name__ == "__main__":
    main()
