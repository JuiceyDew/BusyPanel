"""The CLI commands run against a real database: the service and the operator
both depend on them, and a typo in one is only visible by invoking it."""

from __future__ import annotations

from typer.testing import CliRunner

from busypanel import billing
from busypanel.cli import app
from busypanel.config import settings
from tests.conftest import add_client, add_video

runner = CliRunner()


def test_status_reports_the_books(state_dir):
    from busypanel import db

    con = db.connect(state_dir / "busypanel.db")
    acme = add_client(con, "Acme")
    add_video(con, acme, "2026-08-03", "One", 20000)
    inv = billing.create_monthly_invoice(con, acme, "2026-08-01", "2026-08-31")
    billing.set_status(con, inv, "sent")
    con.commit()
    con.close()

    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert "1 active" in r.output
    assert "$200.00 (sent, unpaid)" in r.output


def test_status_on_an_empty_database(state_dir):
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert "$0.00 (sent, unpaid)" in r.output


def test_doctor_creates_and_inspects_the_schema(state_dir):
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0, r.output
    assert "invoice_line" in r.output and "login gate: off" in r.output


def test_passwd_sets_and_clears_the_stored_password(state_dir):
    from busypanel.state import load_overrides

    assert runner.invoke(app, ["passwd", "--password", "s3cret"]).exit_code == 0
    assert load_overrides()["auth_password"] == "s3cret"
    assert runner.invoke(app, ["passwd", "--disable"]).exit_code == 0
    assert load_overrides()["auth_password"] == ""


def test_backup_copies_the_database_safely(state_dir, tmp_path):
    from busypanel import db

    con = db.connect(settings.db_path)
    add_client(con, "Acme")
    con.commit()
    con.close()

    out = tmp_path / "copy.db"
    r = runner.invoke(app, ["backup", "--out", str(out)])
    assert r.exit_code == 0, r.output
    copy = db.connect(out)
    try:
        assert copy.execute("SELECT COUNT(*) AS c FROM client").fetchone()["c"] == 1
    finally:
        copy.close()


def test_restore_replaces_the_database(state_dir, tmp_path):
    from busypanel import db

    con = db.connect(settings.db_path)
    add_client(con, "Acme")
    con.commit()
    con.close()

    out = tmp_path / "b.db"
    assert runner.invoke(app, ["backup", "--out", str(out)]).exit_code == 0

    # A second client arrives after the backup.
    con = db.connect(settings.db_path)
    add_client(con, "Beta")
    con.commit()
    con.close()

    r = runner.invoke(app, ["restore", "--force", str(out)])
    assert r.exit_code == 0, r.output
    assert "Restored" in r.output

    con = db.connect(settings.db_path)
    try:
        names = {row["name"] for row in con.execute("SELECT name FROM client")}
    finally:
        con.close()
    assert names == {"Acme"}

    safety = list(state_dir.glob("*.pre-restore-*.db"))
    assert len(safety) == 1
    copy = db.connect(safety[0])
    try:
        # The safety copy is the state as it was just before the restore.
        assert copy.execute("SELECT COUNT(*) AS c FROM client").fetchone()["c"] == 2
    finally:
        copy.close()


def test_restore_refuses_a_non_database(state_dir, tmp_path):
    from busypanel import db

    con = db.connect(settings.db_path)
    add_client(con, "Acme")
    con.commit()
    con.close()

    junk = tmp_path / "notes.txt"
    junk.write_text("this is not a database\n")

    r = runner.invoke(app, ["restore", "--force", str(junk)])
    assert r.exit_code != 0
    assert "Not a BusyPanel database" in r.output

    # The live database is untouched, and no safety copy was made.
    con = db.connect(settings.db_path)
    try:
        assert con.execute("SELECT COUNT(*) AS c FROM client").fetchone()["c"] == 1
    finally:
        con.close()
    assert list(state_dir.glob("*.pre-restore-*.db")) == []

    missing = runner.invoke(app, ["restore", "--force", str(tmp_path / "nope.db")])
    assert missing.exit_code != 0
    assert "No such file" in missing.output


def test_restore_refuses_a_database_missing_its_tables(state_dir, tmp_path):
    import sqlite3

    from busypanel import db

    con = db.connect(settings.db_path)
    add_client(con, "Acme")
    con.commit()
    con.close()

    other = tmp_path / "other.db"
    stray = sqlite3.connect(other)
    stray.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    stray.commit()
    stray.close()

    r = runner.invoke(app, ["restore", "--force", str(other)])
    assert r.exit_code != 0
    assert "missing tables" in r.output

    con = db.connect(settings.db_path)
    try:
        assert con.execute("SELECT COUNT(*) AS c FROM client").fetchone()["c"] == 1
    finally:
        con.close()
