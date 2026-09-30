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
import csv
import io
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from busypanel import billing, db, report
from busypanel.config import settings
from busypanel.money import fmt_cents, parse_cents, plain_cents
from busypanel.state import (
    EDITABLE,
    SECRET,
    apply_overrides,
    load_overrides,
    save_overrides,
)
from busypanel.web import auth

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

app = FastAPI(title="busypanel")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

# Templates format money through the same helper the rest of the app uses.
templates.env.globals["fmt_cents"] = fmt_cents


def _line_link(text: str) -> str:
    """The http(s) URL inside an invoice line, or ''.

    Lines are plain text, but a video with a link carries that link in
    parentheses, so the print view needs it back to make it clickable. Only
    http(s) is ever returned, so a line can never smuggle in a javascript: or
    data: URL.
    """
    for word in (text or "").split():
        token = word.strip("()[],;")
        if token.startswith(("http://", "https://")):
            return token
    return ""


templates.env.globals["line_link"] = _line_link
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


def _sel(value: str | None) -> int | None:
    """A validated row id from ?edit=. Never raises: a bad value just means
    'no row selected', which renders the dialog closed."""
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _find(rows: list[sqlite3.Row], row_id: int | None) -> sqlite3.Row | None:
    """The row with that id, or None. A linear scan: these lists are one
    operator's clients or invoices, and it saves a second query."""
    if row_id is None:
        return None
    return next((r for r in rows if r["id"] == row_id), None)


def _clients(include_archived: bool = False) -> list[sqlite3.Row]:
    con = _conn()
    try:
        sql = "SELECT * FROM client"
        if not include_archived:
            sql += " WHERE archived = 0"
        return list(con.execute(sql + " ORDER BY name"))
    finally:
        con.close()


def _client(client_id: int) -> sqlite3.Row | None:
    con = _conn()
    try:
        return con.execute("SELECT * FROM client WHERE id=?", (client_id,)).fetchone()
    finally:
        con.close()


def _videos(client_id: int | None = None, ym: str | None = None,
            q: str = "") -> list[sqlite3.Row]:
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
        if q.strip():
            sql += " AND (v.title LIKE ? OR v.link LIKE ? OR c.name LIKE ?)"
            like = f"%{q.strip()}%"
            params.extend([like, like, like])
        sql += " ORDER BY v.shot_on DESC, v.id DESC"
        return list(con.execute(sql, params))
    finally:
        con.close()


def _expenses(ym: str | None = None, category: str | None = None,
              client_id: int | None = None) -> list[sqlite3.Row]:
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
        if client_id:
            sql += " AND e.client_id = ?"
            params.append(client_id)
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


def _invoices(status: str | None = None, client_id: int | None = None,
              q: str = "") -> list[sqlite3.Row]:
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
        if client_id:
            sql += " AND i.client_id = ?"
            params.append(client_id)
        if q.strip():
            sql += " AND (i.number LIKE ? OR c.name LIKE ?)"
            like = f"%{q.strip()}%"
            params.extend([like, like])
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
def home(request: Request, month: str | None = None, start: str | None = None,
         end: str | None = None, all: int = 0, empty: int = 0):
    """Uninvoiced work.

    The month picker stays the quick path, so a plain visit still means "this
    month"; `start`/`end` are the flexible one and win when either is given.
    `all=1` shows everything outstanding, which is the case a once-a-month flow
    keeps people waiting on -- it needs its own flag because a blank date input
    and an absent one are the same query string.
    """
    if all:
        period_start = period_end = None
        ym = ""
    elif (start or "").strip() or (end or "").strip():
        period_start = _parse_date(start or "")
        period_end = _parse_date(end or "")
        ym = ""
    else:
        ym = _parse_month(month)
        period_start, period_end = _month_bounds(ym)
    con = _conn()
    try:
        unbilled = billing.unbilled_summary(con, period_start, period_end)
    finally:
        con.close()
    return templates.TemplateResponse(request, "home.html", {
        "s": settings, "month": ym, "start": period_start or "",
        "end": period_end or "", "period_start": period_start,
        "period_end": period_end, "all": bool(all),
        "range_label": billing.human_range(period_start, period_end),
        "unbilled": unbilled,
        "total_cents": sum(r["total_cents"] for r in unbilled),
        "clients": _clients(),
        "today": billing.today(),
        "empty": bool(empty),
        "active": "home",
    })


# --- videos ---------------------------------------------------------------------


