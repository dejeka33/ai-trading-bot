"""
Odesílání Web Push notifikací přímo na nainstalovaný dashboard (PWA) na
telefonu - na rozdíl od notify.py (Telegram) notifikace vypadá, že jde
přímo z appky/ikony dashboardu, ne z jiné appky.

Volitelné a nezávislé na notify.py - dá se používat jedno, druhé, nebo obojí
najednou. Bez VAPID_PRIVATE_KEY nebo bez data/push_subscription.json se jen
nic neodešle (appka se chová stejně jako bez notifikací).

Předpoklad: uživatel si na telefonu otevřel dashboard (docs/index.html),
klikl na "Zapnout push notifikace", zkopíroval vygenerovaný JSON a ten byl
uložen sem: data/push_subscription.json (mimo docs/, aby nebyl veřejně
dostupný přes GitHub Pages - GitHub Pages servíruje jen obsah docs/).
"""
import json
import os

from pywebpush import webpush, WebPushException

SUBSCRIPTION_PATH = "data/push_subscription.json"
# VAPID vyžaduje kontakt v "sub" claimu (mailto: nebo https: URL - ČISTĚ
# origin, bez cesty, jinak to knihovna odmítne). Nemá smysl do zdrojáku
# (repo může být veřejné) dávat soukromý e-mail, takhle stačí - GitHub Pages
# origin je stejně veřejně vidět přes samotný dashboard.
VAPID_CLAIMS_SUB = "https://dejeka33.github.io"


def send_web_push(title, body, url="./"):
    private_key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not private_key or not os.path.exists(SUBSCRIPTION_PATH):
        return

    try:
        with open(SUBSCRIPTION_PATH, "r", encoding="utf-8") as f:
            subscription_info = json.load(f)

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
