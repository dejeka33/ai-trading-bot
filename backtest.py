"""
Backtest: přehraje appku (stejný decision.py/risk_rules.py jako živý provoz)
přes HISTORICKÁ data den po dni a simuluje, jak by si vedla - bez skutečných
obchodů, jen v paměti.

PŘEPSÁNO NA TRADING 212 / EODHD ARCHITEKTURU (2026-08) - nahrazuje dřívější
verzi založenou na alpaca-py (StockBarsRequest/CryptoBarsRequest/NewsRequest),
která odpovídala staré appce na Alpace. Zdroj historických cen je teď stejný
jako u živého provozu (viz market_data.py - EODHD primárně, Stooq záložně) a
měna účtu je CZK (jako živý pilot na Trading 212 - viz broker_t212.py). Ceny
se před vstupem do decision.py převádí přes HISTORICKÉ kurzy Frankfurter API
(stejný zdroj jako fx.py u živého provozu, ale s kurzem PLATNÝM K DANÉMU DNI,
ne "aktuálním" - appka přece jen simuluje víc měsíců zpátky, kurz se za tu
dobu reálně mění - viz fetch_fx_series/fx_rate_as_of níže).

Krypto appka v této verzi neobchoduje vůbec (risk_limits.yaml,
allowed_instruments.crypto: []) - Trading 212 Crypto je samostatný účet mimo
Invest/ISA, beta obchodovací API na něj nesahá, backtest ho proto neřeší.

POZOR - přidáno 21.8.2026: appka teď (viz news_data.py) umí volitelně
stahovat zprávy přes Alpha Vantage NEWS_SENTIMENT - stejně jako živý provoz
(main.py), i backtest je používá, pokud je nastavený ALPHAVANTAGE_API_KEY
(bez něj appka pokračuje jako dřív, news=None). Na rozdíl od cen appka
zprávy pro CELÉ testované období stáhne JEDNÍM voláním (fetch_all_news) a
pak si je pro každý simulovaný den vyřízne (news_as_of) - stejný princip
úspory volání jako u cen (fetch_all_bars) a makra (fetch_all_fred) níže.

DŮLEŽITÉ principy (nezměněno oproti dřívější verzi):
1. AI dostává pro každý simulovaný den POUZE data, která by v ten den reálně
   měla k dispozici (žádný pohled do budoucnosti) - ceny/makro/FX kurzy jsou
   vždy oříznuté k danému dni.
2. Pro každý simulovaný den appka SKUTEČNĚ zavolá Claude (stejné volání jako
   naživo) - to je jediný způsob, jak zjistit, co by appka tehdy udělala.
   Znamená to reálné náklady na Anthropic API (cca jako tolik běžných denních
   běhů, kolik je v období obchodních dní).
3. Fill (provedení obchodu) se simuluje za ZAVÍRACÍ cenu daného dne, PŘEVEDENOU
   do CZK historickým kurzem k tomu dni - appka se v živém provozu rozhoduje
   po zavření trhu, takže je to rozumná aproximace, ne dokonalá realita
   (žádný spread/slippage, žádné zaokrouhlení brokera).
4. Simuluje jen obchodní dny podle kalendáře BENCHMARK_SYMBOL (CSPX/LSE).
5. Spouští se ručně přes GitHub Actions (.github/workflows/backtest.yml),
   NE podle rozvrhu - je to jednorázová analýza, ne běžný provoz.

Použití: python backtest.py [--start-date YYYY-MM-DD] [--end-date YYYY-MM-DD]
Bez parametrů: posledních DEFAULT_LOOKBACK_DAYS dní do včerejška (kratší
výchozí okno než dřívější rok, ať jde snadno spustit "měsíční test" bez
parametrů - delší období si appka řekne přes --start-date).
"""
import argparse
import bisect
import json
import os
import time
from datetime import datetime, timedelta, timezone

import urllib.request
import urllib.error
import requests

