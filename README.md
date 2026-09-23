# BusyPanel

A self-hosted business panel for a one-person video marketing company: the videos
produced for each client, the monthly invoice they roll into, one-off jobs billed
as their own invoices, and the expenses (including the tax-deductible ones) that
sit against the year.

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
nix develop --command python -m pytest -q    # 62 tests
nix build                                     # package builds
nix flake check                               # service boots in a VM
```

If `git push` fails with `Permission denied (publickey)` or
`agent refused operation`, the SSH key is locked rather than misconfigured —
unlock it with `ssh-add ~/.ssh/id_ed25519` and retry.

## Screens

- **Uninvoiced** (`/`) — the landing page. Unbilled videos for a month, grouped by
  client with a count and a total, each with a **Create invoice** button, plus the
  quick-add form for a new video. A blank rate falls back to the client's default.
- **Clients** (`/clients`) — name, default per-video rate, email, notes; archive
  hides a client from the pickers without touching their history.
- **Videos** (`/videos`) — every video shot, filterable by client and month, showing
  whether it is billed and on which invoice.
- **Invoices** (`/invoices`) — the list, filtered by status, plus the one-off
  invoice button. `/invoices/{id}` edits one: status, lines, add/remove lines.
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
| `client` | Name (unique), default video rate, contact details, archived flag |
| `video` | One row per video: client, date shot, title, rate, and `invoice_id` |
| `invoice` | Monthly or one-off, with its own `period_start`/`period_end`, dates, status |
| `invoice_line` | Materialised at invoice creation: description, qty, unit price |
| `expense` | Money out, with a `deductible` flag ("writeoff") and an optional client |
| `meta` | Small key/value store; holds the per-year invoice counter |

Two links carry the design:

- **`video.invoice_id` is a soft link**, not a lock. Deleting an invoice sets it
  back to NULL, so billed work returns to the unbilled pool instead of being
  stranded, and a video that has been billed cannot be deleted from the Videos page —
  the invoice is its record until you delete the invoice.
- **Invoice lines are a snapshot.** Creating a monthly invoice copies each video's
  title and rate into a line, so a sent invoice does not change when a video is
  edited afterwards.

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

## Testing

```bash
nix develop --command python -m pytest -q   # 62 tests
nix flake check                             # additionally boots the service in a VM
```

`tests/` follows pytest-function conventions with `tmp_path`/`monkeypatch`: each
test gets its own state directory and database, so the suite is order-independent
and leaves nothing behind. Most of the coverage sits on the parts that would be
expensive to get wrong:

- `test_money.py` — parsing and formatting, including that more than two decimal
  places is rejected rather than silently rounded.
- `test_billing.py` — a monthly invoice pulls exactly one client's unbilled videos
  inside the period, one line per video; a second invoice for the same client and
  period is refused; an empty period raises instead of creating a $0 invoice;
  deleting an invoice releases its videos; one-off invoices leave videos untouched;
  invoice numbers are not reused.
- `test_report.py` — drafts excluded from invoiced/paid, and the deductible subset
  separated from total expenses.
- `test_db.py` — schema idempotence, `PRAGMA foreign_keys` actually on, uniqueness
  constraints, cascade on invoice delete.
- `test_web.py` — every screen renders, unknown ids 404, bad input is a 400 rather
  than a 500, the login gate redirects while `/health` stays open.
- `test_cli.py` — `status`, `doctor`, `passwd` and `backup` against a real database.

The VM check in `flake.nix` is deliberately stronger than evaluation: it boots the
module under QEMU and asserts the unit starts, `/var/lib/busypanel` exists as
`busypanel` mode 0700, a client → video → invoice round trip produces the right
total on the print page, and the books survive `systemctl restart`. Breaking an
assertion makes it fail, so it is not a vacuous pass.

## No sales tax, no PDF library, no email

Deliberate: invoices leave the app through the print view. Adding tax means one
percentage applied in `billing.invoice_total`, `report.monthly_summary` and
`print.html`; a real PDF means a new dependency in `pyproject.toml` and `uv.lock`.