def _video_form(client_id: int, shot_on: str, title: str, rate: str,
                link: str = "") -> tuple[str, dict]:
    """Validate one video form. Returns (error, {}) or ("", values).

    Shared by the add and the update route so both reject and accept exactly the
    same input: `client_id` must exist, `shot_on` a real date, a title *or* a
    link present (either identifies the video), and a blank rate falls back to
    that client's default. Check order and wording are the form's contract -- a
    rejected post re-renders with the message.
    """
    day = _parse_date(shot_on)
    title = title.strip()
    link = link.strip()
    con = _conn()
    try:
        client = con.execute("SELECT * FROM client WHERE id=?", (client_id,)).fetchone()
        if not client:
            return "Pick a client.", {}
        if not day:
            return "Date must be a date like 2026-08-14.", {}
        if not title and not link:
            return "A title or a link is required.", {}
        if rate.strip():
            try:
                cents = parse_cents(rate)
            except ValueError as e:
                return f"Rate: {e}.", {}
        else:
            cents = int(client["video_rate_cents"])
    finally:
        con.close()
    if cents <= 0:
        return "The rate must be greater than zero.", {}
    return "", {"client_id": client_id, "shot_on": day, "title": title,
                "link": link, "rate_cents": cents}


def _videos_context(mode: str | None, edit_video: sqlite3.Row | None,
                    form: dict | None, error: str, client: int | None,
                    month: str | None, q: str) -> dict:
    """The full videos_page context plus a dialog state: both error paths
    re-render the same page, so the table behind the dialog never goes stale."""
    ym = _parse_month(month) if month else None
    return {
        "s": settings, "videos": _videos(client, ym, q), "clients": _clients(True),
        "client": client, "month": month or "", "q": q, "active": "videos",
        "today": billing.today(),
        "mode": mode, "edit_video": edit_video, "form": form, "error": error,
    }


@app.post("/videos")
def video_add(
    request: Request,
    client_id: int = Form(...),
    shot_on: str = Form(""),
    title: str = Form(""),
    rate: str = Form(""),
    link: str = Form(""),
):
    error, values = _video_form(client_id, shot_on, title, rate, link)
    if not error:
        con = _conn()
        try:
            con.execute(
                "INSERT INTO video (client_id, shot_on, title, link, rate_cents, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (values["client_id"], values["shot_on"], values["title"],
                 values["link"], values["rate_cents"], billing.today()),
            )
            con.commit()
        finally:
            con.close()
        return RedirectResponse("/videos", status_code=303)
    return templates.TemplateResponse(request, "videos.html", _videos_context(
        "add", None,
        {"client_id": client_id, "shot_on": shot_on, "title": title, "rate": rate,
         "link": link},
        error, None, None, "",
    ), status_code=400)


@app.post("/videos/{video_id}")
def video_update(
    request: Request,
    video_id: int,
    client_id: int = Form(...),
    shot_on: str = Form(""),
    title: str = Form(""),
    rate: str = Form(""),
    link: str = Form(""),
):
    con = _conn()
    try:
        row = con.execute("SELECT * FROM video WHERE id=?", (video_id,)).fetchone()
    finally:
        con.close()
    if not row:
        raise HTTPException(404, "no such video")
    # A billed video is the invoice's record: its line was copied at creation,
    # so editing the video now would silently contradict a sent invoice. The
    # delete route refuses the same thing for the same reason.
    if row["invoice_id"] is not None:
        raise HTTPException(400, "that video is already billed; delete the invoice instead")
    error, values = _video_form(client_id, shot_on, title, rate, link)
    if not error:
        con = _conn()
        try:
            con.execute(
                "UPDATE video SET client_id=?, shot_on=?, title=?, link=?, rate_cents=? "
                "WHERE id=?",
                (values["client_id"], values["shot_on"], values["title"],
                 values["link"], values["rate_cents"], video_id),
            )
            con.commit()
        finally:
            con.close()
        return RedirectResponse("/videos", status_code=303)
    return templates.TemplateResponse(request, "videos.html", _videos_context(
        "edit", row,
        {"client_id": client_id, "shot_on": shot_on, "title": title, "rate": rate,
         "link": link},
        error, None, None, "",
    ), status_code=400)


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
    return RedirectResponse("/videos", status_code=303)


@app.get("/videos", response_class=HTMLResponse)
def videos_page(request: Request, client: int | None = None, month: str | None = None,
                edit: str | None = None, q: str = ""):
    ym = _parse_month(month) if month else None
    # Resolved against the whole table on purpose: a row's Edit link carries no
    # filter, and with a search active the filtered list may not contain it, so
    # the dialog would silently fail to open.
    edit_video = _find(_videos(), _sel(edit))
    return templates.TemplateResponse(request, "videos.html", _videos_context(
        "edit" if edit_video else None, edit_video, None, "", client, month, q,
    ))