from instruments import INSTRUMENTS
from market_data import fetch_symbol_bars_raw
from risk_rules import (
    load_risk_limits, allowed_symbols, validate_decision,
    clip_oversized_trades, clip_concentrated_trades,
)
from decision import get_decision
from fred_data import SERIES as FRED_SERIES, FRED_BASE_URL
from news_data import fetch_all_news, news_as_of

STARTING_CASH = float((os.environ.get("BACKTEST_STARTING_CASH") or "").strip() or "10000")
ACCOUNT_CURRENCY = os.environ.get("BACKTEST_CURRENCY", "CZK").strip().upper()
# POZOR - přidáno 21.8.2026: umožňuje pro tenhle konkrétní běh vypnout zprávy
# (news=[] pro každý den, i když ALPHAVANTAGE_API_KEY je nastavený) - k
# porovnání appky SE zprávami / BEZ zpráv na STEJNÉM období, aniž by se
# muselo sahat na GitHub secret (ten sdílí i živý denní provoz, viz
# daily_trading.yml - appka ho proto nechává eventy dál nastavený).
DISABLE_NEWS = os.environ.get("BACKTEST_DISABLE_NEWS", "").strip().lower() in ("1", "true", "yes")
BENCHMARK_SYMBOL = "CSPX"  # nahrazuje dřívější SPY (viz risk_limits.yaml - PRIIPs)
DEFAULT_LOOKBACK_DAYS = 30
# POZOR - 21.8.2026 krátce zkusmo prodlouženo z 14 na 30, ale vráceno zpátky
# týž den - měsíční backtest s delší historií (2026-03-01 -> 2026-03-31)
# dopadl hůř než s 14 dny (viz POZOR u get_recent_bars v market_data.py pro
# celé zdůvodnění). MUSÍ zůstat stejné jako lookback_days v market_data.py,
# ať backtest realisticky odpovídá tomu, co appka vidí naživo.
LOOKBACK_DAYS_BARS = 14


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--start-date", default=os.environ.get("BACKTEST_START_DATE", "").strip() or None)
    p.add_argument("--end-date", default=os.environ.get("BACKTEST_END_DATE", "").strip() or None)
    args = p.parse_args()

    if args.end_date:
        end = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    else:
        end = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    if args.start_date:
        start = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    else:
        start = end - timedelta(days=DEFAULT_LOOKBACK_DAYS)

    return start, end


# --- Hromadné (jednorázové) stažení historických dat pro celé období ---

def fetch_all_bars(symbols_map, start, end):
    """Stáhne denní svíčky pro CELÉ období, pro každý nástroj JEDNÍM požadavkem
    (EODHD primárně, Stooq záložně - viz market_data.fetch_symbol_bars_raw,
    sdílené s živým provozem) - padding dozadu, ať má i první simulovaný den
    svých LOOKBACK_DAYS_BARS dní historie k dispozici."""
    padded_start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc) - timedelta(days=LOOKBACK_DAYS_BARS + 10)
    end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
    result = {}
    for symbol, sources in symbols_map.items():
        bars = fetch_symbol_bars_raw(symbol, sources, padded_start, end_dt)
        if bars:
            result[symbol] = bars
        else:
            print(f"POZOR: pro {symbol} se nepodařilo stáhnout žádná historická data "
                  f"(EODHD ani Stooq) - appka ho v tomhle backtestu bude ignorovat.")
    return result


