# Benchmark results

PostgreSQL 17.5; 1,050,000 transactions across 50,000 accounts;
37 partitions; each run appends 10,000 new transactions, then syncs balances.

| operation | legacy (median) | redesign (median) | reduction |
|---|---|---|---|
| balance sync after a 10,000-row batch | 581.0 ms | 129.5 ms | 77.7% |
| 30-day account statement (200 accounts) | 45.38 ms | 0.13 ms | 99.7% |

Correctness: 0 accounts with different balances between the two schemas;
0 rows reported by `ledger.balance_drift()`.
