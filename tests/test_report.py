"""Reporting: the year table the operator reads off the Summary page."""

from __future__ import annotations

from busypanel import billing
from busypanel.report import monthly_summary, year_totals
from tests.conftest import add_client, add_video


def _august(con) -> int:
    acme = add_client(con, "Acme", 20000)
    add_video(con, acme, "2026-08-03", "One", 20000)
    add_video(con, acme, "2026-08-04", "Two", 25000)
    inv = billing.create_monthly_invoice(con, acme, "2026-08-01", "2026-08-31",
                                         issue_date="2026-08-31")
    con.commit()
    return inv


def test_drafts_are_excluded_until_sent(con):
    inv = _august(con)
    aug = monthly_summary(con, 2026)[7]
    assert aug["invoiced_cents"] == 0 and aug["paid_cents"] == 0

    billing.set_status(con, inv, "sent")
    con.commit()
    aug = monthly_summary(con, 2026)[7]
    assert aug["invoiced_cents"] == 45000
    assert aug["paid_cents"] == 0
    assert aug["outstanding_cents"] == 45000


def test_paid_and_outstanding_and_expenses(con):
    inv = _august(con)
    billing.set_status(con, inv, "paid")
    con.execute(
        "INSERT INTO expense (spent_on, amount_cents, category, deductible, created_at) "
        "VALUES ('2026-08-09', 9025, 'software', 1, '2026-08-09')"
    )
    con.execute(
        "INSERT INTO expense (spent_on, amount_cents, category, deductible, created_at) "
        "VALUES ('2026-08-10', 4000, 'travel', 0, '2026-08-10')"
    )
    con.commit()

    aug = monthly_summary(con, 2026)[7]
    assert aug["invoiced_cents"] == 45000
    assert aug["paid_cents"] == 45000
    assert aug["outstanding_cents"] == 0
    assert aug["expenses_cents"] == 13025
    # The writeoff number is the deductible subset only.
    assert aug["deductible_cents"] == 9025
    assert aug["net_cents"] == 45000 - 13025


def test_other_months_and_years_are_zero(con):
    _august(con)
    rows = monthly_summary(con, 2026)
    assert len(rows) == 12
    assert [r["invoiced_cents"] for r in rows[:7]] == [0] * 7
    assert monthly_summary(con, 2025) == [
        {"month": i, "invoiced_cents": 0, "paid_cents": 0, "outstanding_cents": 0,
         "expenses_cents": 0, "deductible_cents": 0, "net_cents": 0}
        for i in range(1, 13)
    ]


def test_year_totals_sum_the_rows(con):
    inv = _august(con)
    billing.set_status(con, inv, "paid")
    con.execute(
        "INSERT INTO expense (spent_on, amount_cents, deductible, created_at) "
        "VALUES ('2026-08-09', 9025, 1, '2026-08-09')"
    )
    con.commit()
    rows = monthly_summary(con, 2026)
    totals = year_totals(rows)
    assert totals["invoiced_cents"] == 45000
    assert totals["paid_cents"] == 45000
    assert totals["expenses_cents"] == 9025
    assert totals["deductible_cents"] == 9025
    assert totals["net_cents"] == 35975
