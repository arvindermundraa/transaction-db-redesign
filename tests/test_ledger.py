"""Correctness tests for the redesigned ledger. Needs a PostgreSQL server:
    export DATABASE_URL=postgresql://postgres@localhost:5432/postgres
"""
import os
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
DSN = os.getenv("DATABASE_URL", "postgresql://postgres@localhost:5432/postgres")


@pytest.fixture()
def db():
    with psycopg.connect(DSN, autocommit=True) as conn:
        for f in ["sql/legacy/schema.sql", "sql/legacy/sync.sql",
                  "sql/redesign/schema.sql", "sql/redesign/procedures.sql"]:
            conn.execute((ROOT / f).read_text())
        conn.execute("CALL ledger.create_month_partitions('2025-01-01', '2025-12-01')")
        conn.execute("INSERT INTO ledger.accounts (account_id, holder_name, overdraft_limit) "
                     "VALUES (1, 'alice', 0), (2, 'bob', 100)")
        yield conn


def post(db, acct, amount, kind, ts="2025-03-15"):
    db.execute("INSERT INTO ledger.transactions (account_id, amount, txn_type, txn_ts) "
               "VALUES (%s, %s, %s, %s)", (acct, amount, kind, ts))


def balance(db, acct):
    return db.execute("SELECT balance FROM ledger.accounts WHERE account_id = %s", (acct,)).fetchone()[0]


def test_rows_land_in_monthly_partition(db):
    post(db, 1, 100, "DEPOSIT", "2025-03-15")
    part = db.execute("SELECT tableoid::regclass::text FROM ledger.transactions").fetchone()[0]
    assert part == "ledger.transactions_2025_03"


def test_incremental_sync_matches_full_recompute(db):
    post(db, 1, 500, "DEPOSIT")
    post(db, 1, -120.25, "WITHDRAWAL")
    db.execute("CALL ledger.sync_balances()")
    post(db, 2, 40, "DEPOSIT")
    post(db, 1, -2.5, "FEE")
    db.execute("CALL ledger.sync_balances(batch_size => 1)")  # force several batches
    assert balance(db, 1) == pytest.approx(377.25)
    assert balance(db, 2) == pytest.approx(40)
    assert db.execute("SELECT count(*) FROM ledger.balance_drift()").fetchone()[0] == 0


def test_sync_is_idempotent(db):
    post(db, 1, 100, "DEPOSIT")
    db.execute("CALL ledger.sync_balances()")
    db.execute("CALL ledger.sync_balances()")
    assert balance(db, 1) == 100


def test_overdraft_is_rejected(db):
    post(db, 1, 50, "DEPOSIT")
    with pytest.raises(psycopg.errors.CheckViolation, match="insufficient funds"):
        post(db, 1, -60, "WITHDRAWAL")


def test_overdraft_limit_allows_negative_balance_up_to_limit(db):
    post(db, 2, -100, "WITHDRAWAL")
    with pytest.raises(psycopg.errors.CheckViolation):
        post(db, 2, -0.01, "FEE")


def test_unsynced_activity_counts_toward_available_funds(db):
    post(db, 1, 100, "DEPOSIT")       # not synced yet
    post(db, 1, -100, "WITHDRAWAL")   # allowed: uses unsynced deposit
    with pytest.raises(psycopg.errors.CheckViolation):
        post(db, 1, -1, "WITHDRAWAL")


@pytest.mark.parametrize("amount,kind", [(-10, "DEPOSIT"), (10, "WITHDRAWAL"), (5, "FEE")])
def test_sign_must_match_type(db, amount, kind):
    with pytest.raises(psycopg.errors.CheckViolation):
        post(db, 2, amount, kind)


def test_unknown_account_rejected(db):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        post(db, 999, 10, "DEPOSIT")


def test_transfer_is_atomic(db):
    post(db, 1, 30, "DEPOSIT")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("CALL ledger.transfer(1, 2, 50)")
    # neither leg was written
    assert db.execute("SELECT count(*) FROM ledger.transactions").fetchone()[0] == 1
    db.execute("CALL ledger.transfer(1, 2, 30, 'rent')")
    db.execute("CALL ledger.sync_balances()")
    assert balance(db, 1) == 0 and balance(db, 2) == 30


def test_statement_uses_partition_pruning(db):
    plan = "\n".join(r[0] for r in db.execute(
        "EXPLAIN SELECT * FROM ledger.transactions WHERE account_id = 1 "
        "AND txn_ts >= '2025-03-01' AND txn_ts < '2025-04-01'"))
    assert "transactions_2025_03" in plan
    assert "transactions_2025_06" not in plan


def test_statement_function_orders_newest_first(db):
    post(db, 1, 10, "DEPOSIT", "2025-03-01")
    post(db, 1, 20, "DEPOSIT", "2025-03-20")
    rows = db.execute("SELECT amount FROM ledger.statement(1, '2025-03-01', '2025-04-01')").fetchall()
    assert [r[0] for r in rows] == [20, 10]


def test_legacy_and_redesign_agree(db):
    db.execute("INSERT INTO legacy.accounts (account_id, holder_name) VALUES (1, 'alice'), (2, 'bob')")
    for acct, amt, kind in [(1, 100, "DEPOSIT"), (1, -40, "WITHDRAWAL"), (2, 15, "DEPOSIT")]:
        post(db, acct, amt, kind)
        db.execute("INSERT INTO legacy.transactions (account_id, amount, txn_type, txn_ts) "
                   "VALUES (%s, %s, %s, '2025-03-15')", (acct, amt, kind))
    db.execute("CALL legacy.sync_balances()")
    db.execute("CALL ledger.sync_balances()")
    diff = db.execute("""SELECT count(*) FROM legacy.accounts l JOIN ledger.accounts n USING (account_id)
                          WHERE l.balance <> n.balance""").fetchone()[0]
    assert diff == 0
