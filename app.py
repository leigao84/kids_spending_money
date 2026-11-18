#!/usr/bin/env python3
"""Lightweight allowance ledger for Emma and Matthew.

Single-file Flask app + SQLite. Balances are integer cents.

Viewing is open (home LAN). Making changes -- adding, editing or
deleting entries, changing settings -- requires the admin password.
Every change is written to an audit log (see /log).

Set or reset the password from the server terminal:
    docker exec -it allowance-ledger python app.py set-password

Weekly allowances (when configured in Settings) post automatically
the first time any page is opened on/after each scheduled weekday.
"""
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, abort, g, redirect, render_template_string, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

TZ = ZoneInfo(os.environ.get("APP_TZ", "America/Los_Angeles"))
DB_PATH = os.environ.get("DB_PATH", "/data/ledger.db")
SECRET_PATH = os.path.join(os.path.dirname(DB_PATH), ".flask_secret")
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

app = Flask(__name__)
app.permanent_session_lifetime = timedelta(days=30)


def _load_secret():
    try:
        with open(SECRET_PATH) as f:
            return f.read().strip()
    except OSError:
        s = secrets.token_hex(32)
        try:
            with open(SECRET_PATH, "w") as f:
                f.write(s)
        except OSError:
            pass
        return s


app.secret_key = _load_secret()


