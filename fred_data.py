"""
Modul pro stahování makroekonomických dat z FRED (Federal Reserve Economic
Data, Federal Reserve Bank of St. Louis) - oficiální, bezplatný a na Alpace
nezávislý zdroj. Používá se jako DOPLŇKOVÝ KONTEXT do promptu pro AI, ne jako
spouštěč obchodů - viz poznámka v decision.py.

Vyžaduje volitelnou proměnnou prostředí FRED_API_KEY (zdarma na
https://fred.stlouisfed.org/docs/api/api_key.html). Pokud není nastavená
nebo API selže, funkce vrátí None a bot pokračuje bez makro kontextu -
stejný "nice to have, nikdy neblokuje běh" princip jako u get_recent_news().
"""
import os

import requests

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

# Sledované řady: 10Y a 2Y výnos státních dluhopisů, jejich rozpětí (klasický
# signál blížící se recese, když je záporné/invertované), a efektivní sazba
# Fedu (DFF - denní hodnota, na rozdíl od FEDFUNDS, což je měsíční průměr).
SERIES = {
    "DGS10": "10Y výnos státního dluhopisu (%)",
    "DGS2": "2Y výnos státního dluhopisu (%)",
    "T10Y2Y": "Rozpětí 10Y-2Y (výnosová křivka, %)",
    "DFF": "Efektivní sazba Fedu (%)",
}


def _fetch_latest(series_id, api_key):
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 5,  # posledních pár pozorování - u víkendů/svátků chybí hodnota za poslední den
    }
    resp = requests.get(FRED_BASE_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    for obs in data.get("observations", []):
        if obs.get("value") not in (None, ".", ""):
            return {"date": obs["date"], "value": float(obs["value"])}
    return None


def get_macro_context():
    """
    Vrátí dict {series_id: {"label", "date", "value"}} pro sledované řady,
    nebo None, pokud FRED_API_KEY není nastavený nebo se nepodařilo stáhnout
    ani jednu řadu.
    """
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if not api_key:
        return None

    result = {}
    for series_id, label in SERIES.items():
        try:
            latest = _fetch_latest(series_id, api_key)
        except Exception as e:
            print(f"FRED: nepodařilo se stáhnout řadu {series_id} (pokračuji bez ní): {e}")
            continue
        if latest:
            result[series_id] = {"label": label, **latest}

    return result or None
