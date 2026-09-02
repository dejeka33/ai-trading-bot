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
from risk_rules import (
    load_risk_limits, allowed_symbols, validate_decision,
    clip_oversized_trades, clip_concentrated_trades,
)
from decision import get_decision
from news_data import get_recent_news
from report import build_report
from history import load_history, update_history
from fred_data import get_macro_context
from webpush_notify import send_web_push, build_short_summary
import tax_ledger


def annotate_realized_pl(account_before, trade_results):
    """
    POZOR - přidáno 21.8.2026 kvůli kartě "Uzavřené obchody" na dashboardu
    (docs/positions.html), která u KAŽDÉHO prodeje zvlášť ukazuje, jestli se
    appce vyplatil (zeleně/červeně) - dřív se realizovaný P/L počítal jen
    souhrnně za celý den (viz compute_realized_pl_delta níže), ne za
    jednotlivý obchod.

    Jako realizační cenu použije cenu pozice v okamžiku rozhodování
    (account_before) - u tržních příkazů je rozdíl oproti přesné fill ceně
    z brokera zanedbatelný, ale nejde o stoprocentně přesné číslo (appka si
    přesnou fill cenu z T212 API zatím nestahuje).

    Mutuje prvky trade_results na místě (doplní jim klíče realized_pl,
    realized_pl_pct, sell_price u prodejů) a zároveň to samé vrací.
    """
    positions_by_symbol = {p["symbol"]: p for p in account_before.get("positions", [])}
    for t in trade_results:
        if t.get("status") != "submitted" or t.get("side") != "sell":
            continue
        pos = positions_by_symbol.get(t.get("symbol"))
        if not pos:
            continue
        qty = t.get("qty", 0) or 0
        sell_price = pos["current_price"]
        cost_basis = qty * pos["avg_entry_price"]
        realized_pl = qty * (sell_price - pos["avg_entry_price"])
        t["realized_pl"] = realized_pl
        t["realized_pl_pct"] = (realized_pl / cost_basis * 100) if cost_basis else 0.0
        t["sell_price"] = sell_price
    return trade_results


def compute_realized_pl_delta(trade_results):
    """
    Součet realizovaného P/L ze všech dnešních prodejů (viz annotate_realized_pl
    výše, který musí proběhnout PŘED voláním téhle funkce) - používá se pro
    kumulativní "realizovaný P/L" na dashboardu (karta "Výkonnost").
    """
    return sum(
        t.get("realized_pl", 0.0)
        for t in trade_results
        if t.get("status") == "submitted" and t.get("side") == "sell"
    )


def compute_cash_flow_delta(prev_history):
    """
    POZOR - přidáno 21.8.2026: appka umí rozeznat, když uživatel na T212 účet
    ručně přidá (nebo vybere) hotovost mimo appku samotnou - jinak by dashboard
    vklad omylem vykazoval jako obchodní zisk (viz diskuze v chatu - appka umí
    vykreslit "Portfolio vs. drž a čekej CSPX", "Od začátku" i denní změnu tak,
    aby vklad/výběr nezkresloval, jen pokud o něm ví). Používá T212 API endpoint
    /equity/history/transactions (viz broker_t212.get_cash_flows).

    Při úplně PRVNÍM běhu po nasazení téhle featury (prev_history ještě nemá
    "last_cash_flow_check") appka žádné starší vklady zpětně NEzapočítává - ty
    už jsou zahrnuté v počáteční hodnotě portfolia (starting_value) - jen si
    zapamatuje aktuální okamžik jako výchozí bod a sledování začne od PŘÍŠTÍHO
    běhu.

    Vrací (net, items, new_check) - net je dnešní čistý součet (kladné = vklad
    převažuje), items jsou syrové položky pro uložení do historie (audit/detail),
    new_check je nové razítko, které se má uložit jako "last_cash_flow_check".
    """
    last_check = prev_history.get("last_cash_flow_check")
    now_iso = datetime.now(timezone.utc).isoformat()

    if last_check is None:
        return 0.0, [], now_iso

    try:
        flows = broker_t212.get_cash_flows(after_datetime=last_check)
    except Exception as e:
        # Appka radši dnešní vklad/výběr přehlédne, než aby kvůli chybě API
        # spadl celý denní běh - jen si NEPOSUNE checkpoint, takže to zkusí
        # dohnat příští den (get_cash_flows si tak jako tak žádá "od" daného
        # data, ne jen "za včerejšek").
        print(f"POZOR: nepodařilo se zjistit vklady/výběry z T212 API ({e}) - "
              f"appka pokračuje bez téhle informace, dnešní případný vklad "
              f"se na dashboardu neodliší od obchodního zisku.")
        return 0.0, [], last_check

    net = sum((f.get("amount") or 0.0) if f.get("type") == "DEPOSIT" else -(f.get("amount") or 0.0)
               for f in flows)
    return net, flows, now_iso


