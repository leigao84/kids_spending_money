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

## Configuration

| Env var   | Default                    | Purpose                        |
|-----------|----------------------------|--------------------------------|
| `APP_TZ`  | `America/Los_Angeles`      | Timezone for dates / weekdays  |
| `DB_PATH` | `/data/ledger.db`          | SQLite database location       |

On first run the app seeds two kids (Emma and Matthew) with the opening
balances it was built with — edit `init_db()` in `app.py` to change the
starting names/balances for a fresh install.
