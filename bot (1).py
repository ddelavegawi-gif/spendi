"""Spendi's brain: takes (phone, text) and returns the WhatsApp reply."""
from datetime import date, datetime, timedelta

from config import Config, normalize_phone
from interpreter import Interpreter
from storage import Store

WEEKS_PER_MONTH = 52 / 12
CONFIRM_MINUTES = 5
YES = {"yes", "y", "si", "sí", "ok", "confirm", "confirmo", "dale"}
NO = {"no", "cancel", "cancelar", "nope"}


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


def period_bounds(day: date, period: str, start_day: int = 1) -> tuple[date, date]:
    """[start, end) of the week (Mon–Sun) or budget cycle containing `day`.

    With start_day=16 the cycle containing 4 Oct is 16 Sep – 15 Oct (end = 16 Oct, exclusive).
    """
    if period == "week":
        start = day - timedelta(days=day.weekday())  # Monday
        return start, start + timedelta(days=7)
    if day.day >= start_day:
        start = day.replace(day=start_day)
    else:
        start = (day.replace(day=1) - timedelta(days=1)).replace(day=start_day)
    end = (start.replace(day=1) + timedelta(days=32)).replace(day=start_day)
    return start, end


def cycle_name(start: date, end: date, start_day: int) -> str:
    """'October' for calendar months, otherwise '16 Sep – 15 Oct'."""
    if start_day == 1:
        return f"{start:%B}"
    last = end - timedelta(days=1)
    return f"{start.day} {start:%b} – {last.day} {last:%b}"


