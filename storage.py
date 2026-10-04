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

    def delete_expense(self, expense_id: int) -> None:
        self._write("DELETE FROM expenses WHERE id = ?", (expense_id,))

    # ── budgets ───────────────────────────────────────────────
    def set_budget(self, category: str, person: str, monthly: float) -> None:
        self._write(
            """INSERT INTO budget_overrides (category, person, monthly) VALUES (?,?,?)
               ON CONFLICT(category, person) DO UPDATE SET monthly = excluded.monthly""",
            (category, person, monthly),
        )

    def budget_override(self, category: str, person: str) -> float | None:
        rows = self._read(
            "SELECT monthly FROM budget_overrides WHERE category = ? AND person = ?",
            (category, person),
        )
        return float(rows[0]["monthly"]) if rows else None
