-- Legacy balance sync: recompute every account's balance from every transaction.
-- Cost grows with the size of the whole ledger, not with the number of new rows.
CREATE OR REPLACE PROCEDURE legacy.sync_balances()
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE legacy.accounts a
       SET balance   = COALESCE(t.total, 0),
           synced_at = now()
      FROM (SELECT account_id, SUM(amount) AS total
              FROM legacy.transactions
             GROUP BY account_id) t
     WHERE t.account_id = a.account_id;
END;
$$;
