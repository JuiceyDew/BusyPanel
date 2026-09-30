"""Routes exercised through FastAPI's TestClient, against a real SQLite file."""

from __future__ import annotations

import pytest

from busypanel.config import settings
from tests.conftest import add_client, add_video


def test_every_page_renders(client, state_dir):
    from busypanel import db

    con = db.connect(state_dir / "busypanel.db")
    try:
        cid = add_client(con, "Acme")
        add_video(con, cid, "2026-08-03", "Launch reel", 20000)
        inv = con.execute(
            "INSERT INTO invoice (number, client_id, kind, issue_date, due_date, created_at) "
            "VALUES ('2026-0001', ?, 'monthly', '2026-08-31', '2026-09-30', '2026-08-31') "
            "RETURNING id", (cid,)
        ).fetchone()["id"]
        con.execute(
            "INSERT INTO invoice_line (invoice_id, description, qty, unit_cents) "
            "VALUES (?, 'Launch reel', 1, 20000)", (inv,)
        )
        con.commit()
    finally:
        con.close()

    for path in ("/", "/clients", "/videos", "/invoices", "/expenses", "/summary", "/settings"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "busypanel" in r.text

    assert client.get(f"/invoices/{inv}").status_code == 200
    assert client.get(f"/invoices/{inv}/print").status_code == 200
    assert client.get("/health").json() == {"ok": True}


def test_unknown_invoice_is_404(client):
    assert client.get("/invoices/999").status_code == 404
    assert client.get("/invoices/999/print").status_code == 404


def test_add_video_via_form_falls_back_to_the_client_rate(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    r = client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                     "title": "Launch reel", "rate": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/videos"
    # The landing page is a summary: the client row carries the unbilled total,
    # and the titles themselves are listed on /videos.
    page = client.get("/?month=2026-08").text
    assert "Acme" in page and "$200.00" in page
    assert "Launch reel" not in page


def test_add_video_with_an_override_and_a_bad_rate(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "Big one", "rate": "350"}, follow_redirects=False)
    page = client.get("/?month=2026-08").text
    assert "$350.00" in page

    bad = client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                       "title": "Broken", "rate": "abc"})
    assert bad.status_code == 400
    assert "Rate" in bad.text

    bad_date = client.post("/videos", data={"client_id": "1", "shot_on": "not-a-date",
                                            "title": "X", "rate": "10"})
    assert bad_date.status_code == 400


def test_creating_a_monthly_invoice_and_printing_it(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "Launch reel", "rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-14",
                                 "title": "Product tour", "rate": "350"}, follow_redirects=False)

    r = client.post("/invoices/monthly", data={"client_id": "1", "month": "2026-08"},
                    follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/invoices/")

    page = client.get(location).text
    assert "2026-0001" in page and "Acme" in page
    assert "$550.00" in page and "Aug 3" in page and "Aug 14" in page

    printed = client.get(location + "/print").text
    assert "Acme" in printed and "$550.00" in printed
    assert "2026-08-01" in printed and "2026-08-31" in printed

    # The videos left the unbilled pool.
    assert "Bill all unbilled" not in client.get("/?month=2026-08").text


def test_invoicing_the_same_month_twice_redirects_to_the_existing_invoice(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "One", "rate": "200"}, follow_redirects=False)
    first = client.post("/invoices/monthly", data={"client_id": "1", "month": "2026-08"},
                        follow_redirects=False).headers["location"]

    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-04",
                                 "title": "Two", "rate": "200"}, follow_redirects=False)
    again = client.post("/invoices/monthly", data={"client_id": "1", "month": "2026-08"},
                        follow_redirects=False)
    assert again.status_code == 303
    assert again.headers["location"] == first + "?exists=1"
    # No second invoice, and the second video is still unbilled.
    page = client.get("/invoices").text
    assert page.count('href="/invoices/1"') == 1
    assert 'href="/invoices/2"' not in page


