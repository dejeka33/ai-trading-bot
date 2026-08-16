"""
Odesílání push notifikací na telefon přes Telegram Bot API. Volitelné - pokud
TELEGRAM_BOT_TOKEN nebo TELEGRAM_CHAT_ID nejsou nastavené, funkce jen tiše
neudělá nic (appka funguje dál úplně stejně jako bez notifikací). Pokud API
volání selže (výpadek, špatný token...), notifikace se jen zaloguje jako
chyba a NIKDY neshodí hlavní běh - notifikace jsou "nice to have", ne kritická
součást obchodní logiky.

Návod na založení vlastního Telegram bota a zjištění chat_id je v README.md.
"""
import os

import requests

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return  # notifikace jsou volitelné - bez nastavení se appka nechová jinak

    try:
        resp = requests.post(
            TELEGRAM_API_URL.format(token=token),
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        print("Nepodařilo se odeslat Telegram notifikaci (pokračuji dál):", e)


def build_daily_summary(app_label, date_str, account, trade_results, validation_reasons):
    """
    Krátké shrnutí pro push notifikaci - na rozdíl od report.py NENÍ určené
    k detailnímu čtení, jen aby bylo na první pohled z telefonu jasné, jestli
    appka dnes něco koupila/prodala, nic neudělala, nebo byl obchod blokovaný.
    """
    lines = [f"{app_label} - {date_str}"]

    if validation_reasons:
        lines.append("Obchody NEBYLY provedeny (porušily rizikové mantinely):")
        for r in validation_reasons:
            lines.append(f"- {r}")
    elif not trade_results:
        lines.append("Bez obchodu dnes.")
    else:
        for r in trade_results:
            status = "OK" if r["status"] == "submitted" else "CHYBA"
            lines.append(f"[{status}] {r['side'].upper()} {r['qty']} {r['symbol']}")
            if r["status"] != "submitted":
                lines.append(f"  -> {r.get('error', '')}")

    lines.append(f"Hodnota portfolia: ${account['portfolio_value']:,.2f}")
    return "\n".join(lines)
