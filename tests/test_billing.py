"""Billing: the load-bearing behaviour. Monthly invoices pull exactly the right
videos, never twice, and deleting one releases the work back to the pool."""

from __future__ import annotations

import pytest

from busypanel import billing
from tests.conftest import add_client, add_video


def test_monthly_invoice_pulls_only_this_clients_unbilled_videos_in_period(con):
    acme = add_client(con, "Acme", 20000)
    beta = add_client(con, "Beta", 10000)
    a1 = add_video(con, acme, "2026-08-03", "Launch reel", 20000)
    a2 = add_video(con, acme, "2026-08-14", "Product tour", 35000)
    july = add_video(con, acme, "2026-07-31", "Old promo", 20000)
    other = add_video(con, beta, "2026-08-10", "Beta ad", 10000)

    inv = billing.create_monthly_invoice(con, acme, "2026-08-01", "2026-08-31")
    con.commit()

    lines = list(con.execute("SELECT * FROM invoice_line WHERE invoice_id=? ORDER BY position", (inv,)))
    assert len(lines) == 2
    assert [l["qty"] * l["unit_cents"] for l in lines] == [20000, 35000]
    assert billing.invoice_total(con, inv) == 55000

    billed = {r["id"]: r["invoice_id"] for r in con.execute("SELECT id, invoice_id FROM video")}
    assert billed[a1] == inv and billed[a2] == inv
    # A July video and another client's August video must stay unbilled.
    assert billed[july] is None and billed[other] is None


def test_monthly_invoice_lines_name_the_video_and_its_date(con):
    acme = add_client(con)
    add_video(con, acme, "2026-08-14", "Reel — shop opening", 20000)
    inv = billing.create_monthly_invoice(con, acme, "2026-08-01", "2026-08-31")
    con.commit()
    line = con.execute("SELECT * FROM invoice_line WHERE invoice_id=?", (inv,)).fetchone()
    assert line["description"] == "Reel — shop opening — Aug 14"
    assert line["video_id"] is not None


def test_second_invoice_for_same_client_and_period_is_refused(con):
    acme = add_client(con)
    add_video(con, acme, "2026-08-03", "One", 20000)
    first = billing.create_monthly_invoice(con, acme, "2026-08-01", "2026-08-31")
    con.commit()
    before = billing.invoice_total(con, first)

    add_video(con, acme, "2026-08-20", "Two", 20000)
    with pytest.raises(billing.AlreadyInvoiced) as e:
        billing.create_monthly_invoice(con, acme, "2026-08-01", "2026-08-31")
    assert e.value.invoice_id == first
    # The first invoice is untouched, and no extra row was created.
    assert billing.invoice_total(con, first) == before
    assert con.execute("SELECT COUNT(*) AS c FROM invoice").fetchone()["c"] == 1


def test_nothing_to_bill_creates_no_row(con):
    acme = add_client(con)
    with pytest.raises(billing.NothingToBill):
        billing.create_monthly_invoice(con, acme, "2026-08-01", "2026-08-31")
    assert con.execute("SELECT COUNT(*) AS c FROM invoice").fetchone()["c"] == 0


def test_delete_invoice_releases_videos_and_drops_lines(con):
    acme = add_client(con)
    add_video(con, acme, "2026-08-03", "One", 20000)
    add_video(con, acme, "2026-08-04", "Two", 20000)
    inv = billing.create_monthly_invoice(con, acme, "2026-08-01", "2026-08-31")
    con.commit()

    billing.delete_invoice(con, inv)
    con.commit()

    assert con.execute("SELECT COUNT(*) AS c FROM invoice_line").fetchone()["c"] == 0
    assert len(billing.unbilled_videos(con, acme, "2026-08-01", "2026-08-31")) == 2