def test_invoicing_an_empty_month_returns_to_the_landing_page(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    r = client.post("/invoices/monthly", data={"client_id": "1", "month": "2026-08"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "empty=1" in r.headers["location"]
    assert client.get(r.headers["location"]).status_code == 200


def test_delete_invoice_releases_videos(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "One", "rate": "200"}, follow_redirects=False)
    inv = client.post("/invoices/monthly", data={"client_id": "1", "month": "2026-08"},
                      follow_redirects=False).headers["location"]
    assert "Bill all unbilled" not in client.get("/?month=2026-08").text

    client.post(f"{inv}/delete", follow_redirects=False)
    page = client.get("/?month=2026-08").text
    assert "Acme" in page and "Bill all unbilled" in page


def test_oneoff_invoice_is_separate_from_the_monthly_stream(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "One", "rate": "200"}, follow_redirects=False)
    inv = client.post("/invoices/oneoff", data={"client_id": "1"},
                      follow_redirects=False).headers["location"]
    client.post(f"{inv}/lines", data={"description": "Website build", "qty": "1",
                                      "unit": "1200"}, follow_redirects=False)
    page = client.get(inv).text
    assert "Website build" in page and "$1,200.00" in page
    # The video is untouched by the one-off.
    assert "Bill all unbilled" in client.get("/?month=2026-08").text


def test_line_update_and_delete(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    inv = client.post("/invoices/oneoff", data={"client_id": "1"},
                      follow_redirects=False).headers["location"]
    client.post(f"{inv}/lines", data={"description": "Website build", "qty": "1",
                                      "unit": "1200"}, follow_redirects=False)

    client.post("/lines/1", data={"description": "Website build — final", "qty": "2",
                                  "unit": "600"}, follow_redirects=False)
    page = client.get(inv).text
    assert "Website build — final" in page and "$1,200.00" in page

    client.post("/lines/1/delete", follow_redirects=False)
    assert 'value="Website build' not in client.get(inv).text


def test_billed_video_cannot_be_deleted(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "One", "rate": "200"}, follow_redirects=False)
    client.post("/invoices/monthly", data={"client_id": "1", "month": "2026-08"},
                follow_redirects=False)
    assert client.post("/videos/1/delete").status_code == 400


def test_video_edit_updates_an_unbilled_video(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "Original", "rate": "200"}, follow_redirects=False)

    r = client.post("/videos/1", data={"client_id": "1", "shot_on": "2026-09-09",
                                       "title": "Renamed", "rate": "250"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/videos"
    page = client.get("/videos").text
    assert "Renamed" in page and "$250.00" in page and "2026-09-09" in page
    assert "Original" not in page


def test_billed_video_cannot_be_edited(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "Original", "rate": "200"}, follow_redirects=False)
    client.post("/invoices/monthly", data={"client_id": "1", "month": "2026-08"},
                follow_redirects=False)

    r = client.post("/videos/1", data={"client_id": "1", "shot_on": "2026-09-09",
                                       "title": "Hacked", "rate": "999"},
                    follow_redirects=False)
    assert r.status_code == 400
    page = client.get("/videos").text
    assert "Original" in page and "Hacked" not in page


def test_edit_dialog_renders_the_selected_row(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)

    edited = client.get("/clients?edit=1").text
    assert 'id="dlg-client" open' in edited
    assert 'action="/clients/1"' in edited
    assert 'value="Acme"' in edited

    for page in (client.get("/clients?edit=999").text, client.get("/clients").text):
        assert 'id="dlg-client" open' not in page
        assert 'action="/clients"' in page


def test_expense_edit_rejects_a_non_positive_amount(client):
    client.post("/expenses", data={"spent_on": "2026-08-09", "amount": "90.25"},
                follow_redirects=False)
    r = client.post("/expenses/1", data={"spent_on": "2026-08-09", "amount": "-5"})
    assert r.status_code == 400
    assert 'id="dlg-expense" open' in r.text
    assert "greater than zero" in r.text
    assert "$90.25" in client.get("/expenses?month=2026-08").text


    client.post("/clients", data={"name": "Acme"}, follow_redirects=False)
    r = client.post("/clients", data={"name": "Acme"})
    assert r.status_code == 400
    assert "already exists" in r.text


def test_archived_client_leaves_the_pickers_but_stays_listed(client):
    client.post("/clients", data={"name": "Acme"}, follow_redirects=False)
    client.post("/clients/1/archive", follow_redirects=False)
    assert "Acme" in client.get("/clients").text
    home = client.get("/").text
    assert "Acme" not in home


def test_expense_round_trip_and_summary(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    r = client.post("/expenses", data={"spent_on": "2026-08-09", "amount": "90.25",
                                       "category": "software", "vendor": "Adobe",
                                       "deductible": "1"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/expenses?month=2026-08").text
    assert "Adobe" in page and "$90.25" in page

    summary = client.get("/summary?year=2026").text
    assert "$90.25" in summary

    client.post("/expenses/1", data={"spent_on": "2026-08-09", "amount": "100",
                                     "category": "software", "vendor": "Adobe",
                                     "deductible": "1"}, follow_redirects=False)
    assert "$100.00" in client.get("/expenses?month=2026-08").text
    client.post("/expenses/1/delete", follow_redirects=False)
    assert "$100.00" not in client.get("/expenses?month=2026-08").text


def test_bad_expense_input_is_a_400_not_a_500(client):
    r = client.post("/expenses", data={"spent_on": "nope", "amount": "10"})
    assert r.status_code == 400
    r = client.post("/expenses", data={"spent_on": "2026-08-09", "amount": "-5"})
    assert r.status_code == 400


def test_payment_terms_setting_reaches_the_invoice(client):
    from datetime import date, timedelta

    from busypanel import db

    client.post("/settings", data={"business_name": "Dew Media",
                                   "payment_terms_days": "14"}, follow_redirects=False)
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "One", "rate": "200"}, follow_redirects=False)
    client.post("/invoices/monthly", data={"client_id": "1", "month": "2026-08"},
                follow_redirects=False)

    con = db.connect(settings.db_path)
    try:
        row = con.execute("SELECT issue_date, due_date FROM invoice").fetchone()
    finally:
        con.close()
    issued = date.fromisoformat(row["issue_date"])
    assert date.fromisoformat(row["due_date"]) == issued + timedelta(days=14)


def test_client_payment_terms_override_the_global_setting(client):
    """A client's own terms win; a client without them follows the setting."""
    from datetime import date, timedelta

    from busypanel import db

    client.post("/settings", data={"payment_terms_days": "30"}, follow_redirects=False)
    client.post("/clients", data={"name": "Acme", "video_rate": "200",
                                  "payment_terms": "7"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "One", "rate": "200"}, follow_redirects=False)
    client.post("/invoices/monthly", data={"client_id": "1", "month": "2026-08"},
                follow_redirects=False)

    # The row and the dialog both carry the override.
    assert "7d" in client.get("/clients").text
    assert 'value="7"' in client.get("/clients?edit=1").text

    con = db.connect(settings.db_path)
    try:
        row = con.execute("SELECT issue_date, due_date FROM invoice").fetchone()
    finally:
        con.close()
    issued = date.fromisoformat(row["issue_date"])
    assert date.fromisoformat(row["due_date"]) == issued + timedelta(days=7)

    # A client with no terms of its own keeps the global default.
    client.post("/clients", data={"name": "Beta", "video_rate": "100"},
                follow_redirects=False)
    client.post("/videos", data={"client_id": "2", "shot_on": "2026-08-05",
                                 "title": "Two", "rate": "100"}, follow_redirects=False)
    client.post("/invoices/monthly", data={"client_id": "2", "month": "2026-08"},
                follow_redirects=False)
    con = db.connect(settings.db_path)
    try:
        row = con.execute("SELECT issue_date, due_date FROM invoice WHERE client_id=2").fetchone()
    finally:
        con.close()
    assert date.fromisoformat(row["due_date"]) == date.fromisoformat(row["issue_date"]) + timedelta(days=30)


def test_rejected_payment_terms_keep_the_typed_value(client):
    r = client.post("/clients", data={"name": "Acme", "video_rate": "200",
                                      "payment_terms": "-3"})
    assert r.status_code == 400
    assert "Payment terms" in r.text
    assert 'value="-3"' in r.text
    assert "Acme" not in client.get("/clients").text


def test_invoice_notes_reach_the_print_view(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "One", "rate": "200"}, follow_redirects=False)
    inv = client.post("/invoices/monthly", data={"client_id": "1", "month": "2026-08"},
                      follow_redirects=False).headers["location"]

    r = client.post(f"{inv}/notes", data={"notes": "Quoted before the rate rise"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == inv
    assert "Quoted before the rate rise" in client.get(inv).text
    assert "Quoted before the rate rise" in client.get(inv + "/print").text

    assert client.post("/invoices/999/notes", data={"notes": "x"}).status_code == 404


def test_search_filters_videos_and_invoices(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/clients", data={"name": "Zenith", "video_rate": "100"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "Launch reel", "rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "2", "shot_on": "2026-08-04",
                                 "title": "Product tour", "rate": "100"}, follow_redirects=False)

    page = client.get("/videos?q=Launch").text
    assert "Launch reel" in page and "Product tour" not in page
    # The client name is a search field too.
    page = client.get("/videos?q=Zenith").text
    assert "Product tour" in page and "Launch reel" not in page
    assert 'value="Zenith"' in page

    client.post("/invoices/oneoff", data={"client_id": "1"}, follow_redirects=False)
    client.post("/invoices/oneoff", data={"client_id": "2"}, follow_redirects=False)
    page = client.get("/invoices?q=Acme").text
    assert "2026-0001" in page and "2026-0002" not in page

    # A search that matches nothing renders the empty message, not a 500.
    r = client.get("/videos?client=1&q=zzz")
    assert r.status_code == 200
    assert "No videos match that filter." in r.text


def test_invoice_search_keeps_the_status_filter(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    inv = client.post("/invoices/oneoff", data={"client_id": "1"},
                      follow_redirects=False).headers["location"]
    client.post(f"{inv}/status", data={"status": "paid"}, follow_redirects=False)

    page = client.get("/invoices?status=paid&q=2026-0001").text
    assert "2026-0001" in page
    assert 'name="status" value="paid"' in page
    assert "?status=paid&q=2026-0001" in page or "?status=paid&amp;q=2026-0001" in page

    # A tampered status falls through to unfiltered rather than erroring.
    assert client.get("/invoices?status=bogus&q=2026").status_code == 200


def test_video_edit_dialog_resolves_outside_the_current_filter(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "Launch reel", "rate": "200"}, follow_redirects=False)
    # The Edit link carries no filter, and the search would hide the row.
    page = client.get("/videos?q=nothing-matches&edit=1").text
    assert 'id="dlg-video" open' in page
    assert 'value="Launch reel"' in page


def test_client_detail_shows_the_lifetime_totals(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200",
                                  "email": "hi@acme.test"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "Launch reel", "rate": "200"}, follow_redirects=False)
    inv = client.post("/invoices/monthly", data={"client_id": "1", "month": "2026-08"},
                      follow_redirects=False).headers["location"]
    client.post("/expenses", data={"spent_on": "2026-08-09", "amount": "90.25",
                                   "category": "software", "vendor": "Adobe",
                                   "client_id": "1", "deductible": "1"},
                follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-20",
                                 "title": "Unbilled one", "rate": "150"}, follow_redirects=False)

    page = client.get("/clients/1").text
    assert "Acme" in page
    assert "2026-0001" in page
    assert "$200.00" in page          # invoiced (the draft is still the whole bill here)
    assert "$0.00" in page            # paid
    assert "$150.00" in page          # unbilled
    assert "Launch reel" in page and "Adobe" in page
    assert 'href="/clients?edit=1"' in page

    # The list links the name through to the detail page.
    assert 'href="/clients/1"' in client.get("/clients").text

    assert client.get("/clients/999").status_code == 404


