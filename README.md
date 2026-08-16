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

## Push notifikace na telefon (volitelné)

Appka umí po každém běhu poslat krátké shrnutí (obchod proveden/neproveden/blokován)
přes Telegram bota. Bez nastavení se appka chová úplně stejně jako dřív, jen
notifikace neposílá.

1. V Telegramu si napiš s **@BotFather**, pošli mu `/newbot` a projdi krátký
   dialog (jméno bota, uživatelské jméno musí končit na `bot`) - na konci ti
   dá **token** (dlouhý řetězec).
2. Napiš svému novému botovi jakoukoliv zprávu (např. "ahoj"), aby věděl, komu
   má psát zpátky.
3. Otevři v prohlížeči (nahraď TOKEN svým): `https://api.telegram.org/botTOKEN/getUpdates`
   - ve výsledku najdeš `"chat":{"id": ČÍSLO, ...}` - to číslo je tvoje `chat_id`.
4. Přidej do GitHub Secrets tohoto repozitáře:
   - `TELEGRAM_BOT_TOKEN` (token z kroku 1)
   - `TELEGRAM_CHAT_ID` (číslo z kroku 3)

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
- `notify.py` - volitelné push notifikace přes Telegram
- `report.py` - generování denního reportu
- `main.py` - spojuje všechno dohromady
- `reports/` - sem se ukládají denní reporty (commitují se zpět do repa)
