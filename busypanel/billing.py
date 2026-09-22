"""Billing: turning unbilled videos into a monthly invoice, and one-off invoices.

Every function takes an open connection and does **not** commit -- callers commit,
so a route can wrap several calls in one transaction and a failure halfway through
a monthly invoice leaves nothing behind.

The invariants encoded here are the ones a billing tool gets wrong:

* A client+period can only produce one monthly invoice (``AlreadyInvoiced``).
* A $0 invoice is never created (``NothingToBill``).
* A monthly invoice only ever pulls that client's *unbilled* videos inside the
  period, and materialises them as lines so the sent invoice is a snapshot.
* Deleting an invoice releases its videos back to the unbilled pool.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from busypanel.db import next_invoice_number

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

STATUSES = ("draft", "sent", "paid")


class NothingToBill(Exception):
    """No unbilled videos in the requested period."""


class AlreadyInvoiced(Exception):
    """A monthly invoice already exists for this client and period."""

    def __init__(self, invoice_id: int):
        super().__init__(f"already invoiced as #{invoice_id}")
        self.invoice_id = invoice_id


def today() -> str:
    """The one place the current date is read.

    Stored dates are `YYYY-MM-DD` strings compared as strings, and this is the
    system's local date, so a month boundary matches the operator's calendar
    rather than UTC. If month-end invoices ever land on the wrong day, this
    function is the single place to change.
    """
    return date.today().isoformat()


def human_day(iso: str) -> str:
    """'2026-08-14' -> 'Aug 14'. The date part of a monthly invoice line."""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
    except ValueError:
        return iso
    return f"{MONTHS[d.month - 1]} {d.day}"


def unbilled_videos(con: sqlite3.Connection, client_id: int,
                    period_start: str, period_end: str) -> list[sqlite3.Row]:
    return list(con.execute(
        "SELECT * FROM video WHERE client_id=? AND invoice_id IS NULL "
        "AND shot_on BETWEEN ? AND ? ORDER BY shot_on, id",
        (client_id, period_start, period_end),
    ))


def unbilled_summary(con: sqlite3.Connection, period_start: str, period_end: str) -> list[dict]:
    """One dict per active client that has unbilled videos in the period.

    Key names are read literally by the landing template: client_id, client_name,
    count, total_cents.
    """
    rows = con.execute(
        "SELECT c.id AS client_id, c.name AS client_name, COUNT(v.id) AS count, "
        "       COALESCE(SUM(v.rate_cents), 0) AS total_cents "
        "FROM client c JOIN video v ON v.client_id = c.id "
        "WHERE v.invoice_id IS NULL AND v.shot_on BETWEEN ? AND ? AND c.archived = 0 "
        "GROUP BY c.id, c.name ORDER BY c.name",
        (period_start, period_end),
    )
    return [dict(r) for r in rows]


def add_line(con: sqlite3.Connection, invoice_id: int, description: str,
             qty: int, unit_cents: int, video_id: int | None = None) -> int:
    """Append a line. `position` continues from the highest one on the invoice."""
    row = con.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM invoice_line WHERE invoice_id=?",
        (invoice_id,),
    ).fetchone()
    cur = con.execute(
        "INSERT INTO invoice_line (invoice_id, video_id, description, qty, unit_cents, position) "
        "VALUES (?,?,?,?,?,?)",
        (invoice_id, video_id, description, qty, unit_cents, row["p"]),
    )
    return int(cur.lastrowid)


def update_line(con: sqlite3.Connection, line_id: int, *, description: str,
                qty: int, unit_cents: int) -> None:
    con.execute(
        "UPDATE invoice_line SET description=?, qty=?, unit_cents=? WHERE id=?",
        (description, qty, unit_cents, line_id),
    )


def delete_line(con: sqlite3.Connection, line_id: int) -> None:
    con.execute("DELETE FROM invoice_line WHERE id=?", (line_id,))


def invoice_total(con: sqlite3.Connection, invoice_id: int) -> int:
    row = con.execute(
        "SELECT COALESCE(SUM(qty * unit_cents), 0) AS t FROM invoice_line WHERE invoice_id=?",
        (invoice_id,),
    ).fetchone()
    return int(row["t"])


def set_status(con: sqlite3.Connection, invoice_id: int, status: str) -> None:
    """Move an invoice between draft/sent/paid, stamping paid_date on payment."""
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status!r}")
    paid = today() if status == "paid" else None
    con.execute(
        "UPDATE invoice SET status=?, paid_date=? WHERE id=?", (status, paid, invoice_id)
    )


def _insert_invoice(con: sqlite3.Connection, client_id: int, kind: str,
                    issue_date: str, due_days: int,
                    period_start: str | None, period_end: str | None) -> int:
    due = (datetime.strptime(issue_date, "%Y-%m-%d") + timedelta(days=due_days)).date().isoformat()
    cur = con.execute(
        "INSERT INTO invoice (number, client_id, kind, period_start, period_end, "
        "issue_date, due_date, status, created_at) VALUES (?,?,?,?,?,?,?, 'draft', ?)",
        (next_invoice_number(con, issue_date), client_id, kind, period_start,
         period_end, issue_date, due, datetime.now().isoformat(timespec="seconds")),
    )
    return int(cur.lastrowid)


def create_monthly_invoice(con: sqlite3.Connection, client_id: int,
                           period_start: str, period_end: str, *,
                           issue_date: str | None = None, due_days: int = 30) -> int:
    """Bill one client's unbilled videos in a period. Returns the new invoice id.

    Raises AlreadyInvoiced if this client already has a monthly invoice for this
    exact period (the dedup guard -- invoicing the same month twice is the
    expensive mistake), and NothingToBill rather than creating an empty invoice.
    """
    issue = issue_date or today()
    existing = con.execute(
        "SELECT id FROM invoice WHERE client_id=? AND kind='monthly' "
        "AND period_start=? AND period_end=?",
        (client_id, period_start, period_end),
    ).fetchone()
    if existing:
        raise AlreadyInvoiced(int(existing["id"]))

    videos = unbilled_videos(con, client_id, period_start, period_end)
    if not videos:
        raise NothingToBill(
            f"no unbilled videos for client {client_id} between {period_start} and {period_end}"
        )

    invoice_id = _insert_invoice(con, client_id, "monthly", issue, due_days,
                                 period_start, period_end)
    for v in videos:
        # The line is the snapshot of the work; the video's invoice_id is the
        # soft link that keeps it out of the unbilled pool until deletion.
        add_line(con, invoice_id, f"{v['title']} — {human_day(v['shot_on'])}",
                 1, int(v["rate_cents"]), video_id=int(v["id"]))
        con.execute("UPDATE video SET invoice_id=? WHERE id=?", (invoice_id, v["id"]))
    return invoice_id


def create_oneoff_invoice(con: sqlite3.Connection, client_id: int, *,
                          issue_date: str | None = None, due_days: int = 30) -> int:
    """An empty one-off invoice (website build, phone system).

    Touches no videos: the one-off stream and the monthly stream stay separate,
    which is the stated requirement.
    """
    issue = issue_date or today()
    return _insert_invoice(con, client_id, "oneoff", issue, due_days, None, None)


def delete_invoice(con: sqlite3.Connection, invoice_id: int) -> None:
    """Delete an invoice and release its videos back to the unbilled pool.

    Lines go via ON DELETE CASCADE. The number is not reused (see
    next_invoice_number).
    """
    con.execute("UPDATE video SET invoice_id=NULL WHERE invoice_id=?", (invoice_id,))
    con.execute("DELETE FROM invoice WHERE id=?", (invoice_id,))
