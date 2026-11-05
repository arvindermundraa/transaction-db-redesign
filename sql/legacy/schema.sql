-- Legacy design: the starting point this project replaces.
-- One unpartitioned table, a surrogate primary key and nothing else. Account balances are
-- rebuilt from scratch by summing every transaction (see sync.sql).
DROP SCHEMA IF EXISTS legacy CASCADE;
CREATE SCHEMA legacy;

CREATE TABLE legacy.accounts (
    account_id  BIGINT PRIMARY KEY,
    holder_name TEXT NOT NULL,
    balance     NUMERIC(14, 2) NOT NULL DEFAULT 0,
    synced_at   TIMESTAMPTZ
);

CREATE TABLE legacy.transactions (
    txn_id      BIGSERIAL PRIMARY KEY,
    account_id  BIGINT NOT NULL,          -- no foreign key, no index
    amount      NUMERIC(14, 2) NOT NULL,  -- positive = credit, negative = debit
    txn_type    TEXT NOT NULL,
    txn_ts      TIMESTAMPTZ NOT NULL,
    description TEXT
);
