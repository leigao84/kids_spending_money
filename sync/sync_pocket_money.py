#!/usr/bin/env python3
"""Mirror Allowance Ledger entries into Kinboard pocket money.

The Allowance Ledger (SQLite) is the single source of truth. This script
reconciles Kinboard's pocket_money_transactions and account balances to
match it, remembering what it mirrored in a small state DB so edits and
deletes propagate too. Kinboard-side pocket-money changes are not read
back; Kinboard transactions on the kids' accounts that are not mirrored
from the ledger are removed.
"""
import os
import sqlite3

import psycopg2

LEDGER_DB = os.environ.get("LEDGER_DB", "/ledger/ledger.db")
STATE_DB = os.environ.get("STATE_DB", "/state/sync_state.db")
KINBOARD_ENV = os.environ.get("KINBOARD_ENV", "/kinboard-env")

KID_TO_ACCOUNT = {
    1: "299a0f01-9999-4198-ad7d-ff2346f4d3a0",  # Emma
    2: "e1831a10-bd9c-4623-ae5e-0205b7299549",  # Matthew
}


def pg_password():
    for line in open(KINBOARD_ENV):
        if line.startswith("POSTGRES_PASSWORD="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("POSTGRES_PASSWORD not found in kinboard env")


def tx_type(amount_cents, auto):
    if auto:
        return "allowance"
    return "manual_deposit" if amount_cents >= 0 else "withdrawal"


def normalize(cur, txid, amount, note, auto, edate):
    cur.execute(
        """UPDATE pocket_money_transactions
           SET amount_cents = %s, note = %s, type = %s,
               created_at = (%s::timestamp AT TIME ZONE 'America/Los_Angeles')
           WHERE id = %s""",
        (amount, note, tx_type(amount, auto), f"{edate} 12:00:00", txid),
    )


def main():
    led = sqlite3.connect(f"file:{LEDGER_DB}?mode=ro", uri=True)
    entries = led.execute(
        "SELECT id, kid_id, amount_cents, note, entry_date, auto "
        "FROM entries ORDER BY id"
    ).fetchall()
    kids = dict(led.execute("SELECT id, name FROM kids"))
    led.close()

    os.makedirs(os.path.dirname(STATE_DB), exist_ok=True)
    st = sqlite3.connect(STATE_DB)
    st.execute(
        """CREATE TABLE IF NOT EXISTS mirror (
               ledger_entry_id INTEGER PRIMARY KEY,
               kinboard_tx_id TEXT NOT NULL,
               amount_cents INTEGER NOT NULL,
               note TEXT,
               entry_date TEXT NOT NULL)"""
    )

    pg = psycopg2.connect(
        host="kinboard-db",
        dbname="postgres",
        user="supabase_admin",
        password=pg_password(),
    )
    cur = pg.cursor()

    mirrored = {
        r[0]: r
        for r in st.execute(
            "SELECT ledger_entry_id, kinboard_tx_id, amount_cents, note, "
            "entry_date FROM mirror"
        )
    }
    live_ids = set()
    ins = upd = dele = adopt = 0

    existing = {}
    for account in KID_TO_ACCOUNT.values():
        cur.execute(
            "SELECT id, amount_cents FROM pocket_money_transactions "
            "WHERE account_id = %s",
            (account,),
        )
        existing[account] = cur.fetchall()
    used_tx = {r[1] for r in mirrored.values()}

    for eid, kid, amount, note, edate, auto in entries:
        live_ids.add(eid)
        account = KID_TO_ACCOUNT[kid]
        if eid in mirrored:
            _, txid, m_amount, m_note, m_date = mirrored[eid]
            if (m_amount, m_note, m_date) != (amount, note, edate):
                normalize(cur, txid, amount, note, auto, edate)
                st.execute(
                    "UPDATE mirror SET amount_cents = ?, note = ?, "
                    "entry_date = ? WHERE ledger_entry_id = ?",
                    (amount, note, edate, eid),
                )
                upd += 1
            continue
        # Adopt a pre-existing Kinboard transaction (e.g. the Nov 17
        # opening balances) when account + amount match, else insert.
        match = next(
            (
                t
                for t in existing.get(account, [])
                if t[1] == amount and t[0] not in used_tx
            ),
            None,
        )
        if match:
            txid = match[0]
            used_tx.add(txid)
            normalize(cur, txid, amount, note, auto, edate)
            adopt += 1
        else:
            cur.execute(
                """INSERT INTO pocket_money_transactions
                       (account_id, amount_cents, type, note, created_at)
                   VALUES (%s, %s, %s, %s,
                           (%s::timestamp AT TIME ZONE 'America/Los_Angeles'))
                   RETURNING id""",
                (account, amount, tx_type(amount, auto), note,
                 f"{edate} 12:00:00"),
            )
            txid = cur.fetchone()[0]
            ins += 1
        st.execute(
            "INSERT INTO mirror VALUES (?, ?, ?, ?, ?)",
            (eid, txid, amount, note, edate),
        )

    # Ledger deletions propagate.
    for eid, row in list(mirrored.items()):
        if eid not in live_ids:
            cur.execute(
                "DELETE FROM pocket_money_transactions WHERE id = %s",
                (row[1],),
            )
            st.execute(
                "DELETE FROM mirror WHERE ledger_entry_id = ?", (eid,)
            )
            dele += 1

    # Kinboard is display-only: drop transactions that are not mirrored.
    keep = {r[0] for r in st.execute("SELECT kinboard_tx_id FROM mirror")}
    for account in KID_TO_ACCOUNT.values():
        cur.execute(
            "SELECT id FROM pocket_money_transactions WHERE account_id = %s",
            (account,),
        )
        for (txid,) in cur.fetchall():
            if txid not in keep:
                cur.execute(
                    "DELETE FROM pocket_money_transactions WHERE id = %s",
                    (txid,),
                )
                dele += 1

    # Balances := ledger sums.
    sums = {}
    for _eid, kid, amount, _note, _edate, _auto in entries:
        sums[kid] = sums.get(kid, 0) + amount
    for kid, account in KID_TO_ACCOUNT.items():
        cur.execute(
            "UPDATE pocket_money_accounts SET balance_cents = %s "
            "WHERE id = %s",
            (sums.get(kid, 0), account),
        )

    pg.commit()
    st.commit()
    print(
        f"sync ok: adopted={adopt} inserted={ins} updated={upd} "
        f"deleted={dele} "
        + " ".join(
            f"{kids[k]}=${sums.get(k, 0) / 100:.2f}"
            for k in sorted(KID_TO_ACCOUNT)
        )
    )


if __name__ == "__main__":
    main()