def today():
    return datetime.now(TZ).date()


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS kids(
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL UNIQUE,
          weekly_allowance_cents INTEGER NOT NULL DEFAULT 0,
          allowance_weekday INTEGER,
          allowance_since TEXT
        );
        CREATE TABLE IF NOT EXISTS entries(
          id INTEGER PRIMARY KEY,
          kid_id INTEGER NOT NULL REFERENCES kids(id),
          amount_cents INTEGER NOT NULL,
          note TEXT NOT NULL DEFAULT '',
          entry_date TEXT NOT NULL,
          auto INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings(
          key TEXT PRIMARY KEY,
          value TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log(
          id INTEGER PRIMARY KEY,
          ts TEXT NOT NULL,
          action TEXT NOT NULL,
          details TEXT NOT NULL
        );
        """
    )
    if c.execute("SELECT count(*) FROM kids").fetchone()[0] == 0:
        for name, cents in (("Emma", 15600), ("Matthew", 15047)):
            cur = c.execute("INSERT INTO kids(name) VALUES(?)", (name,))
            c.execute(
                "INSERT INTO entries(kid_id, amount_cents, note, entry_date, created_at)"
                " VALUES(?,?,?,?,?)",
                (cur.lastrowid, cents, "Opening balance (carried over from Kinboard)",
                 "2026-11-17", now_iso()),
            )
        c.execute(
            "INSERT INTO audit_log(ts, action, details) VALUES(?,?,?)",
            (now_iso(), "system",
             "Ledger created. Opening balances carried over from Kinboard: "
             "Emma $156.00, Matthew $150.47."),
        )
    c.commit()
    c.close()


# ---------- settings / auth ----------

def get_setting(key):
    row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key, value):
    db().execute(
        "INSERT INTO settings(key, value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    db().commit()


def password_is_set():
    return bool(get_setting("admin_hash"))


def is_admin():
    return bool(session.get("admin")) and password_is_set()


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not password_is_set():
            return redirect(url_for("login", setup=1))
        if not is_admin():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def audit(action, details):
    db().execute(
        "INSERT INTO audit_log(ts, action, details) VALUES(?,?,?)",
        (now_iso(), action, details),
    )
    db().commit()


# ---------- money helpers ----------

def fmt(cents):
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"


def fmt_signed(cents):
    return ("+" + fmt(cents)) if cents >= 0 else fmt(cents)


def balance(kid_id):
    row = db().execute(
        "SELECT coalesce(sum(amount_cents),0) AS b FROM entries WHERE kid_id=?", (kid_id,)
    ).fetchone()
    return row["b"]


def kid_name(kid_id):
    row = db().execute("SELECT name FROM kids WHERE id=?", (kid_id,)).fetchone()
    return row["name"] if row else "?"


def parse_amount(raw, direction):
    try:
        cents = int((Decimal(raw) * 100).to_integral_value())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if cents <= 0:
        return None
    return cents if direction == "in" else -cents


def post_due_allowances():
    """Insert any scheduled weekly allowances that are due (idempotent)."""
    c = db()
    t = today()
    kids = c.execute(
        "SELECT * FROM kids WHERE weekly_allowance_cents > 0 AND allowance_weekday IS NOT NULL"
    ).fetchall()
    for kid in kids:
        delta = (t.weekday() - kid["allowance_weekday"]) % 7
        d = t - timedelta(days=delta)  # most recent scheduled day
        due = []
        while kid["allowance_since"] is None or d.isoformat() >= kid["allowance_since"]:
            due.append(d)
            if len(due) >= 52:
                break
            d -= timedelta(days=7)
        for d in due:
            exists = c.execute(
                "SELECT 1 FROM entries WHERE kid_id=? AND auto=1 AND entry_date=?",
                (kid["id"], d.isoformat()),
            ).fetchone()
            if not exists:
                c.execute(
                    "INSERT INTO entries(kid_id, amount_cents, note, entry_date, auto, created_at)"
                    " VALUES(?,?,?,?,1,?)",
                    (kid["id"], kid["weekly_allowance_cents"], "Weekly allowance",
                     d.isoformat(), now_iso()),
                )
                audit("auto",
                      f"Weekly allowance {fmt_signed(kid['weekly_allowance_cents'])} "
                      f"posted to {kid['name']} for {d.isoformat()}.")
    c.commit()


@app.before_request
def _before():
    if request.endpoint != "static":
        post_due_allowances()


# ---------- templates ----------

BASE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Allowance Ledger</title>
<style>
  :root { --accent:#6d5cff; --ink:#1d1d28; --muted:#6b6b7a; --line:#e7e7ef;
          --in:#178a4c; --out:#cf3f3f; --card:#ffffff; --bg:#f5f5fa; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system, "Segoe UI", Roboto, sans-serif;
         background:var(--bg); color:var(--ink); }
  header { max-width:860px; margin:0 auto; padding:20px 16px 4px;
           display:flex; justify-content:space-between; align-items:baseline; }
  header h1 { font-size:22px; margin:0; }
  header nav a { color:var(--accent); text-decoration:none; font-weight:600; margin-left:14px; }
  main { max-width:860px; margin:0 auto; padding:12px 16px 40px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:18px; }
  .card .name { font-size:16px; font-weight:700; }
  .card .bal { font-size:36px; font-weight:800; margin:6px 0 2px; }
  .card .sub { color:var(--muted); font-size:13px; }
  .card a.open { display:inline-block; margin-top:10px; color:var(--accent);
                 text-decoration:none; font-weight:600; }
  h2 { font-size:16px; margin:24px 0 8px; }
  .entry { background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:10px 14px; margin-bottom:8px; display:flex; gap:10px; align-items:center; }
  .entry .grow { flex:1; min-width:0; }
  .entry .note { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .entry .meta { color:var(--muted); font-size:12.5px; }
  .amt { font-weight:800; white-space:nowrap; }
  .amt.in { color:var(--in); } .amt.out { color:var(--out); }
  .run { color:var(--muted); font-size:12.5px; white-space:nowrap; }
  .iconbtn { color:var(--muted); text-decoration:none; font-size:15px; padding:2px 4px; }
  form.add, form.settings, form.login { background:var(--card); border:1px solid var(--line);
           border-radius:16px; padding:16px; display:grid; gap:10px; }
  label { font-size:13px; font-weight:600; color:var(--muted); display:grid; gap:4px; }
  input, select { font-size:16px; padding:9px 10px; border:1px solid var(--line);
           border-radius:10px; background:#fff; color:var(--ink); width:100%; }
  .row { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  .seg { display:flex; gap:8px; }
  .seg label { flex:1; }
  .seg input { display:none; }
  .seg span { display:block; text-align:center; padding:9px; border:1px solid var(--line);
              border-radius:10px; font-size:15px; color:var(--ink); cursor:pointer; }
  .seg input:checked + span.in { background:#e5f6ec; border-color:var(--in); color:var(--in); font-weight:700; }
  .seg input:checked + span.out { background:#fdecec; border-color:var(--out); color:var(--out); font-weight:700; }
  button { background:var(--accent); color:#fff; border:0; border-radius:10px;
           padding:11px 16px; font-size:16px; font-weight:700; cursor:pointer; }
  .lockbox { background:var(--card); border:1px dashed var(--line); border-radius:16px;
             padding:18px; text-align:center; color:var(--muted); }
  .lockbox a { color:var(--accent); font-weight:700; text-decoration:none; }
  .err { background:#fdecec; border:1px solid #f2b8b8; color:var(--out);
         border-radius:10px; padding:10px 14px; }
  .logrow { background:var(--card); border:1px solid var(--line); border-radius:12px;
            padding:10px 14px; margin-bottom:8px; }
  .logrow .when { color:var(--muted); font-size:12.5px; }
  .chip { display:inline-block; font-size:11px; font-weight:700; padding:2px 8px;
          border-radius:99px; background:#eceaff; color:var(--accent);
          text-transform:uppercase; letter-spacing:.04em; margin-right:6px; }
  a.back { color:var(--accent); text-decoration:none; font-weight:600; }
</style></head>
<body>
<header><h1>💰 Allowance Ledger</h1>
<nav><a href="{{ url_for('index') }}">Home</a><a href="{{ url_for('log_page') }}">Log</a><a href="{{ url_for('settings') }}">Settings</a>{% if admin %}<a href="{{ url_for('change_password') }}">Password</a><a href="{{ url_for('logout') }}">Log out</a>{% else %}<a href="{{ url_for('login') }}">Log in</a>{% endif %}</nav>
</header>
<main>{% block body %}{% endblock %}</main>
</body></html>"""

ADD_FORM = """
{% if admin %}
<form class="add" method="post" action="{{ url_for('add') }}">
  {% if fixed_kid %}
    <input type="hidden" name="kid_id" value="{{ fixed_kid }}">
  {% else %}
    <label>Kid
      <select name="kid_id">{% for k in kids %}<option value="{{ k.id }}">{{ k.name }}</option>{% endfor %}</select>
    </label>
  {% endif %}
  <div class="seg">
    <label><input type="radio" name="direction" value="in" checked><span class="in">＋ Money in</span></label>
    <label><input type="radio" name="direction" value="out"><span class="out">－ Money out</span></label>
  </div>
  <div class="row">
    <label>Amount $<input name="amount" inputmode="decimal" placeholder="5.00" required></label>
    <label>Date<input type="date" name="entry_date" value="{{ today }}"></label>
  </div>
  <label>Note<input name="note" placeholder="e.g. Mowed the lawn, Robux, birthday money"></label>
  <button type="submit">Add entry</button>
</form>
{% else %}
<div class="lockbox">🔒 <a href="{{ url_for('login') }}">Log in</a> to add or change entries.</div>
{% endif %}"""

INDEX = BASE.replace("{% block body %}{% endblock %}", """
<div class="cards">
{% for k in kids %}
  <div class="card">
    <div class="name">{{ k.name }}</div>
    <div class="bal">{{ fmt(k.balance) }}</div>
    <div class="sub">{% if k.weekly_allowance_cents %}Allowance: {{ fmt(k.weekly_allowance_cents) }} every {{ weekdays[k.allowance_weekday] }}{% else %}No weekly allowance set{% endif %}</div>
    <a class="open" href="{{ url_for('kid_page', kid_id=k.id) }}">Open ledger →</a>
  </div>
{% endfor %}
</div>
<h2>Add an entry</h2>
""" + ADD_FORM + """
<h2>Recent activity</h2>
{% for e in recent %}
  <div class="entry">
    <div class="grow">
      <div class="note">{{ e.note or ('Money in' if e.amount_cents >= 0 else 'Money out') }}</div>
      <div class="meta">{{ e.kid_name }} · {{ e.entry_date }}{% if e.auto %} · auto{% endif %}</div>
    </div>
    <div class="amt {{ 'in' if e.amount_cents >= 0 else 'out' }}">{{ fmt(e.amount_cents) }}</div>
  </div>
{% else %}
  <p class="sub">No entries yet.</p>
{% endfor %}
""")

KID = BASE.replace("{% block body %}{% endblock %}", """
<p><a class="back" href="{{ url_for('index') }}">← All kids</a></p>
<div class="card">
  <div class="name">{{ kid.name }}</div>
  <div class="bal">{{ fmt(bal) }}</div>
  <div class="sub">{% if kid.weekly_allowance_cents %}Allowance: {{ fmt(kid.weekly_allowance_cents) }} every {{ weekdays[kid.allowance_weekday] }}{% else %}No weekly allowance set{% endif %}</div>
</div>
<h2>Add an entry</h2>
""" + ADD_FORM + """
<h2>History</h2>
{% for e in entries %}
  <div class="entry">
    <div class="grow">
      <div class="note">{{ e.note or ('Money in' if e.amount_cents >= 0 else 'Money out') }}</div>
      <div class="meta">{{ e.entry_date }}{% if e.auto %} · auto allowance{% endif %}</div>
    </div>
    <div class="run">{{ fmt(e.running) }}</div>
    <div class="amt {{ 'in' if e.amount_cents >= 0 else 'out' }}">{{ fmt(e.amount_cents) }}</div>
    {% if admin %}
    <a class="iconbtn" href="{{ url_for('edit_entry', eid=e.id) }}" title="Edit">✏️</a>
    <form method="post" action="{{ url_for('delete_entry', eid=e.id) }}" onsubmit="return confirm('Delete this entry?')">
      <button class="iconbtn" style="background:none;border:0;cursor:pointer" title="Delete">✕</button>
    </form>
    {% endif %}
  </div>
{% else %}
  <p class="sub">No entries yet.</p>
{% endfor %}
""")

EDIT = BASE.replace("{% block body %}{% endblock %}", """
<p><a class="back" href="{{ url_for('kid_page', kid_id=entry.kid_id) }}">← Back to ledger</a></p>
<h2>Edit entry — {{ kid_name }}</h2>
<form class="add" method="post">
  <div class="seg">
    <label><input type="radio" name="direction" value="in" {% if entry.amount_cents >= 0 %}checked{% endif %}><span class="in">＋ Money in</span></label>
    <label><input type="radio" name="direction" value="out" {% if entry.amount_cents < 0 %}checked{% endif %}><span class="out">－ Money out</span></label>
  </div>
  <div class="row">
    <label>Amount $<input name="amount" inputmode="decimal" value="{{ '%.2f' % (entry.amount_cents|abs / 100) }}" required></label>
    <label>Date<input type="date" name="entry_date" value="{{ entry.entry_date }}"></label>
  </div>
  <label>Note<input name="note" value="{{ entry.note }}"></label>
  <button type="submit">Save changes</button>
</form>
""")

SETTINGS = BASE.replace("{% block body %}{% endblock %}", """
<h2>Weekly allowance</h2>
<p style="color:var(--muted)">Set an amount and a weekday per kid and the app posts it automatically.
Changing either restarts the schedule from today, so nothing back-fills.</p>
<form class="settings" method="post">
{% for k in kids %}
  <div class="row">
    <label>{{ k.name }} — amount $
      <input name="amt_{{ k.id }}" inputmode="decimal"
             value="{{ ('%.2f' % (k.weekly_allowance_cents / 100)) if k.weekly_allowance_cents else '' }}"
             placeholder="0.00">
    </label>
    <label>Weekday
      <select name="day_{{ k.id }}">
        <option value="">— off —</option>
        {% for d in weekdays %}
          <option value="{{ loop.index0 }}" {% if k.allowance_weekday == loop.index0 %}selected{% endif %}>{{ d }}</option>
        {% endfor %}
      </select>
    </label>
  </div>
{% endfor %}
  <button type="submit">Save settings</button>
</form>
""")

LOGIN = BASE.replace("{% block body %}{% endblock %}", """
<h2>Log in to make changes</h2>
{% if setup_needed %}
<div class="err">No password has been set yet, so changes are locked. Set one from the
server terminal:<br><code>docker exec -it allowance-ledger python app.py set-password</code></div>
{% endif %}
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<form class="login" method="post">
  <label>Password<input type="password" name="password" autofocus required></label>
  <button type="submit">Log in</button>
</form>
<p style="color:var(--muted)">Viewing balances and history doesn't need a password — only changing things does.
A login lasts 30 days on this device.</p>
""")

CHANGE_PW = BASE.replace("{% block body %}{% endblock %}", """
<h2>Change password</h2>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
{% if done %}<div class="err" style="background:#e5f6ec;border-color:#a8dcbb;color:var(--in)">Password updated.</div>{% endif %}
<form class="login" method="post">
  <label>Current password<input type="password" name="current" required></label>
  <label>New password<input type="password" name="new" required></label>
  <label>New password again<input type="password" name="confirm" required></label>
  <button type="submit">Update password</button>
</form>
""")

LOG = BASE.replace("{% block body %}{% endblock %}", """
<h2>Change log</h2>
<p style="color:var(--muted)">Every change to the ledger, newest first. This log can't be edited from the app.</p>
{% for r in rows %}
  <div class="logrow">
    <div><span class="chip">{{ r.action }}</span>{{ r.details }}</div>
    <div class="when">{{ r.ts[:16].replace('T', ' ') }}</div>
  </div>
{% else %}
  <p>No changes logged yet.</p>
{% endfor %}
""")


# ---------- routes ----------

@app.route("/")
def index():
    c = db()
    kids = [dict(r) for r in c.execute("SELECT * FROM kids ORDER BY name").fetchall()]
    for k in kids:
        k["balance"] = balance(k["id"])
    recent = c.execute(
        "SELECT e.*, k.name AS kid_name FROM entries e JOIN kids k ON k.id = e.kid_id"
        " ORDER BY e.entry_date DESC, e.id DESC LIMIT 12"
    ).fetchall()
    return render_template_string(
        INDEX, kids=kids, recent=recent, fmt=fmt, weekdays=WEEKDAYS,
        today=today().isoformat(), fixed_kid=None, admin=is_admin(),
    )


@app.route("/kid/<int:kid_id>")
def kid_page(kid_id):
    c = db()
    kid = c.execute("SELECT * FROM kids WHERE id=?", (kid_id,)).fetchone()
    if kid is None:
        abort(404)
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM entries WHERE kid_id=? ORDER BY entry_date, id", (kid_id,)
    ).fetchall()]
    run = 0
    for r in rows:
        run += r["amount_cents"]
        r["running"] = run
    rows.reverse()
    return render_template_string(
        KID, kid=kid, bal=run, entries=rows, fmt=fmt, weekdays=WEEKDAYS,
        today=today().isoformat(), fixed_kid=kid_id, kids=[kid], admin=is_admin(),
    )


