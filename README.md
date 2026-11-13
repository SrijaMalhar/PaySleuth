# PaySleuth

[![CI](https://github.com/SrijaMalhar/PaySleuth/actions/workflows/ci.yml/badge.svg)](https://github.com/SrijaMalhar/PaySleuth/actions/workflows/ci.yml)

PaySleuth reads the bank statement a tuition centre, PG or housing society already gets, works out
which member each UPI transfer is for, applies it to what they owe, and sends receipts and reminders.
It never moves money and never talks to a payment network.

Python 3.12, FastAPI, PostgreSQL 16, psycopg 3 (no ORM). 150 tests against a real database.

## The problem

A 300-student centre gets statement lines like these, and the owner matches them by hand every month:

```
UPI/CR/615412345601/SUNITA SHARMA/SBIN/sunita.sharma@oksbi/June fees
UPI-R K VERMA-rk.verma@okhdfcbank-HDFC0001234-615412345602-JUNE FEES      <- Rs 3,500 for two children
UPI/CR/615412345604/LAKSHMI IYER/UBIN/lakshmi.iyer@oksbi/meera fees       <- unknown UPI ID
NEFT-N156260012345-ANIL NAIR-PS1004 june fee
```

Banks print names their own way ("R K VERMA", "SUNITA SHAR"), siblings' fees arrive as one amount,
payments are partial, late, early or doubled, alerts are re-sent and statements overlap.

## What it does

| Piece | What it does |
|---|---|
| Billing | Monthly fees with fee history, concessions, scholarships, catch-up and late fees |
| Statement import | Bank CSV in, debits skipped, bad lines reported by line number, re-import changes nothing |
| Matching | Five strict rules; anything unsure goes to a review queue with ranked suggestions |
| Learning | When a person assigns a credit, the payer's UPI ID is remembered for next month |
| Applying money | Oldest dues first; overpayment becomes credit; payments can move between members |
| Messages | Receipts and day 1, 5 and 10 reminders through a transactional outbox, never in quiet hours |
| Reports | Monthly collection, ageing buckets, defaulters, per-member statements |
| Multi-tenant | Many institutions in one database, isolated by PostgreSQL row-level security |

The matching rules, in order. A wrong match marks the wrong family as paid, so only these decide
automatically:

1. Exactly one reference code (`PS1004`) in the note: that member.
2. Several codes, and the amount equals exactly what they owe together: split by what each owes.
3. A UPI ID registered to exactly one member: that member.
4. A UPI ID shared by siblings, and the amount equals what they owe together or their fees together: split.
5. Anything else: the review queue.

## Try it

```bash
docker compose up -d db
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn paysleuth.main:app &          # API docs at http://localhost:8000/docs
tools/demo.sh
```

Part of the demo's real output, importing `sample_data/june_2026_statement.csv`:

```
== Import the bank statement
{'recorded': 5, 'matched': 3, 'needs_review': 2, 'duplicates': 1, 'skipped_debits': 2}

== Review queue: what could not be matched safely, with suggestions
credit 4: Rs 2500.00 from LAKSHMI IYER (lakshmi.iyer@oksbi), note: meera fees
    115  PS1005 Meera Iyer: amount equals the Rs 2500.00 outstanding; payer name matches Lakshmi Iyer; payer has the same surname; note mentions Meera
credit 5: Rs 12000.00 from GREEN LEAF CAFE (greenleaf@icici), note: rent june
     15  PS1001 Diya Sharma: amount is exactly 6 months of fees
     15  PS1002 Rohan Verma: amount is exactly 6 months of fees

== Assign Lakshmi Iyer's transfer to the top suggestion (her UPI ID is remembered)
615412345604 -> PS1005, matched by manual

== July: the same payer now matches without anyone looking
618612345611 -> PS1005, matched by known_upi_id
```

## How it works

```
bank CSV ──parse──> credits ──group (union-find)──> worker threads
                                                        │ per credit, one transaction:
                                                        │   lock candidate members (id order)
                                                        │   INSERT bank_credit ON CONFLICT (utr) DO NOTHING
                                                        │   decide (5 rules) ──no──> review queue ──person──┐
                                                        │   payment -> settle oldest-first -> receipt row   │
                                                        ▼                                    ▲──────────────┘
                                                     outbox ──SKIP LOCKED workers──> gateway (SMS / WhatsApp)
```

## Design decisions

**Integer paise, no stored balances.** Amounts are parsed with `Decimal` into `bigint` paise and never
touch a float (`int(0.29 * 100)` is 28). Only charges, payments and allocations are stored; outstanding
and credit are computed, so they cannot drift.

**The UTR is the idempotency key.** `UNIQUE (institution_id, utr)` plus `ON CONFLICT DO NOTHING`. The
lookup before the insert only saves work; the constraint is the guarantee when two copies race. The same
UTR with a different amount is a 409 for a person to look at.

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
| Many outbox workers | Double send | `FOR UPDATE SKIP LOCKED` | `test_many_workers_deliver_every_message_exactly_once` |
| API and workers start together | `CREATE POLICY` fails | Advisory lock around migration | `test_processes_starting_together_can_all_migrate` |

**Parallel import that equals serial import.** Random threads would keep the books valid (locks) but not
deterministic. Credits are grouped with union-find over UTR and candidate-member nodes, so everything
that shares a member or a UTR runs in statement order on one thread. The result is compared with a
line-by-line import by UTR and reference code.

**Transactional outbox.** A receipt is inserted in the payment's transaction. Workers claim with
`SKIP LOCKED`, commit, send outside any transaction, and record the result guarded on their claim. A
claim older than 60 seconds returns to pending, so delivery is at-least-once and every payload carries a
`message_id`. Failures back off from 5 seconds to an hour and park as dead after 8 attempts.

**Row-level security.** Every tenant table has a `FORCE`d policy on `app.institution_id`, set per
transaction. With nothing set the policy matches no rows, and the app refuses to start as a role that
bypasses RLS.

**No Kafka, no microservices, no ML matching.** A 500-student centre is a few thousand rows a month and
fits on one PostgreSQL. In money, a rule that can be explained beats a model that cannot.

## Testing

| File | Covers |
|---|---|
| `test_parsing.py` | Money, months, names, narrations, CSV layouts |
| `test_billing.py` | Catch-up, fee history, scholarships, late fees, optimistic locking |
| `test_payments.py` | Matching, idempotency, allocation, assign, split, reassign |
| `test_suggestions_and_tenancy.py` | Rules, suggestion ranking, tenant isolation |
| `test_import.py` | Parallel import equals serial import |
| `test_messages.py` | Outbox delivery, retries, ordering, reminders, quiet hours |
| `test_api.py` | Keys, ETags, validation, end-to-end flows, reports |
| `test_concurrency.py` | Every race in the table above |
| `test_invariants.py` | 8 seeds x 150 random operations, books checked after every step |

`tools/mutation_check.py` removes one safety mechanism at a time and requires its test to fail:

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

11/11 mutations caught
```

## Bugs the randomised test guards against

Putting any of these back makes `test_random_histories_keep_the_books_correct` fail:

- A sibling on a full scholarship got a Rs 0 share of a split (`all(fee > 0)` in rule 4;
  `test_a_scholarship_sibling_gets_no_zero_share`).
- After a payment was moved away, another payment could not return to a charge it had already part-paid
  (`ON CONFLICT ... DO UPDATE` in `settle_member`;
  `test_a_payment_can_return_to_a_charge_it_already_part_paid`).
- Siblings paying before their first month were not split, because no fee was in force yet
  (`test_siblings_paying_before_they_start_split_by_their_first_fees`).

## Benchmarks

`python bench/benchmark.py`, on a 2-core x86_64 cloud VM with PostgreSQL 16 on the same machine:

```
import scaling: 4675 statement lines, 500 members, 694 groups
workers  seconds  lines/s speed-up  fingerprint
      1     14.3      328    1.00x  1c0a9b351413
      2      9.3      504    1.54x  1c0a9b351413
      4      9.4      496    1.51x  1c0a9b351413
      8     12.1      387    1.18x  1c0a9b351413
fingerprints identical

lock granularity: 8 threads x 100 transfers
  different members    303.7 transfers/s
  one member           190.3 transfers/s
```

The speed-up stops at 2 workers because the machine has 2 cores and they are shared with PostgreSQL;
more threads only add contention. The fingerprint is a hash of the books and is the same on every row.

## Limits

- Statement parsing is tested on synthetic statements in common Indian bank layouts, not yet on real
  exports from each bank.
- One API key per institution; no staff accounts.
- A reversed credit has to be handled by hand.
- No web UI; the API and `/docs` are the interface.
