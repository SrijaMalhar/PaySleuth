# PaySleuth

[![CI](https://github.com/SrijaMalhar/PaySleuth/actions/workflows/ci.yml/badge.svg)](https://github.com/SrijaMalhar/PaySleuth/actions/workflows/ci.yml)

PaySleuth reads the bank statement a tuition centre, PG or housing society already gets, works out
which member each UPI transfer is for, applies it to what they owe, sends receipts and reminders, and
tells the owner when the statement itself is missing something. It never moves money and never talks
to a payment network.

Python 3.12, FastAPI, PostgreSQL 16, psycopg 3, no ORM. 246 tests against a real database, and a
mutation check that removes each safety mechanism in turn to prove the tests would notice.

## The problem

A 300-student centre gets statement lines like these, and the owner matches them by hand every month:

```
UPI/CR/615412345601/SUNITA SHARMA/SBIN/sunita.sharma@oksbi/June fees
UPI-R K VERMA-rk.verma@okhdfcbank-HDFC0001234-615412345602-JUNE FEES      <- Rs 3,500 for two children
UPI/CR/615412345604/LAKSHMI IYER/UBIN/lakshmi.iyer@oksbi/meera fees       <- unknown UPI ID
NEFT-N156260012345-ANIL NAIR-PS1004 june fee
```

Banks print names their own way ("R K VERMA", "SUNITA SHAR"), siblings' fees arrive as one amount,
payments are partial, late, early or doubled, alerts are re-sent, statements overlap, and sometimes
the bank takes a credit back.

## What it does

| Piece | What it does |
|---|---|
| Billing | Monthly fees with fee history, concessions, scholarships, catch-up and late fees |
| Statement import | Bank CSV in, five bank layouts recognised, debits skipped, bad lines reported by line number, re-import changes nothing |
| Daily close | Checks each statement follows the last and accounts for itself, and that every credited rupee is applied, held, under review or returned |
| Matching | Five strict rules; anything unsure goes to a review queue with ranked suggestions |
| Learning | When a person assigns a credit, the payer's UPI ID is remembered for next month |
| Applying money | Oldest dues first; overpayment becomes credit; payments can move between members |
| Reversals | A credit the bank returns reopens the charges it paid and tells the parent |
| Messages | Receipts and day 1, 5 and 10 reminders through a transactional outbox, signed, never in quiet hours |
| Staff and control | Owner and clerk keys, who-did-what audit log, two-person approval for large reassignments |
| Measurement | Auto-match rate, suggestion accuracy and signal precision from recorded decisions |
| Reports | Monthly collection, ageing buckets, defaulters, per-member statements |
| Monitoring | Prometheus metrics, alerts for anything a person must look at, logs with phone numbers masked |
| Multi-tenant | Many institutions in one database, isolated by PostgreSQL row-level security |

The matching rules, in order. A wrong match marks the wrong family as paid, so only these decide
automatically:

1. Exactly one reference code (`PS1004`) in the note: that member.
2. Several codes, and the amount equals exactly what they owe together: split by what each owes.
3. A UPI ID registered to exactly one member: that member.
4. A UPI ID shared by siblings, and the amount equals what they owe together or their fees together: split.
5. Anything else: the review queue, with suggestions that are shown and never applied.

## Try it

```bash
docker compose up -d db
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m tools.seed_demo          # a centre mid-month: billed, imported, two credits to review
uvicorn paysleuth.main:app &       # API docs at http://localhost:8000/docs
tools/demo.sh                      # or walk a month through the API from scratch
```

Real output from the seeded demo's review queue, where two credits could not be matched safely:

```
Rs 1800.00 from ANIL NAIR: PS1004 june fee
    70  PS1009 Arjun Nair: amount equals the Rs 1800.00 monthly fee; payer name matches Anil Nair;
                           payer has the same surname
Rs 2500.00 from LAKSHMI IYER: meera fees
    75  PS1010 Meera Iyer: payer name matches Lakshmi Iyer; payer has the same surname;
                           note mentions Meera
Rs 12000.00 from GREEN LEAF CAFE: rent june
    15  PS1006 Diya Sharma: amount is exactly 6 months of fees
```

Assigning the second one remembers `lakshmi.iyer@oksbi`, and July's transfer from the same payer
matches by itself.

## How it works

```
bank CSV ──parse──> continuity check ──> credits ──group (union-find)──> worker threads
                      │ opening = previous closing?                          │ per credit, one transaction:
                      │ closing = opening + credits - debits?                │   lock candidate members (id order)
                      ▼ no: alert, still imported                            │   INSERT bank_credit ON CONFLICT (utr)
                   alerts                                                     │   decide (5 rules) ──no──> review queue
                      ▲                                                       │   payment -> settle oldest-first        │
                      │ every rupee applied, held, reviewed or returned?      │   -> receipt row                  person │
                   daily close <───────────────────────────────────────────────┘                        ◄──────────────┘
                                                                             outbox ──SKIP LOCKED──> signed webhook
```

## Design decisions

**Integer paise, no stored balances.** Amounts are parsed with `Decimal` into `bigint` paise and never
touch a float (`int(0.29 * 100)` is 28). Only charges, payments and allocations are stored; outstanding
and credit are computed, so they cannot drift.

**The UTR is the idempotency key.** `UNIQUE (institution_id, utr)` plus `ON CONFLICT DO NOTHING`. The
lookup before the insert only saves work; the constraint is the guarantee when two copies race. The same
UTR with a different amount is a 409 for a person to look at.

**Money history is append-only.** Database triggers refuse `UPDATE` and `DELETE` on payments and
allocations. Moving or reversing money writes correction rows: a negative allocation per charge, and a
negative payment pointing at the original. Every reader already sums, so balances and reports work
unchanged, and no money record is ever lost. A partial unique index lets a payment be voided once.

**Noticing when data is missing, not just when it is wrong.** Banks print a running balance, so every
line's balance must equal the previous plus its credit minus its debit, and each statement's opening
balance must equal the previous statement's closing balance. A dropped page, an unreadable line or an
altered amount breaks the chain and raises an alert with the unaccounted amount. The daily close then
checks the other direction: every rupee the bank credited is applied to a bill, held as an advance,
waiting for review, or returned.

**Concurrency.** Money operations lock the bank credit row first (manual actions), then member rows in
ascending id order.

| Race | Without protection | What prevents it | Test |
|---|---|---|---|
| One alert delivered 20 times at once | Paid 20 times | UNIQUE on UTR | `test_the_same_transfer_arriving_20_times_at_once_counts_once` |
| 20 payments for one member at once | A fee paid twice | `SELECT ... FOR UPDATE` on the member | `test_20_payments_for_one_member_at_once_never_pay_a_fee_twice` |
| Opposite reassignments | Deadlock | Members locked in id order | `test_opposite_reassignments_at_once_never_deadlock` |
| Two clerks resolve one credit | Applied twice | Row lock on the credit | `test_two_people_resolving_the_same_credit_differently` |
| Late-fee job vs a payment | Late fee for someone who just paid | Re-check after taking the lock | `test_late_fee_is_rechecked_after_waiting_for_the_lock` |
| 10 people save one member | Lost update | Version column, sent as ETag (412) | `test_ten_people_saving_the_same_member_at_once` |
| Two owners approve at once | Reassigned twice | Row lock on the approval | `test_two_approvers_at_once_apply_it_once` |
| Many outbox workers | Double send | `FOR UPDATE SKIP LOCKED` | `test_many_workers_deliver_every_message_exactly_once` |
| API and workers start together | `CREATE POLICY` fails | Advisory lock around migration | `test_processes_starting_together_can_all_migrate` |

**Parallel import that equals serial import.** Random threads would keep the books valid (locks) but not
deterministic, and the same upload could text a parent two different things. Credits are grouped with
union-find over UTR and candidate-member nodes, so everything that shares a member or a UTR runs in
statement order on one thread. The result is compared with a line-by-line import by UTR and reference
code, for 1, 4, 8 and 16 workers. Written up in [docs/parallel-import.md](docs/parallel-import.md),
including the bug it caught.

**Transactional outbox.** A receipt is inserted in the payment's transaction. Workers claim with
`SKIP LOCKED`, commit, send outside any transaction, and record the result guarded on their claim. A
claim older than 60 seconds returns to pending, so delivery is at-least-once and every payload carries a
`message_id`. Failures back off from 5 seconds to an hour and park as dead after 8 attempts.

**Signed messages.** Each POST carries `X-PaySleuth-Signature: t=<unix>,v1=<hex HMAC-SHA256>` over
`"<t>.<body bytes>"`. Signing the timestamp with the body is what stops a captured message being
replayed later with a fresh `t`. The verifier is in the same module, tested against the signer through a
real HTTP gateway, and can be copied into a gateway as it is.

**Two-person approval.** A reassignment above the institution's threshold returns 202 and waits: a
different person must approve it, and one of the two must be an owner. A `CHECK` constraint makes
self-approval impossible in the database as well as in code.

**Row-level security.** Every tenant table has a `FORCE`d policy on `app.institution_id`, set per
transaction. With nothing set the policy matches no rows, and the app refuses to start as a role that
bypasses RLS.

**Weights measured, not guessed.** The suggestion scores started as guesses. Every review decision is
now recorded with the signals that fired for each candidate, so `tools/tune_weights.py` can replay any
weights against real decisions, cross-validate on held-out ones, and propose changes. It refuses to
propose anything from fewer than 50 decisions.

**No Kafka, no microservices, no ML matching.** A 500-student centre is a few thousand rows a month and
fits on one PostgreSQL. In money, a rule that can be explained beats a model that cannot.

## Testing

| File | Tests | Covers |
|---|---|---|
| `test_payments.py` | 34 | Matching, idempotency, allocation, assign, split, reassign, append-only history |
| `test_parsing.py` | 29 | Money, months, names, narrations, CSV layouts |
| `test_suggestions_and_tenancy.py` | 19 | Rules, suggestion ranking, tenant isolation |
| `test_messages.py` | 19 | Outbox delivery, retries, ordering, reminders, quiet hours |
| `test_webhooks.py` | 18 | Signing, verification, replay and tamper refusal |
| `test_monitoring.py` | 18 | Metrics, masked logs, the demo seed |
| `test_billing.py` | 17 | Catch-up, fee history, scholarships, late fees, optimistic locking |
| `test_close.py` | 15 | Statement continuity, gaps, alerts, the daily close |
| `test_api.py` | 14 | Keys, ETags, validation, end-to-end flows, reports |
| `test_staff.py` | 12 | Roles, audit actors, two-person approval |
| `test_concurrency.py` | 11 | Every race in the table above |
| `test_reversals.py` | 9 | Returned credits, reopened charges, statement reversals |
| `test_performance.py` | 9 | Query counts per credit, held to a number |
| `test_banks.py` | 9 | Golden statement per bank, anonymiser |
| `test_import.py` | 7 | Parallel import equals serial import |
| `test_evaluation.py` | 5 | Matching report, weight tuning, cross-validation |
| `test_invariants.py` | 1 | Hypothesis state machine over every operation |

Nothing is mocked: every test runs against a real PostgreSQL in its own schema.

**The property test.** `test_invariants.py` is a Hypothesis `RuleBasedStateMachine`. It chooses the
operations (bill, late fees, pay by code, by known UPI ID, from a stranger, re-sent alert, assign,
reassign with splits, bank reversal, concession change, time passing), and after every step the books
must satisfy every rule and the daily close must account for every credited rupee. The gain over a
hand-written loop is shrinking: reintroducing the scholarship-sibling bug used to fail at "seed 3,
step 41" and now reduces to one line.

```
state.pay_from_a_known_upi_id(amount='2500', family='verma@oksbi')
```

**The mutation check.** `tools/mutation_check.py` removes one safety mechanism at a time and requires
its test to fail:

```
FOR UPDATE removed from lock_members                       caught
members locked in request order, not id order              caught
bank credit row lock removed                               caught
FOR UPDATE SKIP LOCKED removed from the outbox claim       caught
per-member outbox ordering removed                         caught
UNIQUE (institution_id, utr) removed                       caught
optimistic version check removed                           caught
late-fee re-check removed                                  caught
row-level security policy set to USING (true)              caught
migration advisory lock removed                            caught
union-find replaced by "first candidate"                   caught
append-only triggers removed                               caught
self-approval allowed                                      caught
approval row lock removed                                  caught
webhook timestamp not covered by the signature             caught
webhook timestamp window removed                           caught

16/16 mutations caught
```

One mechanism is deliberately not in that list. Swapping `hmac.compare_digest` for `==` answers
identically for every input and only leaks timing, so no behaviour test can catch it; it is a review
rule, not a tested one.

## Bugs the tests guard against

Putting any of these back fails a test by name:

- A sibling on a full scholarship got a Rs 0 share of a split (`all(fee > 0)` in rule 4).
- After a payment was moved away, another payment could not return to a charge it had already
  part-paid.
- Siblings paying before their first month were not split, because no fee was in force yet.
- Grouping a parallel import by each credit's first candidate member (see the write-up).
- A truncated or masked UPI ID (`sunita.sha`, `**sharma@oksbi`) stored as if it were the payer's.

## Benchmarks

On a 2-core x86_64 cloud VM with PostgreSQL 16 on the same machine. Full output in
[bench/results/](bench/results/).

Queries per credit, before and after the performance pass:

| | before | after |
|---|---|---|
| one member, by reference code | 13 | 11 |
| two siblings, split by dues | 22 | 13 |
| reversing a 6-child split | 23 | 13 |
| review queue, 25 waiting credits | ~100 | 6 |

```
import scaling: 4675 statement lines, 500 members, 694 groups
workers  seconds  lines/s speed-up  fingerprint
      1     18.9      247    1.00x  1c0a9b351413
      2     11.3      413    1.67x  1c0a9b351413
      4     10.1      463    1.87x  1c0a9b351413
      8     10.0      467    1.89x  1c0a9b351413
fingerprints identical

lock granularity: 8 threads x 100 transfers
  different members    503.1 transfers/s
  one member           164.3 transfers/s

API latency, 8 clients, 500 members, 60s: 2686 requests, 44/s
endpoint                              n   p50 ms   p95 ms   p99 ms
GET /members/{id}/statement         270       57      311      427
GET /reports/ageing                 269      173      408      517
GET /reports/collection             272      195      548      690
GET /review                         279      455      794     1126
POST /bank-credits                 1211       80      370      563
POST /bank-credits (split)          385       67      335      447
```

The speed-up flattens at 2 workers because the machine has 2 cores and shares them with PostgreSQL.
The fingerprint is a hash of the whole set of books: it is the same for every worker count, and the
same before and after the performance pass, so neither changed a result.

Load testing found the one real performance bug: `GET /review` re-read every member, their dues, their
fees and the payer's family for each waiting credit, giving a p50 of 2057ms and a p95 of 3976ms.
Reading them once per page took that to 262ms and 495ms, and throughput from 26/s to 63/s.

## Deploying

```bash
docker build -t paysleuth .
docker run -e PAYSLEUTH_DATABASE_URL=... -p 8000:8000 paysleuth           # API
docker run -e PAYSLEUTH_DATABASE_URL=... paysleuth python -m paysleuth.worker   # worker
```

One image serves both roles, so the API and the worker cannot drift apart. `render.yaml` brings up a
free PostgreSQL, the API and the worker. Settings: `PAYSLEUTH_DATABASE_URL`,
`PAYSLEUTH_MESSAGE_WEBHOOK`, `PAYSLEUTH_WEBHOOK_SECRET`, `PAYSLEUTH_POOL_SIZE`, `PAYSLEUTH_LOG_LEVEL`.
`/metrics` is unauthenticated and carries counts only; keep it off the public internet.

## Limits

- The bank adapters are written against each bank's published export layout, and the golden statements
  in `tests/golden/` are synthetic. Each should be re-checked against a real anonymised export before
  it is trusted; `tools/anonymise_statement.py` prepares one.
- The matching numbers (`GET /reports/matching`) are computed from an institution's own recorded
  decisions. No real institution has used this yet, so there are no real-world numbers to quote, and
  none are invented here.
- A change to a statement file's very first line is only visible against the previous statement, not
  within the file. There is a test that says so.
- SBI and Canara print UPI IDs cut short or masked, and Axis prints none, so for those banks matching
  relies on reference codes or review.
- No web UI; the API and `/docs` are the interface.
