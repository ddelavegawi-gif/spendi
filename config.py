"""Loads config.yaml: people, categories and default budgets."""
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


def normalize_phone(raw: str) -> str:
    """'whatsapp:+5215512345678' -> '+525512345678'.

    Mexican mobile numbers sometimes arrive with an extra '1' after +52,
    so we strip it to make config numbers and incoming numbers match.
    """
    p = re.sub(r"[^\d+]", "", (raw or "").replace("whatsapp:", ""))
    if p and not p.startswith("+"):
        p = "+" + p
    if p.startswith("+521") and len(p) == 14:
        p = "+52" + p[4:]
    return p


class Config:
    def __init__(self, path: str = "config.yaml"):
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        self.bot_name = data.get("bot_name", "Spendi")
        self.currency = str(data.get("currency", "MXN")).upper()
        self.tz = ZoneInfo(data.get("timezone", "America/Mexico_City"))
        self.cycle_start_day = int(data.get("budget_cycle_start_day", 1))
        if not 1 <= self.cycle_start_day <= 28:
            raise ValueError("budget_cycle_start_day must be between 1 and 28")
        self.users = {normalize_phone(str(k)): v for k, v in data["users"].items()}
        self.people = list(dict.fromkeys(self.users.values()))
        self.categories: dict = data["categories"]
        self.fallback_category = data.get("fallback_category", "Other")
        self.fx_rates = {k.upper(): float(v) for k, v in (data.get("fx_rates") or {}).items()}
        self.renamed_categories = dict(data.get("renamed_categories") or {})
        for old, new in self.renamed_categories.items():
            if new not in self.categories or self.categories[new].get("scope") == "personal":
                raise ValueError(f"renamed_categories: {old} → {new}: target must be a shared category")
        if self.fallback_category not in self.categories:
            raise ValueError(f"fallback_category '{self.fallback_category}' is not a category")

    def is_personal(self, category: str) -> bool:
        return self.categories[category].get("scope") == "personal"

    def is_goal(self, category: str) -> bool:
        return self.categories[category].get("type") == "goal"

    def emoji(self, category: str) -> str:
        return self.categories[category].get("emoji", "•")

    def label(self, category: str, person: str = "") -> str:
        return f"{category} {person}" if person else category

    def default_monthly(self, category: str, person: str = "") -> float:
        monthly = self.categories[category].get("monthly", 0)
        if isinstance(monthly, dict):
            return float(monthly.get(person, 0))
        return float(monthly)

    def budget_lines(self) -> list[tuple[str, str]]:
        """Every (category, person) budget line: shared first, then personal per person."""
        shared = [(c, "") for c in self.categories if not self.is_personal(c)]
        personal = [(c, p) for c in self.categories if self.is_personal(c) for p in self.people]
        return shared + personal
