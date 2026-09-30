# BusyPanel

A self-hosted business panel for a one-person video marketing company: the videos
produced for each client, the invoice they roll into — for any date range, not just
a calendar month — one-off jobs billed as their own invoices, and the expenses
(including the tax-deductible ones) that sit against the year.

Server-rendered FastAPI + Jinja2 + SQLite, packaged with Nix. No build step, no
CDN, no client-side framework. A single small `static/app.js` adds in-place
updates — swapped page regions, modal creation forms and toasts — and degrades to
plain form posts with JavaScript disabled. One SQLite file.

## Running it

```bash
nix run            # web UI on 0.0.0.0:8090; see the bind note below
nix develop        # dev shell with pytest
nix flake check    # boots a VM and proves the service works
./run.sh           # the uv .venv fallback path (uv sync --extra dev first)
```

`nix run` binds `0.0.0.0:8090` — the CLI prints `http://127.0.0.1:8090` as a
convenient URL, but the server also answers on the machine's LAN address. To keep
it to this machine only, pass an explicit loopback host:

```bash
nix run . -- web --host 127.0.0.1 --port 8090
```

Nothing is written outside the state directory, so running it ad-hoc is safe and
leaves the system configuration untouched.

Useful commands:

```bash
busypanel                          # bare = the web UI
busypanel web --port 8090          # the web UI on another port
busypanel passwd                   # set the password that gates the UI
busypanel backup --out books.db    # safe copy of the database while running
busypanel restore books.db         # put a backup back (--force to skip the prompt)
busypanel status                   # what the panel knows about
busypanel doctor                   # database writable, schema present
```

## Installing it as a service

The flake is a NixOS module as well as a package, so BusyPanel installs as a
hardened systemd unit — it starts on boot, restarts on failure, runs as its own
`busypanel` user, and keeps the books in `/var/lib/busypanel` (a systemd
`StateDirectory` created mode 0700 on first start).

You need a flake-based NixOS config. If your `/etc/nixos` has no `flake.nix` yet,
that is step 1 below; enable flakes first:

```nix
# /etc/nixos/configuration.nix
nix.settings.experimental-features = [ "nix-command" "flakes" ];
```

### 1. Add the input and the module

`/etc/nixos/flake.nix` — replace `mymachine` with your hostname (`hostname` will
tell you; it is usually `nixos`):

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    busypanel.url = "github:JuiceyDew/BusyPanel";
  };

  outputs = { self, nixpkgs, busypanel, ... }: {
    nixosConfigurations.mymachine = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        ./configuration.nix
        busypanel.nixosModules.default
      ];
    };
  };
}
```

If you already have a flake, you only need two additions to it — the `busypanel`
input above, and `busypanel.nixosModules.default` in `modules` (after your own
`./configuration.nix`). Add `busypanel` to the `outputs` argument list, and to any
`specialArgs = { inherit inputs; }` if you pass inputs that way.

### 2. Enable the service

In `/etc/nixos/configuration.nix`:

```nix
services.busypanel = {
  enable = true;
  host = "0.0.0.0";
  port = 8090;
  openFirewall = true;   # opens only `port`
};
```

### 3. Rebuild

```bash
cd /etc/nixos
sudo nixos-rebuild switch --flake .#mymachine
```

The first rebuild resolves the input and writes `busypanel` into `flake.lock`, so
the deployment is pinned to an exact commit from then on.

### 4. Verify

```bash
systemctl status busypanel         # active (running)
curl -s localhost:8090/health      # {"ok":true}
journalctl -u busypanel -f         # live logs
```

### 5. Set a password before exposing it

The panel is **ungated** until a password is set. Either keep the secret out of the
Nix store entirely:

```nix
services.busypanel.authPasswordFile = "/run/secrets/busypanel-password";
```

or set it once through the CLI after first start. Run it as the service user *and*
point it at the service's state directory — `passwd` resolves its own state dir
from `BUSYPANEL_STATE_DIR`, and without it the CLI would write to
`~/.local/state/busypanel` and report success while the service never saw it:

```bash
sudo -u busypanel env BUSYPANEL_STATE_DIR=/var/lib/busypanel \
  $(nix build --no-link --print-out-paths github:JuiceyDew/BusyPanel)/bin/busypanel passwd
