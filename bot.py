"""Spendi's brain: takes (phone, text) and returns the WhatsApp reply."""
import calendar
from datetime import date, datetime, timedelta

from config import Config, normalize_phone
from interpreter import Interpreter
from storage import Store

WEEKS_PER_MONTH = 52 / 12


def money(x: float, whole: bool = False) -> str:
    """$1,250 / $99.50. whole=True rounds to the peso (used for budgets)."""
    sign = "-" if x < 0 else ""
    x = abs(x)
    if whole or float(x).is_integer():
        return f"{sign}${x:,.0f}"
    return f"{sign}${x:,.2f}"


def bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, round(pct * width)))
    return "▓" * filled + "░" * (width - filled)


def period_bounds(day: date, period: str) -> tuple[date, date]:
    if period == "week":
        start = day - timedelta(days=day.weekday())  # Monday
        return start, start + timedelta(days=7)
    start = day.replace(day=1)
    return start, (start + timedelta(days=32)).replace(day=1)


class Spendi:
    def __init__(self, cfg: Config, store: Store, interpreter: Interpreter):
        self.cfg, self.store, self.interpreter = cfg, store, interpreter

    # ── entry point ───────────────────────────────────────────
    def handle(self, phone: str, text: str) -> str | None:
        sender = self.cfg.users.get(normalize_phone(phone))
        if not sender:
            return None  # unknown number: stay silent
        text = (text or "").strip()
        if not text:
            return self.help_text(sender)
        today = datetime.now(self.cfg.tz).date()
        intent = self.interpreter.interpret(text, sender, today)
        handlers = {
            "add_expense": self.add_expenses,
            "show_budget": self.show_budget,
            "list_expenses": self.list_expenses,
            "delete_last": self.delete_last,
            "set_budget": self.set_budget,
            "help": lambda *_: self.help_text(sender),
        }
        handler = handlers.get(intent.get("intent"))
        if not handler:
            return ("🤔 I didn't get that. Send an expense like *100 mxn whole foods*, "
                    "or *show budget*. Type *help* for more.")
        return handler(intent, sender, today, text)

    # ── budgets ───────────────────────────────────────────────
    def monthly_budget(self, category: str, person: str) -> float:
        override = self.store.budget_override(category, person)
        return override if override is not None else self.cfg.default_monthly(category, person)

    def budget_for(self, category: str, person: str, period: str) -> float:
        monthly = self.monthly_budget(category, person)
        return monthly / WEEKS_PER_MONTH if period == "week" else monthly

    def _valid_category(self, name) -> str | None:
        return name if name in self.cfg.categories else None

    # ── intents ───────────────────────────────────────────────
    def add_expenses(self, intent: dict, sender: str, today: date, raw: str) -> str:
        blocks = []
        for e in intent.get("expenses") or []:
            try:
                amount = float(e.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0
            if amount <= 0:
                continue

            category = self._valid_category(e.get("category")) or self.cfg.fallback_category
            person = ""
            if self.cfg.is_personal(category):
                person = e.get("for_person") if e.get("for_person") in self.cfg.people else sender

            currency = (e.get("currency") or self.cfg.currency).upper()
            if currency == self.cfg.currency:
                converted = amount
            elif currency in self.cfg.fx_rates:
                converted = round(amount * self.cfg.fx_rates[currency], 2)
            else:
                blocks.append(f"⚠️ I don't have an exchange rate for {currency}. "
                              f"Send it in {self.cfg.currency} or add {currency} under fx_rates.")
                continue

            spent_on = today
            if e.get("date"):
                try:
                    spent_on = min(date.fromisoformat(e["date"]), today)
                except ValueError:
                    pass

            merchant = (e.get("merchant") or "").strip() or None
            note = (e.get("note") or "").strip() or None
            self.store.add_expense(
                created_at=datetime.now(self.cfg.tz).strftime("%Y-%m-%d %H:%M:%S"),
                spent_on=spent_on.isoformat(), logged_by=sender, amount=converted,
                category=category, person=person, merchant=merchant, note=note,
                original_amount=amount if currency != self.cfg.currency else None,
                original_currency=currency if currency != self.cfg.currency else None,
                raw_text=raw,
            )

            what = " · ".join(x for x in [merchant, note] if x)
            amount_txt = money(converted)
            if currency != self.cfg.currency:
                amount_txt += f" ({amount:,.2f} {currency})"
            verb = "Saved" if self.cfg.is_goal(category) else "Expense saved"
            lines = [f"✅ {verb} under *{self.cfg.label(category, person)}*.",
                     f"{amount_txt}" + (f" · {what}" if what else "")]
            if spent_on != today:
                lines.append(f"📅 Dated {spent_on:%a %d %b}")

            start, end = period_bounds(spent_on, "month")
            budget = self.budget_for(category, person, "month")
            spent = self.store.spent(category, person, start.isoformat(), end.isoformat())
            if self.cfg.is_goal(category):
                if budget > 0:
                    lines.append(f"🎯 {money(spent, True)} saved of the {money(budget, True)} goal this month.")
            elif budget > 0:
                left = budget - spent
                if left >= 0:
                    lines.append(f"{money(left, True)} available out of {money(budget, True)} this month.")
                else:
                    lines.append(f"⚠️ {money(-left, True)} over the {money(budget, True)} monthly budget.")
            blocks.append("\n".join(lines))

        if not blocks:
            return "🤔 I couldn't find an amount. Try something like *100 mxn whole foods*."
        return "\n\n".join(blocks)

    def show_budget(self, intent: dict, sender: str, today: date, raw: str) -> str:
        period = intent.get("period") if intent.get("period") in ("week", "month") else "month"
        only = self._valid_category(intent.get("category"))
        start, end = period_bounds(today, period)

        if period == "month":
            days = calendar.monthrange(today.year, today.month)[1]
            header = f"📊 *{today:%B} budget* · day {today.day} of {days}"
        else:
            header = f"📊 *This week* · {start:%a %d %b} – {end - timedelta(days=1):%a %d %b}"

        lines, total_budget, total_spent, goals = [header, ""], 0.0, 0.0, []
        for category, person in self.cfg.budget_lines():
            if only and category != only:
                continue
            budget = self.budget_for(category, person, period)
            spent = self.store.spent(category, person, start.isoformat(), end.isoformat())
            if budget <= 0 and spent <= 0:
                continue
            name = f"{self.cfg.emoji(category)} *{self.cfg.label(category, person)}*"
            if self.cfg.is_goal(category):
                pct = spent / budget if budget else 1.0
                goals += [f"{name}\n{money(spent, True)} saved of {money(budget, True)} goal",
                          f"{bar(pct)} {pct:.0%}", ""]
                continue
            total_spent += spent
            if budget <= 0:
                lines += [f"{name}\n{money(spent, True)} spent · no budget set", ""]
                continue
            total_budget += budget
            left = budget - spent
            pct = spent / budget
            if left >= 0:
                lines.append(f"{name}\n{money(left, True)} available out of {money(budget, True)}")
            else:
                lines.append(f"{name}\n⚠️ {money(-left, True)} over the {money(budget, True)} budget")
            lines.append(f"{bar(pct)} {pct:.0%} used")
            lines.append("")
        lines += goals

        if not only:
            lines.append(f"💰 *Total spending:* {money(total_budget - total_spent, True)} available "
                         f"out of {money(total_budget, True)}")
        if period == "week":
            lines.append("_Weekly budget = monthly × 12 ÷ 52_")
        return "\n".join(lines).strip()

    def list_expenses(self, intent: dict, sender: str, today: date, raw: str) -> str:
        period = intent.get("period") if intent.get("period") in ("week", "month") else "month"
        only = self._valid_category(intent.get("category"))
        start, end = period_bounds(today, period)
        rows = self.store.list_expenses(start.isoformat(), end.isoformat(), only, limit=15)
        scope = "this week" if period == "week" else f"in {today:%B}"
        title = f"🧾 *Latest expenses {scope}*" + (f" · {only}" if only else "")
        if not rows:
            return f"{title}\nNothing logged yet."
        lines = [title, ""]
        for r in rows:
            d = date.fromisoformat(r["spent_on"])
            label = self.cfg.label(r["category"], r["person"])
            what = r["merchant"] or r["note"] or "—"
            lines.append(f"{d:%d %b} · {money(r['amount'])} · {what} · {label} _({r['logged_by']})_")
        if len(rows) == 15:
            lines.append("_Showing the 15 most recent._")
        return "\n".join(lines)

    def delete_last(self, intent: dict, sender: str, today: date, raw: str) -> str:
        row = self.store.last_logged_by(sender)
        if not row:
            return "There's nothing of yours to delete."
        self.store.delete_expense(row["id"])
        what = row["merchant"] or row["note"] or ""
        return (f"🗑️ Deleted {money(row['amount'])}" + (f" · {what}" if what else "")
                + f" from *{self.cfg.label(row['category'], row['person'])}*.")

    def set_budget(self, intent: dict, sender: str, today: date, raw: str) -> str:
        category = self._valid_category(intent.get("category"))
        try:
            amount = float(intent.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        if not category or amount <= 0:
            cats = ", ".join(self.cfg.categories)
            return f"Tell me the category and amount, e.g. *set Groceries budget to 12000*.\nCategories: {cats}"
        person = ""
        if self.cfg.is_personal(category):
            person = intent.get("for_person") if intent.get("for_person") in self.cfg.people else sender
        self.store.set_budget(category, person, amount)
        return (f"✅ *{self.cfg.label(category, person)}* monthly budget set to {money(amount)} "
                f"(≈ {money(amount / WEEKS_PER_MONTH, True)} per week).")

    def help_text(self, sender: str) -> str:
        cats = "\n".join(f"{self.cfg.emoji(c)} {c}" for c in self.cfg.categories)
        other = next((p for p in self.cfg.people if p != sender), sender)
        return (f"Hi {sender}! I'm {self.cfg.bot_name} 👋\n\n"
                f"*Log an expense*\n• 100 mxn whole foods\n• 350 uber ayer\n• 200 sephora for {other}\n\n"
                "*See budgets*\n• show budget\n• show weekly budget\n• how much is left in groceries?\n\n"
                "*Other*\n• last expenses\n• undo (deletes your last expense)\n"
                "• set groceries budget to 12000\n\n"
                f"*Categories*\n{cats}")
