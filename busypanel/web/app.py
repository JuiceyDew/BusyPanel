"""Minimal server-rendered web UI.

Server-rendered Jinja templates and one small stylesheet. No build step, no CDN,
no client-side state: every value is escaped by Jinja's autoescape, and the only
JavaScript anywhere is the browser's own print dialog.

Single-user tool for a LAN: one password gates the whole UI when
`auth_password`/`AUTH_PASSWORD` is set (see busypanel/web/auth.py). With no
password the panel is open -- fine on a laptop, wrong on a shared network. Put TLS
in front before exposing it beyond a trusted subnet, since the password crosses
plain HTTP.
"""

from __future__ import annotations

import calendar
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from busypanel import billing, db, report
from busypanel.config import settings
from busypanel.money import fmt_cents, parse_cents
from busypanel.state import EDITABLE, SECRET, load_overrides, save_overrides
from busypanel.web import auth

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

app = FastAPI(title="busypanel")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

# Templates format money through the same helper the rest of the app uses.
templates.env.globals["fmt_cents"] = fmt_cents
# Evaluated at render time so the nav reflects the current config without every
# route having to pass a flag.
templates.env.globals["auth_enabled"] = auth.enabled

# Paths reachable without a session when the login gate is on: the login page
# itself, the health probe, and static assets. Everything else redirects.
_OPEN_PATHS = {"/login", "/health"}


@app.middleware("http")
async def _require_login(request: Request, call_next):
    """Gate the whole UI behind one password when auth_password is set.

    The panel is often headless and on a shared LAN, so an unauthenticated
    0.0.0.0 bind exposes every client, invoice and delete route.
    """
    if auth.enabled():
        path = request.url.path
        if path not in _OPEN_PATHS and not path.startswith("/static/"):
            if not auth.valid_token(request.cookies.get(auth.COOKIE)):
                return RedirectResponse(f"/login?next={path}", status_code=303)
    return await call_next(request)


def _conn() -> sqlite3.Connection:
    return db.connect()


# --- shared queries ------------------------------------------------------------


def _parse_month(value: str | None) -> str:
    """A validated `YYYY-MM`, falling back to the current month. Never raises."""
    raw = (value or "").strip()
    try:
        d = datetime.strptime(raw, "%Y-%m")
    except ValueError:
        return billing.today()[:7]
    return d.strftime("%Y-%m")


def _month_bounds(ym: str) -> tuple[str, str]:
    """`YYYY-MM` -> (first day, last day) as `YYYY-MM-DD` strings."""
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{ym}-01", f"{ym}-{calendar.monthrange(y, m)[1]:02d}"


def _parse_date(value: str) -> str | None:
    """Accept only a real `YYYY-MM-DD` date, so a bad form post cannot poison
    the string comparisons every query relies on."""
    raw = (value or "").strip()
    try:
        d = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None
    return d.date().isoformat()


def _clients(include_archived: bool = False) -> list[sqlite3.Row]:
    con = _conn()
    try:
        sql = "SELECT * FROM client"
        if not include_archived:
            sql += " WHERE archived = 0"
        return list(con.execute(sql + " ORDER BY name"))
    finally:
        con.close()


def _videos(client_id: int | None = None, ym: str | None = None) -> list[sqlite3.Row]:
    con = _conn()
    try:
        sql = (
            "SELECT v.*, c.name AS client_name, i.number AS invoice_number "
            "FROM video v JOIN client c ON c.id = v.client_id "
            "LEFT JOIN invoice i ON i.id = v.invoice_id WHERE 1=1"
        )
        params: list[object] = []
        if client_id:
            sql += " AND v.client_id = ?"
            params.append(client_id)
        if ym:
            sql += " AND substr(v.shot_on, 1, 7) = ?"
            params.append(ym)
        sql += " ORDER BY v.shot_on DESC, v.id DESC"
        return list(con.execute(sql, params))
    finally:
        con.close()


