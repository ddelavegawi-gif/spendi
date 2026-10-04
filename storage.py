"""SQLite storage for expenses and budget overrides."""
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,              -- when it was logged (local time)
    spent_on          TEXT NOT NULL,              -- YYYY-MM-DD the money was spent
    logged_by         TEXT NOT NULL,              -- who sent the message
    amount            REAL NOT NULL,              -- in the budget currency
    original_amount   REAL,
    original_currency TEXT,
    merchant          TEXT,
    note              TEXT,
    category          TEXT NOT NULL,
    person            TEXT NOT NULL DEFAULT '',   -- '' = shared, otherwise whose personal budget
    raw_text          TEXT
);
CREATE INDEX IF NOT EXISTS idx_expenses_spent_on ON expenses (spent_on);

-- Nothing is ever destroyed: deleted/reset expenses are copied here first.
CREATE TABLE IF NOT EXISTS deleted_expenses (
    id INTEGER, created_at TEXT, spent_on TEXT, logged_by TEXT, amount REAL,
    original_amount REAL, original_currency TEXT, merchant TEXT, note TEXT,
    category TEXT, person TEXT, raw_text TEXT,
    deleted_at TEXT DEFAULT CURRENT_TIMESTAMP,
    deleted_by TEXT
);

-- Import files that already ran, so each one loads exactly once.
CREATE TABLE IF NOT EXISTS imports_done (
    batch   TEXT PRIMARY KEY,
    done_at TEXT DEFAULT CURRENT_TIMESTAMP,
    count   INTEGER
);

