"""Benchmark the legacy schema against the redesign on identical data.

Steps
  1. Create both schemas and load the same N transactions across A accounts over 24 months.
  2. Run a full balance sync on both so they start equal.
  3. Repeat R times: append a batch of new transactions to both, then time
       legacy.sync_balances()   (recomputes every balance from every row)
       ledger.sync_balances()   (folds in only rows after the watermark)
  4. Time a 30-day account statement query on both for S random accounts.
  5. Check that both schemas hold identical balances (correctness, not just speed).

Usage:
  python bench/benchmark.py --dsn postgresql://postgres@localhost:5432/postgres \
      --transactions 1000000 --accounts 50000 --batch 10000 --runs 5
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
SQL_FILES = ["sql/legacy/schema.sql", "sql/legacy/sync.sql",
             "sql/redesign/schema.sql", "sql/redesign/procedures.sql"]
START = "2024-01-01"
MONTHS = 24

# Deterministic synthetic ledger: row i belongs to account (i * 7919) % A + 1, so every account
# gets activity; type and amount come from hashtext so both schemas receive identical rows.
GEN_TXNS = """
INSERT INTO {table} (account_id, amount, txn_type, txn_ts, description)
SELECT acct,
       CASE WHEN kind < 6 THEN round((50 + (h %% 2000))::numeric, 2)        -- deposit
            WHEN kind < 9 THEN -round((5 + (h %% 400))::numeric, 2)          -- withdrawal
            ELSE -2.50 END,                                                  -- fee
       CASE WHEN kind < 6 THEN 'DEPOSIT' WHEN kind < 9 THEN 'WITHDRAWAL' ELSE 'FEE' END,
       %(start)s::timestamptz + (i::double precision / %(n)s) * %(span)s::interval,
       'txn ' || i
  FROM (SELECT i,
               ((i::bigint * 7919) %% %(accounts)s) + 1 AS acct,
               abs(hashtext(i::text)) AS h,
               abs(hashtext('k' || i::text)) %% 10 AS kind
          FROM generate_series(%(lo)s, %(hi)s) AS i) s
