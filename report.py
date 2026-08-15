"""Generování denního reportu v Markdownu."""
from datetime import datetime, timezone


def build_report(date_str, account_before, account_after, decision, trade_results, validation_reasons):
    lines = [f"# Denní report - {date_str}", ""]

    lines.append("## Shrnutí od AI")
    lines.append(decision.get("market_summary", "(bez shrnutí)"))
    lines.append("")

    lines.append("## Stav portfolia")
    lines.append(f"- Hotovost: {account_before['cash']:.2f} USD")
    lines.append(f"- Hodnota portfolia: {account_before['portfolio_value']:.2f} USD")
    if account_after:
        lines.append(f"- Hodnota portfolia po obchodech: {account_after['portfolio_value']:.2f} USD")
    lines.append("")

    if account_before["positions"]:
        lines.append("## Otevřené pozice (před obchody)")
        lines.append("| Symbol | Množství | Prům. cena | Aktuální cena | Hodnota | Nerealizovaný P/L |")
        lines.append("|---|---|---|---|---|---|")
        for p in account_before["positions"]:
            lines.append(
                f"| {p['symbol']} | {p['qty']} | {p['avg_entry_price']:.2f} | "
                f"{p['current_price']:.2f} | {p['market_value']:.2f} | "
                f"{p['unrealized_pl']:.2f} ({p['unrealized_plpc']*100:.2f}%) |"
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
