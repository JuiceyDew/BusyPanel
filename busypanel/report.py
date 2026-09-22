"""Reporting: a year at a glance.

One row per month with the numbers a one-person business needs: what was billed,
what was actually paid, what is still outstanding, what was spent, how much of it
is deductible, and the net. "Writeoff" is just the deductible subset of expenses.
"""

from __future__ import annotations

import sqlite3


def monthly_summary(con: sqlite3.Connection, year: int) -> list[dict]:
    """Twelve rows (January..December) of the given year.

    Definitions, applied literally:
      invoiced     -- line totals on invoices issued that month, excluding drafts
      paid         -- the same, restricted to status 'paid'
      outstanding  -- invoiced - paid
      expenses     -- all expenses spent that month
      deductible   -- the subset flagged deductible (the "writeoffs" number)
      net          -- invoiced - expenses
    """
    y = str(int(year))

    def month_map(sql: str, *params) -> dict[str, int]:
        return {r["m"]: int(r["t"]) for r in con.execute(sql, params)}

    invoiced = month_map(
        "SELECT substr(i.issue_date, 6, 2) AS m, COALESCE(SUM(l.qty * l.unit_cents), 0) AS t "
        "FROM invoice i JOIN invoice_line l ON l.invoice_id = i.id "
        "WHERE substr(i.issue_date, 1, 4) = ? AND i.status <> 'draft' GROUP BY m", y)
    paid = month_map(
        "SELECT substr(i.issue_date, 6, 2) AS m, COALESCE(SUM(l.qty * l.unit_cents), 0) AS t "
        "FROM invoice i JOIN invoice_line l ON l.invoice_id = i.id "
        "WHERE substr(i.issue_date, 1, 4) = ? AND i.status = 'paid' GROUP BY m", y)
    expenses = month_map(
        "SELECT substr(spent_on, 6, 2) AS m, COALESCE(SUM(amount_cents), 0) AS t "
        "FROM expense WHERE substr(spent_on, 1, 4) = ? GROUP BY m", y)
    deductible = month_map(
        "SELECT substr(spent_on, 6, 2) AS m, COALESCE(SUM(amount_cents), 0) AS t "
        "FROM expense WHERE substr(spent_on, 1, 4) = ? AND deductible = 1 GROUP BY m", y)

    rows = []
    for i in range(1, 13):
        m = f"{i:02d}"
        inv = invoiced.get(m, 0)
        pd = paid.get(m, 0)
        exp = expenses.get(m, 0)
        rows.append({
            "month": i,
            "invoiced_cents": inv,
            "paid_cents": pd,
            "outstanding_cents": inv - pd,
            "expenses_cents": exp,
            "deductible_cents": deductible.get(m, 0),
            "net_cents": inv - exp,
        })
    return rows


def year_totals(rows: list[dict]) -> dict:
    """Sum a monthly_summary result for the header row."""
    keys = ("invoiced_cents", "paid_cents", "outstanding_cents",
            "expenses_cents", "deductible_cents", "net_cents")
    out = {k: sum(int(r[k]) for r in rows) for k in keys}
    out["month"] = 0
    return out