"""


def run_sql_files(conn) -> None:
    for f in SQL_FILES:
        conn.execute((ROOT / f).read_text())


def load(conn, accounts: int, n: int) -> None:
    conn.execute("SET session_replication_role = replica")  # bulk load: skip row triggers
    conn.execute("CALL ledger.create_month_partitions(%s, %s)",
                 (START, f"{2024 + (MONTHS + 1) // 12}-12-01"))
    for table in ("legacy.accounts", "ledger.accounts"):
        conn.execute(f"INSERT INTO {table} (account_id, holder_name) "
                     "SELECT g, 'holder ' || g FROM generate_series(1, %s) g", (accounts,))
    for table in ("legacy.transactions", "ledger.transactions"):
        conn.execute(GEN_TXNS.format(table=table),
                     {"start": START, "n": n, "span": f"{MONTHS} months", "accounts": accounts,
                      "lo": 1, "hi": n})
    conn.execute("SET session_replication_role = origin")
    conn.execute("ANALYZE")


def append_batch(conn, accounts: int, lo: int, size: int) -> None:
    """New activity dated after the historical load (the 'day' since the last sync)."""
    conn.execute("SET session_replication_role = replica")
    for table in ("legacy.transactions", "ledger.transactions"):
        conn.execute(GEN_TXNS.format(table=table),
                     {"start": f"{2024 + MONTHS // 12}-01-01", "n": lo + size,
                      "span": "1 day", "accounts": accounts, "lo": lo, "hi": lo + size - 1})
    conn.execute("SET session_replication_role = origin")


def timed(conn, sql: str, params=None) -> float:
    start = time.perf_counter()
    conn.execute(sql, params)
    return (time.perf_counter() - start) * 1000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://postgres@localhost:5432/postgres")
    ap.add_argument("--transactions", type=int, default=1_000_000)
    ap.add_argument("--accounts", type=int, default=50_000)
    ap.add_argument("--batch", type=int, default=10_000)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--statements", type=int, default=200)
    ap.add_argument("--out", default=str(ROOT / "results"))
    args = ap.parse_args()

    with psycopg.connect(args.dsn, autocommit=True) as conn:
        t0 = time.perf_counter()
        run_sql_files(conn)
        load(conn, args.accounts, args.transactions)
        load_s = time.perf_counter() - t0
        conn.execute("CALL legacy.sync_balances()")
        conn.execute("CALL ledger.sync_balances()")

        legacy_ms, ledger_ms = [], []
        next_id = args.transactions + 1
        for _ in range(args.runs):
            append_batch(conn, args.accounts, next_id, args.batch)
            next_id += args.batch
            conn.execute("ANALYZE legacy.transactions")
            conn.execute("ANALYZE ledger.transactions")
            legacy_ms.append(timed(conn, "CALL legacy.sync_balances()"))
            ledger_ms.append(timed(conn, "CALL ledger.sync_balances()"))

        rng = random.Random(7)
        accts = [rng.randint(1, args.accounts) for _ in range(args.statements)]
        window = ("2025-06-01", "2025-07-01")
        q = ("SELECT txn_id, txn_ts, txn_type, amount FROM {t} WHERE account_id = %s "
             "AND txn_ts >= %s AND txn_ts < %s ORDER BY txn_ts DESC")
        legacy_stmt = [timed(conn, q.format(t="legacy.transactions"), (a, *window)) for a in accts]
        ledger_stmt = [timed(conn, q.format(t="ledger.transactions"), (a, *window)) for a in accts]

        mismatched = conn.execute("""
            SELECT count(*) FROM legacy.accounts l JOIN ledger.accounts n USING (account_id)
             WHERE l.balance <> n.balance""").fetchone()[0]
        drift = conn.execute("SELECT count(*) FROM ledger.balance_drift()").fetchone()[0]
        partitions = conn.execute("""SELECT count(*) FROM pg_inherits
             WHERE inhparent = 'ledger.transactions'::regclass""").fetchone()[0]
        total_rows = conn.execute("SELECT count(*) FROM ledger.transactions").fetchone()[0]
        version = conn.execute("SHOW server_version").fetchone()[0]

    med = statistics.median
    result = {
        "postgres": version, "transactions_after_runs": total_rows, "accounts": args.accounts,
        "batch_size": args.batch, "runs": args.runs, "partitions": partitions,
        "load_seconds": round(load_s, 1),
        "sync_ms": {"legacy_median": round(med(legacy_ms), 1), "redesign_median": round(med(ledger_ms), 1),
                    "legacy_all": [round(x, 1) for x in legacy_ms],
                    "redesign_all": [round(x, 1) for x in ledger_ms]},
        "sync_latency_reduction_pct": round(100 * (1 - med(ledger_ms) / med(legacy_ms)), 1),
        "statement_ms": {"legacy_median": round(med(legacy_stmt), 2),
                         "redesign_median": round(med(ledger_stmt), 2)},
        "statement_latency_reduction_pct": round(100 * (1 - med(ledger_stmt) / med(legacy_stmt)), 1),
        "balance_mismatches": mismatched, "reconciliation_drift_rows": drift,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "benchmark.json").write_text(json.dumps(result, indent=2))
    md = f"""# Benchmark results

PostgreSQL {version}; {total_rows:,} transactions across {args.accounts:,} accounts;
{partitions} partitions; each run appends {args.batch:,} new transactions, then syncs balances.

| operation | legacy (median) | redesign (median) | reduction |
|---|---|---|---|
| balance sync after a {args.batch:,}-row batch | {med(legacy_ms):,.1f} ms | {med(ledger_ms):,.1f} ms | {result['sync_latency_reduction_pct']}% |
| 30-day account statement ({args.statements} accounts) | {med(legacy_stmt):,.2f} ms | {med(ledger_stmt):,.2f} ms | {result['statement_latency_reduction_pct']}% |

Correctness: {mismatched} accounts with different balances between the two schemas;
{drift} rows reported by `ledger.balance_drift()`.
"""
    (out / "benchmark.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