CREATE TABLE IF NOT EXISTS budget_overrides (
    category TEXT NOT NULL,
    person   TEXT NOT NULL DEFAULT '',
    monthly  REAL NOT NULL,
    PRIMARY KEY (category, person)
);
"""


class Store:
    def __init__(self, path: str = "spendi.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def _write(self, sql: str, params=()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.lastrowid

    def _read(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    # ── expenses ──────────────────────────────────────────────
    def add_expense(self, *, created_at, spent_on, logged_by, amount, category, person,
                    merchant, note, original_amount, original_currency, raw_text) -> int:
        return self._write(
            """INSERT INTO expenses (created_at, spent_on, logged_by, amount, original_amount,
                   original_currency, merchant, note, category, person, raw_text)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (created_at, spent_on, logged_by, amount, original_amount, original_currency,
             merchant, note, category, person, raw_text),
        )

    def spent(self, category: str, person: str, start: str, end: str) -> float:
        rows = self._read(
            """SELECT COALESCE(SUM(amount), 0) AS total FROM expenses
               WHERE category = ? AND person = ? AND spent_on >= ? AND spent_on < ?""",
            (category, person, start, end),
        )
        return float(rows[0]["total"])

    def list_expenses(self, start: str, end: str, category: str | None = None,
                      limit: int = 15) -> list[sqlite3.Row]:
        sql = "SELECT * FROM expenses WHERE spent_on >= ? AND spent_on < ?"
        params: list = [start, end]
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += " ORDER BY spent_on DESC, id DESC LIMIT ?"
        params.append(limit)
        return self._read(sql, params)

    def last_logged_by(self, logged_by: str) -> sqlite3.Row | None:
        rows = self._read(
            "SELECT * FROM expenses WHERE logged_by = ? ORDER BY id DESC LIMIT 1", (logged_by,)
        )
        return rows[0] if rows else None

    def get_expense(self, expense_id: int) -> sqlite3.Row | None:
        rows = self._read("SELECT * FROM expenses WHERE id = ?", (expense_id,))
        return rows[0] if rows else None

    def _archive_and_delete(self, where: str, params, deleted_by: str) -> None:
        cols = ("id, created_at, spent_on, logged_by, amount, original_amount, original_currency, "
                "merchant, note, category, person, raw_text")
        with self.lock:
            self.conn.execute(
                f"INSERT INTO deleted_expenses ({cols}, deleted_by) "
                f"SELECT {cols}, ? FROM expenses WHERE {where}", (deleted_by, *params))
            self.conn.execute(f"DELETE FROM expenses WHERE {where}", params)
            self.conn.commit()

    def delete_expense(self, expense_id: int, deleted_by: str = "") -> None:
        self._archive_and_delete("id = ?", (expense_id,), deleted_by)

    def period_summary(self, start: str, end: str) -> tuple[int, float]:
        rows = self._read(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total FROM expenses "
            "WHERE spent_on >= ? AND spent_on < ?", (start, end))
        return int(rows[0]["n"]), float(rows[0]["total"])

    def delete_period(self, start: str, end: str, deleted_by: str = "") -> None:
        self._archive_and_delete("spent_on >= ? AND spent_on < ?", (start, end), deleted_by)

    def update_expense(self, expense_id: int, *, category: str, person: str, amount: float) -> None:
        self._write("UPDATE expenses SET category = ?, person = ?, amount = ? WHERE id = ?",
                    (category, person, amount, expense_id))

    def recategorize(self, old: str, new: str) -> int:
        """Move every expense from category `old` to `new` (safe to run repeatedly)."""
        with self.lock:
            cur = self.conn.execute("UPDATE expenses SET category = ?, person = '' WHERE category = ?", (new, old))
            self.conn.execute("DELETE FROM budget_overrides WHERE category = ?", (old,))
            self.conn.commit()
            return cur.rowcount

    def spent_outside(self, categories: list[str], start: str, end: str) -> list[sqlite3.Row]:
        """Spending in categories that are not in the config (so it never silently disappears)."""
        marks = ",".join("?" * len(categories))
        return self._read(
            f"""SELECT category, COUNT(*) AS n, SUM(amount) AS total FROM expenses
                WHERE spent_on >= ? AND spent_on < ? AND category NOT IN ({marks})
                GROUP BY category""", (start, end, *categories))

    # ── imports ───────────────────────────────────────────────
    def import_done(self, batch: str) -> bool:
        return bool(self._read("SELECT 1 FROM imports_done WHERE batch = ?", (batch,)))

    def run_import(self, batch: str, rows: list[dict], clear_existing: bool, created_at: str,
                   recategorize: dict | None = None, reset_budget_overrides: bool = False) -> int:
        """One transaction: optional cleanup/renames first, then insert the rows."""
        cols = ("id, created_at, spent_on, logged_by, amount, original_amount, original_currency, "
                "merchant, note, category, person, raw_text")
        with self.lock:
            try:
                if clear_existing:
                    self.conn.execute(
                        f"INSERT INTO deleted_expenses ({cols}, deleted_by) "
                        f"SELECT {cols}, ? FROM expenses", (f"import:{batch}",))
                    self.conn.execute("DELETE FROM expenses")
                for old, new in (recategorize or {}).items():
                    self.conn.execute("UPDATE expenses SET category = ?, person = '' WHERE category = ?", (new, old))
                    self.conn.execute("DELETE FROM budget_overrides WHERE category = ?", (old,))
                if reset_budget_overrides:
                    self.conn.execute("DELETE FROM budget_overrides")
                for r in rows:
                    self.conn.execute(
                        """INSERT INTO expenses (created_at, spent_on, logged_by, amount, original_amount,
                               original_currency, merchant, note, category, person, raw_text)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (created_at, r["spent_on"], r["logged_by"], r["amount"], r.get("original_amount"),
                         r.get("original_currency"), r.get("merchant"), r.get("note"), r["category"],
                         r.get("person", ""), f"import:{batch}"))
                self.conn.execute("INSERT INTO imports_done (batch, count) VALUES (?, ?)", (batch, len(rows)))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return len(rows)

    # ── budgets ───────────────────────────────────────────────
    def set_budget(self, category: str, person: str, monthly: float) -> None:
        self._write(
            """INSERT INTO budget_overrides (category, person, monthly) VALUES (?,?,?)
               ON CONFLICT(category, person) DO UPDATE SET monthly = excluded.monthly""",
            (category, person, monthly),
        )

    def clear_budget_overrides(self) -> None:
        self._write("DELETE FROM budget_overrides")

    def budget_override(self, category: str, person: str) -> float | None:
        rows = self._read(
            "SELECT monthly FROM budget_overrides WHERE category = ? AND person = ?",
            (category, person),
        )
        return float(rows[0]["monthly"]) if rows else None