def test_oneoff_invoice_touches_no_videos(con):
    acme = add_client(con)
    add_video(con, acme, "2026-08-03", "One", 20000)
    inv = billing.create_oneoff_invoice(con, acme)
    billing.add_line(con, inv, "Website build", 1, 120000)
    con.commit()

    row = con.execute("SELECT * FROM invoice WHERE id=?", (inv,)).fetchone()
    assert row["kind"] == "oneoff" and row["period_start"] is None
    assert billing.invoice_total(con, inv) == 120000
    # The monthly stream is independent: the video is still unbilled.
    assert len(billing.unbilled_videos(con, acme, "2026-08-01", "2026-08-31")) == 1


def test_invoice_numbers_are_not_reused_after_a_delete(con):
    acme = add_client(con)
    add_video(con, acme, "2026-08-03", "One", 20000)
    first = billing.create_monthly_invoice(con, acme, "2026-08-01", "2026-08-31",
                                           issue_date="2026-09-01")
    first_number = con.execute("SELECT number FROM invoice WHERE id=?", (first,)).fetchone()["number"]
    billing.delete_invoice(con, first)
    con.commit()

    add_video(con, acme, "2026-09-03", "Two", 20000)
    second = billing.create_monthly_invoice(con, acme, "2026-09-01", "2026-09-30",
                                            issue_date="2026-09-30")
    second_number = con.execute("SELECT number FROM invoice WHERE id=?", (second,)).fetchone()["number"]
    assert first_number == "2026-0001" and second_number == "2026-0002"


def test_unbilled_summary_groups_by_client_and_skips_archived(con):
    acme = add_client(con, "Acme", 20000)
    zed = add_client(con, "Zed", 10000)
    gone = add_client(con, "Gone", 5000)
    con.execute("UPDATE client SET archived=1 WHERE id=?", (gone,))
    add_video(con, acme, "2026-08-03", "One", 20000)
    add_video(con, acme, "2026-08-04", "Two", 25000)
    add_video(con, zed, "2026-08-05", "Three", 10000)
    add_video(con, gone, "2026-08-06", "Four", 5000)
    con.commit()

    rows = billing.unbilled_summary(con, "2026-08-01", "2026-08-31")
    assert [(r["client_name"], r["count"], r["total_cents"]) for r in rows] == [
        ("Acme", 2, 45000), ("Zed", 1, 10000),
    ]


def test_set_status_stamps_and_clears_paid_date(con):
    acme = add_client(con)
    add_video(con, acme, "2026-08-03", "One", 20000)
    inv = billing.create_monthly_invoice(con, acme, "2026-08-01", "2026-08-31")
    con.commit()

    billing.set_status(con, inv, "sent")
    assert con.execute("SELECT paid_date FROM invoice WHERE id=?", (inv,)).fetchone()["paid_date"] is None
    billing.set_status(con, inv, "paid")
    assert con.execute("SELECT paid_date FROM invoice WHERE id=?", (inv,)).fetchone()["paid_date"] == billing.today()
    billing.set_status(con, inv, "sent")
    assert con.execute("SELECT paid_date FROM invoice WHERE id=?", (inv,)).fetchone()["paid_date"] is None
    with pytest.raises(ValueError):
        billing.set_status(con, inv, "void")


def test_monthly_period_is_stored_not_inferred(con):
    """The billable window lives on the invoice, so editing lines cannot move it."""
    acme = add_client(con)
    add_video(con, acme, "2026-08-03", "One", 20000)
    inv = billing.create_monthly_invoice(con, acme, "2026-08-01", "2026-08-31")
    line_id = con.execute("SELECT id FROM invoice_line WHERE invoice_id=?", (inv,)).fetchone()["id"]
    billing.update_line(con, line_id, description="Anything", qty=3, unit_cents=1000)
    con.commit()
    row = con.execute("SELECT * FROM invoice WHERE id=?", (inv,)).fetchone()
    assert (row["period_start"], row["period_end"]) == ("2026-08-01", "2026-08-31")
    assert billing.invoice_total(con, inv) == 3000