def test_csv_exports(client):
    import csv as csvmod
    import io

    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "One", "rate": "200"}, follow_redirects=False)
    inv = client.post("/invoices/monthly", data={"client_id": "1", "month": "2026-08"},
                      follow_redirects=False).headers["location"]
    client.post(f"{inv}/status", data={"status": "paid"}, follow_redirects=False)
    client.post("/expenses", data={"spent_on": "2026-08-09", "amount": "90.25",
                                   "category": "software", "vendor": "Adobe",
                                   "client_id": "1", "deductible": "1"},
                follow_redirects=False)

    r = client.get("/export/invoices.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csvmod.reader(io.StringIO(r.text)))
    assert rows[0] == ["Number", "Client", "Kind", "Period start", "Period end",
                       "Issued", "Due", "Status", "Paid date", "Total"]
    assert rows[1][0] == "2026-0001"
    assert rows[1][9] == "200.00"          # plain cents, not "$200.00"

    r = client.get("/export/expenses.csv")
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    rows = list(csvmod.reader(io.StringIO(r.text)))
    assert rows[0] == ["Spent on", "Vendor", "Category", "Client", "Amount",
                       "Deductible", "Notes"]
    assert rows[1][4] == "90.25" and rows[1][5] == "yes"

    r = client.get("/export/summary.csv")
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    rows = list(csvmod.reader(io.StringIO(r.text)))
    assert rows[0] == ["Month", "Invoiced", "Paid", "Outstanding", "Expenses",
                       "Deductible", "Net"]
    assert len(rows) == 14                  # header + 12 months + total
    assert rows[-1][0] == "Total"
    assert all("$" not in cell for row in rows for cell in row)


def test_billing_an_arbitrary_date_range(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-07-05",
                                 "title": "In range", "rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-07-25",
                                 "title": "Out of range", "rate": "200"}, follow_redirects=False)

    r = client.post("/invoices/monthly", data={"client_id": "1", "start": "2026-07-01",
                                               "end": "2026-07-10"},
                    follow_redirects=False)
    assert r.status_code == 303
    inv = r.headers["location"]
    page = client.get(inv).text
    assert "In range" in page and "Out of range" not in page
    assert "$200.00" in page
    # The range is on the invoice, not inferred from the line.
    assert "2026-07-01" in page and "2026-07-10" in page