sudo systemctl restart busypanel
```

The same wrapper applies to any other CLI command against a service deployment
(`status`, `doctor`, `backup`) — they all need `BUSYPANEL_STATE_DIR` to find the
service's books.

> **With no password, anyone who can reach `port` can read, edit and delete every
> client, invoice and expense.** This is a LAN password gate, not internet-grade
> auth — the password crosses plain HTTP, so put TLS in front before it leaves
> your network.

### Options

| Option | Default | Notes |
|---|---|---|
| `enable` | `false` | |
| `package` | this flake's package | override to pin a different build |
| `host` / `port` | `"0.0.0.0"` / `8090` | |
| `stateDir` | `/var/lib/busypanel` | database + `settings.json`; mode 0700 |
| `user` / `group` | `busypanel` | created automatically |
| `environmentFile` | `null` | `Environment=` lines; overrides the UI |
| `authPasswordFile` | `null` | password kept out of the Nix store |
| `credentials` | `{}` | `ENV_VAR = path`, delivered via `LoadCredential` |
| `openFirewall` | `false` | opens only `port` |

The unit is hardened with `NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`,
`ProtectSystem = "strict"`, `ProtectHome`, `ProtectKernel*`,
`ProtectControlGroups`, `RestrictAddressFamilies`, `RestrictNamespaces`,
`LockPersonality`, `RestrictRealtime` and `SystemCallArchitectures = "native"`,
with `ReadWritePaths` limited to the state directory.

### Updating the service

```bash
cd /etc/nixos
nix flake update busypanel                  # or: nix flake update  (all inputs)
sudo nixos-rebuild switch --flake .#mymachine
```

To stay on a known-good revision instead of tracking `master`, pin the input:

```nix
busypanel.url = "github:JuiceyDew/BusyPanel/<commit-sha>";
```

### Using the package without the service

On a non-NixOS machine (or to try it before installing anything system-wide):

```bash
nix run github:JuiceyDew/BusyPanel -- web --host 127.0.0.1 --port 8090
```

### Proving the service works

`nix flake check` boots a QEMU VM with the module enabled and asserts the real
thing, not just that the options evaluate: the unit starts, the state directory
exists as `busypanel` mode 0700, every screen renders, a client → video → invoice
round trip produces a `$550.00` invoice, and the books survive a service restart.

## Publishing changes to GitHub

From a checkout of this repo:

```bash
cd /home/dew/Projects/BusyPanel
git status                        # review what changed
git add -A
git commit -m "describe the change"
git push                          # origin master
```

After changing dependencies in `pyproject.toml`, regenerate the lock first — it
must be done with downloads disabled, or uv fetches a generic-linux CPython that
cannot execute on NixOS:

```bash
nix shell nixpkgs#uv nixpkgs#python312 -c \
  bash -c 'UV_PYTHON_DOWNLOADS=never uv lock'
