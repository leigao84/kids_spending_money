# Kids Spending Money — Allowance Ledger

A lightweight web app to keep track of allowances / spending money for two kids.
Built for a home server: one small Python (Flask) + SQLite container, no build
step, no external services.

## Features

- Home screen with both kids' balances side by side, plus recent activity
- Per-kid ledger with full history and a running balance
- Add / edit / delete entries (money in / money out, note, date)
- Optional weekly allowance per kid (amount + weekday) that posts automatically
  when any page is opened on/after the scheduled day
- Password gate: viewing is open, but making changes requires the admin
  password (30-day login per device)
- Append-only change log at `/log` — every add, edit, delete, settings change,
  auto-posted allowance, and password change, with timestamps

## Run

```sh
docker build -t kids-spending-money .
docker run -d --name allowance-ledger --restart unless-stopped \
  -p 8088:8088 -v "$PWD/data:/data" kids-spending-money
```

or with compose:

```sh
docker compose up -d --build
```

Then open http://localhost:8088.

Data lives in `data/ledger.db` (SQLite). Back it up by copying that file.

## Password

Changes are locked until a password is set. Set or reset it from the server
terminal (it prompts privately, nothing is echoed):

```sh
docker exec -it allowance-ledger python app.py set-password
```

After that, the password can also be changed inside the app (Password link,
once logged in).

## Backups

The whole ledger is one SQLite file (`data/ledger.db`), so a backup is just a
consistent copy of it. Take one with SQLite's backup API (safe while the app
is running):

```sh
docker run --rm -v "$PWD":/al python:3.12-slim python3 -c \
  "import sqlite3; s=sqlite3.connect('/al/data/ledger.db'); d=sqlite3.connect('/al/backups/ledger-backup.db'); s.backup(d)"
```

In production this runs nightly: a scheduled job snapshots the DB, uploads
the snapshot to a Google Drive folder (`AllowanceLedger`), and prunes copies
older than 30 days (always keeping the newest 7). Restore = stop the
container, drop a snapshot in as `data/ledger.db`, start it again.

## Configuration

| Env var   | Default                    | Purpose                        |
|-----------|----------------------------|--------------------------------|
| `APP_TZ`  | `America/Los_Angeles`      | Timezone for dates / weekdays  |
| `DB_PATH` | `/data/ledger.db`          | SQLite database location       |

On first run the app seeds two kids (Emma and Matthew) with the opening
balances it was built with — edit `init_db()` in `app.py` to change the
starting names/balances for a fresh install.
