"""
Odesílání Web Push notifikací přímo na nainstalovaný dashboard (PWA) na
telefonu - notifikace vypadá, že jde přímo z appky/ikony dashboardu, ne
z jiné appky (žádný Telegram ani jiná třetí appka).

Volitelné: bez VAPID_PRIVATE_KEY nebo bez PUSH_SUBSCRIPTION_JSON se jen nic
neodešle (appka se chová stejně jako bez notifikací). Obojí se čte z GitHub
Secrets (proměnných prostředí) - NIC z tohohle se necommituje do repozitáře,
takže i když je repozitář veřejný (kvůli GitHub Pages na zdarma účtu
typicky musí být), zůstává to v bezpečí stejně jako ostatní API klíče.
"""
import json
import os

from pywebpush import webpush, WebPushException

# VAPID vyžaduje kontakt v "sub" claimu (mailto: nebo https: URL - ČISTĚ
# origin, bez cesty, jinak to knihovna odmítne). Nemá smysl do zdrojáku
# (repo může být veřejné) dávat soukromý e-mail, takhle stačí - GitHub Pages
# origin je stejně veřejně vidět přes samotný dashboard.
VAPID_CLAIMS_SUB = "https://dejeka33.github.io"


def build_short_summary(trade_results, validation_reasons):
    """Jednořádkové shrnutí obchodu - text push notifikace."""
    if validation_reasons:
        return "Obchody blokovány rizikovými mantinely"
    if not trade_results:
        return "Bez obchodu dnes"
    parts = [
        f"{r['side'].upper()} {r['qty']} {r['symbol']}"
        for r in trade_results if r["status"] == "submitted"
    ]
    return ", ".join(parts) if parts else "Obchod se nepodařil provést"


def send_web_push(title, body, url="./"):
    private_key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    subscription_raw = os.environ.get("PUSH_SUBSCRIPTION_JSON", "").strip()
    if not private_key or not subscription_raw:
        return

    try:
        subscription_info = json.loads(subscription_raw)
        webpush(
            subscription_info=subscription_info,
            data=json.dumps({"title": title, "body": body, "url": url}),
            vapid_private_key=private_key,
            vapid_claims={"sub": VAPID_CLAIMS_SUB},
        )
    except WebPushException as e:
        print("Nepodařilo se odeslat web push notifikaci (pokračuji dál):", e)
    except Exception as e:
        print("Chyba při přípravě web push notifikace (pokračuji dál):", e)
