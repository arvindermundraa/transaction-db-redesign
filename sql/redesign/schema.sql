-- Redesigned schema.
--  * transactions are range-partitioned by month on txn_ts, so time-bounded queries and
--    archiving touch only the partitions they need
--  * composite index (account_id, txn_ts) serves statement queries without a full scan
--  * a sync watermark lets balance sync process only rows added since the last run
--  * constraints the legacy table lacked: foreign key, CHECK on type and non-zero amount
DROP SCHEMA IF EXISTS ledger CASCADE;
CREATE SCHEMA ledger;

CREATE TABLE ledger.accounts (
    account_id  BIGINT PRIMARY KEY,
    holder_name TEXT NOT NULL,
    balance     NUMERIC(14, 2) NOT NULL DEFAULT 0,
    overdraft_limit NUMERIC(14, 2) NOT NULL DEFAULT 0 CHECK (overdraft_limit >= 0),
    synced_at   TIMESTAMPTZ
);

CREATE TABLE ledger.transactions (
    txn_id      BIGINT GENERATED ALWAYS AS IDENTITY,
    account_id  BIGINT NOT NULL REFERENCES ledger.accounts (account_id),
    amount      NUMERIC(14, 2) NOT NULL CHECK (amount <> 0),
    txn_type    TEXT NOT NULL CHECK (txn_type IN ('DEPOSIT', 'WITHDRAWAL', 'TRANSFER_IN', 'TRANSFER_OUT', 'FEE')),
    txn_ts      TIMESTAMPTZ NOT NULL,
    description TEXT,
    PRIMARY KEY (txn_id, txn_ts)          -- partition key must be part of the primary key
) PARTITION BY RANGE (txn_ts);

-- Index on the parent is created on every partition automatically.
CREATE INDEX transactions_account_ts_idx ON ledger.transactions (account_id, txn_ts DESC);
CREATE INDEX transactions_txn_id_idx ON ledger.transactions (txn_id);

-- One row: the highest txn_id already folded into ledger.accounts.balance.
CREATE TABLE ledger.sync_state (
    id          BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    last_txn_id BIGINT NOT NULL DEFAULT 0,
    last_run_at TIMESTAMPTZ
);
INSERT INTO ledger.sync_state DEFAULT VALUES;

-- Create monthly partitions covering [from_month, to_month], plus a default partition.
CREATE OR REPLACE PROCEDURE ledger.create_month_partitions(from_month DATE, to_month DATE)
LANGUAGE plpgsql AS $$
DECLARE
    m DATE := date_trunc('month', from_month);
BEGIN
    WHILE m <= to_month LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS ledger.transactions_%s PARTITION OF ledger.transactions
               FOR VALUES FROM (%L) TO (%L)',
            to_char(m, 'YYYY_MM'), m, (m + INTERVAL '1 month')::DATE);
        m := (m + INTERVAL '1 month')::DATE;
    END LOOP;
    EXECUTE 'CREATE TABLE IF NOT EXISTS ledger.transactions_default
               PARTITION OF ledger.transactions DEFAULT';
END;
$$;
