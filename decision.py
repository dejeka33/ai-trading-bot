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


def build_prompt(account_snapshot, bars, risk_limits):
    return f"""
Jsi obchodní asistent spravující PAPER TRADING účet (fiktivní peníze, reálná tržní data).
Tvým úkolem je jednou denně vyhodnotit situaci a navrhnout maximálně konzervativní obchody
v rámci přísných mantinelů. Když si nejsi jistý nebo data nejsou přesvědčivá, je naprosto
v pořádku nenavrhnout žádný obchod (prázdné pole trades).

AKTUÁLNÍ STAV ÚČTU:
{json.dumps(account_snapshot, indent=2, ensure_ascii=False)}

TRŽNÍ DATA (posledních ~14 dní, denní svíčky):
{json.dumps(bars, indent=2, ensure_ascii=False)}

RIZIKOVÉ MANTINELY (ZÁVAZNÉ - nesmíš je porušit):
{json.dumps(risk_limits, indent=2, ensure_ascii=False)}

Zavolej nástroj record_trading_decision se svým rozhodnutím. Buď stručný a konkrétní
v poli reasoning u každého obchodu - bude se ukazovat v denním reportu uživateli.
""".strip()


def get_decision(account_snapshot, bars, risk_limits, model=None):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = model or os.environ.get("DECISION_MODEL", "claude-haiku-4-5")

    prompt = build_prompt(account_snapshot, bars, risk_limits)

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