@app.route("/add", methods=["POST"])
@admin_required
def add():
    kid_id = request.form.get("kid_id", type=int)
    cents = parse_amount(request.form.get("amount", ""), request.form.get("direction", "in"))
    entry_date = request.form.get("entry_date") or today().isoformat()
    note = (request.form.get("note") or "").strip()
    if kid_id is None or cents is None:
        return redirect(request.referrer or url_for("index"))
    db().execute(
        "INSERT INTO entries(kid_id, amount_cents, note, entry_date, created_at) VALUES(?,?,?,?,?)",
        (kid_id, cents, note, entry_date, now_iso()),
    )
    db().commit()
    audit("add",
          f"{fmt_signed(cents)} to {kid_name(kid_id)}"
          + (f" — “{note}”" if note else "")
          + f" (dated {entry_date}). New balance {fmt(balance(kid_id))}.")
    return redirect(url_for("kid_page", kid_id=kid_id))


@app.route("/entry/<int:eid>/edit", methods=["GET", "POST"])
@admin_required
def edit_entry(eid):
    c = db()
    entry = c.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
    if entry is None:
        abort(404)
    if request.method == "POST":
        cents = parse_amount(request.form.get("amount", ""), request.form.get("direction", "in"))
        if cents is not None:
            new_note = (request.form.get("note") or "").strip()
            new_date = request.form.get("entry_date") or entry["entry_date"]
            c.execute(
                "UPDATE entries SET amount_cents=?, note=?, entry_date=? WHERE id=?",
                (cents, new_note, new_date, eid),
            )
            c.commit()
            audit("edit",
                  f"Entry for {kid_name(entry['kid_id'])} changed: "
                  f"{fmt_signed(entry['amount_cents'])}"
                  + (f" “{entry['note']}”" if entry["note"] else "")
                  + f" ({entry['entry_date']}) → {fmt_signed(cents)}"
                  + (f" “{new_note}”" if new_note else "")
                  + f" ({new_date}). New balance {fmt(balance(entry['kid_id']))}.")
        return redirect(url_for("kid_page", kid_id=entry["kid_id"]))
    return render_template_string(
        EDIT, entry=entry, kid_name=kid_name(entry["kid_id"]), admin=True)