# --- clients --------------------------------------------------------------------


@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request, edit: str | None = None):
    rows = _clients(True)
    edit_client = _find(rows, _sel(edit))
    return templates.TemplateResponse(request, "clients.html", {
        "s": settings, "clients": rows,
        "edit_client": edit_client, "mode": "edit" if edit_client else None,
        "form": None, "error": "", "active": "clients",
    })


def _client_saved(request: Request, name: str, video_rate: str, email: str,
                  notes: str, payment_terms: str, client_id: int | None) -> RedirectResponse:
    """Insert (client_id None) or update one client, then land back on /clients.

    The redirect carries no selection: the dialog has closed, and a hand-reloaded
    ?edit=<id> must mean "open this row's editor", not "the save you just made".
    """
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
    if not error and payment_terms.strip() and not payment_terms.strip().isdigit():
        error = "Payment terms must be a whole number of days."
    # NULL is "follow the global setting"; a stored 0 means due immediately.
    terms = int(payment_terms) if payment_terms.strip().isdigit() else None
    if not error:
        con = _conn()
        try:
            if client_id is not None:
                if not con.execute("SELECT 1 FROM client WHERE id=?", (client_id,)).fetchone():
                    raise HTTPException(404, "no such client")
                sql = ("UPDATE client SET name=?, video_rate_cents=?, email=?, notes=?, "
                       "payment_terms_days=? WHERE id=?")
                params = (name, rate, email.strip(), notes.strip(), terms, client_id)
            else:
                sql = ("INSERT INTO client (name, video_rate_cents, email, notes, "
                       "payment_terms_days, created_at) VALUES (?,?,?,?,?,?)")
                params = (name, rate, email.strip(), notes.strip(), terms, billing.today())
            try:
                con.execute(sql, params)
                con.commit()
            except sqlite3.IntegrityError:
                error = "A client with that name already exists."
        finally:
            con.close()
    if error:
        rows = _clients(True)
        # The rejected values go back into the dialog that was open: the row
        # being edited when there is one, otherwise a synthetic row built from
        # the post, so an add that failed keeps what was typed.
        if client_id is not None:
            edit_client = _find(rows, client_id) or {
                "id": client_id, "name": name, "video_rate_cents": rate,
                "email": email, "notes": notes, "archived": 0,
                "payment_terms_days": terms,
            }
            mode = "edit"
        else:
            edit_client, mode = None, "add"
        return templates.TemplateResponse(request, "clients.html", {
            "s": settings, "clients": rows,
            "edit_client": edit_client, "mode": mode,
            "error": error,
            "form": {"name": name, "video_rate": video_rate, "email": email,
                     "notes": notes, "payment_terms": payment_terms},
            "active": "clients",
        }, status_code=400)
    return RedirectResponse("/clients", status_code=303)


@app.post("/clients")
def client_add(
    request: Request,
    name: str = Form(""),
    video_rate: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
    payment_terms: str = Form(""),
):
    return _client_saved(request, name, video_rate, email, notes, payment_terms, None)


@app.post("/clients/{client_id}")
def client_update(
    request: Request,
    client_id: int,
    name: str = Form(""),
    video_rate: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
    payment_terms: str = Form(""),
):
    return _client_saved(request, name, video_rate, email, notes, payment_terms, client_id)


@app.get("/clients/{client_id}", response_class=HTMLResponse)
def client_page(request: Request, client_id: int):
    row = _client(client_id)
    if not row:
        raise HTTPException(404, "no such client")
    invoices = _invoices(client_id=client_id)
    videos = _videos(client_id=client_id)
    return templates.TemplateResponse(request, "client.html", {
        "s": settings, "c": row, "invoices": invoices, "videos": videos,
        "expenses": _expenses(client_id=client_id),
        "invoiced_cents": sum(int(i["total_cents"]) for i in invoices if i["status"] != "draft"),
        "paid_cents": sum(int(i["total_cents"]) for i in invoices if i["status"] == "paid"),
        "unbilled_cents": sum(int(v["rate_cents"]) for v in videos if v["invoice_id"] is None),
        "active": "clients",
    })


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
def invoice_monthly(
    client_id: int = Form(...),
    month: str = Form(""),
    start: str = Form(""),
    end: str = Form(""),
    group: str = Form(""),
    label: str = Form(""),
):
    """Bill one client's unbilled videos.

    `month` is the quick "just bill July" path and stays the default; `start` and
    `end` are the flexible one, either may be blank (open-ended), and both blank
    bills everything still outstanding. `group` folds the whole range into a
    single line. A bad date is treated as "no bound", never a 500.
    """
    start, end = start.strip(), end.strip()
    if month.strip():
        start, end = _month_bounds(_parse_month(month))
    period_start = _parse_date(start)
    period_end = _parse_date(end)
    con = _conn()
    try:
        try:
            invoice_id = billing.create_invoice(
                con, client_id,
                period_start=period_start, period_end=period_end,
                group=bool(group.strip()), label=label,
                due_days=billing.payment_terms_days(
                    con, client_id, settings.payment_terms_days),
            )
            con.commit()
        except billing.AlreadyInvoiced as e:
            con.rollback()
            return RedirectResponse(f"/invoices/{e.invoice_id}?exists=1", status_code=303)
    finally:
        con.close()
    if not invoice_id:
        return RedirectResponse(
            f"/?start={period_start or ''}&end={period_end or ''}&empty=1", status_code=303)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@app.post("/invoices/oneoff")