class Spendi:
    def __init__(self, cfg: Config, store: Store, interpreter: Interpreter):
        self.cfg, self.store, self.interpreter = cfg, store, interpreter
        self.pending: dict[str, tuple[dict, datetime]] = {}  # sender -> (action, expires)

    # ── entry point ───────────────────────────────────────────
    def handle(self, phone: str, text: str) -> str | None:
        sender = self.cfg.users.get(normalize_phone(phone))
        if not sender:
            return None  # unknown number: stay silent
        text = (text or "").strip()
        if not text:
            return self.help_text(sender)
        now = datetime.now(self.cfg.tz)
        today = now.date()

        # A risky action (like a reset) is waiting for this person's YES.
        prefix = ""
        if sender in self.pending:
            action, expires = self.pending.pop(sender)
            answer = text.lower().strip(" .!¡")
            if now <= expires and answer in YES:
                return self.run_confirmed(action, sender)
            if answer in NO or answer in YES:
                return "👍 Cancelled. Nothing was deleted."
            prefix = "_(Reset cancelled.)_\n\n"

        intent = self.interpreter.interpret(text, sender, today)
        handlers = {
            "add_expense": self.add_expenses,
            "show_budget": self.show_budget,
            "list_expenses": self.list_expenses,
            "delete_last": self.delete_last,
            "delete_expense": self.delete_expense,
            "edit_expense": self.edit_expense,
            "reset_period": self.reset_period,
            "set_budget": self.set_budget,
            "reset_budgets": self.reset_budgets,
            "help": lambda *_: self.help_text(sender),
        }
        handler = handlers.get(intent.get("intent"))
        if not handler:
            return prefix + ("🤔 I didn't get that. Send an expense like *100 mxn whole foods*, "
                             "or *show budget*. Type *help* for more.")
        return prefix + handler(intent, sender, today, text)

    # ── periods & budgets ─────────────────────────────────────
    def bounds(self, day: date, period: str) -> tuple[date, date]:
        return period_bounds(day, period, self.cfg.cycle_start_day)

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
            expense_id = self.store.add_expense(
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
                     f"{amount_txt}" + (f" · {what}" if what else "") + f" · #{expense_id}"]
            if spent_on != today:
                lines.append(f"📅 Dated {spent_on:%a %d %b}")

            start, end = self.bounds(spent_on, "month")
            budget = self.budget_for(category, person, "month")
            spent = self.store.spent(category, person, start.isoformat(), end.isoformat())
            if self.cfg.is_goal(category):
                if budget > 0:
                    lines.append(f"🎯 {money(spent, True)} saved of the {money(budget, True)} goal this cycle.")
            elif budget > 0:
                left = budget - spent
                if left >= 0:
                    lines.append(f"{money(left, True)} available out of {money(budget, True)} this cycle.")
                else:
                    lines.append(f"⚠️ {money(-left, True)} over the {money(budget, True)} monthly budget.")
            blocks.append("\n".join(lines))

        if not blocks:
            return "🤔 I couldn't find an amount. Try something like *100 mxn whole foods*."
        return "\n\n".join(blocks)

    def show_budget(self, intent: dict, sender: str, today: date, raw: str) -> str:
        period = intent.get("period") if intent.get("period") in ("week", "month") else "month"
        only = self._valid_category(intent.get("category"))
        start, end = self.bounds(today, period)

        if period == "month":
            days = (end - start).days
            day_n = (today - start).days + 1
            name = cycle_name(start, end, self.cfg.cycle_start_day)
            header = f"📊 *Budget {name}* · day {day_n} of {days}"
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
        start, end = self.bounds(today, period)
        rows = self.store.list_expenses(start.isoformat(), end.isoformat(), only, limit=15)
        scope = "this week" if period == "week" else cycle_name(start, end, self.cfg.cycle_start_day)
        title = f"🧾 *Latest expenses {scope}*" + (f" · {only}" if only else "")
        if not rows:
            return f"{title}\nNothing logged yet."
        lines = [title, ""]
        for r in rows:
            d = date.fromisoformat(r["spent_on"])
            label = self.cfg.label(r["category"], r["person"])
            what = r["merchant"] or r["note"] or "—"
            lines.append(f"#{r['id']} · {d:%d %b} · {money(r['amount'])} · {what} · {label} _({r['logged_by']})_")
        if len(rows) == 15:
            lines.append("_Showing the 15 most recent._")
        lines.append("\n_To fix one: *delete #12* or *move #12 to Groceries*_")
        return "\n".join(lines)

    def delete_last(self, intent: dict, sender: str, today: date, raw: str) -> str:
        row = self.store.last_logged_by(sender)
        if not row:
            return "There's nothing of yours to delete."
        self.store.delete_expense(row["id"], deleted_by=sender)
        what = row["merchant"] or row["note"] or ""
        return (f"🗑️ Deleted {money(row['amount'])}" + (f" · {what}" if what else "")
                + f" from *{self.cfg.label(row['category'], row['person'])}*.")

    def _describe(self, row) -> str:
        what = row["merchant"] or row["note"] or ""
        return f"#{row['id']} · {money(row['amount'])}" + (f" · {what}" if what else "")

    def delete_expense(self, intent: dict, sender: str, today: date, raw: str) -> str:
        row = self.store.get_expense(intent.get("expense_id") or 0)
        if not row:
            return "I can't find that expense number. Send *last expenses* to see the numbers."
        self.store.delete_expense(row["id"], deleted_by=sender)
        return f"🗑️ Deleted {self._describe(row)} from *{self.cfg.label(row['category'], row['person'])}*."

    def edit_expense(self, intent: dict, sender: str, today: date, raw: str) -> str:
        row = (self.store.get_expense(intent["expense_id"]) if intent.get("expense_id")
               else self.store.last_logged_by(sender))
        if not row:
            return "I can't find that expense. Send *last expenses* to see the numbers."
        category = self._valid_category(intent.get("category")) or row["category"]
        person = ""
        if self.cfg.is_personal(category):
            wanted = intent.get("for_person")
            if wanted in self.cfg.people:
                person = wanted
            elif row["person"]:
                person = row["person"]
            else:
                person = row["logged_by"]
        try:
            amount = float(intent.get("amount") or row["amount"])
        except (TypeError, ValueError):
            amount = row["amount"]
        if amount <= 0:
            amount = row["amount"]
        if (category, person, amount) == (row["category"], row["person"], row["amount"]):
            return ("Tell me what to change, e.g. *move #12 to Groceries*, "
                    "*#12 was personal*, or *change #12 to 150*.")
        self.store.update_expense(row["id"], category=category, person=person, amount=amount)
        old_label = self.cfg.label(row["category"], row["person"])
        new_label = self.cfg.label(category, person)
        changes = []
        if new_label != old_label:
            changes.append(f"{old_label} → *{new_label}*")
        if amount != row["amount"]:
            changes.append(f"{money(row['amount'])} → *{money(amount)}*")
        what = row["merchant"] or row["note"] or ""
        return f"✏️ Updated #{row['id']}" + (f" · {what}" if what else "") + "\n" + "\n".join(changes)

    def reset_period(self, intent: dict, sender: str, today: date, raw: str) -> str:
        period = intent.get("period") if intent.get("period") in ("week", "month") else "month"
        start, end = self.bounds(today, period)
        count, total = self.store.period_summary(start.isoformat(), end.isoformat())
        name = "this week" if period == "week" else f"the {cycle_name(start, end, self.cfg.cycle_start_day)} cycle"
        if count == 0:
            return f"There are no expenses in {name} to reset."
        expires = datetime.now(self.cfg.tz) + timedelta(minutes=CONFIRM_MINUTES)
        self.pending[sender] = ({"type": "reset_period", "start": start.isoformat(),
                                 "end": end.isoformat(), "name": name}, expires)
        things = "the 1 expense" if count == 1 else f"all {count} expenses"
        return (f"⚠️ This will delete *{things}* from {name} "
                f"({money(total)} total, logged by both of you) and every budget goes back to full.\n\n"
                f"Reply *YES* within {CONFIRM_MINUTES} minutes to confirm. Anything else cancels.")

    def run_confirmed(self, action: dict, sender: str) -> str:
        if action["type"] == "reset_period":
            count, total = self.store.period_summary(action["start"], action["end"])
            self.store.delete_period(action["start"], action["end"], deleted_by=sender)
            things = "1 expense" if count == 1 else f"{count} expenses"
            return (f"🧹 Done. Deleted {things} ({money(total)}) from {action['name']}. "
                    "All budgets are back to full.")
        return "Nothing to confirm."

    def reset_budgets(self, intent: dict, sender: str, today: date, raw: str) -> str:
        self.store.clear_budget_overrides()
        return "↩️ All budgets are back to the amounts in your settings. Send *show budget* to see them."

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
                "*Fix & manage*\n• last expenses (shows #numbers)\n• undo (deletes your last expense)\n"
                f"• delete #12\n• move #12 to Groceries / #12 was {other}'s\n• change #12 to 150\n"
                "• set groceries budget to 12000\n• reset budget (wipes this cycle, asks first)\n\n"
                f"*Categories*\n{cats}")