@app.route("/entry/<int:eid>/delete", methods=["POST"])
@admin_required
def delete_entry(eid):
    c = db()
    entry = c.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
    if entry is None:
        abort(404)
    c.execute("DELETE FROM entries WHERE id=?", (eid,))
    c.commit()
    audit("delete",
          f"Removed {fmt_signed(entry['amount_cents'])} from {kid_name(entry['kid_id'])}"
          + (f" — “{entry['note']}”" if entry["note"] else "")
          + f" (dated {entry['entry_date']}). New balance {fmt(balance(entry['kid_id']))}.")
    return redirect(url_for("kid_page", kid_id=entry["kid_id"]))


@app.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    c = db()
    if request.method == "POST":
        changes = []
        for kid in c.execute("SELECT * FROM kids").fetchall():
            raw = (request.form.get(f"amt_{kid['id']}") or "").strip()
            day_raw = request.form.get(f"day_{kid['id']}") or ""
            try:
                cents = int((Decimal(raw) * 100).to_integral_value()) if raw else 0
            except InvalidOperation:
                cents = kid["weekly_allowance_cents"]
            weekday = int(day_raw) if day_raw != "" else None
            if cents <= 0:
                weekday = None
                cents = 0
            changed = (cents != kid["weekly_allowance_cents"]
                       or weekday != kid["allowance_weekday"])
            if changed:
                old = (f"{fmt(kid['weekly_allowance_cents'])} every "
                       f"{WEEKDAYS[kid['allowance_weekday']]}"
                       if kid["weekly_allowance_cents"] else "off")
                new = (f"{fmt(cents)} every {WEEKDAYS[weekday]}" if cents else "off")
                changes.append(f"{kid['name']} allowance: {old} → {new}.")
            c.execute(
                "UPDATE kids SET weekly_allowance_cents=?, allowance_weekday=?,"
                " allowance_since=? WHERE id=?",
                (cents, weekday,
                 today().isoformat() if changed else kid["allowance_since"], kid["id"]),
            )
        c.commit()
        if changes:
            audit("settings", " ".join(changes))
        return redirect(url_for("settings"))
    kids = c.execute("SELECT * FROM kids ORDER BY name").fetchall()
    return render_template_string(SETTINGS, kids=kids, weekdays=WEEKDAYS, admin=True)


