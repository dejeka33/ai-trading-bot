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


def clip_concentrated_trades(decision, limits, account_snapshot, prices=None):
    """
    POZOR - přidáno 21.8.2026, po měsíčním backtestu (2026-03-01 -> 2026-03-31)
    s už nasazenou clip_oversized_trades: limit na JEDEN obchod přestal appku
    blokovat (0 dní za měsíc), ale limit na CELKOVOU koncentraci v jedné pozici
    (max_position_size_pct) začal blokovat ještě častěji (13 z 22 dní) - appka
    se opakovaně snažila natáhnout GOOGL/NVDA/JNJ přes 10 % portfolia a celý
    den se kvůli tomu (stejnou "všechno nebo nic" logikou ve validate_decision)
    zahodil, i když šlo dotyčný nákup jen zmenšit a zbytek dne v klidu provést.

    Stejný princip jako clip_oversized_trades: volá se PŘED validate_decision
    (a PO clip_oversized_trades, ať pracuje už se zmenšenými qty) - u nákupů,
    které by natáhly pozici NAD limit koncentrace, jejich celkovou hodnotu (za
    daný symbol, pokud je nákupů na stejný symbol víc) úměrně ZMENŠÍ přesně na
    tolik, kolik se do limitu ještě vejde (s rezervou 0,5 %, stejně jako u
    clip_oversized_trades) - místo aby appka o ten nákup, a tím pádem (kvůli
    ok=False pro celý den) i o VŠECHNY ostatní, jinak v pořádku velké obchody
    toho dne, přišla úplně.

    Prodeje stejného symbolu ve stejný den se do výpočtu čistě NETTUJÍ (snižují
    potřebu klipovat) - prodeje samotné se neklipují, ty koncentraci jen snižují.

    Pokud je pozice na limitu (nebo přes limit) UŽ TEĎ, i bez jakéhokoli
    nového nákupu (např. pohybem trhu od minulého rozhodnutí), nákup(y) do
    tohodle symbolu se z rozhodnutí úplně ODEBEROU (ne jen qty=0 - appka by se
    zbytečně pokoušela poslat brokerovi nulový pokyn).

    Mutuje decision["trades"] na místě (může prvky i mazat) a zároveň to samé
    vrací.
    """
    portfolio_value = account_snapshot["portfolio_value"]
    max_position_value = portfolio_value * (limits["position_limits"]["max_position_size_pct"] / 100)
    safety_margin = 0.995

    existing_value_by_symbol = {
        p["symbol"]: p.get("market_value", 0.0) for p in account_snapshot.get("positions", [])
    }

    def trade_value(t):
        # Stejná priorita jako jinde v tomhle souboru - nezávisle přepočítaná
        # hodnota (skutečná tržní cena) je spolehlivější než estimated_value od AI.
        symbol = t.get("symbol")
        qty = t.get("qty")
        price = (prices or {}).get(symbol)
        if price is not None and qty is not None:
            return qty * price
        return t.get("estimated_value")

    trades = decision.get("trades", [])

    sell_value_by_symbol = {}
    for t in trades:
        if t.get("side") != "sell":
            continue
        symbol = t.get("symbol")
        val = trade_value(t)
        if symbol is not None and val is not None:
            sell_value_by_symbol[symbol] = sell_value_by_symbol.get(symbol, 0.0) + val

    buy_trades_by_symbol = {}
    for t in trades:
        if t.get("side") != "buy":
            continue
        symbol = t.get("symbol")
        if symbol is None:
            continue
        buy_trades_by_symbol.setdefault(symbol, []).append(t)

    trades_to_remove_ids = set()

    for symbol, buy_trades in buy_trades_by_symbol.items():
        total_buy_value = sum(v for v in (trade_value(t) for t in buy_trades) if v is not None)
        if total_buy_value <= 0:
            continue

        existing = existing_value_by_symbol.get(symbol, 0.0)
        sells = sell_value_by_symbol.get(symbol, 0.0)
        allowed_buy_value = (max_position_value * safety_margin) - existing + sells

        if allowed_buy_value >= total_buy_value:
            continue  # v pořádku, nic se neklipuje

        if allowed_buy_value <= 0:
            print(f"POZOR: pozice {symbol} je už na/přes limit koncentrace "
                  f"({existing:.2f} vs. {max_position_value:.2f}) - dnešní nákup(y) "
                  f"tohohle symbolu se úplně odeberou z rozhodnutí, ať appka nepřijde "
                  f"o ostatní, jinak v pořádku obchody.")
            trades_to_remove_ids.update(id(t) for t in buy_trades)
            continue

        scale = allowed_buy_value / total_buy_value
        print(f"POZOR: nákupy {symbol} by pozici natáhly na {existing + total_buy_value:.2f} "
              f"(limit koncentrace {max_position_value:.2f}) - zmenšuji jejich celkovou "
              f"hodnotu z {total_buy_value:.2f} na {allowed_buy_value:.2f} (x{scale:.3f}), "
              f"místo abych je (a tím i zbytek dne) zahodil/a.")
        for t in buy_trades:
            qty = t.get("qty")
            if qty is None:
                continue
            new_qty = qty * scale
            t["qty"] = new_qty
            price = (prices or {}).get(symbol)
            if price is not None:
                t["estimated_value"] = new_qty * price
            elif t.get("estimated_value") is not None:
                t["estimated_value"] = t["estimated_value"] * scale

    if trades_to_remove_ids:
        decision["trades"] = [t for t in trades if id(t) not in trades_to_remove_ids]

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

        # POZOR - bug nalezený 22.8.2026 v 6měsíčním backtestu (2026-02-21 ->
        # 2026-08-21): limit na jeden obchod (max_single_trade_pct) se tu
        # dřív kontroloval u VŠECH obchodů stejně přísně, včetně prodejů -
        # ale clip_oversized_trades() prodeje ZÁMĚRNĚ neklipuje (viz komentář
        # tam - "snížení pozice/rizika, ne jeho navýšení", limit na jeden
        # obchod u prodeje logicky nedává smysl). Výsledek: když appka chtěla
        # prodat celou (větší) pozici najednou, klipovací funkce ji nechala
        # beze změny, ale tahle kontrola ji stejně odmítla - a kvůli
        # "všechno nebo nic" designu celého validate_decision() se tím
        # zahodil CELÝ den, i jiné v pořádku obchody. Živě se to stalo 8x za
        # 6 měsíců - u 6 z 8 dní appka zkoušela prodat ZTRÁTOVOU pozici
        # (JNJ/MSFT v mínusu -0,75 až -8,1 %), takže appka byla nucená dál
        # držet ztrátu, místo aby ji omezila. Přesně stejný typ chyby, jakou
        # řešila oprava z 21.8.2026 (viz clip_oversized_trades výš), jen na
        # prodejní straně, kterou tehdejší oprava nepokrývala. Teď se limit
        # na jeden obchod u prodejů vůbec nekontroluje - konzistentně s tím,
        # jak se k nim appka chová při klipování.
        is_sell = t.get("side") == "sell"

        est_value = t.get("estimated_value")
        if not is_sell and est_value is not None and est_value > max_trade_value:
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
            if not is_sell and computed_value > max_trade_value:
                reasons.append(
                    f"Obchod {symbol}: {qty} ks x {price:.2f} = {computed_value:.2f} přesahuje "
                    f"limit na jeden obchod ({max_trade_value:.2f}) - nezávisle přepočítáno z tržní ceny."
                )
            elif est_value is not None and est_value > 0:
                # Konzistenční kontrola (AI qty vs. AI estimated_value) platí
                # dál i pro prodeje - jen limit na jeden obchod se u nich
                # neuplatňuje (viz POZOR výš).
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