```

After changing `flake.nix` inputs:

```bash
nix flake update                  # or: nix flake update <input>
git add flake.lock && git commit -m "flake: update inputs" && git push
```

Before pushing, the checks that must pass:

```bash
nix develop --command python -m pytest -q    # 77 tests
nix build                                     # package builds
nix flake check                               # service boots in a VM
```

If `git push` fails with `Permission denied (publickey)` or
`agent refused operation`, the SSH key is locked rather than misconfigured —
unlock it with `ssh-add ~/.ssh/id_ed25519` and retry.

## Screens

Every list is a **table with an Actions column**, and editing happens in a
**dialog** over the same server-rendered form and the same form-POST route. The
dialog holds one row (`?edit=<id>` selects which — with JavaScript off that
address renders the same form inline above the table, and the no-JS submit is an
ordinary POST followed by a redirect). The one screen that is a real page rather
than a table is an invoice.

- **Uninvoiced** (`/`) — the landing page. A table of clients with unbilled work
  for the chosen range: video count, unbilled total, default rate, and two ways to
  bill it — **Bill itemised** (one line per video, what a client sees itemised) or
  **Bill as one** (the whole batch on a single line, optionally labelled). The range
  is a month, or explicit **From**/**To** dates where either end may be blank, or
  **Everything outstanding** — billing is not tied to the calendar. A blank rate on
  a new video falls back to the client's default.
- **Clients** (`/clients`) — the list of clients with their default rate, payment
  terms, email and archived flag. **+ Add client** and each row's **Edit** open the
  same dialog; the destructive per-row action is archive, which hides a client from
  the pickers without touching their history. A client on different terms to the
  global setting stores its own; blank means "follow the setting", so changing the
  default still moves every client that never overrode it. A client name links to
  its own page.
- **Client** (`/clients/{id}`) — one client's lifetime figures (invoiced, paid,
  outstanding, unbilled) with their invoices, videos and expenses below.
- **Videos** (`/videos`) — every video shot, filterable by client, month and a
  free-text search over the title, the link and the client name, showing whether it
  is billed and on which invoice. A video needs a **title or a link** — pasting the
  URL alone is enough, and the link is carried onto the invoice line and made
  clickable there. Unbilled rows carry **Edit** and **Delete**; a billed row
  carries **Open invoice** instead, because its invoice line is the record from then
  on.
- **Invoices** (`/invoices`) — the list, filtered by status and searchable by
  invoice number or client, with **Open**, **Print** and **Delete** per row.
  `/invoices/{id}` edits one in full: status, lines, add/remove lines, a free-text
  note that reaches the printout, and the Danger panel.
- **Print view** (`/invoices/{id}/print`) — a standalone document for Ctrl+P → PDF:
  business details, client, period, lines, total. Chrome's print rules hide the nav
  and every control, so the preview is the invoice.
- **Expenses** (`/expenses`) — month total and its deductible subtotal (the writeoff
  number), with category, vendor, client link and a deductible flag.
- **Summary** (`/summary`) — twelve rows of invoiced / paid / outstanding / expenses /
  deductible / net for a year, with a totals row.
- **Settings** (`/settings`) — business name, address, email, payment terms, invoice
  footer, and the UI password.

## Data model

Six tables in one SQLite file:

| Table | What it holds |
|---|---|
| `client` | Name (unique), default video rate, contact details, optional `payment_terms_days`, archived flag |
| `video` | One row per video: client, date shot, title, optional `link`, rate, and `invoice_id` |
| `invoice` | Monthly or one-off, with its own `period_start`/`period_end`, dates, status |
| `invoice_line` | Materialised at invoice creation: description, qty, unit price |
| `expense` | Money out, with a `deductible` flag ("writeoff") and an optional client |
| `meta` | Small key/value store; holds the per-year invoice counter |

Two links carry the design:

- **`video.invoice_id` is a soft link**, not a lock. Deleting an invoice sets it
  back to NULL, so billed work returns to the unbilled pool instead of being
  stranded, and a video that has been billed cannot be deleted from the Videos page —
  the invoice is its record until you delete the invoice.
- **Invoice lines are a snapshot.** Creating an invoice copies each video's title
  and rate into a line, so a sent invoice does not change when a video is edited
  afterwards. The billable window is whatever range was chosen — a month, a
  fortnight, or everything outstanding — and it lives on the invoice, so editing a
  line cannot move it. A second invoice for the same client and *same bounded
  range* is refused; an unbounded bill has no period to dedupe on.

Invoice numbers are `YYYY-NNNN`, and the sequence only moves forward: deleting an
invoice never hands its number to the next one.

## State and backups

Everything lives in one SQLite file in the state directory — `data/` in a source
checkout, `$BUSYPANEL_STATE_DIR` when set, and `/var/lib/busypanel` under the NixOS
module. Settings (business details, the UI password) live beside it in
`settings.json`, mode 0600. Back up the whole panel with:

```bash
busypanel backup --out busypanel-$(date +%F).db
```

That uses SQLite's backup API rather than copying the file, so the copy is
consistent even while the server is running.

`busypanel restore <file>` puts a backup back. It validates the file is a real
BusyPanel database *before* touching the live one, writes a
`busypanel.db.pre-restore-<epoch>.db` safety copy of the current books beside it,
and then confirms unless `--force` is given. Only the database is replaced:
`settings.json` and the session secret next to it are left alone, so a restore
never silently changes the login. A backup taken before a schema change restores
cleanly and gains the new column on the next connection.

Schema changes are additive only, applied by `db._add_missing_columns` on every
connection: a column added to `SCHEMA` reaches an existing database without a
version table, because `ALTER TABLE ADD COLUMN` is the whole story. Dropping or
retyping a column would need a real migration instead.

## CSV exports

Each list screen carries an **Export CSV** button, and the routes are plain GETs a
bookkeeper can fetch directly: `/export/invoices.csv`, `/export/expenses.csv` and
`/export/summary.csv` (the current year, one row per month plus a total). Amounts
are written by `money.plain_cents` — a bare `1200.50`, no currency symbol and no
thousands separator, computed in integer arithmetic so no cent is lost to a float.
The three routes sit behind the same login gate as every other page.

## Testing

```bash
nix develop --command python -m pytest -q   # 77 tests
nix flake check                             # additionally boots the service in a VM
```

`tests/` follows pytest-function conventions with `tmp_path`/`monkeypatch`: each
test gets its own state directory and database, so the suite is order-independent
and leaves nothing behind. Most of the coverage sits on the parts that would be
expensive to get wrong:

- `test_money.py` — parsing and formatting, including that more than two decimal
  places is rejected rather than silently rounded.
- `test_billing.py` — an invoice pulls exactly one client's unbilled videos inside
  the chosen range, one line per video (or one line for the batch when grouped); a
  short or open-ended range bills only that window; a second invoice for the same
  client and bounded range is refused; nothing to bill returns 0 instead of creating
  a $0 invoice;
  deleting an invoice releases its videos; one-off invoices leave videos untouched;
  invoice numbers are not reused.
- `test_report.py` — drafts excluded from invoiced/paid, and the deductible subset
  separated from total expenses.
- `test_db.py` — schema idempotence, `PRAGMA foreign_keys` actually on, uniqueness
  constraints, cascade on invoice delete, and that an old database gains a new
  column on open without losing its rows.
- `test_web.py` — every screen renders, unknown ids 404, bad input is a 400 rather
  than a 500, the login gate redirects while `/health` stays open, per-client
  payment terms reach the invoice, an invoice note reaches the print view, search
  filters both lists, the client detail page shows lifetime totals, and the CSV
  exports carry plain cents.
- `test_cli.py` — `status`, `doctor`, `passwd`, `backup` and `restore` against a
  real database, including that restore refuses a non-database and leaves the live
  books untouched when it does.

The VM check in `flake.nix` is deliberately stronger than evaluation: it boots the
module under QEMU and asserts the unit starts, `/var/lib/busypanel` exists as
`busypanel` mode 0700, a client → video → invoice round trip produces the right
total on the print page, a per-client payment term reaches the due date, an invoice
note reaches the printout, search and the client detail page answer, the CSV export
carries its header and no `$`, and the books survive `systemctl restart`. Breaking an
assertion makes it fail, so it is not a vacuous pass.

## No sales tax, no PDF library, no email

Deliberate: invoice *documents* leave the app through the print view, and the
books leave as the three CSV exports above. Adding tax means one percentage applied
in `billing.invoice_total`, `report.monthly_summary` and `print.html`; a real PDF
means a new dependency in `pyproject.toml` and `uv.lock`.
