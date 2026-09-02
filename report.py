"""Generování denního reportu v Markdownu."""
from datetime import datetime, timezone


def build_report(date_str, account_before, account_after, decision, trade_results, validation_reasons,
                  dividend_net=0.0):
    lines = [f"# Denní report - {date_str}", ""]

    lines.append("## Shrnutí od AI")
    lines.append(decision.get("market_summary", "(bez shrnutí)"))
    lines.append("")

    # Trading 212 účet může být v jakékoliv měně (u tohoto pilota CZK, ne USD
    # jako dřív u Alpaky) - bere se přímo z account snapshotu, ne natvrdo.
    currency = account_before.get("currency", "USD")

    lines.append("## Stav portfolia")
    lines.append(f"- Hotovost: {account_before['cash']:.2f} {currency}")
    lines.append(f"- Hodnota portfolia: {account_before['portfolio_value']:.2f} {currency}")
    if account_after:
        lines.append(f"- Hodnota portfolia po obchodech: {account_after['portfolio_value']:.2f} {currency}")
    # POZOR - přidáno 2.9.2026 (viz main.compute_dividend_delta) - řádek se
    # zobrazí jen v den, kdy appka nějakou dividendu skutečně zaznamenala.
    if dividend_net:
        lines.append(f"- Dividendy dnes: +{dividend_net:.2f} {currency}")
    lines.append("")

    if account_before["positions"]:
        lines.append("## Otevřené pozice (před obchody)")
        lines.append("| Symbol | Množství | Prům. cena | Aktuální cena | Hodnota | Nerealizovaný P/L |")
        lines.append("|---|---|---|---|---|---|")
        for p in account_before["positions"]:
            lines.append(
                f"| {p['symbol']} | {p['qty']} | {p['avg_entry_price']:.2f} | "
                f"{p['current_price']:.2f} | {p['market_value']:.2f} | "
                # POZOR - bug nalezený 21.8.2026: unrealized_plpc z broker_t212.py je
                # UŽ v procentních bodech (např. -1.77 = -1.77 %, ne -0.0177), protože
                # tam se počítá jako "... * 100". Tady se násobilo *100 podruhé, takže
                # report ukazoval -177.08 % místo -1.77 % (viz denní report 21.8.2026 -
                # AAPL, MSFT, CSPX měly nesmyslně vysoké P/L procenta).
                f"{p['unrealized_pl']:.2f} ({p['unrealized_plpc']:.2f}%) |"
            )
        lines.append("")

    lines.append("## Rozhodnutí a provedené obchody")
    if validation_reasons:
        lines.append("**Obchody NEBYLY provedeny - porušily rizikové mantinely:**")
        for r in validation_reasons:
            lines.append(f"- {r}")
    elif not trade_results:
        lines.append("Dnes AI nenavrhla žádný obchod.")
    else:
        lines.append("| Symbol | Strana | Množství | Stav | Důvod |")
        lines.append("|---|---|---|---|---|")
        for r in trade_results:
            status = "✅ provedeno" if r["status"] == "submitted" else f"❌ chyba: {r.get('error')}"
            lines.append(f"| {r['symbol']} | {r['side']} | {r['qty']} | {status} | {r.get('reasoning','')} |")
    lines.append("")

    lines.append(f"_Vygenerováno automaticky {datetime.now(timezone.utc).isoformat()} UTC._")
    return "\n".join(lines)
