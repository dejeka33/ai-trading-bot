"""
Mapování interních symbolů appky (jak se používají v decision.py, risk_rules.py,
report.py - stejně jako dřív "SPY", "AAPL" apod.) na konkrétní nástroje potřebné
pro Trading 212 (ISIN - jednoznačný, nezávislý na tvaru tickeru) a pro tržní
data (Stooq/EODHD tickery).

Nahrazuje původní allowed_instruments v risk_limits.yaml jen o metadata navíc -
seznam POVOLENÝCH symbolů (co appka smí obchodovat) pořád řídí risk_limits.yaml,
tohle je "slovník", kde ke každému z nich najít data/ISIN.

ISIN hodnoty ověřené z více nezávislých zdrojů (justetf, fidelity.co.uk,
cbonds, blackrock/ishares fact sheety) - viz diskuze v chatu, ne z jednoho zdroje.

POZOR - jednotky cen na LSE (GBP vs GBX/pence): při ověřování cen appka narazila
na to, že londýnská burza kotuje různé nástroje v RŮZNÝCH jednotkách - stejný
zdroj (stockanalysis.com) ukázal CSPX přímo v GBP (838.69 GBP/kus), ale EQQQ a
XSPS v GBX/pencích (53590 GBX = 535.90 GBP; 427 GBX = 4.27 GBP). "price_divisor"
níže z toho vychází (1 pro GBP, 100 pro GBX) - JE TO ALE ZATÍM ODHAD, ne živě
ověřené proti tomu, jakou konkrétně jednotku vrací Stooq/EODHD CSV pro tyhle
konkrétní tickery (různí data provideři se u LSE cen v jednotkách někdy liší).
PRVNÍ věc k ověření při prvním běhu: porovnat cenu, kterou appka reálně stáhne
pro CSPX/EQQQ, s aktuální cenou z veřejné stránky (např. stockanalysis.com) -
pokud se liší přibližně 100x, price_divisor u toho nástroje je špatně.

Proč přes ISIN, ne přímo přes T212 "ticker" string: Trading 212 API dokumentace
a komunitní diskuze si u formátu tickeru protiřečily (viděli jsme "AAPL_US_EQ",
"BPl_EQ", "VUSA_LSE_EQ" - tři různé konvence). ISIN je jednoznačný mezinárodní
identifikátor nezávislý na tom, jaký tvar tickeru si zrovna T212 API zvolí -
přesný T212 ticker si appka sama dohledá za běhu přes
GET /equity/metadata/instruments (viz broker_t212.py, resolve_ticker_by_isin).

POZOR - "currency" pole: měna, ve které jsou ceny z market_data.py PO
price_divisor úpravě (GBP u CSPX/EQQQ, USD u AAPL/MSFT/GOOGL). Trading 212
účet v tomto pilotu je veden v CZK - market_data.py proto tyhle ceny ještě
převádí přes fx.py na měnu účtu, než se dostanou do decision.py/risk_rules.py.
Bez tohohle převodu appka na živém testu (19.8.2026) porovnávala číslo v cizí
měně přímo proti mantinelu v CZK - obchody vypadaly "pod limitem", ale ve
skutečnosti stály řádově víc (u USD nástrojů to brokera rovnou odmítlo jako
"Insufficient funds", u GBP nástrojů to prošlo, ale utratilo ~25x víc, než
appka počítala).
"""

INSTRUMENTS = {
    "CSPX": {  # iShares Core S&P 500 UCITS ETF (nahrazuje SPY i VOO - obě sledují stejný index)
        "isin": "IE00B5BMR087",
        "stooq": "cspx.uk",
        "eodhd": "CSPX.LSE",
        "price_divisor": 1,     # kotovaný přímo v GBP (viz POZOR výše)
        "currency": "GBP",      # měna ceny PO price_divisor úpravě - viz fx.py
    },
    "EQQQ": {  # Invesco EQQQ Nasdaq-100 UCITS ETF (nahrazuje QQQ)
        "isin": "IE0032077012",
        "stooq": "eqqq.uk",
        "eodhd": "EQQQ.LSE",
        "price_divisor": 100,   # kotovaný v GBX/pencích (viz POZOR výše)
        "currency": "GBP",      # měna ceny PO price_divisor úpravě - viz fx.py
    },
    "AAPL": {
        "isin": "US0378331005",
        "stooq": "aapl.us",
        "eodhd": "AAPL.US",
        "price_divisor": 1,     # USD, žádný pence problém
        "currency": "USD",
    },
    "MSFT": {
        "isin": "US5949181045",
        "stooq": "msft.us",
        "eodhd": "MSFT.US",
        "price_divisor": 1,
        "currency": "USD",
    },
    "GOOGL": {  # Alphabet Class A (ne GOOG/Class C)
        "isin": "US02079K3059",
        "stooq": "googl.us",
        "eodhd": "GOOGL.US",
        "price_divisor": 1,
        "currency": "USD",
    },
}