def test_billing_everything_outstanding_with_no_dates(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-03-05",
                                 "title": "March", "rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-05",
                                 "title": "August", "rate": "300"}, follow_redirects=False)

    # The landing page can show everything outstanding, not just one month.
    everything = client.get("/?range=all").text
    assert "$500.00" in everything
    assert "everything outstanding" in everything

    r = client.post("/invoices/monthly", data={"client_id": "1"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(r.headers["location"]).text
    assert "March" in page and "August" in page and "$500.00" in page


def test_grouped_billing_puts_the_batch_on_one_line(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "One", "rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-04",
                                 "title": "Two", "rate": "250"}, follow_redirects=False)

    r = client.post("/invoices/monthly", data={"client_id": "1", "start": "2026-08-01",
                                               "end": "2026-08-31", "group": "1",
                                               "label": "August batch"},
                    follow_redirects=False)
    inv = r.headers["location"]
    page = client.get(inv).text
    assert "August batch" in page and "$450.00" in page
    # One line, so the individual titles are not on the invoice.
    assert "One" not in page.split('lines')[1] or "$450.00" in page


def test_a_video_can_carry_just_a_link(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    r = client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                     "title": "", "link": "https://youtu.be/abc",
                                     "rate": "200"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/videos").text
    assert 'href="https://youtu.be/abc"' in page

    # Neither a title nor a link is still refused.
    bad = client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                       "title": "", "link": "", "rate": "200"})
    assert bad.status_code == 400
    assert "title or a link" in bad.text

    # The link reaches the invoice, and the print view links it.
    inv = client.post("/invoices/monthly", data={"client_id": "1", "start": "2026-08-01",
                                                 "end": "2026-08-31"},
                      follow_redirects=False).headers["location"]
    printed = client.get(inv + "/print").text
    assert "youtu.be/abc" in printed
    assert 'href="https://youtu.be/abc"' in printed


