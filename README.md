# AI trading bot (Alpaca paper trading + Claude)

Denní automatický běh: stáhne tržní data z Alpaca, pošle je Claude modelu k
rozhodnutí, zkontroluje návrh proti rizikovým mantinelům, provede schválené
obchody a uloží/zveřejní report. Běží přes GitHub Actions, ne uvnitř Cowork
(kvůli síťovým omezením cloudového sandboxu).

## Co je potřeba nastavit (jednorázově)

1. **Vytvoř GitHub repozitář** (klidně privátní) a nahraj do něj tento
   adresář (`trading_bot/`) - všechny soubory KROMĚ `config/.env`, ten
   zůstává jen lokálně (a je i tak v `.gitignore`, takže by se sám
   nenahrál).

2. **Anthropic API klíč** - jdi na https://console.anthropic.com,
   vytvoř API klíč (je to jiný účet/systém než Cowork/claude.ai, platí se
   zvlášť podle spotřeby - viz odhad nákladů níže).

3. V nastavení GitHub repozitáře: **Settings → Secrets and variables →
   Actions → New repository secret** a přidej tyto 4 hodnoty:
   - `ALPACA_API_KEY_ID`
   - `ALPACA_API_SECRET_KEY`
   - `ALPACA_API_BASE_URL` (hodnota: `https://paper-api.alpaca.markets/v2`)
   - `ANTHROPIC_API_KEY`

4. V GitHub repozitáři je již připravený workflow
   `.github/workflows/daily_trading.yml` - spouští se automaticky každý den
   ve 21:30 UTC, nebo ho můžeš ručně spustit přes záložku **Actions →
   Daily AI Trading Run → Run workflow**.

5. Doporučuju první běh spustit ručně (workflow_dispatch) a zkontrolovat
   výstup, než necháš běžet automaticky každý den.

## Push notifikace přímo z ikony dashboardu (volitelné)

Appka umí po každém běhu poslat push notifikaci nainstalovaného dashboardu
(PWA) na telefonu - vypadá, že jde přímo z appky, žádná další appka (Telegram
apod.) není potřeba. Bez nastavení se appka chová úplně stejně jako dřív, jen
notifikace neposílá.

Obě hodnoty níže (`VAPID_PRIVATE_KEY` i výsledek "přihlášení" z dashboardu)
se ukládají jen jako GitHub Secrets, NIKDY jako obyčejný soubor v
repozitáři - i když je repozitář veřejný (kvůli GitHub Pages na zdarma účtu
typicky musí být), secrets zůstávají skryté stejně jako ostatní API klíče.

1. Otevři si dashboard appky na telefonu (v prohlížeči, ideálně tu
   nainstalovanou verzi) a klikni na tlačítko **"Zapnout push notifikace"**
   nahoře pod nadpisem. Povol notifikace, když se o to prohlížeč zeptá.
2. Zobrazí se text (JSON) - zkopíruj ho celý.
3. Přidej do GitHub Secrets tohoto repozitáře (Settings → Secrets and
   variables → Actions → New repository secret):
   - `PUSH_SUBSCRIPTION_JSON` (text z kroku 2, vlož ho celý přesně jak je)
   - `VAPID_PRIVATE_KEY` - hodnotu ti dám já (je to vygenerovaný
     kryptografický klíč, ne heslo k ničemu tvému)
4. Pokud by notifikace časem přestaly chodit (prohlížeč umí odhlášení
   samo zneplatnit), stačí zopakovat kroky 1-3 (přepsat starý secret novým
   textem).

## Odhad nákladů

- GitHub Actions: zdarma (běžný denní běh trvá řádově desítky sekund až
  pár minut, hluboko pod bezplatným limitem).
- Anthropic API: řádově 2-3 centy na jedno denní rozhodnutí (při použití
  levnějšího modelu Haiku ještě méně) - v přepočtu zhruba 20-40 Kč/měsíc.

## Struktura projektu

- `config/risk_limits.yaml` - rizikové mantinely (uprav podle sebe)
- `data_fetch.py` - stahování dat z Alpaca
- `fred_data.py` - volitelný makro kontext z FRED
- `decision.py` - dotaz na Claude a strukturované rozhodnutí
- `risk_rules.py` - validace rozhodnutí proti mantinelům
- `execute.py` - provedení obchodů
- `webpush_notify.py` - volitelné push notifikace přímo do ikony dashboardu (Web Push)
- `report.py` - generování denního reportu
- `main.py` - spojuje všechno dohromady
- `reports/` - sem se ukládají denní reporty (commitují se zpět do repa)
