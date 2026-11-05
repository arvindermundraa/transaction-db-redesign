# High-Volume Transaction Database Redesign

A banking ledger in PostgreSQL, rebuilt from a naive single-table design into a partitioned schema
with incremental balance sync, validation triggers and stored procedures. Both designs run on the
same 1M+ synthetic transactions so the speedup is measured, not estimated.

## Results

PostgreSQL 17.5, 1,050,000 transactions, 50,000 accounts, 37 monthly partitions, laptop-class
machine. Each run appends 10,000 new transactions and then syncs balances (5 runs, median).
Full output: [`results/benchmark.md`](results/benchmark.md).

| operation | legacy | redesign | change |
|---|---|---|---|
| balance sync after a 10,000-row batch | 581 ms | 130 ms | **77.7% faster** |
| 30-day account statement (200 random accounts) | 45.4 ms | 0.13 ms | **99.7% faster** |
| balances that differ between the two designs | | | **0 of 50,000** |

## What changed and why

| Problem in the legacy design | Redesign | File |
|---|---|---|
| Balance sync sums the entire ledger every run, so it slows as history grows | `ledger.sync_balances()` keeps a watermark (last synced `txn_id`) and folds in only newer rows, in batches | `sql/redesign/procedures.sql` |
| Statement queries scan every row | Composite index `(account_id, txn_ts DESC)` plus monthly range partitions, so a date-bounded query reads one partition's index | `sql/redesign/schema.sql` |
| No constraints: orphan rows, wrong signs, unknown types | Foreign key to accounts, CHECK on type and non-zero amount, trigger that checks sign vs. type | `schema.sql`, `procedures.sql` |
| Overdrafts only caught after the fact | `BEFORE INSERT` trigger checks synced balance + unsynced activity + overdraft limit, with a row lock on the account | `procedures.sql` |
| Transfers written as two separate inserts | `ledger.transfer()` writes both legs in one statement; a failed debit leaves nothing behind | `procedures.sql` |
| No way to check the incremental sync | `ledger.balance_drift()` recomputes from scratch and lists any account that disagrees | `procedures.sql` |

Partition management: `ledger.create_month_partitions(from, to)` creates one partition per month
plus a default partition for out-of-range dates.

## Run it

```bash
docker compose up -d                     # PostgreSQL 17 on localhost:5432
pip install -r requirements.txt
export DATABASE_URL=postgresql://postgres@localhost:5432/postgres
pytest -q                                # 14 correctness tests
python bench/benchmark.py --dsn $DATABASE_URL   # ~1 min; writes results/
```

CI (GitHub Actions) runs the tests and a 100k-row smoke benchmark against a PostgreSQL service
container on every push.

## Trade-offs

- Inserts pay for the trigger and the extra index. For bulk loads the benchmark disables row
  triggers (`session_replication_role = replica`), the same way a nightly import would.
- The primary key must include the partition key, so it is `(txn_id, txn_ts)`; a separate index on
  `txn_id` keeps watermark lookups fast.
- Balances are eventually consistent between syncs; the overdraft trigger includes unsynced rows so
  the business rule still holds in between.
- Written in PL/pgSQL (PostgreSQL). The same design maps to Oracle PL/SQL with interval partitioning.
