# BusyPanel

A self-hosted business panel for a one-person video marketing company: the videos
produced for each client, the monthly invoice they roll into, one-off jobs billed
as their own invoices, and the expenses (including the tax-deductible ones) that
sit against the year.

Server-rendered FastAPI + Jinja2 + SQLite, packaged with Nix. No build step, no
CDN, no JavaScript, one SQLite file.

## Running it

```bash
nix run            # open the web UI on http://127.0.0.1:8090
nix develop        # dev shell with pytest
nix flake check    # boots a VM and proves the service works
./run.sh           # the uv .venv fallback path (uv sync --extra dev first)
```

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

The flake is a NixOS module as well as a package. In your system flake:

```nix
{
  inputs.busypanel.url = "github:JuiceyDew/BusyPanel";

  outputs = { self, nixpkgs, busypanel, ... }: {
    nixosConfigurations.host = nixpkgs.lib.nixosSystem {
      modules = [
        busypanel.nixosModules.default
        ({ ... }: {
          services.busypanel = {
            enable = true;
            host = "0.0.0.0";
            port = 8090;
            openFirewall = true;
          };
        })
      ];
    };
  };
}
```

Then `sudo nixos-rebuild switch`. The service is hardened (`ProtectSystem = "strict"`,
`PrivateDevices`, `RestrictNamespaces`, no new privileges), runs as its own
`busypanel` user, and keeps the books in `/var/lib/busypanel` — a systemd
`StateDirectory` created mode 0700 on first start. It restarts on failure.

| Option | Default | Notes |
|---|---|---|
| `enable` | `false` | |
| `package` | this flake's package | |
| `host` / `port` | `"0.0.0.0"` / `8090` | |
| `stateDir` | `/var/lib/busypanel` | where the database and settings live |
| `user` / `group` | `busypanel` | created automatically |
| `environmentFile` | `null` | `Environment=` lines; overrides the UI |
| `authPasswordFile` | `null` | password kept out of the Nix store |
| `credentials` | `{}` | `ENV_VAR = path`, delivered via `LoadCredential` |
| `openFirewall` | `false` | opens only `port` |

Setting the password without putting it in the store:

```nix
services.busypanel.authPasswordFile = "/run/secrets/busypanel-password";
```

### Proving the service works

`nix flake check` boots a QEMU VM with the module enabled and asserts the real
thing, not just that the options evaluate: the unit starts, the state directory
exists as `busypanel` mode 0700, every screen renders, a client → video → invoice
round trip produces a `$550.00` invoice, and the books survive a service restart.

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

Five tables in one SQLite file:

| Table | What it holds |
|---|---|
| `client` | Name (unique), default video rate, contact details, archived flag |
| `video` | One row per video: client, date shot, title, rate, and `invoice_id` |
| `invoice` | Monthly or one-off, with its own `period_start`/`period_end`, dates, status |
| `invoice_line` | Materialised at invoice creation: description, qty, unit price |
| `expense` | Money out, with a `deductible` flag ("writeoff") and an optional client |

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

## No sales tax, no PDF library, no email

Deliberate: invoices leave the app through the print view. Adding tax means one
percentage applied in `billing.invoice_total`, `report.monthly_summary` and
`print.html`; a real PDF means a new dependency in `pyproject.toml` and `uv.lock`.
