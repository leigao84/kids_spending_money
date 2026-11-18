# Kinboard pocket-money sync

The Allowance Ledger is the single source of truth for the kids' pocket
money. `sync_pocket_money.py` mirrors its entries into Kinboard's
`pocket_money_transactions` and keeps `pocket_money_accounts.balance_cents`
matched, so the family board widget always shows the real numbers.

- Runs as a one-shot container (`ledger-sync:local`) on the Beelink,
  scheduled every 5 minutes by Dagu (`ledger-kinboard-sync`).
- Remembers what it mirrored in a state DB (`~/allowance-ledger/sync-state/`)
  so edits and deletes in the ledger propagate to Kinboard.
- Kinboard is display-only for pocket money: transactions on the kids'
  accounts that did not come from the ledger are removed.
- Ledger kid 1 (Emma) -> Kinboard account 299a0f01..., kid 2 (Matthew)
  -> e1831a10... (mapped by person name at setup time).
