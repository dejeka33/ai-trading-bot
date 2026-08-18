"""
Rozhodovací modul - pošle stav účtu, tržní data a mantinely modelu Claude
přes Anthropic API a dostane zpět strukturované rozhodnutí (co koupit/prodat).
"""
import json
import os

import anthropic

DECISION_TOOL = {
    "name": "record_trading_decision",
    "description": "Zaznamená dnešní obchodní rozhodnutí ve strukturované podobě.",
    "input_schema": {
        "type": "object",
        "properties": {
            "market_summary": {
                "type": "string",
                "description": "Krátké shrnutí toho, jak vypadá trh a portfolio dnes (2-4 věty).",
            },
            "trades": {
                "type": "array",
                "description": "Seznam navržených obchodů. Prázdné pole = dnes se neobchoduje.",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "side": {"type": "string", "enum": ["buy", "sell"]},
                        "qty": {"type": "number"},
                        "order_type": {"type": "string", "enum": ["market", "limit"]},
                        "limit_price": {"type": "number"},
                        "estimated_value": {"type": "number"},
                        "reasoning": {
                            "type": "string",
                            "description": "Proč tento obchod dává smysl vzhledem k datům a mantinelům.",
                        },
                    },
                    "required": ["symbol", "side", "qty", "order_type", "estimated_value", "reasoning"],
                },
            },
        },
        "required": ["market_summary", "trades"],
    },
}


def build_prompt(account_snapshot, bars, risk_limits, news=None, macro=None):
    news_section = ""
    if news:
        news_section = f"""
NEDÁVNÉ ZPRÁVY (posledních pár dní, k povoleným akciím/ETF):
{json.dumps(news, indent=2, ensure_ascii=False)}
"""
    else:
        news_section = "\nNEDÁVNÉ ZPRÁVY: žádné relevantní zprávy se nepodařilo najít/stáhnout.\n"

    macro_section = ""
    if macro:
        macro_section = f"""
MAKROEKONOMICKÝ KONTEXT (zdroj: FRED, Federal Reserve Bank of St. Louis - oficiální,
na Alpace nezávislý zdroj):
{json.dumps(macro, indent=2, ensure_ascii=False)}
Toto je jen doplňkový kontext o prostředí úrokových sazeb, ne přímý signál k obchodu -
neuprav kvůli němu frekvenci ani styl obchodování, jen ho zohledni při zdůvodnění.
Např. záporné/invertované rozpětí T10Y2Y (10Y výnos nižší než 2Y) bývá historicky
spojováno s vyšší pravděpodobností ekonomického zpomalení v následujících měsících.
"""

    return f"""
Jsi obchodní asistent spravující PAPER TRADING účet (fiktivní peníze, reálná tržní data).
Tvým úkolem je jednou denně aktivně vyhodnotit situaci a v rámci přísných mantinelů navrhnout
obchody, které dávají rozumný smysl vzhledem k datům. Nejsi konzervativní fond čekající na
dokonalou příležitost - i střední míra přesvědčení, rozumně podložená cenovými daty, trendem
nebo zprávami, je dostatečný důvod k obchodu, pokud se vejde do mantinelů. Prázdné pole trades
(žádný obchod) používej jen tehdy, když jsou data skutečně rozporuplná nebo neexistuje žádný
rozumný krok - ne jako výchozí bezpečnou volbu jen proto, že si nejsi stoprocentně jistý.

AKTUÁLNÍ STAV ÚČTU:
{json.dumps(account_snapshot, indent=2, ensure_ascii=False)}

TRŽNÍ DATA (posledních ~14 dní, denní svíčky):
{json.dumps(bars, indent=2, ensure_ascii=False)}
{news_section}{macro_section}
RIZIKOVÉ MANTINELY (ZÁVAZNÉ - nesmíš je porušit):
{json.dumps(risk_limits, indent=2, ensure_ascii=False)}

Zprávy jsou jen doplňkový kontext (mohou být neúplné nebo chybět) - nikdy jim nevěř
víc než mantinelům a nepoužívej je jako jediný důvod k obchodu; kombinuj je s cenovými daty.

Zavolej nástroj record_trading_decision se svým rozhodnutím. Buď stručný a konkrétní
v poli reasoning u každého obchodu - bude se ukazovat v denním reportu uživateli.
""".strip()


def get_decision(account_snapshot, bars, risk_limits, news=None, macro=None, model=None):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"].strip())
    model = model or os.environ.get("DECISION_MODEL", "claude-sonnet-4-6").strip()

    prompt = build_prompt(account_snapshot, bars, risk_limits, news=news, macro=macro)

    response = client.messages.create(
        model=model,
        max_tokens=2000,
        tools=[DECISION_TOOL],
        tool_choice={"type": "tool", "name": "record_trading_decision"},
        messages=[{"role": "user", "content": prompt}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "record_trading_decision":
            return block.input

    raise RuntimeError("Model nevrátil očekávané strukturované rozhodnutí.")
