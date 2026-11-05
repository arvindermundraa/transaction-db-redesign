# Interview guide: Transaction Database Redesign

## 30-second version

"I took a naive ledger design, one big transactions table where every balance sync summed the whole
history, and redesigned it in PostgreSQL. Transactions are partitioned by month, there's a composite
index for statement queries, and balance sync is incremental: it keeps a watermark and only folds in
rows added since the last run. On 1.05 million transactions, sync after a 10,000-row batch went from
581 to 130 milliseconds, about 78% faster, and 30-day statements went from 45 ms to 0.13 ms. A
reconciliation check confirms both designs produce identical balances for all 50,000 accounts."

## Walk through the design

1. **Watermark sync.** `sync_state.last_txn_id` records the last row folded in. Each run sums only
   `txn_id > watermark`, grouped by account, and adds the delta to the stored balance. Cost scales with
   new rows, not total history. Batching keeps any one transaction short.
2. **Partitioning.** Range partitions by month on `txn_ts`. A query for June touches only the June
   partition (partition pruning, verified by an EXPLAIN test). Old months can be detached and archived
   without a big DELETE.
3. **Indexing.** `(account_id, txn_ts DESC)` matches the statement query exactly: equality on the
   account, range on time, already in the output order.
4. **Integrity.** Foreign key, CHECK constraints, and a BEFORE INSERT trigger: credits must be positive,
   debits negative, and a debit cannot exceed balance + unsynced activity + overdraft limit. The trigger
   locks the account row (`FOR UPDATE`) so two concurrent withdrawals cannot both pass.
5. **Proof.** `balance_drift()` recomputes every balance from scratch and lists mismatches. The
   benchmark also compares legacy and redesign balances account by account: 0 differences.

## Likely questions

**Why did statements get 300x faster but sync only 4.5x?** Statements went from a full scan to an
index lookup in one partition. Sync still has to update one row per touched account (about 10,000
here); the win is that it no longer reads a million rows to do it.

**Why is txn_id not the primary key on its own?** PostgreSQL requires the partition key in every
unique constraint on a partitioned table, so the key is `(txn_id, txn_ts)`, with a separate index on
`txn_id` for the watermark range scan.

**What happens if sync crashes halfway?** Each batch updates balances and the watermark in the same
transaction, so a crash leaves a consistent state, and the next run resumes from the watermark.

**Isn't the trigger slow for high write volume?** It adds an index lookup and a row lock per debit.
For bulk imports you disable it and validate in the batch, which the benchmark does. For very high
contention you would move to a reserved-funds or queue-per-account model.

**How would this map to Oracle PL/SQL?** Interval partitioning (`PARTITION BY RANGE ... INTERVAL`),
the same composite local index, a `BEFORE INSERT` row trigger, and procedures in a package.

**How is the test data generated?** Deterministically in SQL with `generate_series` and `hashtext`,
so both schemas get identical rows and the run is reproducible.