def compute_dividend_delta(prev_history, instruments_map):
    """
    POZOR - přidáno 2.9.2026: appka umí zjistit vyplacené dividendy z T212 účtu
    (viz broker_t212.get_dividends, endpoint /equity/history/dividends - jiný
    než ten u compute_cash_flow_delta výše). Na rozdíl od vkladu/výběru se
    dividenda NEODEČÍTÁ z výkonu appky (je to skutečný investiční výnos, ne
    externí kapitál) - appka ji jen dopočítá pro zobrazení na dashboardu
    (samostatný řádek/kartička, dividendy podle akcie v popup okně) a pro
    kontext do promptu pro AI (viz decision.build_prompt), aby AI vědělo, že
    část dnešní hotovosti nepřišla z obchodu.

    Stejný "od prvního běhu dál" princip jako u compute_cash_flow_delta - starší
    dividendy před zavedením téhle featury appka zpětně nedohledává (jsou už
    zahrnuté v historické hodnotě portfolia).

    Vrací (net, items, new_check) - net je součet dnešních dividend (vždy >= 0),
    items jsou ZNORMALIZOVANÉ položky {"symbol", "amount", "date", "raw"} - viz
    broker_t212.resolve_dividend_symbol/_dividend_amount/_dividend_date
    (normalizace tady na jednom místě, aby dashboard (per-akcie součet v
    popup okně) i report/AI prompt pracovaly se stejným, spolehlivým tvarem
    místo aby si každý spotřebitel dat musel syrová pole T212 API luštit
    sám). "raw" obsahuje původní položku beze změny (audit, kdyby appka
    trefila špatné pole - viz POZOR u get_dividends), new_check je nové
    razítko pro "last_dividend_check".
    """
    last_check = prev_history.get("last_dividend_check")
    now_iso = datetime.now(timezone.utc).isoformat()

    if last_check is None:
        return 0.0, [], now_iso

    try:
        raw_items = broker_t212.get_dividends(after_datetime=last_check)
    except Exception as e:
        # Stejný princip jako u compute_cash_flow_delta - appka radši dividendu
        # dnes přehlédne, než aby kvůli chybě API spadl celý běh; checkpoint se
        # neposune, takže to zkusí dohnat příští den.
        print(f"POZOR: nepodařilo se zjistit dividendy z T212 API ({e}) - "
              f"appka pokračuje bez téhle informace.")
        return 0.0, [], last_check

    items = [
        {
            "symbol": broker_t212.resolve_dividend_symbol(it, instruments_map),
            "amount": broker_t212._dividend_amount(it),
            "date": broker_t212._dividend_date(it),
            "raw": it,
        }
        for it in raw_items
    ]
    net = sum(it["amount"] for it in items)
    return net, items, now_iso


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

    # POZOR - přidáno 21.8.2026: zprávy přes Alpha Vantage NEWS_SENTIMENT
    # (viz news_data.py) - volitelné, appka bez nastaveného
    # ALPHAVANTAGE_API_KEY dál funguje jako dřív (news=None, viz news_section
    # fallback v decision.py.build_prompt). Jen pro "obyčejné" US tickery
    # (ne CSPX/EQQQ, viz POZOR v news_data.py).
    news = get_recent_news(stocks)

    # FRED je volitelný a nezávislý na brokerovi - pokud FRED_API_KEY není
    # nastavený, macro bude None a appka pokračuje úplně stejně jako dřív.
    macro = get_macro_context()

    # POZOR - přidáno 2.9.2026: dividendy se appka dozví PŘED voláním AI (ne až
    # při ukládání historie jako cash_flow_net níže), aby o nich AI mohla vědět
    # při rozhodování (viz decision.build_prompt, dividend_section) - jinak by
    # nevysvětlitelný nárůst hotovosti mohla mylně považovat za starý obchod.
    prev_history_for_prompt = load_history()
    dividend_net, dividend_items, dividend_check = compute_dividend_delta(prev_history_for_prompt, INSTRUMENTS)

    decision = get_decision(account_before, bars, limits, news=news, macro=macro, dividends=dividend_items)

    # Aktuální ceny z nezávislého zdroje (tržní data, ne to, co si spočítala AI) -
    # slouží k přepočtu qty * cena při validaci, viz risk_rules.validate_decision.
    prices = {symbol: series[-1]["c"] for symbol, series in bars.items() if series}
    # clip_oversized_trades i clip_concentrated_trades MUSÍ proběhnout před
    # validate_decision - zmenší nákupy přesahující limit na jeden obchod,
    # resp. limit na koncentraci v jedné pozici, místo aby appka kvůli
    # jedinému přesahujícímu nákupu zahodila CELÝ den (viz POZOR u obou
    # funkcí v risk_rules.py; druhá nalezena 21.8.2026 v měsíčním backtestu,
    # potom co první opravu odhalila druhý, dřív skrytý limit).
    clip_oversized_trades(decision, limits, account_before, prices=prices)
    clip_concentrated_trades(decision, limits, account_before, prices=prices)
    ok, reasons = validate_decision(decision, limits, account_before, prices=prices)

    trade_results = []
    if ok and decision.get("trades"):
        trade_results = broker_t212.execute_trades(
            decision["trades"], INSTRUMENTS, prices=prices, account=account_before, limits=limits
        )
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
        dividend_net=dividend_net,
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

    # annotate_realized_pl MUSÍ proběhnout před uložením do historie (níže) i
    # před sestavením reportu výše by bylo taky možné, ale report tahle pole
    # nepoužívá - jen dashboard (docs/positions.html čte trade["realized_pl"]
    # přímo z uložených dat v history.json).
    annotate_realized_pl(account_before, trade_results)
    realized_pl_delta = compute_realized_pl_delta(trade_results)

    # POZOR - dividend_net/dividend_items/dividend_check appka spočítala už
    # výš (před voláním AI, viz komentář u compute_dividend_delta) - tady se jen
    # znovu použijí, appka to nepočítá podruhé (mezitím se stav nezměnil,
    # broker_t212.get_dividends se volá jen jednou za běh).
    cash_flow_net, cash_flow_items, cash_flow_check = compute_cash_flow_delta(prev_history_for_prompt)

    # POZOR - přidáno 2.9.2026: evidence pro daňové účely (viz tax_ledger.py -
    # kapitálové zisky/ztráty, hrubý příjem z prodejů kvůli limitu 100 000 Kč,
    # dividendy podle roku). Úmyslně ODDĚLENÝ soubor od history.json (jiná
    # povaha dat - průběžně mutovaný "stav dávek", ne append-only log dní) a
    # úmyslně se NIKAM neposílá do decision.py/AI (viz POZOR v tax_ledger.py -
    # appka nechce, aby si AI kvůli dani upravovalo obchodní rozhodnutí).
    # Ceny pro dávky appka bere ze stejného zdroje jako risk_rules.validate_decision
    # (`prices`, tržní cena) - PŘIBLIŽNÉ, ne přesná fill cena z T212, stejný
    # princip jako main.annotate_realized_pl výše.
    tax_data = tax_ledger.load_ledger()
    tax_ledger.seed_initial_lots(tax_data, account_before.get("positions", []), date_str)
    for t in trade_results:
        if t.get("status") != "submitted":
            continue
        trade_price = prices.get(t.get("symbol"))
        if trade_price is None:
            print(f"POZOR: appka nemá tržní cenu pro {t.get('symbol')} - obchod se "
                  f"do daňové evidence (tax_ledger.py) nezapíše, aby appka radši "
                  f"nezaevidovala špatnou cenu.")
            continue
        if t.get("side") == "buy":
            tax_ledger.record_buy(tax_data, t["symbol"], date_str, t.get("qty"), trade_price)
        elif t.get("side") == "sell":
            tax_ledger.record_sell(tax_data, t["symbol"], date_str, t.get("qty"), trade_price)
    for d in dividend_items:
        tax_ledger.record_dividend_for_tax(tax_data, d.get("date") or date_str, d.get("amount"))
    tax_ledger.save_ledger(tax_data)

    update_history(
        date_str, account_after, decision, trade_results, reasons if not ok else [],
        spy_price=spy_price, realized_pl_delta=realized_pl_delta,
        cash_flow_net=cash_flow_net, cash_flow_items=cash_flow_items, cash_flow_check=cash_flow_check,
        dividend_net=dividend_net, dividend_items=dividend_items, dividend_check=dividend_check,
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
