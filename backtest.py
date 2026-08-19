"""
Backtest: přehraje appku (stejný decision.py/risk_rules.py jako živý provoz)
přes HISTORICKÁ data den po dni a simuluje, jak by si vedla - bez skutečných
obchodů, jen v paměti.

DŮLEŽITÉ principy:
1. AI dostává pro každý simulovaný den POUZE data, která by v ten den reálně
   měla k dispozici (žádný pohled do budoucnosti) - ceny/zprávy/makro jsou
   vždy oříznuté k danému dni.
2. Pro každý simulovaný den appka SKUTEČNĚ zavolá Claude (stejné volání jako
   naživo) - to je jediný způsob, jak zjistit, co by appka tehdy udělala.
   Znamená to reálné náklady na Anthropic API (viz README).
3. Fill (provedení obchodu) se simuluje za ZAVÍRACÍ cenu daného dne - appka
   se v živém provozu rozhoduje po zavření trhu, takže je to rozumná
   aproximace, ne dokonalá realita (žádný spread/slippage).
4. Krypto appka i naživo běží jen jednou denně (cron), takže backtest
   simuluje jen obchodní dny akciového trhu (ne víkendy) - i pro BTC/ETH.
   Je to zjednodušení oproti reálné appce, která by se teoreticky mohla
   spustit i o víkendu, ale prakticky odpovídá tomu, co appka dělá.
5. Spouští se ručně přes GitHub Actions (.github/workflows/backtest.yml),
   NE podle rozvrhu - je to jednorázová analýza, ne běžný provoz.

Použití: python backtest.py [--start-date YYYY-MM-DD] [--end-date YYYY-MM-DD]
Bez parametrů: posledních 365 dní do včerejška.
"""
import argparse
import bisect
import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest, NewsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

from data_fetch import get_clients
from risk_rules import load_risk_limits, allowed_symbols, validate_decision
from decision import get_decision
from fred_data import SERIES as FRED_SERIES, FRED_BASE_URL

STARTING_CASH = 100_000.0
LOOKBACK_DAYS_BARS = 14
LOOKBACK_DAYS_NEWS = 3
NEWS_LIMIT_PER_DAY = 10


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
        start = end - timedelta(days=365)

    return start, end


# --- Hromadné (jednorázové) stažení historických dat pro celé období ---

def fetch_all_bars(stock_client, crypto_client, stock_symbols, crypto_symbols, start, end):
    """Stáhne denní svíčky pro CELÉ období v jednom volání na typ (akcie/krypto) -
    alpaca-py si interně poradí se stránkováním. Padding dozadu, ať má i první
    simulovaný den svých 14 dní historie k dispozici."""
    padded_start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc) - timedelta(days=LOOKBACK_DAYS_BARS + 10)
    end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)
    result = {}

    if stock_symbols:
        req = StockBarsRequest(
            symbol_or_symbols=stock_symbols,
            timeframe=TimeFrame.Day,
            start=padded_start,
            end=end_dt,
            feed=DataFeed.IEX,
        )
        bars = stock_client.get_stock_bars(req)
        for symbol in stock_symbols:
            if symbol in bars.data:
                result[symbol] = [
                    {"t": b.timestamp.isoformat(), "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume}
                    for b in bars.data[symbol]
                ]

    if crypto_symbols:
        req = CryptoBarsRequest(
            symbol_or_symbols=crypto_symbols,
            timeframe=TimeFrame.Day,
            start=padded_start,
            end=end_dt,
        )
        bars = crypto_client.get_crypto_bars(req)
        for symbol in crypto_symbols:
            if symbol in bars.data:
                result[symbol] = [
                    {"t": b.timestamp.isoformat(), "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume}
                    for b in bars.data[symbol]
                ]

    return result


def fetch_all_news(news_client, stock_symbols, start, end):
    if not stock_symbols:
        return []
    padded_start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc) - timedelta(days=LOOKBACK_DAYS_NEWS + 2)
    end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)
    req = NewsRequest(
        symbols=",".join(stock_symbols),
        start=padded_start,
        end=end_dt,
        limit=5000,  # bezpečný horní strop na celé období - stejně použijeme jen okno 3 dní kolem každého simulovaného dne
        include_content=False,
        exclude_contentless=True,
        sort="desc",
    )
    try:
        news_set = news_client.get_news(req)
    except Exception as e:
        print("Nepodařilo se stáhnout historické zprávy (pokračuji bez nich):", e)
        return []
    items = news_set.data.get("news", [])
    return [
        {
            "headline": n.headline,
            "summary": (n.summary or "")[:300],
            "symbols": n.symbols,
            "source": n.source,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in items
    ]


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


def news_as_of(all_news, day_str, lookback_days=LOOKBACK_DAYS_NEWS, limit=NEWS_LIMIT_PER_DAY):
    day = datetime.strptime(day_str, "%Y-%m-%d").date()
    window_start = (day - timedelta(days=lookback_days)).isoformat()
    items = [
        n for n in all_news
        if n["created_at"] and window_start <= n["created_at"][:10] <= day_str
    ]
    items.sort(key=lambda n: n["created_at"], reverse=True)
    return items[:limit]


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
            "unrealized_plpc": (price / pos["avg_entry_price"] - 1) if pos["avg_entry_price"] else 0.0,
        })
    return {"cash": cash, "portfolio_value": total_value, "buying_power": cash, "positions": pos_list}