def _expenses(ym: str | None = None, category: str | None = None) -> list[sqlite3.Row]:
    con = _conn()
    try:
        sql = (
            "SELECT e.*, c.name AS client_name FROM expense e "
            "LEFT JOIN client c ON c.id = e.client_id WHERE 1=1"
        )
        params: list[object] = []
        if ym:
            sql += " AND substr(e.spent_on, 1, 7) = ?"
            params.append(ym)
        if category:
            sql += " AND e.category = ?"
            params.append(category)
        sql += " ORDER BY e.spent_on DESC, e.id DESC"
        return list(con.execute(sql, params))
    finally:
        con.close()


def _expense_categories() -> list[str]:
    con = _conn()
    try:
        return [r["category"] for r in con.execute(
            "SELECT DISTINCT category FROM expense WHERE category <> '' ORDER BY category")]
    finally:
        con.close()


def _invoices(status: str | None = None) -> list[sqlite3.Row]:
    con = _conn()
    try:
        sql = (
            "SELECT i.*, c.name AS client_name, "
            "       COALESCE((SELECT SUM(qty * unit_cents) FROM invoice_line l "
            "                 WHERE l.invoice_id = i.id), 0) AS total_cents "
            "FROM invoice i JOIN client c ON c.id = i.client_id WHERE 1=1"
        )
        params: list[object] = []
        if status in billing.STATUSES:
            sql += " AND i.status = ?"
            params.append(status)
        sql += " ORDER BY i.issue_date DESC, i.id DESC"
        return list(con.execute(sql, params))
    finally:
        con.close()


def _invoice(invoice_id: int) -> sqlite3.Row | None:
    con = _conn()
    try:
        return con.execute(
            "SELECT i.*, c.name AS client_name, c.email AS client_email "
            "FROM invoice i JOIN client c ON c.id = i.client_id WHERE i.id = ?",
            (invoice_id,),
        ).fetchone()
    finally:
        con.close()


def _lines(invoice_id: int) -> list[sqlite3.Row]:
    con = _conn()
    try:
        return list(con.execute(
            "SELECT * FROM invoice_line WHERE invoice_id=? ORDER BY position, id",
            (invoice_id,),
        ))
    finally:
        con.close()


# --- landing: unbilled work -----------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def home(request: Request, month: str | None = None, empty: int = 0):
    ym = _parse_month(month)
    start, end = _month_bounds(ym)
    con = _conn()
    try:
        unbilled = billing.unbilled_summary(con, start, end)
    finally:
        con.close()
    return templates.TemplateResponse(request, "home.html", {
        "s": settings, "month": ym, "period_start": start, "period_end": end,
        "unbilled": unbilled,
        "total_cents": sum(r["total_cents"] for r in unbilled),
        "clients": _clients(),
        "today": billing.today(),
        "empty": bool(empty),
        "active": "home",
    })


# --- videos ---------------------------------------------------------------------


@app.post("/videos")
def video_add(
    request: Request,
    client_id: int = Form(...),
    shot_on: str = Form(""),
    title: str = Form(""),
    rate: str = Form(""),
):
    error = ""
    day = _parse_date(shot_on)
    title = title.strip()
    con = _conn()
    try:
        client = con.execute("SELECT * FROM client WHERE id=?", (client_id,)).fetchone()
        if not client:
            error = "Pick a client."
        elif not day:
            error = "Date shot must be a date like 2026-08-14."
        elif not title:
            error = "A title is required."
        else:
            if rate.strip():
                try:
                    cents = parse_cents(rate)
                except ValueError as e:
                    error = f"Rate: {e}."
                    cents = 0
            else:
                cents = int(client["video_rate_cents"])
            if not error and cents <= 0:
                error = "The rate must be greater than zero."
            if not error:
                con.execute(
                    "INSERT INTO video (client_id, shot_on, title, rate_cents, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (client_id, day, title, cents, billing.today()),
                )
                con.commit()
    finally:
        con.close()
    if error:
        ym = _parse_month(None)
        start, end = _month_bounds(ym)
        con = _conn()
        try:
            unbilled = billing.unbilled_summary(con, start, end)
        finally:
            con.close()
        return templates.TemplateResponse(request, "home.html", {
            "s": settings, "month": ym, "period_start": start, "period_end": end,
            "unbilled": unbilled,
            "total_cents": sum(r["total_cents"] for r in unbilled),
            "clients": _clients(), "today": billing.today(),
            "error": error, "form": {"client_id": client_id, "shot_on": shot_on,
                                     "title": title, "rate": rate},
            "active": "home",
        }, status_code=400)
    return RedirectResponse("/", status_code=303)


