-- Stored procedures and triggers for the redesigned ledger.

-- Incremental balance sync: fold in only transactions newer than the watermark.
-- Processes in batches so a large backlog never holds one huge transaction open.
CREATE OR REPLACE PROCEDURE ledger.sync_balances(batch_size INT DEFAULT 50000)
LANGUAGE plpgsql AS $$
DECLARE
    start_id BIGINT;
    max_id   BIGINT;
    end_id   BIGINT;
BEGIN
    SELECT last_txn_id INTO start_id FROM ledger.sync_state FOR UPDATE;
    SELECT COALESCE(MAX(txn_id), start_id) INTO max_id
      FROM ledger.transactions WHERE txn_id > start_id;

    WHILE start_id < max_id LOOP
        end_id := LEAST(start_id + batch_size, max_id);

        UPDATE ledger.accounts a
           SET balance   = a.balance + d.delta,
               synced_at = now()
          FROM (SELECT account_id, SUM(amount) AS delta
                  FROM ledger.transactions
                 WHERE txn_id > start_id AND txn_id <= end_id
                 GROUP BY account_id) d
         WHERE d.account_id = a.account_id;

        UPDATE ledger.sync_state SET last_txn_id = end_id, last_run_at = now();
        start_id := end_id;
    END LOOP;
END;
$$;

-- Full rebuild, kept for reconciliation checks (should always agree with the incremental sync).
CREATE OR REPLACE FUNCTION ledger.balance_drift()
RETURNS TABLE (account_id BIGINT, stored NUMERIC, recomputed NUMERIC)
LANGUAGE sql STABLE AS $$
    SELECT a.account_id, a.balance, COALESCE(SUM(t.amount), 0)
      FROM ledger.accounts a
      LEFT JOIN ledger.transactions t
        ON t.account_id = a.account_id
       AND t.txn_id <= (SELECT last_txn_id FROM ledger.sync_state)
     GROUP BY a.account_id, a.balance
    HAVING a.balance <> COALESCE(SUM(t.amount), 0);
$$;

-- Validate a transaction before it is written: sign must match the type, and a debit may
-- not push the account past its overdraft limit (balance + unsynced activity).
CREATE OR REPLACE FUNCTION ledger.check_transaction()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    available NUMERIC;
BEGIN
    IF NEW.txn_type IN ('DEPOSIT', 'TRANSFER_IN') AND NEW.amount < 0 THEN
        RAISE EXCEPTION 'credit % must be positive', NEW.txn_type USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.txn_type IN ('WITHDRAWAL', 'TRANSFER_OUT', 'FEE') AND NEW.amount > 0 THEN
        RAISE EXCEPTION 'debit % must be negative', NEW.txn_type USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.amount < 0 THEN
        SELECT a.balance + a.overdraft_limit + COALESCE((
                   SELECT SUM(t.amount) FROM ledger.transactions t
                    WHERE t.account_id = NEW.account_id
                      AND t.txn_id > (SELECT last_txn_id FROM ledger.sync_state)), 0)
          INTO available
          FROM ledger.accounts a
         WHERE a.account_id = NEW.account_id
           FOR UPDATE;
        IF available + NEW.amount < 0 THEN
            RAISE EXCEPTION 'insufficient funds on account %', NEW.account_id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

-- Row trigger used for live writes. Bulk loads disable it (see bench/benchmark.py).
CREATE TRIGGER transactions_check
BEFORE INSERT ON ledger.transactions
FOR EACH ROW EXECUTE FUNCTION ledger.check_transaction();

-- Transfer between two accounts as one atomic unit.
CREATE OR REPLACE PROCEDURE ledger.transfer(from_acct BIGINT, to_acct BIGINT, amt NUMERIC,
                                            note TEXT DEFAULT NULL)
LANGUAGE plpgsql AS $$
BEGIN
    IF amt <= 0 THEN
        RAISE EXCEPTION 'transfer amount must be positive';
    END IF;
    IF from_acct = to_acct THEN
        RAISE EXCEPTION 'cannot transfer to the same account';
    END IF;
    INSERT INTO ledger.transactions (account_id, amount, txn_type, txn_ts, description)
    VALUES (from_acct, -amt, 'TRANSFER_OUT', now(), note),
           (to_acct,    amt, 'TRANSFER_IN',  now(), note);
END;
$$;

-- Account statement for a date range: served by the (account_id, txn_ts) index and
-- partition pruning.
CREATE OR REPLACE FUNCTION ledger.statement(acct BIGINT, from_ts TIMESTAMPTZ, to_ts TIMESTAMPTZ)
RETURNS TABLE (txn_id BIGINT, txn_ts TIMESTAMPTZ, txn_type TEXT, amount NUMERIC, description TEXT)
LANGUAGE sql STABLE AS $$
    SELECT txn_id, txn_ts, txn_type, amount, description
      FROM ledger.transactions
     WHERE account_id = acct AND txn_ts >= from_ts AND txn_ts < to_ts
     ORDER BY txn_ts DESC;
$$;