def simulate_trade(cash, positions, trade, all_bars, day_str):
    """Simuluje jeden obchod za zavírací cenu dne. Vrací (nový cash, popis výsledku)."""
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
        return cash, {"symbol": symbol, "side": side, "qty": fill_qty, "fill_price": price, "status": "filled", "reasoning": trade.get("reasoning", "")}

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
        return cash, {"symbol": symbol, "side": side, "qty": fill_qty, "fill_price": price, "status": "filled", "realized_pl": realized_pl, "reasoning": trade.get("reasoning", "")}

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
    print(f"Backtest {start.isoformat()} -> {end.isoformat()}")

    limits = load_risk_limits()
    stocks, crypto = allowed_symbols(limits)
    _, stock_data_client, crypto_data_client, news_client = get_clients()

    print("Stahuji historická data (jednorázově, celé období)...")
    all_bars = fetch_all_bars(stock_data_client, crypto_data_client, stocks, crypto, start, end)
    all_news = fetch_all_news(news_client, stocks, start, end)
    all_fred = fetch_all_fred(start, end)

    if "SPY" not in all_bars or not all_bars["SPY"]:
        raise RuntimeError("Nepodařilo se stáhnout data pro SPY - z nich se odvozuje kalendář obchodních dní.")

    trading_days = sorted({b["t"][:10] for b in all_bars["SPY"] if start.isoformat() <= b["t"][:10] <= end.isoformat()})
    print(f"Obchodních dní v období: {len(trading_days)}")

    cash = STARTING_CASH
    positions = {}
    spy_start_price = close_price(all_bars, "SPY", trading_days[0])
    spy_shares_benchmark = STARTING_CASH / spy_start_price

    log = []
    result_path = f"backtest/result_{start.isoformat()}_{end.isoformat()}.json"
    os.makedirs("backtest", exist_ok=True)

    for i, day_str in enumerate(trading_days):
        bars_today = bars_as_of(all_bars, stocks + crypto, day_str)
        news_today = news_as_of(all_news, day_str)
        macro_today = macro_as_of(all_fred, day_str)
        account_snapshot = make_account_snapshot(cash, positions, all_bars, day_str)

        decision = get_decision_with_retry(account_snapshot, bars_today, limits, news=news_today, macro=macro_today)

        trade_results = []
        blocked_reasons = []
        if decision is None:
            blocked_reasons = ["Rozhodnutí AI selhalo, den přeskočen."]
        else:
            # Stejná nezávislá kontrola ceny jako v živém provozu (main.py) - ať
            # backtest odhalí stejný typ chyby (qty neodpovídá estimated_value).
            prices_today = {s: close_price(all_bars, s, day_str) for s in stocks + crypto}
            prices_today = {s: p for s, p in prices_today.items() if p is not None}
            ok, reasons = validate_decision(decision, limits, account_snapshot, prices=prices_today)
            if ok and decision.get("trades"):
                for t in decision["trades"]:
                    cash, res = simulate_trade(cash, positions, t, all_bars, day_str)
                    trade_results.append(res)
            elif not ok:
                blocked_reasons = reasons

        final_snapshot = make_account_snapshot(cash, positions, all_bars, day_str)
        spy_price_today = close_price(all_bars, "SPY", day_str)
        log.append({
            "date": day_str,
            "market_summary": decision.get("market_summary") if decision else None,
            "trades": trade_results,
            "blocked_reasons": blocked_reasons,
            "portfolio_value": final_snapshot["portfolio_value"],
            "cash": cash,
            "positions": final_snapshot["positions"],
            "benchmark_spy_buy_hold_value": spy_shares_benchmark * spy_price_today if spy_price_today else None,
        })

        if (i + 1) % 10 == 0 or i == len(trading_days) - 1:
            print(f"[{i+1}/{len(trading_days)}] {day_str}  portfolio=${final_snapshot['portfolio_value']:,.2f}")

        # Průběžné ukládání - kdyby běh spadl v půlce, nepřijdeme o dosavadní výsledky.
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump({"start": start.isoformat(), "end": end.isoformat(), "days_done": i + 1,
                       "days_total": len(trading_days), "entries": log}, f, indent=2, ensure_ascii=False)

    portfolio_values = [e["portfolio_value"] for e in log]
    benchmark_values = [e["benchmark_spy_buy_hold_value"] for e in log if e["benchmark_spy_buy_hold_value"]]
    final_value = portfolio_values[-1]
    final_benchmark = benchmark_values[-1] if benchmark_values else None
    total_trades = sum(1 for e in log for t in e["trades"] if t["status"] == "filled")
    # Skutečně použitý model (ne natvrdo napsaný text) - ať shrnutí vždy odpovídá
    # tomu, co je aktuálně nastavené jako výchozí v decision.py / DECISION_MODEL.
    model_used = os.environ.get("DECISION_MODEL", "claude-sonnet-4-6").strip()

    summary = {
        "starting_cash": STARTING_CASH,
        "final_portfolio_value": final_value,
        "total_return_pct": (final_value / STARTING_CASH - 1) * 100,
        "final_benchmark_spy_value": final_benchmark,
        "benchmark_return_pct": (final_benchmark / STARTING_CASH - 1) * 100 if final_benchmark else None,
        "max_drawdown_pct": max_drawdown(portfolio_values) * 100,
        "total_filled_trades": total_trades,
        "days_simulated": len(trading_days),
        "assumptions": [
            "Fill se simuluje za zavírací cenu daného dne (žádný spread/slippage).",
            "Simulují se jen obchodní dny akciového trhu (víkendy vynechány, i pro krypto).",
            f"AI se volá reálně pro každý den (max_tokens=2000, použitý model: {model_used}).",
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
            f.write(f"- Počáteční kapitál: ${STARTING_CASH:,.2f}\n")
            f.write(f"- Konečná hodnota appky: **${final_value:,.2f}** ({summary['total_return_pct']:+.2f} %)\n")
            if final_benchmark:
                f.write(f"- Srovnání (drž a čekej SPY): ${final_benchmark:,.2f} ({summary['benchmark_return_pct']:+.2f} %)\n")
            f.write(f"- Maximální propad appky: {summary['max_drawdown_pct']:.2f} %\n")
            f.write(f"- Počet provedených obchodů: {summary['total_filled_trades']}\n")
            f.write(f"- Simulováno obchodních dní: {summary['days_simulated']}\n\n")
            f.write("Plný denní log je v `" + result_path + "` (commitnutý zpět do repozitáře).\n")


if __name__ == "__main__":
    main()