def fetch_fx_series(base_currency, quote_currency, start, end):
    """Stáhne historickou řadu denních kurzů base->quote za dané období
    (Frankfurter time-series endpoint) - na rozdíl od fx.get_fx_rate() (jen
    "aktuální" kurz, pro živý provoz) backtest potřebuje kurz PLATNÝ K
    TEHDEJŠÍMU DNI. Vrací seznam (datum, kurz) seřazený vzestupně, nebo []."""
    base_currency = base_currency.upper()
    quote_currency = quote_currency.upper()
    if base_currency == quote_currency:
        return [(start.isoformat(), 1.0)]
    url = (f"https://api.frankfurter.app/{start.isoformat()}..{end.isoformat()}"
           f"?from={base_currency}&to={quote_currency}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError, json.JSONDecodeError) as e:
        print(f"FX historie: chyba při stahování {base_currency}->{quote_currency}: {e}")
        return []
    rates = data.get("rates", {})
    series = sorted((d, r[quote_currency]) for d, r in rates.items() if quote_currency in r)
    return series


def fx_rate_as_of(fx_series, day_str):
    """Kurz platný k danému dni (nejbližší dostupný den <= day_str - ECB kurzy
    chybí o víkendech/svátcích). Pro den PŘED první dostupnou hodnotou vrátí
    nejstarší známý kurz (lepší přiblížení než appku kvůli chybějícímu dni
    nechat spadnout nebo cenu nepřevést vůbec)."""
    if not fx_series:
        return None
    dates = [d for d, _ in fx_series]
    idx = bisect.bisect_right(dates, day_str) - 1
    if idx >= 0:
        return fx_series[idx][1]
    return fx_series[0][1]


def apply_fx_to_bars(bars, fx_series):
    """Vrátí NOVÝ seznam barů s cenami převedenými přes historický kurz PLATNÝ
    K DATU KAŽDÉHO BARU zvlášť (ne jeden kurz pro celou historii)."""
    if not fx_series:
        return bars
    converted = []
    for b in bars:
        rate = fx_rate_as_of(fx_series, b["t"][:10])
        if rate is None:
            continue
        converted.append({**b, "o": b["o"] * rate, "h": b["h"] * rate, "l": b["l"] * rate, "c": b["c"] * rate})
    return converted


def fetch_all_fred(start, end):
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if not api_key:
        return None
    padded_start = (start - timedelta(days=40)).isoformat()
    result = {}
    for series_id, label in FRED_SERIES.items():
        try:
            resp = requests.get(FRED_BASE_URL, params={
                "series_id": series_id, "api_key": api_key, "file_type": "json",
                "observation_start": padded_start, "observation_end": end.isoformat(),
                "sort_order": "asc", "limit": 5000,
            }, timeout=15)
            resp.raise_for_status()
            obs = [
                (o["date"], float(o["value"]))
                for o in resp.json().get("observations", [])
                if o.get("value") not in (None, ".", "")
            ]
            result[series_id] = {"label": label, "obs": obs}
        except Exception as e:
            print(f"FRED: nepodařilo se stáhnout řadu {series_id} (pokračuji bez ní): {e}")
    return result or None


# --- Pomocné funkce pro "jen data do dneška" ---

def bars_as_of(all_bars, symbols, day_str):
    result = {}
    for symbol in symbols:
        series = all_bars.get(symbol, [])
        window = [b for b in series if b["t"][:10] <= day_str][-LOOKBACK_DAYS_BARS:]
        if window:
            result[symbol] = window
    return result


def macro_as_of(all_fred, day_str):
    if not all_fred:
        return None
    result = {}
    for series_id, data in all_fred.items():
        dates = [d for d, _ in data["obs"]]
        idx = bisect.bisect_right(dates, day_str) - 1
        if idx >= 0:
            d, v = data["obs"][idx]
            result[series_id] = {"label": data["label"], "date": d, "value": v}
    return result or None


def close_price(all_bars, symbol, day_str):
    series = all_bars.get(symbol, [])
    for b in reversed(series):
        if b["t"][:10] <= day_str:
            return b["c"]
    return None


# --- Simulace portfolia ---

def make_account_snapshot(cash, positions, all_bars, day_str):
    pos_list = []
    total_value = cash
    for symbol, pos in positions.items():
        price = close_price(all_bars, symbol, day_str) or pos["avg_entry_price"]
        market_value = pos["qty"] * price
        total_value += market_value
        pos_list.append({
            "symbol": symbol, "qty": pos["qty"], "avg_entry_price": pos["avg_entry_price"],
            "current_price": price, "market_value": market_value,
            "unrealized_pl": market_value - pos["qty"] * pos["avg_entry_price"],
            "unrealized_plpc": (price / pos["avg_entry_price"] - 1) * 100 if pos["avg_entry_price"] else 0.0,
        })
    return {
        "cash": cash, "portfolio_value": total_value, "buying_power": cash,
        # "currency" - viz decision.py build_prompt(), který se na tohle pole
        # přímo odkazuje při vysvětlení, že tržní data jsou už převedená.
        "currency": ACCOUNT_CURRENCY,
        "positions": pos_list,
    }


def simulate_trade(cash, positions, trade, all_bars, day_str):
    """Simuluje jeden obchod za zavírací cenu dne (už převedenou do měny účtu -
    viz apply_fx_to_bars). Vrací (nový cash, popis výsledku). "status" hodnoty
    (submitted/skipped_*) se drží stejného tvaru jako broker_t212.execute_trades,
    aby appka do backtest logu ukládala kompatibilní data s živým report.py."""
    symbol = trade.get("symbol")
    side = trade.get("side")
    qty = trade.get("qty") or 0
    price = close_price(all_bars, symbol, day_str)

    if price is None:
        return cash, {"symbol": symbol, "side": side, "qty": 0, "status": "skipped_no_price", "reasoning": trade.get("reasoning", "")}

    if side == "buy":
        max_affordable = cash / price if price > 0 else 0
        fill_qty = min(qty, max_affordable)
        if fill_qty <= 0:
            return cash, {"symbol": symbol, "side": side, "qty": 0, "status": "skipped_insufficient_cash", "reasoning": trade.get("reasoning", "")}
        pos = positions.get(symbol, {"qty": 0.0, "avg_entry_price": 0.0})
        new_qty = pos["qty"] + fill_qty
        pos["avg_entry_price"] = (pos["qty"] * pos["avg_entry_price"] + fill_qty * price) / new_qty if pos["qty"] > 0 else price
        pos["qty"] = new_qty
        positions[symbol] = pos
        cash -= fill_qty * price
        return cash, {"symbol": symbol, "side": side, "qty": fill_qty, "fill_price": price, "status": "submitted", "reasoning": trade.get("reasoning", "")}

    if side == "sell":
        pos = positions.get(symbol)
        if not pos or pos["qty"] <= 0:
            return cash, {"symbol": symbol, "side": side, "qty": 0, "status": "skipped_no_position", "reasoning": trade.get("reasoning", "")}
        fill_qty = min(qty, pos["qty"])
        realized_pl = fill_qty * (price - pos["avg_entry_price"])
        cash += fill_qty * price
        pos["qty"] -= fill_qty
        if pos["qty"] <= 1e-9:
            del positions[symbol]
        else:
            positions[symbol] = pos
        return cash, {"symbol": symbol, "side": side, "qty": fill_qty, "fill_price": price, "status": "submitted", "realized_pl": realized_pl, "reasoning": trade.get("reasoning", "")}

    return cash, {"symbol": symbol, "side": side, "qty": 0, "status": "skipped_unknown_side", "reasoning": trade.get("reasoning", "")}


def get_decision_with_retry(*args, max_retries=3, **kwargs):
    for attempt in range(max_retries):
        try:
            return get_decision(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"Rozhodnutí selhalo i po {max_retries} pokusech, den se přeskočí:", e)
                return None
            wait = 5 * (2 ** attempt)
            print(f"Volání AI selhalo ({e}), zkouším znovu za {wait}s...")
            time.sleep(wait)


def max_drawdown(values):
    peak = values[0] if values else 0
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, (v - peak) / peak)
    return worst


