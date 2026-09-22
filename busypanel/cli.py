"""BusyPanel CLI.

The web UI is the primary interface; the subcommands exist for scripting, for the
NixOS service, and for the one operation that is easier in a terminal (setting the
password).
"""

from __future__ import annotations

import logging
import sqlite3

import typer
from rich.console import Console
from rich.logging import RichHandler

from busypanel.config import settings
from busypanel.state import load_overrides, save_overrides

app = typer.Typer(
    add_completion=False,
    help="Client videos, monthly invoices, one-off jobs and expenses.",
    invoke_without_command=True,
)
console = Console()


@app.callback()
def main(ctx: typer.Context) -> None:
    """Launch the web UI when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        web(host=None, port=None)


def _setup(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, show_time=False)],
    )
    settings.ensure_dirs()


@app.command()
def web(
    host: str = typer.Option(None, help="Bind address (default 0.0.0.0)."),
    port: int = typer.Option(None, help="Port (default 8090)."),
) -> None:
    """Open the web UI (same as running `busypanel` bare)."""
    _setup()
    from busypanel.web.app import serve

    shown = host or settings.web_host
    # 0.0.0.0 is a bind address, not a destination; show a navigable URL.
    if shown in ("0.0.0.0", "::"):
        shown = "127.0.0.1"
    console.print(f"busypanel on [bold]http://{shown}:{port or settings.web_port}[/]  (Ctrl-C to stop)")
    serve(host=host, port=port)


@app.command()
def passwd(
    password: str = typer.Option(
        None, "--password", "-p",
        help="Set non-interactively (for scripts); omit to be prompted.",
    ),
    disable: bool = typer.Option(False, "--disable", help="Remove the password and open the UI."),
) -> None:
    """Set (or clear) the password that gates the web UI."""
    if disable:
        save_overrides({"auth_password": ""})
        console.print("[green]Password cleared.[/] The panel is now open to anyone who can reach it.")
        return

    if password is None:
        password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    if not password.strip():
        console.print("[red]Empty password.[/] Use --disable to remove the gate deliberately.")
        raise typer.Exit(1)

    save_overrides({"auth_password": password})
    console.print("[green]Password set.[/] Stored in settings.json (mode 0600).")
    console.print("[dim]Restart the service for a systemd deployment to pick it up: "
                  "systemctl restart busypanel[/]")


@app.command()
def backup(
    out: str = typer.Option(None, "--out", help="Destination file (default ./busypanel-<date>.db)."),
) -> None:
    """Copy the database somewhere safe, while the server is running."""
    _setup()
    from busypanel.billing import today

    target = out or f"busypanel-{today()}.db"
    # sqlite3's backup API, not a file copy: a live server may hold pages in its
    # WAL/journal, and copying the file alone can produce a torn database.
    src = sqlite3.connect(settings.db_path)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    console.print(f"[green]Backed up[/] {settings.db_path} → [bold]{target}[/]")


@app.command()
def status() -> None:
    """Print where the books live and what the panel knows about."""
    _setup()
    from busypanel import db
    from busypanel.money import fmt_cents

    con = db.connect()
    try:
        clients = con.execute("SELECT COUNT(*) AS c FROM client WHERE archived=0").fetchone()["c"]
        videos = con.execute("SELECT COUNT(*) AS c FROM video").fetchone()["c"]
        unbilled = con.execute("SELECT COUNT(*) AS c FROM video WHERE invoice_id IS NULL").fetchone()["c"]
        invoices = con.execute("SELECT COUNT(*) AS c FROM invoice").fetchone()["c"]
        drafts = con.execute("SELECT COUNT(*) AS c FROM invoice WHERE status='draft'").fetchone()["c"]
        oustanding = con.execute(
            "SELECT COALESCE(SUM(l.qty * l.unit_cents), 0) AS t FROM invoice i "
            "JOIN invoice_line l ON l.invoice_id = i.id WHERE i.status = 'sent'"
        ).fetchone()["t"]
    finally:
        con.close()

    console.print(f"database   [bold]{settings.db_path}[/]")
    console.print(f"state dir  {settings.db_path.parent}")
    console.print(f"clients    {clients} active")
    console.print(f"videos     {videos} ({unbilled} unbilled)")
    console.print(f"invoices   {invoices} ({drafts} draft)")
    console.print(f"awaiting   {fmt_cents(int(oustandings))} (sent, unpaid)")
    console.print(
        "[dim]Settings live in "
        f"{settings.db_path.parent / 'settings.json'}; "
        f"password {'set' if (load_overrides().get('auth_password') or settings.auth_password) else 'NOT set'}[/]"
    )


@app.command()
def doctor() -> None:
    """Check that the database is writable and the schema is current."""
    _setup()
    from busypanel import db

    try:
        con = db.connect()
        try:
            tables = sorted(r["name"] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
        finally:
            con.close()
    except (sqlite3.Error, OSError) as e:
        console.print(f"[red]Database unusable:[/] {e}")
        raise typer.Exit(1)
    console.print(f"[green]ok[/] {settings.db_path}")
    console.print(f"[dim]tables: {', '.join(tables)}[/]")
    from busypanel.web import auth

    console.print(f"[dim]login gate: {'on' if auth.enabled() else 'off'}[/]")


if __name__ == "__main__":
    app()
