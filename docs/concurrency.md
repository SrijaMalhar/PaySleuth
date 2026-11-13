# Concurrency

How PaySleuth keeps the books correct when twenty copies of one bank alert, a billing run and a
clerk's correction all arrive at the same instant. Summarised in the
[README](../README.md#concurrency-one-lock-order); this is the full argument.

This is the part I would most want to be asked about. Every money operation follows one order:

```
1. the approval_requests row     (maker-checker actions)        SELECT ... FOR UPDATE
2. the bank_credits row          (anything a person does to an existing credit)
3. member rows                   ALWAYS in ascending id order   SELECT ... WHERE id = ANY(...) ORDER BY id FOR UPDATE
```

**Ascending id order is the whole deadlock argument.** If every transaction takes the lower id first,
no transaction can hold B while waiting for A, so a wait cycle cannot form. The alternative — locking
"from, then to" when moving money — deadlocks the moment one clerk moves money from Diya to Kabir while
another moves different money from Kabir to Diya. `lock_members()` is the only place that takes a member
lock, and it sorts; there is no second path to get it wrong.

A second ordering decision: **decide while holding the locks, then write.** A sibling split divides by
what each child owes *right now*, so the candidate members are locked, then the rules run, then the
credit row is inserted with the status it ends up in. Deciding before locking would mean splitting on
figures that could change before the write landed.

| Race | Without protection | Mechanism | Test |
|---|---|---|---|
| One bank alert delivered 20× at once | Paid 20 times | `UNIQUE` on UTR + `ON CONFLICT DO NOTHING` | `test_the_same_transfer_arriving_20_times_at_once_counts_once` |
| 20 payments for one member at once | A fee paid twice | Member row lock | `test_20_payments_for_one_member_at_once_never_pay_a_fee_twice` |
| Opposite reassignments | **Deadlock** | Ascending id order | `test_opposite_reassignments_at_once_never_deadlock` |
| Two clerks resolve one queued credit | Applied twice | Credit row lock | `test_two_people_resolving_the_same_credit_differently` |
| Two owners approve one correction | Reassigned twice | Approval row lock | `test_two_approvers_at_once_apply_it_once` |
| Late-fee job vs a payment landing | Fee charged to someone who just paid | Check, lock, **re-check** | `test_late_fee_is_rechecked_after_waiting_for_the_lock` |
| Two people editing one member | Lost update | `version` column, surfaced as an ETag → 412 | `test_ten_people_saving_the_same_member_at_once` |
| Many relay workers | Double send | `FOR UPDATE SKIP LOCKED` | `test_many_workers_deliver_every_message_exactly_once` |
| API and workers starting together | `CREATE POLICY` fails | `pg_advisory_xact_lock` around migration | `test_processes_starting_together_can_all_migrate` |

The late-fee test is the one I would point at. Checking before locking is a classic time-of-check bug,
and an ordinary test cannot force the bad interleaving. That test polls `pg_stat_activity` until the job
is observably blocked on the member lock, *then* lands the payment — so the worst order happens on every
run, not occasionally.

### Parallel import that is equivalent to serial import

Random threads would keep the books valid — every write takes member locks — but not **deterministic**.
Two of one member's payments racing means a different allocation split, so the same upload could text a
parent two different things. Non-determinism in a money system is a defect even when every row is legal.

The rule: **parallel across members, in statement order within a member.** Credits are grouped with a
disjoint set (union-find, path halving and union by size) over two kinds of node — a credit's UTR, and
each member it could belong to:

```
  Rs 3,500 from rk.verma@okhdfcbank ──┬── ("member", 41)  Rohan
                                      └── ("member", 42)  Kabir
  Rs 1,500 "PS1042" ────────────────────────┘                 same group: shares Kabir
  Rs 2,000 "PS1055" ───── ("member", 55)  Diya                separate group
```

Groups cannot affect each other by construction, so each runs in statement order on one connection and
they run concurrently. Union-find fits because the joins arrive one credit at a time and the only
question is ever "are these two already connected?".

The grouping is a **determinism layer, not the safety layer**. If it were wrong tomorrow the books would
still be valid, because the locks are untouched; the result would merely depend on timing. Keeping those
jobs separate means a bug in the fast path cannot lose money, only reproducibility.

It is proved, not asserted: the same statement is imported line-by-line into one schema and in parallel
into another, at 1, 4, 8 and 16 workers, and the books are compared by UTR and reference code rather
than row id. [parallel-import.md](parallel-import.md) has the full argument and the bug this
caught.

---