def main():
    start, end = parse_args()
    print(f"Backtest {start.isoformat()} -> {end.isoformat()} "
          f"(měna účtu: {ACCOUNT_CURRENCY}, počáteční kapitál: {STARTING_CASH:,.2f})")

    limits = load_risk_limits()
    stocks, crypto = allowed_symbols(limits)  # crypto je v tomto pilotu vždy [] - viz risk_limits.yaml
    active_instruments = {s: INSTRUMENTS[s] for s in stocks if s in INSTRUMENTS}

    print("Stahuji historická data (jednorázově, celé období, EODHD/Stooq - viz market_data.py)...")
    raw_bars = fetch_all_bars(active_instruments, start, end)

    padded_start = start - timedelta(days=LOOKBACK_DAYS_BARS + 10)
    needed_currencies = {
        info["currency"] for info in active_instruments.values()
        if info.get("currency") and info["currency"].upper() != ACCOUNT_CURRENCY
    }
    fx_series_by_currency = {}
    for cur in needed_currencies:
        print(f"Stahuji historické kurzy {cur}->{ACCOUNT_CURRENCY}...")
        fx_series_by_currency[cur] = fetch_fx_series(cur, ACCOUNT_CURRENCY, padded_start, end)

    all_bars = {}
    for symbol, bars in raw_bars.items():
        cur = active_instruments[symbol].get("currency")
        if cur and cur.upper() != ACCOUNT_CURRENCY:
            all_bars[symbol] = apply_fx_to_bars(bars, fx_series_by_currency.get(cur, []))
        else:
            all_bars[symbol] = bars

    all_fred = fetch_all_fred(start, end)

    if DISABLE_NEWS:
        print("Zprávy jsou pro tenhle běh VYPNUTÉ (BACKTEST_DISABLE_NEWS) - appka "
              "poběží se stejnými zprávami jako appka bez news integrace, i kdyby "
              "ALPHAVANTAGE_API_KEY byl nastavený.")
        all_news = []
    else:
        print("Stahuji zprávy (Alpha Vantage NEWS_SENTIMENT, pokud je nastavený "
              "ALPHAVANTAGE_API_KEY - jinak appka pokračuje bez nich)...")
        all_news = fetch_all_news(list(active_instruments.keys()), start, end)

    if BENCHMARK_SYMBOL not in all_bars or not all_bars[BENCHMARK_SYMBOL]:
        raise RuntimeError(
            f"Nepodařilo se stáhnout data pro {BENCHMARK_SYMBOL} - z nich se odvozuje "
            "kalendář obchodních dní i benchmark 'drž a čekej'."
        )

    trading_days = sorted({
        b["t"][:10] for b in all_bars[BENCHMARK_SYMBOL]
        if start.isoformat() <= b["t"][:10] <= end.isoformat()
    })
    print(f"Obchodních dní v období: {len(trading_days)}")
    if not trading_days:
        raise RuntimeError("Pro zadané období se nenašel žádný obchodní den - zkontroluj rozsah dat.")

    cash = STARTING_CASH
    positions = {}
    bench_start_price = close_price(all_bars, BENCHMARK_SYMBOL, trading_days[0])
    bench_shares = STARTING_CASH / bench_start_price if bench_start_price else None

    log = []
    # POZOR - přidáno 21.8.2026: běh s BACKTEST_DISABLE_NEWS má JINÝ název
    # souboru ("_no_news" navíc) - jinak by pro STEJNÉ období přepsal výsledek
    # normálního (se zprávami) běhu, čímž by appka přišla o srovnávací data
    # (přesně proto appka tohle přepínání dělá - porovnat obojí na stejném období).
    suffix = "_no_news" if DISABLE_NEWS else ""
    result_path = f"backtest/result_{start.isoformat()}_{end.isoformat()}{suffix}.json"
    os.makedirs("backtest", exist_ok=True)

    for i, day_str in enumerate(trading_days):
        bars_today = bars_as_of(all_bars, list(active_instruments.keys()), day_str)
        macro_today = macro_as_of(all_fred, day_str)
        news_today = news_as_of(all_news, list(active_instruments.keys()), day_str)
        account_snapshot = make_account_snapshot(cash, positions, all_bars, day_str)

        decision = get_decision_with_retry(account_snapshot, bars_today, limits, news=news_today, macro=macro_today)

        trade_results = []
        blocked_reasons = []
        if decision is None:
            blocked_reasons = ["Rozhodnutí AI selhalo, den přeskočen."]
        else:
            # Stejná nezávislá kontrola ceny jako v živém provozu (main.py) - ať
            # backtest odhalí stejný typ chyby (qty neodpovídá estimated_value).
            prices_today = {s: close_price(all_bars, s, day_str) for s in active_instruments}
            prices_today = {s: p for s, p in prices_today.items() if p is not None}
            # clip_oversized_trades i clip_concentrated_trades MUSÍ proběhnout
            # před validate_decision - stejná logika a stejný důvod jako
            # v main.py (viz POZOR u obou funkcí v risk_rules.py; druhá
            # nalezena 21.8.2026 v měsíčním backtestu, potom co první oprava
            # odhalila druhý, dřív skrytý limit).
            clip_oversized_trades(decision, limits, account_snapshot, prices=prices_today)
            clip_concentrated_trades(decision, limits, account_snapshot, prices=prices_today)
            ok, reasons = validate_decision(decision, limits, account_snapshot, prices=prices_today)
            if ok and decision.get("trades"):
                for t in decision["trades"]:
                    cash, res = simulate_trade(cash, positions, t, all_bars, day_str)
                    trade_results.append(res)
            elif not ok:
                blocked_reasons = reasons

        final_snapshot = make_account_snapshot(cash, positions, all_bars, day_str)
        bench_price_today = close_price(all_bars, BENCHMARK_SYMBOL, day_str)
        submitted_count = len([t for t in trade_results if t.get("status") == "submitted"])
        log.append({
            "date": day_str,
            "market_summary": decision.get("market_summary") if decision else None,
            "trades": trade_results,
            "trade_count": submitted_count,
            "blocked_reasons": blocked_reasons,
            "portfolio_value": final_snapshot["portfolio_value"],
            "cash": cash,
            "currency": ACCOUNT_CURRENCY,
            "positions": final_snapshot["positions"],
            # Pole se z historických důvodů pořád jmenuje "spy_price", i když jde
            # o CSPX (stejná konvence jako v main.py/history.py) - kvůli
            # kompatibilitě s formátem, který appka jinde ukládá do history.json.
            "spy_price": bench_price_today,
            "benchmark_buy_hold_value": bench_shares * bench_price_today if (bench_shares and bench_price_today) else None,
        })

        if (i + 1) % 5 == 0 or i == len(trading_days) - 1:
            print(f"[{i+1}/{len(trading_days)}] {day_str}  portfolio={final_snapshot['portfolio_value']:,.2f} {ACCOUNT_CURRENCY}")

        # Průběžné ukládání - kdyby běh spadl v půlce, nepřijdeme o dosavadní výsledky.
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump({"start": start.isoformat(), "end": end.isoformat(), "days_done": i + 1,
                       "days_total": len(trading_days), "currency": ACCOUNT_CURRENCY, "entries": log},
                      f, indent=2, ensure_ascii=False)

    portfolio_values = [e["portfolio_value"] for e in log]
    benchmark_values = [e["benchmark_buy_hold_value"] for e in log if e["benchmark_buy_hold_value"]]
    final_value = portfolio_values[-1]
    final_benchmark = benchmark_values[-1] if benchmark_values else None
    total_trades = sum(1 for e in log for t in e["trades"] if t["status"] == "submitted")
    model_used = os.environ.get("DECISION_MODEL", "claude-sonnet-4-6").strip()

    # POZOR - přidáno 22.8.2026: strojově čitelná konfigurace běhu přímo v
    # summary, ne jen jako věta v "assumptions" - kvůli incidentu z 22.8.2026,
    # kdy appka omylem použila zastaralý výsledek 30denního lookback
    # experimentu jako "baseline bez zpráv" (starý soubor měl stejný název
    # jako čerstvý 14denní běh, oboje jen podle testovaného období, ne podle
    # konfigurace - viz `oprava-vsechno-nebo-nic-limity.md` a
    # `zpravy-pruzkum-api.md`). Tahle pole appce/uživateli/Claude dovolí
    # kdykoliv ověřit PŘESNOU konfiguraci daného výsledku přímo z JSON, ne
    # jen podle názvu souboru nebo paměti/historie chatu.
    # GITHUB_SHA appka nemusí nikde explicitně předávat - GitHub Actions ho
    # runneru dává automaticky do prostředí u každého kroku.
    run_config = {
        "news_enabled": not DISABLE_NEWS,
        "lookback_days_bars": LOOKBACK_DAYS_BARS,
        "model": model_used,
        "git_commit": (os.environ.get("GITHUB_SHA") or "")[:8] or None,
    }

    summary = {
        "starting_cash": STARTING_CASH,
        "currency": ACCOUNT_CURRENCY,
        "final_portfolio_value": final_value,
        "total_return_pct": (final_value / STARTING_CASH - 1) * 100,
        "final_benchmark_value": final_benchmark,
        "benchmark_label": f"drž a čekej {BENCHMARK_SYMBOL}",
        "benchmark_return_pct": (final_benchmark / STARTING_CASH - 1) * 100 if final_benchmark else None,
        "max_drawdown_pct": max_drawdown(portfolio_values) * 100,
        "total_filled_trades": total_trades,
        "days_simulated": len(trading_days),
        "run_config": run_config,
        "assumptions": [
            "Fill se simuluje za zavírací cenu daného dne, převedenou historickým kurzem "
            "do měny účtu (žádný spread/slippage/zaokrouhlení brokera).",
            f"Simulují se jen obchodní dny podle kalendáře {BENCHMARK_SYMBOL} (LSE).",
            f"AI se volá reálně pro každý den (max_tokens=2000, použitý model: {model_used}).",
            "Zprávy (Alpha Vantage NEWS_SENTIMENT) appka používá jen pokud je nastavený "
            "ALPHAVANTAGE_API_KEY (a jen pro US tickery, ne CSPX/EQQQ - viz news_data.py); "
            "bez klíče pokračuje bez nich, stejně jako živý provoz na Trading 212 (main.py)."
            + (" Pro TENHLE běh byly zprávy záměrně VYPNUTÉ (BACKTEST_DISABLE_NEWS=true) "
               "kvůli srovnání se/bez zpráv na stejném období." if DISABLE_NEWS else ""),
        ],
    }

    with open(result_path, "w", encoding="utf-8") as f:
        json.dump({"start": start.isoformat(), "end": end.isoformat(), "summary": summary, "entries": log},
                   f, indent=2, ensure_ascii=False)

    print("\n=== VÝSLEDEK BACKTESTU ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(f"# Backtest {start.isoformat()} -> {end.isoformat()}\n\n")
            f.write(f"- Počáteční kapitál: {STARTING_CASH:,.2f} {ACCOUNT_CURRENCY}\n")
            f.write(f"- Konečná hodnota appky: **{final_value:,.2f} {ACCOUNT_CURRENCY}** ({summary['total_return_pct']:+.2f} %)\n")
            if final_benchmark:
                f.write(f"- Srovnání ({summary['benchmark_label']}): {final_benchmark:,.2f} {ACCOUNT_CURRENCY} ({summary['benchmark_return_pct']:+.2f} %)\n")
            f.write(f"- Maximální propad appky: {summary['max_drawdown_pct']:.2f} %\n")
            f.write(f"- Počet provedených obchodů: {summary['total_filled_trades']}\n")
            f.write(f"- Simulováno obchodních dní: {summary['days_simulated']}\n")
            f.write(f"- Zprávy: {'zapnuté' if run_config['news_enabled'] else 'VYPNUTÉ'}, "
                    f"lookback: {run_config['lookback_days_bars']} dní, model: {run_config['model']}\n\n")
            f.write("Plný denní log je v `" + result_path + "` (commitnutý zpět do repozitáře).\n")


if __name__ == "__main__":
    main()
