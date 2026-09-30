"""The database opens idempotently and enforces its constraints."""

from __future__ import annotations

import sqlite3

import pytest

from busypanel import db


def test_connect_is_idempotent_and_preserves_data(tmp_path):
    path = tmp_path / "t.db"
    con = db.connect(path)
    con.execute("INSERT INTO client (name, created_at) VALUES ('Acme', '2026-08-01')")
    con.commit()
    con.close()

    again = db.connect(path)
    try:
        assert again.execute("SELECT COUNT(*) AS c FROM client").fetchone()["c"] == 1
    finally:
        again.close()


def test_foreign_keys_are_on(con):
    # invoice_line relies on ON DELETE CASCADE; SQLite has this off by default.
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_client_name_is_unique(con):
    con.execute("INSERT INTO client (name, created_at) VALUES ('Acme', '2026-08-01')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO client (name, created_at) VALUES ('Acme', '2026-08-01')")


def test_invoice_kind_and_status_are_constrained(con):
    con.execute("INSERT INTO client (id, name, created_at) VALUES (1, 'Acme', '2026-08-01')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO invoice (number, client_id, kind, issue_date, due_date, created_at) "
            "VALUES ('2026-0001', 1, 'weekly', '2026-08-01', '2026-08-31', '2026-08-01')"
        )


def test_deleting_an_invoice_cascades_its_lines(con):
    con.execute("INSERT INTO client (id, name, created_at) VALUES (1, 'Acme', '2026-08-01')")
    con.execute(
        "INSERT INTO invoice (id, number, client_id, kind, issue_date, due_date, created_at) "
        "VALUES (1, '2026-0001', 1, 'oneoff', '2026-08-01', '2026-08-31', '2026-08-01')"
    )
    con.execute(
        "INSERT INTO invoice_line (invoice_id, description, qty, unit_cents) "
        "VALUES (1, 'Website build', 1, 120000)"
    )
    con.execute("DELETE FROM invoice WHERE id=1")
    assert con.execute("SELECT COUNT(*) AS c FROM invoice_line").fetchone()["c"] == 0


def test_next_invoice_number_starts_at_one_and_uses_the_issue_year(con):
    assert db.next_invoice_number(con, "2026-08-14") == "2026-0001"
    con.execute("INSERT INTO client (id, name, created_at) VALUES (1, 'Acme', '2026-08-01')")
    con.execute(
        "INSERT INTO invoice (number, client_id, kind, issue_date, due_date, created_at) "
        "VALUES ('2026-0001', 1, 'oneoff', '2026-08-01', '2026-08-31', '2026-08-01')"
    )
    assert db.next_invoice_number(con, "2026-08-14") == "2026-0002"
    # A different year has its own sequence.
    assert db.next_invoice_number(con, "2027-01-14") == "2027-0001"


def test_migration_adds_the_new_column_to_an_old_database(tmp_path):
    """A database written before a column existed still opens and gains it."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    # The client table as it was before payment_terms_days: no such column.
    old.execute(
        "CREATE TABLE client (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, "
        "video_rate_cents INTEGER NOT NULL DEFAULT 0, email TEXT NOT NULL DEFAULT '', "
        "notes TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL)"
    )
    old.execute("INSERT INTO client (name, created_at) VALUES ('Acme', '2026-08-01')")
    old.commit()
    old.close()

    con = db.connect(path)
    try:
        columns = {r["name"] for r in con.execute("PRAGMA table_info(client)")}
        assert "payment_terms_days" in columns
        row = con.execute("SELECT name, payment_terms_days FROM client").fetchone()
        assert row["name"] == "Acme"
        assert row["payment_terms_days"] is None
    finally:
        con.close()