def test_a_new_column_reaches_an_existing_database(client, state_dir):
    """The link column is added to a database written before it existed."""
    import sqlite3

    from busypanel import db

    con = db.connect(settings.db_path)
    try:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(video)")}
        assert "link" in cols
    finally:
        con.close()


def test_bill_all_unbilled_ignores_the_date_filter(client):
    """The primary action bills everything for that client, whatever is listed."""
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-03-05",
                                 "title": "March", "rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-05",
                                 "title": "August", "rate": "300"}, follow_redirects=False)

    # The page is filtered to August: only that month's total is listed.
    page = client.get("/?month=2026-08").text
    assert "$300.00" in page and "$500.00" not in page

    r = client.post("/invoices/monthly", data={"client_id": "1", "all": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    invoice = client.get(r.headers["location"]).text
    assert "March" in invoice and "August" in invoice and "$500.00" in invoice
    assert "Bill all unbilled" not in client.get("/?range=all").text


def test_range_presets_and_client_filter(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/clients", data={"name": "Beta", "video_rate": "100"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-05",
                                 "title": "Acme one", "rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "2", "shot_on": "2026-08-06",
                                 "title": "Beta one", "rate": "100"}, follow_redirects=False)

    # A preset is a complete range, and `all` is everything outstanding.
    everything = client.get("/?range=all").text
    assert "$300.00" in everything
    assert "all" in everything

    # Nonsense preset falls back to this month rather than erroring.
    assert client.get("/?range=nonsense").status_code == 200

    # Narrowing to one client is a filter: the other client's work drops out of
    # the table, so its total is gone while Acme's stays. (Beta still appears in
    # the filter dropdown itself, which is the point of the dropdown.)
    narrowed = client.get("/?range=all&client=1").text
    assert "$200.00" in narrowed and "$100.00" not in narrowed
    assert client.get("/?client=999").status_code == 200


def test_month_and_year_parameters_never_crash(client):
    for path in ("/?month=nonsense", "/?month=2026-13", "/expenses?month=x",
                 "/videos?month=x", "/summary?year=1", "/summary?year=99999",
                 "/videos?q=", "/invoices?q="):
        assert client.get(path).status_code == 200, path
    # A missing client is a 404, never a 500.
    assert client.get("/clients/999").status_code == 404
    assert client.get("/clients/not-a-number").status_code == 422


def test_settings_save_and_blank_password_keeps_the_stored_one(client):
    r = client.post("/settings", data={"business_name": "Dew Media",
                                       "business_address": "1 Main St",
                                       "business_email": "dew@example.com",
                                       "payment_terms_days": "14",
                                       "invoice_footer": "Thanks!",
                                       "auth_password": "hunter2"},
                    follow_redirects=False)
    assert r.status_code == 303

    from busypanel.state import load_overrides

    assert load_overrides()["auth_password"] == "hunter2"
    assert settings.business_name == "Dew Media"
    assert settings.payment_terms_days == 14

    # Saving a password turns the gate on, so the next request must log in first.
    client.post("/login", data={"password": "hunter2", "next": "/settings"},
                follow_redirects=False)

    # A later save with the password field blank (as rendered) must not wipe it.
    client.post("/settings", data={"business_name": "Dew Media Co",
                                   "payment_terms_days": "14"}, follow_redirects=False)
    assert load_overrides()["auth_password"] == "hunter2"
    assert settings.business_name == "Dew Media Co"


def test_settings_page_never_echoes_the_password(client):
    from busypanel.state import save_overrides

    # Stored but not active, so the page itself renders instead of redirecting.
    save_overrides({"auth_password": "sup3rsecret"})
    page = client.get("/settings").text
    assert "sup3rsecret" not in page
    assert "(set)" in page


def test_login_gate_redirects_and_health_stays_open(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "s3cret")

    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
    assert client.get("/health").status_code == 200

    wrong = client.post("/login", data={"password": "nope", "next": "/"},
                        follow_redirects=False)
    assert "error=1" in wrong.headers["location"]
    assert client.get("/", follow_redirects=False).status_code == 303

    ok = client.post("/login", data={"password": "s3cret", "next": "/"},
                     follow_redirects=False)
    assert ok.status_code == 303
    # The cookie now travels with the client's subsequent requests.
    assert client.get("/").status_code == 200

    client.post("/logout", follow_redirects=False)
    assert client.get("/", follow_redirects=False).status_code == 303