@app.post("/videos/{video_id}/delete")
def video_delete(video_id: int):
    con = _conn()
    try:
        row = con.execute("SELECT * FROM video WHERE id=?", (video_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such video")
        # Billed work is the invoice's record now; deleting the invoice is what
        # releases it. Removing it here would silently change a sent invoice.
        if row["invoice_id"] is not None:
            raise HTTPException(400, "that video is already billed; delete the invoice instead")
        con.execute("DELETE FROM video WHERE id=?", (video_id,))
        con.commit()
    finally:
        con.close()
    return RedirectResponse("/", status_code=303)


@app.get("/videos", response_class=HTMLResponse)
def videos_page(request: Request, client: int | None = None, month: str | None = None):
    ym = None
    if month:
        ym = _parse_month(month)
    return templates.TemplateResponse(request, "videos.html", {
        "s": settings, "videos": _videos(client, ym), "clients": _clients(True),
        "client": client, "month": month or "", "active": "videos",
    })


# --- clients --------------------------------------------------------------------


@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request):
    return templates.TemplateResponse(request, "clients.html", {
        "s": settings, "clients": _clients(True), "active": "clients",
    })


@app.post("/clients")
def client_add(
    request: Request,
    name: str = Form(""),
    video_rate: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
):
    name = name.strip()
    error = ""
    if not name:
        error = "A name is required."
    try:
        rate = parse_cents(video_rate) if video_rate.strip() else 0
    except ValueError as e:
        error, rate = f"Rate: {e}.", 0
    if rate < 0:
        error = "The rate cannot be negative."
    if not error:
        con = _conn()
        try:
            try:
                con.execute(
                    "INSERT INTO client (name, video_rate_cents, email, notes, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (name, rate, email.strip(), notes.strip(), billing.today()),
                )
                con.commit()
            except sqlite3.IntegrityError:
                error = "A client with that name already exists."
        finally:
            con.close()
    if error:
        return templates.TemplateResponse(request, "clients.html", {
            "s": settings, "clients": _clients(True), "error": error,
            "form": {"name": name, "video_rate": video_rate, "email": email, "notes": notes},
            "active": "clients",
        }, status_code=400)
    return RedirectResponse("/clients", status_code=303)


@app.post("/clients/{client_id}")
def client_update(
    request: Request,
    client_id: int,
    name: str = Form(""),
    video_rate: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
):
    name = name.strip()
    error = ""
    if not name:
        error = "A name is required."
    try:
        rate = parse_cents(video_rate) if video_rate.strip() else 0
    except ValueError as e:
        error, rate = f"Rate: {e}.", 0
    if rate < 0:
        error = "The rate cannot be negative."
    if not error:
        con = _conn()
        try:
            if not con.execute("SELECT 1 FROM client WHERE id=?", (client_id,)).fetchone():
                raise HTTPException(404, "no such client")
            try:
                con.execute(
                    "UPDATE client SET name=?, video_rate_cents=?, email=?, notes=? WHERE id=?",
                    (name, rate, email.strip(), notes.strip(), client_id),
                )
                con.commit()
            except sqlite3.IntegrityError:
                error = "A client with that name already exists."
        finally:
            con.close()
    if error:
        return templates.TemplateResponse(request, "clients.html", {
            "s": settings, "clients": _clients(True), "error": error, "active": "clients",
        }, status_code=400)
    return RedirectResponse("/clients", status_code=303)


@app.post("/clients/{client_id}/archive")
def client_archive(client_id: int):
    con = _conn()
    try:
        if not con.execute("SELECT 1 FROM client WHERE id=?", (client_id,)).fetchone():
            raise HTTPException(404, "no such client")
        con.execute("UPDATE client SET archived = 1 - archived WHERE id=?", (client_id,))
        con.commit()
    finally:
        con.close()
    return RedirectResponse("/clients", status_code=303)


# --- invoices -------------------------------------------------------------------


@app.post("/invoices/monthly")
def invoice_monthly(client_id: int = Form(...), month: str = Form("")):
    ym = _parse_month(month)
    start, end = _month_bounds(ym)
    con = _conn()
    try:
        try:
            invoice_id = billing.create_monthly_invoice(con, client_id, start, end)
            con.commit()
        except billing.AlreadyInvoiced as e:
            con.rollback()
            return RedirectResponse(f"/invoices/{e.invoice_id}?exists=1", status_code=303)
        except billing.NothingToBill:
            con.rollback()
            return RedirectResponse(f"/?month={ym}&empty=1", status_code=303)
    finally:
        con.close()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@app.post("/invoices/oneoff")
def invoice_oneoff(client_id: int = Form(...)):
    con = _conn()
    try:
        invoice_id = billing.create_oneoff_invoice(con, client_id)
        con.commit()
    finally:
        con.close()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@app.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request, status: str = "all"):
    rows = _invoices(None if status == "all" else status)
    return templates.TemplateResponse(request, "invoices.html", {
        "s": settings, "invoices": rows, "status": status, "clients": _clients(),
        "totals": {s: sum(int(r["total_cents"]) for r in rows if r["status"] == s)
                   for s in billing.STATUSES},
        "active": "invoices",
    })


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_page(request: Request, invoice_id: int, exists: int = 0):
    inv = _invoice(invoice_id)
    if not inv:
        raise HTTPException(404, "no such invoice")
    lines = _lines(invoice_id)
    return templates.TemplateResponse(request, "invoice.html", {
        "s": settings, "inv": inv, "lines": lines,
        "total_cents": sum(int(l["qty"]) * int(l["unit_cents"]) for l in lines),
        "exists": bool(exists), "active": "invoices",
    })


