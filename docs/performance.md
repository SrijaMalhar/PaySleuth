# Performance, measured

Numbers from a 2-core x86_64 VM with PostgreSQL on the same machine, and the one real bug load testing
found. Summarised in the [README](../README.md). Full output in [../bench/results/](../bench/results/).

Queries per credit, before and after the one performance pass. The "after" column is asserted by
`tests/test_performance.py`, so it cannot drift; the two starred figures were counted against code that no
longer exists.

| | before | after |
|---|---|---|
| one member, by reference code | 13 | 11 |
| two siblings, split by dues | 22 | 13 |
| reversing a 6-child split | 23* | 13 |
| review queue, 25 waiting credits | ~100* | 6 |

```
import scaling: 4675 statement lines, 500 members, 694 groups
workers  seconds  lines/s speed-up  fingerprint
      1     14.5      323    1.00x  39467cfe985a
      2      9.3      502    1.55x  39467cfe985a
      4      9.1      511    1.58x  39467cfe985a
      8      8.4      558    1.73x  39467cfe985a

lock granularity: 8 threads x 100 transfers
  different members    520.5 transfers/s
  one member           182.9 transfers/s
```

The speed-up flattens early because the machine has two cores shared with PostgreSQL. Load testing found
the one real performance bug: `GET /review` re-read every member, their dues, their fees and the payer's
family **for each waiting credit** — p50 2057ms, p95 3976ms. Reading them once per page took that to
231ms and 351ms, and throughput from 26/s to 67/s. Every credit on a page is still scored against every
active member, so the page is bounded (default 50, max 200) rather than letting the largest institution
set the slowest request.

---
