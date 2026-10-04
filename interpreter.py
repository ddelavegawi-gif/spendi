"""Turns a free-text WhatsApp message into a structured intent.

Uses Claude (tool use for guaranteed-structured output). If no API key is set
or the call fails, a simple keyword/regex parser takes over so Spendi keeps working.
"""
import logging
import os
import re
from datetime import date

from config import Config

log = logging.getLogger("spendi.interpreter")

DEFAULT_MODEL = "claude-haiku-4-5-20251001"  # fast + cheap; plenty for this task
INTENTS = ["add_expense", "show_budget", "list_expenses", "delete_last", "set_budget", "help", "unknown"]


def build_tool(cfg: Config) -> dict:
    categories = list(cfg.categories)
    return {
        "name": "record_intent",
        "description": "Record what the user wants Spendi to do with their message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": INTENTS},
                "expenses": {
                    "type": "array",
                    "description": "For add_expense: one entry per distinct purchase in the message.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "amount": {"type": "number"},
                            "currency": {"type": "string", "description": f"ISO code, default {cfg.currency}"},
                            "merchant": {"type": "string", "description": "Store/brand, nicely capitalized"},
                            "note": {"type": "string", "description": "What was bought, if stated"},
                            "category": {"type": "string", "enum": categories},
                            "for_person": {"type": "string", "enum": cfg.people,
                                           "description": "Only if the message explicitly assigns it to someone else"},
                            "date": {"type": "string", "description": "YYYY-MM-DD, only if not today"},
                        },
                        "required": ["amount", "category"],
                    },
                },
                "period": {"type": "string", "enum": ["week", "month"],
                           "description": "For show_budget / list_expenses"},
                "category": {"type": "string", "enum": categories,
                             "description": "For show_budget / list_expenses / set_budget, if one is named"},
                "for_person": {"type": "string", "enum": cfg.people,
                               "description": "For set_budget on a personal category"},
                "amount": {"type": "number", "description": "For set_budget: the new monthly budget"},
            },
            "required": ["intent"],
        },
    }


def build_system_prompt(cfg: Config, sender: str, today: date) -> str:
    cats = []
    for name, c in cfg.categories.items():
        scope = "one budget per person" if c.get("scope") == "personal" else "shared"
        hints = ", ".join(c.get("keywords", [])[:15])
        cats.append(f"- {name} ({scope}): {c.get('description', '')}" + (f". Examples: {hints}" if hints else ""))
    return f"""You are the message parser for {cfg.bot_name}, a WhatsApp expense tracker shared by a couple: {', '.join(cfg.people)}.
This message was sent by {sender}. Today is {today:%A %Y-%m-%d}. Default currency: {cfg.currency}.
Users write in English or Spanish, often very briefly ("100 whole foods", "350 pesos uber ayer").

Categories:
{chr(10).join(cats)}

Rules:
- An amount plus something bought → add_expense. One entry per distinct purchase.
- Choose the single best category from the merchant and context. Use "{cfg.fallback_category}" only if nothing fits.
- Personal categories are for things one person buys mainly for themself (clothes, beauty, sports gear...). Household items go to shared categories even when bought at a store like Liverpool.
- for_person: set ONLY if the message explicitly says the expense belongs to someone else ("for Romi", "de Diego"). Otherwise omit it; the sender is assumed.
- Currency: "pesos"/"mxn"/"$" = MXN; "dlls"/"dólares"/"usd" = USD.
- date: set only if a different day is implied ("yesterday", "ayer", "el viernes"), as YYYY-MM-DD, never in the future.
- Asking about budget/remaining money/how we're doing → show_budget (period "week" if they mention week/semana, else "month"; category if they ask about one).
- Asking what was spent / list / history → list_expenses.
- "undo", "delete last", "borra el último" → delete_last.
- Changing a budget ("set groceries budget to 12000") → set_budget with category and amount (for_person if personal).
- Greetings or "what can you do" → help. Anything else → unknown.
Always call record_intent."""


class Interpreter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model = os.getenv("SPENDI_MODEL", DEFAULT_MODEL)
        self.client = None
        if os.getenv("ANTHROPIC_API_KEY"):
            import anthropic
            self.client = anthropic.Anthropic()
        else:
            log.warning("ANTHROPIC_API_KEY not set — using the offline keyword parser.")

    def interpret(self, text: str, sender: str, today: date) -> dict:
        if self.client:
            try:
                return self._ask_claude(text, sender, today)
            except Exception:
                log.exception("Claude call failed; falling back to keyword parser")
        return rule_based_parse(self.cfg, text)

    def _ask_claude(self, text: str, sender: str, today: date) -> dict:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=800,
            system=build_system_prompt(self.cfg, sender, today),
            tools=[build_tool(self.cfg)],
            tool_choice={"type": "tool", "name": "record_intent"},
            messages=[{"role": "user", "content": text}],
        )
        for block in resp.content:
            if block.type == "tool_use":
                return dict(block.input)
        raise RuntimeError("Claude returned no tool call")


# ── Offline fallback ──────────────────────────────────────────
AMOUNT_RE = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")
CURRENCY_WORDS = {"mxn": "MXN", "pesos": "MXN", "peso": "MXN", "usd": "USD", "dlls": "USD",
                  "dolares": "USD", "dólares": "USD", "dollars": "USD", "eur": "EUR", "euros": "EUR"}
FILLER = {"in", "en", "at", "on", "for", "de", "a", "the", "el", "la", "spent", "gaste", "gasté", "$"}


def _find_category(cfg: Config, text: str) -> str | None:
    low = text.lower()
    for name, c in cfg.categories.items():
        for term in [name, *c.get("keywords", [])]:
            if re.search(rf"(?<!\w){re.escape(term.lower())}(?!\w)", low):
                return name
    return None


def rule_based_parse(cfg: Config, text: str) -> dict:
    low = text.lower().strip()
    period = "week" if re.search(r"\b(week|semana|weekly|semanal)\b", low) else "month"

    if re.search(r"\b(help|ayuda|hola|hi|hello)\b", low):
        return {"intent": "help"}
    if re.search(r"\b(undo|delete|borra|borrar|elimina)\b", low):
        return {"intent": "delete_last"}
    m = re.search(r"set\s+(.+?)\s+budget\s+(?:to\s+)?\$?([\d,\.]+)", low)
    if m:
        return {"intent": "set_budget", "category": _find_category(cfg, m.group(1)),
                "amount": float(m.group(2).replace(",", ""))}
    if re.search(r"\b(budget|presupuesto|available|disponible|left|queda)\b", low):
        return {"intent": "show_budget", "period": period, "category": _find_category(cfg, low)}
    if re.search(r"\b(list|history|historial|last|últimos|ultimos|expenses|gastos)\b", low):
        return {"intent": "list_expenses", "period": period, "category": _find_category(cfg, low)}

    m = AMOUNT_RE.search(text)
    if not m:
        return {"intent": "unknown"}
    amount = float(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else ""))
    rest = (text[:m.start()] + " " + text[m.end():]).strip()
    currency = cfg.currency
    words = []
    for w in rest.split():
        lw = w.lower().strip(".,!")
        if lw in CURRENCY_WORDS:
            currency = CURRENCY_WORDS[lw]
        elif lw not in FILLER:
            words.append(w)
    merchant = " ".join(words).strip().title() or None
    return {"intent": "add_expense", "expenses": [{
        "amount": amount, "currency": currency, "merchant": merchant,
        "category": _find_category(cfg, text) or cfg.fallback_category,
    }]}
