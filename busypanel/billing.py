"""Billing: turning unbilled videos into a monthly invoice, and one-off invoices.

Every function takes an open connection and does **not** commit -- callers commit,
so a route can wrap several calls in one transaction and a failure halfway through
a monthly invoice leaves nothing behind.

The invariants encoded here are the ones a billing tool gets wrong:

* A client+period can only produce one invoice (``AlreadyInvoiced``); an
  unbounded bill has no period to dedupe on.
* Nothing to bill is not an error: ``create_invoice`` returns 0 and the caller
  decides where to send the operator.
* An invoice only ever pulls that client's *unbilled* videos inside the range,
  and materialises them as lines so the sent invoice is a snapshot. The range
  is whatever the operator chose -- a month, a fortnight, or everything.
* Deleting an invoice releases its videos back to the unbilled pool.
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime, timedelta

from busypanel.db import next_invoice_number

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

STATUSES = ("draft", "sent", "paid")


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


def payment_terms_days(con: sqlite3.Connection, client_id: int, fallback: int) -> int:
    """The client's own terms, or the global setting when it has none.

    Stored as NULL rather than copied from settings at insert time, so changing
    the global default still moves every client that never overrode it.
    """
    row = con.execute(
        "SELECT payment_terms_days FROM client WHERE id=?", (client_id,)
    ).fetchone()
    if row and row["payment_terms_days"] is not None:
        return int(row["payment_terms_days"])
    return fallback


def human_range(period_start: str | None, period_end: str | None) -> str:
    """A period as a person would write it: 'Aug 2026', 'Jul 1 – Aug 15 2026'.

    Falls back to a bare day when one end is missing, and to 'everything
    outstanding' when there is no range at all -- an unbounded bill has no
    period to name.
    """
    if not period_start and not period_end:
        return "everything outstanding"
    if period_start and period_end:
        a = datetime.strptime(period_start, "%Y-%m-%d")
        b = datetime.strptime(period_end, "%Y-%m-%d")
        if (a.year, a.month) == (b.year, b.month):
            if a.day == 1 and b.day == calendar.monthrange(b.year, b.month)[1]:
                return f"{MONTHS[a.month - 1]} {a.year}"
            return f"{MONTHS[a.month - 1]} {a.day} – {b.day} {b.year}"
        return (f"{MONTHS[a.month - 1]} {a.day} {a.year} – "
                f"{MONTHS[b.month - 1]} {b.day} {b.year}")
    one = period_start or period_end
    d = datetime.strptime(one, "%Y-%m-%d")
    if period_start:
        return f"from {MONTHS[d.month - 1]} {d.day} {d.year}"
    return f"up to {MONTHS[d.month - 1]} {d.day} {d.year}"


def line_description(title: str, shot_on: str, link: str, label: str = "") -> str:
    """The invoice line for one video.

    A video is identified by its title, or by its link when it has no title --
    the point of `link` is to be able to bill work that was never given a name.
    A video that has both names the link in parentheses, so a client can click
    through from the printed page. `label` prefixes the whole line, which is how
    a group of videos is billed under one heading without a line per video.
    """
    name = title.strip() or link.strip() or "Video"
    if title.strip() and link.strip():
        name = f"{name} ({link.strip()})"
    if label.strip():
        name = f"{label.strip()}: {name}"
    if not shot_on:
        return name
    d = datetime.strptime(shot_on, "%Y-%m-%d")
    return f"{name} — {MONTHS[d.month - 1]} {d.day}"


def unbilled_videos(con: sqlite3.Connection, client_id: int,
                    period_start: str | None = None,
                    period_end: str | None = None) -> list[sqlite3.Row]:
    """Unbilled videos for a client, optionally bounded by a date range.

    Either bound may be omitted, which is what makes a flexible bill possible:
    no bounds at all means "everything unbilled", and one bound means "from
    here on" or "up to here".
    """
    sql = "SELECT * FROM video WHERE client_id=? AND invoice_id IS NULL"
    params: list[object] = [client_id]
    if period_start:
        sql += " AND shot_on >= ?"
        params.append(period_start)
    if period_end:
        sql += " AND shot_on <= ?"
        params.append(period_end)
    sql += " ORDER BY shot_on, id"
    return list(con.execute(sql, params))


def unbilled_summary(con: sqlite3.Connection, period_start: str | None = None,
                     period_end: str | None = None) -> list[dict]:
    """One dict per active client that has unbilled videos in the range.

    Key names are read literally by the landing template: client_id, client_name,
    count, total_cents, rate_cents.
    """
    sql = (
        "SELECT c.id AS client_id, c.name AS client_name, COUNT(v.id) AS count, "
        "       COALESCE(SUM(v.rate_cents), 0) AS total_cents, "
        "       c.video_rate_cents AS rate_cents "
        "FROM client c JOIN video v ON v.client_id = c.id "
        "WHERE v.invoice_id IS NULL AND c.archived = 0"
    )
    params: list[object] = []
    if period_start:
        sql += " AND v.shot_on >= ?"
        params.append(period_start)
    if period_end:
        sql += " AND v.shot_on <= ?"
        params.append(period_end)
    sql += " GROUP BY c.id, c.name, c.video_rate_cents ORDER BY c.name"
    return [dict(r) for r in con.execute(sql, params)]


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


def create_invoice(con: sqlite3.Connection, client_id: int, *,
                   period_start: str | None = None, period_end: str | None = None,
                   group: bool = False, label: str = "",
                   issue_date: str | None = None, due_days: int = 30) -> int:
    """Bill one client's unbilled videos, optionally bounded to a date range.

    Returns the new invoice id, or 0 when there was nothing to bill.

    An invoice is created once per client and period: a second one for the same
    bounded range is refused (AlreadyInvoiced) rather than double-billing, but
    the range is whatever the operator chose, so "August", "1–15 July" and
    "this week" are all expressible. The `group` flag folds every video into a
    single line priced at their total, for clients who are billed per batch
    rather than per video.

    An unbounded call has no period to dedupe on and bills whatever is left.
    """
    issue = issue_date or today()
    existing = con.execute(
        "SELECT id FROM invoice WHERE client_id=? AND kind='monthly' "
        "AND period_start IS ? AND period_end IS ?",
        (client_id, period_start, period_end),
    ).fetchone()
    if existing:
        raise AlreadyInvoiced(int(existing["id"]))

    videos = unbilled_videos(con, client_id, period_start, period_end)
    if not videos:
        return 0

    invoice_id = _insert_invoice(con, client_id, "monthly", issue, due_days,
                                 period_start, period_end)
    if group:
        total = sum(int(v["rate_cents"]) for v in videos)
        if label.strip():
            description = label.strip()
        else:
            description = f"{len(videos)} videos — {human_range(period_start, period_end)}"
        add_line(con, invoice_id, description, 1, total)
    for v in videos:
        if not group:
            # The line is the snapshot of the work; the video's invoice_id is
            # the soft link that keeps it out of the unbilled pool until
            # deletion.
            add_line(con, invoice_id, line_description(v["title"], v["shot_on"], v["link"]),
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