@app.route("/login", methods=["GET", "POST"])
def login():
    setup_needed = not password_is_set()
    error = None
    if request.method == "POST":
        if setup_needed:
            error = "No password is set yet — set one from the server terminal first."
        elif check_password_hash(get_setting("admin_hash"),
                                 request.form.get("password", "")):
            session["admin"] = True
            session.permanent = True
            nxt = request.args.get("next") or url_for("index")
            return redirect(nxt)
        else:
            error = "That password doesn't match."
    return render_template_string(
        LOGIN, error=error, setup_needed=setup_needed, admin=is_admin())


@app.route("/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


@app.route("/change-password", methods=["GET", "POST"])
@admin_required
def change_password():
    error = None
    done = False
    if request.method == "POST":
        cur = request.form.get("current", "")
        new = request.form.get("new", "")
        confirm = request.form.get("confirm", "")
        if not check_password_hash(get_setting("admin_hash"), cur):
            error = "Current password doesn't match."
        elif len(new) < 6:
            error = "New password should be at least 6 characters."
        elif new != confirm:
            error = "New passwords don't match each other."
        else:
            set_setting("admin_hash", generate_password_hash(new))
            audit("password", "Admin password changed from the app.")
            done = True
    return render_template_string(CHANGE_PW, error=error, done=done, admin=True)


@app.route("/log")
def log_page():
    rows = db().execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT 200").fetchall()
    return render_template_string(LOG, rows=rows, admin=is_admin())


init_db()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "set-password":
        import getpass
        pw = getpass.getpass("New ledger password: ")
        if len(pw) < 6:
            print("Too short — use at least 6 characters.")
            sys.exit(1)
        if pw != getpass.getpass("Repeat it: "):
            print("Passwords don't match — nothing changed.")
            sys.exit(1)
        c = sqlite3.connect(DB_PATH)
        c.execute(
            "INSERT INTO settings(key, value) VALUES('admin_hash',?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (generate_password_hash(pw),),
        )
        c.execute(
            "INSERT INTO audit_log(ts, action, details) VALUES(?,?,?)",
            (now_iso(), "password", "Admin password set or reset from the server terminal."),
        )
        c.commit()
        print("Password saved. Changes in the app now require it.")
    else:
        app.run(host="0.0.0.0", port=8088, threaded=True)
