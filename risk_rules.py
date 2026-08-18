"""Načtení a základní validace rizikových mantinelů z config/risk_limits.yaml."""
import yaml


def load_risk_limits(path="config/risk_limits.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def allowed_symbols(limits):
    stocks = limits["allowed_instruments"].get("stocks_etfs", [])
    crypto = limits["allowed_instruments"].get("crypto", [])
    return stocks, crypto


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

    # Denní pojistka - pokud je portfolio dnes už v hlubší ztrátě, než je povoleno, žádné nové obchody
    # (toto se v praxi porovnává s hodnotou na začátku dne - viz main.py, kde se počítá daily_pl_pct)

    return (len(reasons) == 0), reasons
