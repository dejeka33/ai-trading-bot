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
    # Přidáno 21.8.2026 na základě rozboru koncentračního rizika (viz risk_rules.py
    # POZOR výše u max_position_size_pct) - cíl je rozložit univerzum přes různé
    # sektory, ne jen přidat další tech tituly silně korelované s AAPL/MSFT/GOOGL.
    # ISIN ověřeny přes více nezávislých zdrojů (Deutsche Börse, justETF, Fidelity,
    # Morgan Stanley, Euronext) - viz diskuze v chatu.
    "AMZN": {  # Amazon.com - e-commerce/cloud (AWS), jiný sektor než AAPL/MSFT/GOOGL
        "isin": "US0231351067",
        "stooq": "amzn.us",
        "eodhd": "AMZN.US",
        "price_divisor": 1,
        "currency": "USD",
    },
    "JPM": {  # JPMorgan Chase - finanční sektor
        "isin": "US46625H1005",
        "stooq": "jpm.us",
        "eodhd": "JPM.US",
        "price_divisor": 1,
        "currency": "USD",
    },
    "JNJ": {  # Johnson & Johnson - zdravotnictví/farmacie
        "isin": "US4781601046",
        "stooq": "jnj.us",
        "eodhd": "JNJ.US",
        "price_divisor": 1,
        "currency": "USD",
    },
    "NVDA": {  # Nvidia - polovodiče (populární, ale POZOR: silně korelované s
               # existujícím tech univerzem, nepřidává skutečnou sektorovou diverzitu)
        "isin": "US67066G1040",
        "stooq": "nvda.us",
        "eodhd": "NVDA.US",
        "price_divisor": 1,
        "currency": "USD",
    },

    # Přidáno 24.8.2026 - rozšíření univerza z 9 na 20 titulů na žádost uživatele,
    # cíleně do sektorů, které appka dosud vůbec nepokrývala (energetika, letecký
    # průmysl, telekomunikace, maloobchod, platby...), ať nejde jen o další tech
    # tituly korelované s AAPL/MSFT/GOOGL/NVDA. ISIN ověřeny přes více nezávislých
    # zdrojů (justETF, Fidelity, Vídeňská burza, ISIN.org) - viz diskuze v chatu.
    # POZOR - EODHD free tarif má strop 20 dotazů/den; appka teď má přesně 20
    # nástrojů = 20 EODHD volání/den živě, BEZ jakékoli rezervy. Při dalším
    # rozšiřování univerza už by appka narazila na limit a musela by řešit
    # placený tarif (viz broker-pruzkum.md) nebo dávkové stahování.
    "TSLA": {  # Tesla - elektromobily/energetika
        "isin": "US88160R1014",
        "stooq": "tsla.us",
        "eodhd": "TSLA.US",
        "price_divisor": 1,
        "currency": "USD",
    },
    "V": {  # Visa - platební sítě (jiný typ finančního byznysu než JPM banking)
        "isin": "US92826C8394",
        "stooq": "v.us",
        "eodhd": "V.US",
        "price_divisor": 1,
        "currency": "USD",
    },
    "WMT": {  # Walmart - maloobchod
        "isin": "US9311421039",
        "stooq": "wmt.us",
        "eodhd": "WMT.US",
        "price_divisor": 1,
        "currency": "USD",
    },
    "KO": {  # Coca-Cola - spotřební zboží/nápoje
        "isin": "US1912161007",
        "stooq": "ko.us",
        "eodhd": "KO.US",
        "price_divisor": 1,
        "currency": "USD",
    },
    "XOM": {  # ExxonMobil - energetika. POZOR: firma se v červenci 2026
              # přejmenovala/přesídlila (na "ExxonMobil Holdings Corporation") a
              # dostala NOVÝ ISIN - tohle je aktuální hodnota (ověřeno přes SEC
              # 8-K a Bloomberg), ticker XOM i burza (NYSE) zůstaly stejné.
        "isin": "US30233Q1085",
        "stooq": "xom.us",
        "eodhd": "XOM.US",
        "price_divisor": 1,
        "currency": "USD",
    },
    "UNH": {  # UnitedHealth Group - zdravotní pojištění (jiný typ zdravotnictví než JNJ farmacie)
        "isin": "US91324P1021",
        "stooq": "unh.us",
        "eodhd": "UNH.US",
        "price_divisor": 1,
        "currency": "USD",
    },
    "DIS": {  # Walt Disney - média/zábava
        "isin": "US2546871060",
        "stooq": "dis.us",
        "eodhd": "DIS.US",
        "price_divisor": 1,
        "currency": "USD",
    },
    "BA": {  # Boeing - letecký/obranný průmysl
        "isin": "US0970231058",
        "stooq": "ba.us",
        "eodhd": "BA.US",
        "price_divisor": 1,
        "currency": "USD",
    },
    "MCD": {  # McDonald's - restaurace/spotřebitelský sektor
        "isin": "US5801351017",
        "stooq": "mcd.us",
        "eodhd": "MCD.US",
        "price_divisor": 1,
        "currency": "USD",
    },
    "COST": {  # Costco Wholesale - velkoobchod/členský maloobchod
        "isin": "US22160K1051",
        "stooq": "cost.us",
        "eodhd": "COST.US",
        "price_divisor": 1,
        "currency": "USD",
    },
    "VZ": {  # Verizon Communications - telekomunikace
        "isin": "US92343V1044",
        "stooq": "vz.us",
        "eodhd": "VZ.US",
        "price_divisor": 1,
        "currency": "USD",
    },
}
