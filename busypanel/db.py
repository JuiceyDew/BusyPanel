"""SQLite store: clients, videos, invoices, lines and expenses.

One file, opened per request, `sqlite3.Row` rows so the templates can use column
names. Schema decisions worth keeping in mind when editing this file:

* ``video.invoice_id`` is a **soft link**, not a lock. Deleting an invoice sets it
  back to NULL, so billed work returns to the unbilled pool instead of being
  stranded. (Harvest hard-locks its timesheets and needs a report-based "mark
  uninvoiced" escape hatch; Toggl has no linkage at all and needs a manual tag.)
* ``invoice.period_start``/``period_end`` are stored on the invoice rather than
  inferred from its lines, so the billable window survives line edits.
* Invoice lines are **materialised at creation**, so a sent invoice is a stable
  snapshot and does not silently change when a video is edited afterwards.
* ``client.name`` and ``invoice.number`` are UNIQUE -- the database, not the
  route handler, is the last line of defence against a duplicate.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from busypanel.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS client (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  name             TEXT    NOT NULL UNIQUE,
  video_rate_cents INTEGER NOT NULL DEFAULT 0,
  email            TEXT    NOT NULL DEFAULT '',
  notes            TEXT    NOT NULL DEFAULT '',
  archived         INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  number       TEXT    NOT NULL UNIQUE,
  client_id    INTEGER NOT NULL REFERENCES client(id),
  kind         TEXT    NOT NULL CHECK (kind IN ('monthly','oneoff')),
  period_start TEXT,                -- YYYY-MM-DD; NULL for oneoff
  period_end   TEXT,
  issue_date   TEXT    NOT NULL,
  due_date     TEXT    NOT NULL,
  status       TEXT    NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','sent','paid')),
  paid_date    TEXT,
  notes        TEXT    NOT NULL DEFAULT '',
  created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_invoice_client ON invoice(client_id, issue_date);
CREATE INDEX IF NOT EXISTS ix_invoice_status ON invoice(status);

CREATE TABLE IF NOT EXISTS video (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id  INTEGER NOT NULL REFERENCES client(id),
  shot_on    TEXT    NOT NULL,      -- YYYY-MM-DD
  title      TEXT    NOT NULL,
  rate_cents INTEGER NOT NULL,
  invoice_id INTEGER REFERENCES invoice(id),   -- NULL = unbilled
  created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_video_unbilled ON video(client_id, shot_on) WHERE invoice_id IS NULL;

CREATE TABLE IF NOT EXISTS invoice_line (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_id  INTEGER NOT NULL REFERENCES invoice(id) ON DELETE CASCADE,
  video_id    INTEGER REFERENCES video(id),    -- NULL for free-text / one-off lines
  description TEXT    NOT NULL,
  qty         INTEGER NOT NULL DEFAULT 1,
  unit_cents  INTEGER NOT NULL,
  position    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_line_invoice ON invoice_line(invoice_id, position);

CREATE TABLE IF NOT EXISTS expense (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  spent_on     TEXT    NOT NULL,
  amount_cents INTEGER NOT NULL,
  category     TEXT    NOT NULL DEFAULT '',
  vendor       TEXT    NOT NULL DEFAULT '',
  client_id    INTEGER REFERENCES client(id),
  deductible   INTEGER NOT NULL DEFAULT 1,   -- "writeoff" = deductible expense
  notes        TEXT    NOT NULL DEFAULT '',
  created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_expense_date ON expense(spent_on);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the database, creating the schema on first use.

    Safe to call repeatedly: every statement in SCHEMA is IF NOT EXISTS, and an
    existing file keeps its data.
    """
    p = path or settings.db_path
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    # Off by default in SQLite, and invoice_line relies on ON DELETE CASCADE.
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


def next_invoice_number(con: sqlite3.Connection, issue_date: str) -> str:
    """The next invoice number for the issue date's year: ``YYYY-NNNN``.

    The sequence lives in ``meta`` rather than being derived from the invoices
    that exist right now. ``MAX(number) + 1`` looks equivalent but is not: delete
    the newest invoice and MAX drops back, handing the same number to the next
    invoice -- two invoices on file under one number, which is exactly the thing
    an invoice number exists to prevent. The stored counter is seeded from
    existing rows (so a database written before this counter existed still
    continues sensibly) and only ever moves forward.
    """
    year = issue_date[:4]
    key = f"invoice_seq:{year}"
    existing = int(con.execute(
        "SELECT COALESCE(MAX(CAST(substr(number, 6) AS INTEGER)), 0) AS n "
        "FROM invoice WHERE number LIKE ?",
        (f"{year}-%",),
    ).fetchone()["n"])
    stored = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    n = max(existing, int(stored["value"]) if stored else 0) + 1
    con.execute(
        "INSERT INTO meta (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(n)),
    )
    return f"{year}-{n:04d}"
