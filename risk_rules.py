"""Načtení a základní validace rizikových mantinelů z config/risk_limits.yaml."""
import yaml


def load_risk_limits(path="config/risk_limits.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def allowed_symbols(limits):
    stocks = limits["allowed_instruments"].get("stocks_etfs", [])
    crypto = limits["allowed_instruments"].get("crypto", [])
    return stocks, crypto


def clip_oversized_trades(decision, limits, account_snapshot, prices=None):
    """
    POZOR - přidáno 21.8.2026 po rozboru 108denního backtestu (2026-03-01 ->
    2026-07-31): appka dřív při PŘEKROČENÍ limitu na jeden obchod
    (max_single_trade_pct) zahodila CELÉ dnešní rozhodnutí - i jiné, jinak
    v pořádku velké obchody navržené ten samý den (viz validate_decision níže,
    kde jeden "reasons" důvod shodí ok=False pro celý den). Živě se to stalo ve
    26 z 108 dní backtestu, často jen o pár desítek korun přes limit (např. 4.
    3. 2026: EQQQ 513,69 Kč vs. limit 501,41 Kč - přesah jen 2,4 %, celý den
    appka kvůli tomu neobchodovala vůbec, i když měla jasnou vůli).

    Tahle funkce se volá PŘED validate_decision - u každého nákupu, který
    přesahuje limit na jeden obchod, qty úměrně ZMENŠÍ, ať se vejde pod limit
    (s malou rezervou 0,5 %, ať to po zaokrouhlení nezávisle přepočítané ceny
    znovu nepřesáhne o pár haléřů) - místo aby appka obchod celý zahodila.
    validate_decision zůstává i tak spuštěná hned potom jako finální pojistka
    (kontroluje i jiné věci - koncentraci v pozici, počet obchodů za den,
    povolené symboly - ty se tímhle klipováním neřeší, jen limit na 1 obchod).

    Prodeje (side="sell") se neklipují - u těch limit na jeden obchod nedává
    stejný smysl (snížení pozice/rizika, ne jeho navýšení).

    Mutuje decision["trades"] na místě a zároveň to samé vrací.
    """
    portfolio_value = account_snapshot["portfolio_value"]
    max_trade_value = portfolio_value * (limits["position_limits"]["max_single_trade_pct"] / 100)
    safety_margin = 0.995

    for t in decision.get("trades", []):
        if t.get("side") != "buy":
            continue
        symbol = t.get("symbol")
        qty = t.get("qty")
        if qty is None:
            continue

        # Stejná priorita jako ve validate_decision - nezávisle přepočítaná
        # hodnota (skutečná tržní cena) je spolehlivější než to, co si AI sama
        # spočítala do estimated_value.
        price = (prices or {}).get(symbol)
        est_value = t.get("estimated_value")
        current_value = (qty * price) if price is not None else est_value
        if current_value is None or current_value <= max_trade_value:
            continue

        scale = (max_trade_value * safety_margin) / current_value
        new_qty = qty * scale
        print(f"POZOR: obchod {symbol} ({current_value:.2f}) přesahoval limit na jeden obchod "
              f"({max_trade_value:.2f}) - zmenšuji qty z {qty} na {new_qty:.4f}, místo abych "
              f"obchod celý zahodil/a.")
        t["qty"] = new_qty
        if price is not None:
            t["estimated_value"] = new_qty * price
        elif est_value is not None:
            t["estimated_value"] = est_value * scale

    return decision


def validate_decision(decision, limits, account_snapshot, prices=None):
    """
    Zkontroluje navržené obchody proti mantinelům PŘED provedením.
    Vrací (ok: bool, důvody: list[str]) - pokud ok=False, obchody se NEPROVEDOU.

    `prices` (volitelné): slovník {symbol: aktuální cena} z NEZÁVISLÉHO zdroje
    (skutečná tržní data, ne z rozhodnutí AI). Bez něj appka věří jen číslu
    estimated_value, které si AI spočítala sama - u reálných peněz je bezpečnější
    si qty * skutečnou cenu přepočítat nezávisle a ověřit, že to sedí.
    """
    reasons = []
    stocks, crypto = allowed_symbols(limits)
    allowed_all = set(stocks) | set(crypto)

    trades = decision.get("trades", [])

    if len(trades) > limits["position_limits"]["max_daily_trades"]:
        reasons.append(
            f"Počet obchodů ({len(trades)}) přesahuje denní limit "
            f"({limits['position_limits']['max_daily_trades']})."
        )

    portfolio_value = account_snapshot["portfolio_value"]
    max_trade_value = portfolio_value * (limits["position_limits"]["max_single_trade_pct"] / 100)
    max_position_value = portfolio_value * (limits["position_limits"]["max_position_size_pct"] / 100)

    # POZOR - bug nalezený 21.8.2026 při rozboru 5měsíčního backtestu: appka měla
    # v konfiguraci max_position_size_pct (max. % portfolia CELKEM v jedné pozici,
    # napříč všemi dřívějšími nákupy), ale tahle funkce ho nikdy nekontrolovala -
    # hlídal se jen max_single_trade_pct (limit na JEDEN obchod za den). Výsledek:
    # AI mohla postupně, obchod po obchodu (každý jednotlivě pod limitem), nabudovat
    # v jednom titulu klidně třetinu celého portfolia (živě se to stalo - GOOGL
    # 34 %, MSFT 33 %, AAPL 16 % po 108 dnech backtestu), aniž by to appka byť
    # jednou odmítla. Teď se navíc kontroluje: existující tržní hodnota pozice
    # (z account_snapshot) + součet VŠECH nákupů daného symbolu v tomto rozhodnutí
    # (mínus prodeje) proti max_position_size_pct. Prodeje se nekontrolují - ty
    # koncentraci jen snižují.
    existing_value_by_symbol = {
        p["symbol"]: p.get("market_value", 0.0) for p in account_snapshot.get("positions", [])
    }
    projected_delta_by_symbol = {}

    for t in trades:
        symbol = t.get("symbol")
        if symbol not in allowed_all:
            reasons.append(f"Symbol {symbol} není na seznamu povolených nástrojů.")

        if t.get("side") == "sell_short" or t.get("order_type") == "short":
            if not limits["risk_controls"]["allow_short_selling"]:
                reasons.append(f"Short selling není povolen ({symbol}).")

        est_value = t.get("estimated_value")
        if est_value is not None and est_value > max_trade_value:
            reasons.append(
                f"Obchod {symbol} v hodnotě {est_value:.2f} přesahuje limit na jeden obchod "
                f"({max_trade_value:.2f})."
            )

        # Nezávislá kontrola: qty * skutečná tržní cena (ne číslo, které si AI sama
        # dopočítala) - chytí případ, kdy AI navrhne qty, které neodpovídá tomu,
        # co si myslí, že to stojí. Bez tohohle appka věřila jen estimated_value.
        price = (prices or {}).get(symbol)
        qty = t.get("qty")
        computed_value = None
        if price is not None and qty is not None:
            computed_value = qty * price
            if computed_value > max_trade_value:
                reasons.append(
                    f"Obchod {symbol}: {qty} ks x {price:.2f} = {computed_value:.2f} přesahuje "
                    f"limit na jeden obchod ({max_trade_value:.2f}) - nezávisle přepočítáno z tržní ceny."
                )
            elif est_value is not None and est_value > 0:
                diff_pct = abs(computed_value - est_value) / est_value * 100
                if diff_pct > 20:
                    reasons.append(
                        f"Obchod {symbol}: uvedená estimated_value ({est_value:.2f}) se výrazně liší "
                        f"od skutečné hodnoty qty x cena ({computed_value:.2f}, rozdíl {diff_pct:.0f} %) "
                        "- rozhodnutí vypadá nekonzistentně, pro jistotu se neprovede."
                    )

        # Preferuj nezávisle přepočítanou hodnotu (computed_value) před tím, co
        # tvrdí AI (est_value) - stejná logika jako u kontroly jednoho obchodu výše.
        trade_value = computed_value if computed_value is not None else est_value
        if trade_value is not None and symbol is not None:
            if t.get("side") == "buy":
                projected_delta_by_symbol[symbol] = projected_delta_by_symbol.get(symbol, 0.0) + trade_value
            elif t.get("side") == "sell":
                projected_delta_by_symbol[symbol] = projected_delta_by_symbol.get(symbol, 0.0) - trade_value

    for symbol, delta in projected_delta_by_symbol.items():
        if delta <= 0:
            continue
        projected_value = existing_value_by_symbol.get(symbol, 0.0) + delta
        if projected_value > max_position_value:
            reasons.append(
                f"Pozice {symbol} by po tomto obchodu (obchodech) dosáhla {projected_value:.2f} "
                f"({(projected_value / portfolio_value * 100) if portfolio_value else 0:.1f} % portfolia), "
                f"což přesahuje limit na celkovou koncentraci v jedné pozici "
                f"({limits['position_limits']['max_position_size_pct']} % = {max_position_value:.2f})."
            )

    # Denní pojistka - pokud je portfolio dnes už v hlubší ztrátě, než je povoleno, žádné nové obchody
    # (toto se v praxi porovnává s hodnotou na začátku dne - viz main.py, kde se počítá daily_pl_pct)

    return (len(reasons) == 0), reasons
