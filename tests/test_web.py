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
    page = client.get("/?month=2026-08").text
    assert "Acme" in page and "200" in page
    assert "Launch reel" not in page  # the landing page summarises, /videos lists


def test_add_video_with_an_override_and_a_bad_rate(client):
    client.post("/clients", data={"name": "Acme", "video_rate": "200"}, follow_redirects=False)
    client.post("/videos", data={"client_id": "1", "shot_on": "2026-08-03",
                                 "title": "Big one", "rate": "350"}, follow_redirects=False)
    page = client.get("/?month=2026-08").text
    assert "350" in page

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
    assert "Create invoice" not in client.get("/?month=2026-08").text


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
    assert client.get("/invoices").text.count("2026-0001") == 1


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
    assert "Create invoice" not in client.get("/?month=2026-08").text

    client.post(f"{inv}/delete", follow_redirects=False)
    page = client.get("/?month=2026-08").text
    assert "Acme" in page and "Create invoice" in page


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
    assert "Create invoice" in client.get("/?month=2026-08").text


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


def test_duplicate_client_name_renders_an_error_not_a_500(client):
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


def test_month_and_year_parameters_never_crash(client):
    for path in ("/?month=nonsense", "/?month=2026-13", "/expenses?month=x",
                 "/videos?month=x", "/summary?year=1", "/summary?year=99999"):
        assert client.get(path).status_code == 200, path


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