def invoice_oneoff(client_id: int = Form(...)):
    con = _conn()
    try:
        invoice_id = billing.create_oneoff_invoice(
            con, client_id,
            due_days=billing.payment_terms_days(
                con, client_id, settings.payment_terms_days),
        )
        con.commit()
    finally:
        con.close()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@app.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request, status: str = "all", q: str = ""):
    rows = _invoices(None if status == "all" else status, q=q)
    return templates.TemplateResponse(request, "invoices.html", {
        "s": settings, "invoices": rows, "status": status, "clients": _clients(),
        "q": q,
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


def _invoice_saved(request: Request, invoice_id: int, error: str) -> RedirectResponse:
    """A rejected line edit re-renders the invoice page, so the panel keeps the
    fields that were typed."""
    inv = _invoice(invoice_id)
    lines = _lines(invoice_id)
    return templates.TemplateResponse(request, "invoice.html", {
        "s": settings, "inv": inv, "lines": lines,
        "total_cents": sum(int(l["qty"]) * int(l["unit_cents"]) for l in lines),
        "error": error, "active": "invoices",
    }, status_code=400)


def _line_write(request: Request, line_id: int, description: str, qty: str,
                unit: str) -> RedirectResponse:
    """Update one line and return to the invoice page."""
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
        return _invoice_saved(request, invoice_id, error)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


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
    return _invoice_saved(request, invoice_id, error)


@app.post("/lines/{line_id}")
def line_update(
    request: Request,
    line_id: int,
    description: str = Form(""),
    qty: str = Form("1"),
    unit: str = Form(""),
):
    return _line_write(request, line_id, description, qty, unit)


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


@app.post("/invoices/{invoice_id}/notes")
def invoice_notes(invoice_id: int, notes: str = Form("")):
    con = _conn()
    try:
        if not con.execute("SELECT 1 FROM invoice WHERE id=?", (invoice_id,)).fetchone():
            raise HTTPException(404, "no such invoice")
        con.execute("UPDATE invoice SET notes=? WHERE id=?", (notes.strip(), invoice_id))
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
def expenses_page(request: Request, month: str | None = None, category: str = "",
                  edit: str | None = None):
    ym = _parse_month(month) if month else billing.today()[:7]
    rows = _expenses(ym, category or None)
    edit_expense = _find(rows, _sel(edit))
    return templates.TemplateResponse(request, "expenses.html", {
        "s": settings, "expenses": rows, "month": ym, "category": category,
        "categories": _expense_categories(), "clients": _clients(True),
        "today": billing.today(),
        "edit_expense": edit_expense, "mode": "edit" if edit_expense else None,
        "form": None, "error": "",
        "expense_total": sum(int(r["amount_cents"]) for r in rows),
        "deductible_total": sum(int(r["amount_cents"]) for r in rows if r["deductible"]),
        "active": "expenses",
    })


def _expense_context(month: str, category: str, mode: str | None,
                     edit_expense, form: dict | None, error: str) -> dict:
    """The expenses page with a dialog state. Both rejected posts re-render the
    same page, so the table behind the dialog matches the filter in the URL."""
    rows = _expenses(month, category or None)
    return {
        "s": settings, "expenses": rows, "month": month, "category": category,
        "categories": _expense_categories(), "clients": _clients(True),
        "today": billing.today(),
        "edit_expense": edit_expense, "mode": mode, "form": form, "error": error,
        "expense_total": sum(int(r["amount_cents"]) for r in rows),
        "deductible_total": sum(int(r["amount_cents"]) for r in rows if r["deductible"]),
        "active": "expenses",
    }


def _expense_form(spent_on: str, amount: str, client_id: str) -> tuple[str, str, int, int | None]:
    """Validate the expense fields shared by add and update.

    Returns (error, day, cents, cid); `error` is the message to show and every
    other value is only meaningful when it is empty. Both routes use this so the
    two sides accept and reject exactly the same input.
    """
    day = _parse_date(spent_on)
    if not day:
        return "Spent on must be a date like 2026-08-14.", "", 0, None
    try:
        cents = parse_cents(amount)
    except ValueError as e:
        return f"Amount: {e}.", day, 0, None
    if cents <= 0:
        return "The amount must be greater than zero.", day, cents, None
    cid = int(client_id) if client_id.strip().isdigit() else None
    return "", day, cents, cid


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
    error, day, cents, cid = _expense_form(spent_on, amount, client_id)
    form = {"spent_on": spent_on, "amount": amount, "category": category,
            "vendor": vendor, "client_id": cid, "deductible": bool(deductible),
            "notes": notes}
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
    month = billing.today()[:7]
    return templates.TemplateResponse(request, "expenses.html", _expense_context(
        month, "", "add", None, form, error,
    ), status_code=400)


@app.post("/expenses/{expense_id}")
def expense_update(
    request: Request,
    expense_id: int,
    spent_on: str = Form(""),
    amount: str = Form(""),
    category: str = Form(""),
    vendor: str = Form(""),
    client_id: str = Form(""),
    deductible: str = Form(""),
    notes: str = Form(""),
):
    con = _conn()
    try:
        row = con.execute("SELECT * FROM expense WHERE id=?", (expense_id,)).fetchone()
    finally:
        con.close()
    if not row:
        raise HTTPException(404, "no such expense")
    error, day, cents, cid = _expense_form(spent_on, amount, client_id)
    form = {"spent_on": spent_on, "amount": amount, "category": category,
            "vendor": vendor, "client_id": cid, "deductible": bool(deductible),
            "notes": notes}
    if not error:
        con = _conn()
        try:
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
    # The stored row is still on its own month's page, and that is the table the
    # dialog sits over, so re-render that month unfiltered: a category filter
    # could otherwise hide the very row being edited.
    return templates.TemplateResponse(request, "expenses.html", _expense_context(
        row["spent_on"][:7], "", "edit", row, form, error,
    ), status_code=400)


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


# --- exports --------------------------------------------------------------------


def _csv_response(rows: list[list[object]], filename: str) -> Response:
    """A CSV download. `lineterminator` is explicit because the csv module's
    default is the RFC-4180 CRLF, which is also what Excel expects."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerows(rows)
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export/invoices.csv")
def export_invoices():
    rows = [["Number", "Client", "Kind", "Period start", "Period end", "Issued",
             "Due", "Status", "Paid date", "Total"]]
    for i in _invoices():
        rows.append([
            i["number"], i["client_name"], i["kind"], i["period_start"] or "",
            i["period_end"] or "", i["issue_date"], i["due_date"], i["status"],
            i["paid_date"] or "", plain_cents(int(i["total_cents"])),
        ])
    return _csv_response(rows, f"busypanel-invoices-{billing.today()}.csv")


@app.get("/export/expenses.csv")
def export_expenses():
    rows = [["Spent on", "Vendor", "Category", "Client", "Amount", "Deductible", "Notes"]]
    for e in _expenses():
        rows.append([
            e["spent_on"], e["vendor"], e["category"], e["client_name"] or "",
            plain_cents(int(e["amount_cents"])),
            "yes" if e["deductible"] else "no", e["notes"],
        ])
    return _csv_response(rows, f"busypanel-expenses-{billing.today()}.csv")


@app.get("/export/summary.csv")
def export_summary():
    year = int(billing.today()[:4])
    con = _conn()
    try:
        summary = report.monthly_summary(con, year)
    finally:
        con.close()
    rows = [["Month", "Invoiced", "Paid", "Outstanding", "Expenses", "Deductible", "Net"]]
    for r in summary:
        rows.append([
            calendar.month_name[r["month"]],
            plain_cents(r["invoiced_cents"]), plain_cents(r["paid_cents"]),
            plain_cents(r["outstanding_cents"]), plain_cents(r["expenses_cents"]),
            plain_cents(r["deductible_cents"]), plain_cents(r["net_cents"]),
        ])
    totals = report.year_totals(summary)
    rows.append([
        "Total",
        plain_cents(totals["invoiced_cents"]), plain_cents(totals["paid_cents"]),
        plain_cents(totals["outstanding_cents"]), plain_cents(totals["expenses_cents"]),
        plain_cents(totals["deductible_cents"]), plain_cents(totals["net_cents"]),
    ])
    return _csv_response(rows, f"busypanel-summary-{year}.csv")


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