@app.post("/invoices/{invoice_id}/lines")
def line_add(
    request: Request,
    invoice_id: int,
    description: str = Form(""),
    qty: str = Form("1"),
    unit: str = Form(""),
):
    inv = _invoice(invoice_id)
    if not inv:
        raise HTTPException(404, "no such invoice")
    description = description.strip()
    error = ""
    if not description:
        error = "A description is required."
    try:
        n = int(qty or "1")
        if n < 1:
            raise ValueError("quantity must be at least 1")
    except ValueError as e:
        error, n = str(e).capitalize() + ".", 1
    try:
        cents = parse_cents(unit)
    except ValueError as e:
        error, cents = f"Unit price: {e}.", 0
    if not error:
        con = _conn()
        try:
            billing.add_line(con, invoice_id, description, n, cents)
            con.commit()
        finally:
            con.close()
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    lines = _lines(invoice_id)
    return templates.TemplateResponse(request, "invoice.html", {
        "s": settings, "inv": inv, "lines": lines,
        "total_cents": sum(int(l["qty"]) * int(l["unit_cents"]) for l in lines),
        "error": error, "active": "invoices",
    }, status_code=400)


@app.post("/lines/{line_id}")
def line_update(
    request: Request,
    line_id: int,
    description: str = Form(""),
    qty: str = Form("1"),
    unit: str = Form(""),
):
    con = _conn()
    try:
        row = con.execute("SELECT * FROM invoice_line WHERE id=?", (line_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such line")
        invoice_id = int(row["invoice_id"])
        description = description.strip()
        error = ""
        if not description:
            error = "A description is required."
        try:
            n = int(qty or "1")
            if n < 1:
                raise ValueError("quantity must be at least 1")
        except ValueError as e:
            error, n = str(e).capitalize() + ".", 1
        try:
            cents = parse_cents(unit)
        except ValueError as e:
            error, cents = f"Unit price: {e}.", 0
        if not error:
            billing.update_line(con, line_id, description=description, qty=n, unit_cents=cents)
            con.commit()
    finally:
        con.close()
    if error:
        inv = _invoice(invoice_id)
        lines = _lines(invoice_id)
        return templates.TemplateResponse(request, "invoice.html", {
            "s": settings, "inv": inv, "lines": lines,
            "total_cents": sum(int(l["qty"]) * int(l["unit_cents"]) for l in lines),
            "error": error, "active": "invoices",
        }, status_code=400)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@app.post("/lines/{line_id}/delete")
def line_delete(line_id: int):
    con = _conn()
    try:
        row = con.execute("SELECT invoice_id FROM invoice_line WHERE id=?", (line_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such line")
        invoice_id = int(row["invoice_id"])
        billing.delete_line(con, line_id)
        con.commit()
    finally:
        con.close()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@app.post("/invoices/{invoice_id}/status")
def invoice_status(invoice_id: int, status: str = Form(...)):
    if status not in billing.STATUSES:
        raise HTTPException(400, "unknown status")
    con = _conn()
    try:
        if not con.execute("SELECT 1 FROM invoice WHERE id=?", (invoice_id,)).fetchone():
            raise HTTPException(404, "no such invoice")
        billing.set_status(con, invoice_id, status)
        con.commit()
    finally:
        con.close()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@app.post("/invoices/{invoice_id}/delete")
def invoice_delete(invoice_id: int):
    con = _conn()
    try:
        if not con.execute("SELECT 1 FROM invoice WHERE id=?", (invoice_id,)).fetchone():
            raise HTTPException(404, "no such invoice")
        billing.delete_invoice(con, invoice_id)
        con.commit()
    finally:
        con.close()
    return RedirectResponse("/invoices", status_code=303)


@app.get("/invoices/{invoice_id}/print", response_class=HTMLResponse)
def invoice_print(request: Request, invoice_id: int):
    inv = _invoice(invoice_id)
    if not inv:
        raise HTTPException(404, "no such invoice")
    lines = _lines(invoice_id)
    return templates.TemplateResponse(request, "print.html", {
        "s": settings, "inv": inv, "lines": lines,
        "total_cents": sum(int(l["qty"]) * int(l["unit_cents"]) for l in lines),
    })


# --- expenses -------------------------------------------------------------------


@app.get("/expenses", response_class=HTMLResponse)
def expenses_page(request: Request, month: str | None = None, category: str = ""):
    ym = _parse_month(month) if month else billing.today()[:7]
    rows = _expenses(ym, category or None)
    return templates.TemplateResponse(request, "expenses.html", {
        "s": settings, "expenses": rows, "month": ym, "category": category,
        "categories": _expense_categories(), "clients": _clients(True),
        "today": billing.today(),
        "expense_total": sum(int(r["amount_cents"]) for r in rows),
        "deductible_total": sum(int(r["amount_cents"]) for r in rows if r["deductible"]),
        "active": "expenses",
    })


@app.post("/expenses")
def expense_add(
    request: Request,
    spent_on: str = Form(""),
    amount: str = Form(""),
    category: str = Form(""),
    vendor: str = Form(""),
    client_id: str = Form(""),
    deductible: str = Form(""),
    notes: str = Form(""),
):
    error = ""
    day = _parse_date(spent_on)
    if not day:
        error = "Spent on must be a date like 2026-08-14."
    try:
        cents = parse_cents(amount)
    except ValueError as e:
        error, cents = f"Amount: {e}.", 0
    if not error and cents <= 0:
        error = "The amount must be greater than zero."
    cid = int(client_id) if client_id.strip().isdigit() else None
    if not error:
        con = _conn()
        try:
            con.execute(
                "INSERT INTO expense (spent_on, amount_cents, category, vendor, client_id, "
                "deductible, notes, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (day, cents, category.strip(), vendor.strip(), cid,
                 1 if deductible else 0, notes.strip(), billing.today()),
            )
            con.commit()
        finally:
            con.close()
        return RedirectResponse(f"/expenses?month={day[:7]}", status_code=303)
    rows = _expenses(billing.today()[:7])
    return templates.TemplateResponse(request, "expenses.html", {
        "s": settings, "expenses": rows, "month": billing.today()[:7], "category": "",
        "categories": _expense_categories(), "clients": _clients(True),
        "today": billing.today(), "error": error,
        "form": {"spent_on": spent_on, "amount": amount, "category": category,
                 "vendor": vendor, "client_id": cid, "deductible": bool(deductible),
                 "notes": notes},
        "expense_total": sum(int(r["amount_cents"]) for r in rows),
        "deductible_total": sum(int(r["amount_cents"]) for r in rows if r["deductible"]),
        "active": "expenses",
    }, status_code=400)


@app.post("/expenses/{expense_id}")
def expense_update(
    expense_id: int,
    spent_on: str = Form(""),
    amount: str = Form(""),
    category: str = Form(""),
    vendor: str = Form(""),
    client_id: str = Form(""),
    deductible: str = Form(""),
    notes: str = Form(""),
):
    day = _parse_date(spent_on)
    if not day:
        raise HTTPException(400, "bad date")
    try:
        cents = parse_cents(amount)
    except ValueError as e:
        raise HTTPException(400, f"bad amount: {e}") from e
    cid = int(client_id) if client_id.strip().isdigit() else None
    con = _conn()
    try:
        if not con.execute("SELECT 1 FROM expense WHERE id=?", (expense_id,)).fetchone():
            raise HTTPException(404, "no such expense")
        con.execute(
            "UPDATE expense SET spent_on=?, amount_cents=?, category=?, vendor=?, "
            "client_id=?, deductible=?, notes=? WHERE id=?",
            (day, cents, category.strip(), vendor.strip(), cid,
             1 if deductible else 0, notes.strip(), expense_id),
        )
        con.commit()
    finally:
        con.close()
    return RedirectResponse(f"/expenses?month={day[:7]}", status_code=303)


@app.post("/expenses/{expense_id}/delete")
def expense_delete(expense_id: int):
    con = _conn()
    try:
        if not con.execute("SELECT 1 FROM expense WHERE id=?", (expense_id,)).fetchone():
            raise HTTPException(404, "no such expense")
        con.execute("DELETE FROM expense WHERE id=?", (expense_id,))
        con.commit()
    finally:
        con.close()
    return RedirectResponse("/expenses", status_code=303)


# --- summary --------------------------------------------------------------------


@app.get("/summary", response_class=HTMLResponse)
def summary_page(request: Request, year: int | None = None):
    y = year or int(billing.today()[:4])
    con = _conn()
    try:
        rows = report.monthly_summary(con, y)
    finally:
        con.close()
    return templates.TemplateResponse(request, "summary.html", {
        "s": settings, "year": y, "rows": rows,
        "totals": report.year_totals(rows),
        "month_name": calendar.month_name,
        "active": "summary",
    })


# --- settings -------------------------------------------------------------------


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0):
    return templates.TemplateResponse(request, "settings.html", {
        "s": settings, "saved": bool(saved),
        "secret_flags": {k: bool(str(load_overrides().get(k) or "").strip()) for k in SECRET},
        "active": "settings",
    })


@app.post("/settings")
async def settings_save(request: Request):
    """Persist the editable settings. Unknown fields are dropped by save_overrides,
    and secret fields left blank are kept rather than wiped -- the form only ever
    shows a masked placeholder, so submitting it must not clear the password."""
    form = await request.form()
    updates: dict[str, object] = {}
    for key in EDITABLE:
        if key not in form:
            continue
        raw = str(form.get(key, "")).strip()
        if key in SECRET:
            # Blank means "leave as-is": the browser never received the value.
            if raw:
                updates[key] = raw
        elif key == "payment_terms_days":
            if raw.isdigit():
                updates[key] = int(raw)
        else:
            updates[key] = raw
    save_overrides(updates)
    from busypanel.state import apply_overrides

    apply_overrides(settings)
    return RedirectResponse("/settings?saved=1", status_code=303)


# --- auth -----------------------------------------------------------------------


def _safe_next(target: str) -> str:
    """Only allow same-site relative redirects, never an open redirect."""
    if target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: int = 0):
    if not auth.enabled():
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"next": _safe_next(next), "error": bool(error), "s": settings, "active": "login"},
    )


@app.post("/login")
def login(password: str = Form(""), next: str = Form("/")):
    target = _safe_next(next)
    if not auth.check_password(password):
        return RedirectResponse(f"/login?error=1&next={target}", status_code=303)
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(
        auth.COOKIE, auth.make_token(), max_age=auth.SESSION_TTL,
        httponly=True, samesite="lax",
        # secure=True would be right behind TLS; the default deployment is plain
        # HTTP on a LAN, so leave it off or the cookie is dropped entirely.
    )
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/health")
def health():
    return {"ok": True}


def serve(host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    settings.ensure_dirs()
    uvicorn.run(
        app,
        host=host or settings.web_host,
        port=port or settings.web_port,
        log_level="info",
    )
