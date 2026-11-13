# PaySleuth

[![CI](https://github.com/SrijaMalhar/PaySleuth/actions/workflows/ci.yml/badge.svg)](https://github.com/SrijaMalhar/PaySleuth/actions/workflows/ci.yml)

Fee reconciliation for tuition centres, PGs and housing societies. It reads the bank statement they
already get, decides which member each UPI transfer belongs to, applies it to what they owe, sends
receipts and reminders — and tells the owner when the statement itself is missing a line. It never moves
money and never touches a payment network.

The CRUD here is an afternoon's work. The design problem is that the input is partial, repeated and
adversarially messy, the output is **money attributed to a named family**, and **a wrong attribution is
worse than no attribution** — the right family gets a reminder they do not deserve and the wrong one
stops paying. Three real statement lines, and what each one costs:

```
UPI-R K VERMA-rk.verma@okhdfcbank-HDFC0001234-615412345602-JUNE FEES
      one transfer, two children -> split by what each owes at that instant

UPI/CR/615412345604/LAKSHMI IYER/UBIN/lakshmi.iyer@oksbi/meera fees
      a UPI ID nobody registered -> rank it, explain it, never apply it

BY TRANSFER-UPI/CR/615412345601/SUNITA S/SBIN/sunita.sha/June fees--
      SBI truncates the name and the UPI ID -> neither may be stored as the payer's
```

So: no stored balances, append-only money, one lock order, seven idempotency keys, and a system built to
refuse rather than guess.

### What is actually proven, not claimed

| | |
|---|---|
| **343 tests, zero mocks** | every one against a real PostgreSQL in its own schema, because locks, constraints and row-level security *are* the subject |
| **22/22 mutations caught** | each safety mechanism deleted one at a time; the test that covers it must go red |
| **9 concurrency races** | real threads released on a barrier — and where an interleaving matters it is *forced* by polling `pg_stat_activity`, not hoped for |
| **1 fingerprint, 4 worker counts** | parallel import produces books that hash identically to a serial import; equivalence is tested at 1, 4, 8 and 16 workers |
| **12 bugs, listed by name** | including the one where a feature's headline claim in this README was false |
| **10 layers, asserted** | `test_architecture.py` reads the AST import graph: acyclic, pure layer closed, HTTP confined to one module |

**If you have five minutes**, read [the locking protocol](#concurrency-the-locking-protocol),
[seven idempotency keys](#idempotency-seven-keys-for-seven-questions), and
[the bug where the feature's own claim was false](#bugs-these-layers-caught-and-how).

Python 3.12 · FastAPI · PostgreSQL 16 · psycopg 3 · no ORM · 4,300 lines of application code, a 320-line
schema, 4,200 lines of tests

**Contents** · [The design problem](#the-design-problem) · [Domain model](#domain-model) ·
[Invariants](#invariants-and-where-they-live) · [State machines](#state-machines) ·
[Concurrency](#concurrency-the-locking-protocol) · [Idempotency](#idempotency-seven-keys-for-seven-questions) ·
[Module structure](#module-structure) · [Extension points](#extension-points) ·
[Patterns](#patterns-used-and-patterns-refused) · [Proving it](#how-correctness-is-established) ·
[Trade-offs](#trade-offs-and-what-i-would-change-at-ten-times-the-size) · [Running it](#running-it)

---

## The design problem

Four properties of the input drive every decision in this codebase:

| Property of the input | What it forces in the design |
|---|---|
| The same credit arrives more than once (bank alert, then the statement, then a re-import) | Every write has to be idempotent on a key the *bank* chose, not one we generate |
| One transfer can pay for several people | Attribution is a decision over a set, and the money has to split by what each owes *at that instant* |
| The payer identifies themselves inconsistently or not at all | Matching needs tiers of confidence, and an explicit "a person must decide" state |
| Lines go missing, repeat, or arrive out of order | The system has to detect absence, which is harder than detecting error |

And one constraint on the output that the asymmetry above imposes: every automatic decision has to be
explainable in one sentence to a tuition centre owner. That rules out a model, and it is why the five
rules that spend money without a human are a closed ordered list rather than a plugin registry.

---

## Domain model

Seventeen tables. The ones that matter:

```
institutions ──┬── staff ········································ API key -> role (owner | clerk)
               │
               ├── members ──┬── member_fees ................... fee history: effective_period -> amount
               │             ├── member_upi_ids ................ many-to-many; siblings share a parent's ID
               │             └── charges ...................... what is owed: (member, period, kind)
               │                      ▲
               │                      │ allocations (append-only, signed)
               │                      ▼
               ├── bank_credits ──── payments (append-only, signed) ──> one member's share of one credit
               │        │
               │        ├── credit_reversals ................... the bank took it back
               │        └── review_outcomes .................... what a person chose, vs what was suggested
               │
               ├── statement_imports ........................... what each file said about itself
               ├── outbox ...................................... messages, queued with the change
               ├── alerts / daily_closes ....................... what a person must look at
               ├── approval_requests ........................... maker-checker for large corrections
               └── audit_log ................................... who changed what
```

### The decision everything else follows from: no stored balances

There is **no balance column anywhere**. Not on `members`, not on `charges`. `outstanding` and `credit`
are derived every time they are asked for:

```
outstanding = SUM(charges.amount) - SUM(allocations.amount)
credit      = SUM(payments.amount) - SUM(allocations.amount)
```

A stored balance is a cache of a sum, and every cache needs invalidation. Under concurrent writes —
twenty copies of one bank alert, a billing run and a payment at the same instant, a correction moving
money between two families — an incremented counter is where the bug lives. Deriving the number means
a lost update can corrupt *a row*, but it cannot corrupt *the answer*: the sum is recomputed from rows
that are themselves constrained.

What this costs: every balance read is an aggregate. The mitigation is that the aggregate is cheap at
this scale (a few thousand rows per institution per month), indexed for it, and never done in a loop —
`settle_members()` and `balances_with_details()` take a list of members and answer in one query each.
`tests/test_performance.py` holds the query count per credit to a constant so a per-member loop cannot
creep back in.

### Money is integer paise, end to end

Parsed once with `Decimal` at the edge, stored as `bigint`, formatted back to a rupee string at the
edge. No float ever touches an amount, because `int(0.29 * 100)` is 28. Three decimal places are
*refused*, not rounded — a fraction of a paisa is bad input, not an amount. Amounts cross the API as
strings (`"2000.50"`), never JSON numbers, because many clients parse those as floats.

### Billing months are strings

A billing month is `"2026-06"`, not a date. Zero-padded, so `"2026-09" < "2026-10"` is a plain string
comparison and PostgreSQL can index and `GROUP BY` it directly. A `CHECK` regex keeps the column
honest. `due_date` is derived, and clamps: billing day 31 in February becomes the 28th or 29th.

---

## Invariants, and where they live

The interesting question is not what the invariants are but **which layer enforces each one**, because
that decides what a bug can do.

| Invariant | Enforced by | Why there |
|---|---|---|
| A month is billed once per member | `UNIQUE (member_id, period, kind)` | Two concurrent billing runs must not double-bill; only the database sees both |
| One bank credit is recorded once | `UNIQUE (institution_id, utr)` | Same reason, and the key comes from the bank |
| A credit is reversed at most once | `UNIQUE` on `credit_reversals.bank_credit_id` | A retried click must be a no-op |
| A payment is voided at most once | Partial unique index on `payments.reverses_id` | Two corrections must not both take the same money back |
| Money rows are never changed or deleted | `BEFORE UPDATE OR DELETE` triggers on `payments` and `allocations` | A future careless `UPDATE` anywhere, including a psql session, has to fail |
| Nobody approves their own correction | `CHECK (decided_by <> requested_by)` | The control has to survive a bug in the code above it |
| A tenant sees only its own rows | `FORCE`d row-level security on 15 tables, keyed on `app.institution_id` | A forgotten `WHERE` clause becomes a no-op rather than a leak |
| `amount = gross - concession` | `CHECK` on `charges` | Arithmetic stated once, next to the data |
| No charge is over-allocated | Code (`settle_members` allocates `min(unspent, unpaid)`) + checked after every step by the property test | Needs the current sums, which a row constraint cannot see |
| Nobody holds unused money while owing | Code, same place | Same |
| Every credit a statement printed is in the books | `close.check_credits_landed`, which queries the books | Spans a file and a table; cannot be a constraint |

Everything a constraint *can* express is a constraint. The three that cannot are the three the
Hypothesis property test re-checks after every single operation.

The tenancy mechanism is worth a note. Every tenant query runs inside:

```python
with tenant_tx(conn, institution_id):          # SET LOCAL app.institution_id
    ...                                        # the policy filters every table
```

`set_config(..., true)` makes it **transaction-local**, so it cannot leak to the next request that
borrows the same pooled connection. With nothing set, the policy matches no rows — it fails closed, not
open. And `check_role()` refuses to start as a role with `SUPERUSER` or `BYPASSRLS`, because either
would silently skip every policy.

Two tables are deliberately outside it: `institutions` and `staff`, because an API key must be looked
up *before* the institution it belongs to is known. Those two are isolated by an explicit
`WHERE institution_id = %s` on every query, which is weaker — a missing clause there is a bug, not a
no-op — and is exactly why nothing else is written that way.

---

## State machines

Four, each with its transitions in one place.

**A bank credit.** The only state that matters to money.

```
                  rules decide
   (new) ──────────────┬──────────> matched ──────┐
                       │               ▲          │ bank returns it
                       │               │ a person │
                       └──────> needs_review ─────┼──> reversed
                                                  │
                           matched -> matched: reassign (correction rows, never a delete)
```

`needs_review` is a first-class state, not an error state. It is where the design's refusal to guess
becomes visible, and the only state a human can act on.

**An outbox message.** This is the delivery contract, so the states carry the guarantees.

```
pending ──claim (FOR UPDATE SKIP LOCKED)──> sending ──┬──> sent
   ▲                                          │       ├──> skipped   (no longer needed at send time)
   │ claim older than 60s, or send failed      │       └──> dead      (12 attempts, ~2h25m)
   └──────────────────────────────────────────┘
```

**An approval request.** `pending -> approved | rejected`, decided by someone other than the requester,
with `CHECK ((status = 'pending') = (decided_by IS NULL))` so there is no half-decided row.

**A charge's payment state** is *derived*, not stored: `unpaid`, `partially_paid`, `paid` are read from
`amount` against `SUM(allocations)`. The same reasoning as balances — a state column would be a cache.

---

## Concurrency: the locking protocol

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
than row id. [docs/parallel-import.md](docs/parallel-import.md) has the full argument and the bug this
caught.

---

## Idempotency: seven keys for seven questions

"Make it idempotent" is not one decision. Each repeated action has its own natural key, and choosing
the wrong one is how a system double-charges.

| Repeated action | Key | Whose key is it |
|---|---|---|
| The same credit arrives again | `(institution_id, utr)` | **The bank's.** Ours would differ between the alert and the statement |
| The same statement file is uploaded again | `(institution_id, file_sha256)` | Content, so a renamed file is still the same file |
| A month is billed again | `(member_id, period, kind)` | The domain's own identity for a charge |
| A reminder stage is queued again | `reminder:<charge>:<stage>` | Composite, so a job that did not run for a week sends one reminder, not three |
| A message is delivered twice | `message_id` in the payload | **The gateway's** problem to dedupe; delivery is at-least-once by design |
| A member edit is submitted twice | `version`, as an HTTP ETag | Optimistic locking: the second gets 412, not a silent overwrite |
| A correction is applied twice | `payments.reverses_id` unique | Money can be taken back once |

Two details worth defending. First, `record_credit` does a cheap `SELECT` before the `INSERT`, but
**that lookup is not the guarantee** — two requests can both see "no row". The unique constraint is the
guarantee; the lookup only saves the work of locking members for the common re-import case. Second, the
same UTR arriving with a *different amount* is not idempotent and not an error to swallow: it is a 409
for a person to look at.

---

## Module structure

Ten layers, acyclic, with the dependency direction pointing one way: **outward layers know about inner
ones, never the reverse.**

```
 main          ASGI entry point
 api           the only module that knows HTTP exists (38 endpoints)
 importer, approvals
 payments, worker, evaluation
 close, matching, reminders
 charges, outbox, metrics, continuity, cli
 institutions, members, statements
 alerts, audit, reports, banks
 db, notify, bank adapters
 money, periods, names, errors, actors, webhooks, allocation, banks.common
```

Three rules hold this shape, and `tests/test_architecture.py` reads the import graph out of the source
and asserts each one, because a layering that exists only in a README stops being true on the first busy
afternoon:

1. **No cycles.** Every module is understandable, and testable, without its callers.
2. **The pure layer is closed.** Money, time, name matching, statement parsing and the bank adapters
   depend only on each other — never on the database. So a statement can be parsed and balance-checked
   with no connection, no institution and no fixtures. (The chain is legitimately five deep:
   `banks.common` → each adapter → `banks` → `statements` → `continuity`.)
3. **HTTP lives only in `api.py`.** Business code raises `NotFound`, `Conflict`, `PreconditionFailed`,
   `Forbidden`; `api.py` maps them to 404, 409, 412, 403 in one place. A test asserts no module outside
   `api.py` raises `HTTPException`. That is what lets the worker, the CLI and the tests call the same
   functions with no request in sight.

`allocation.py` sits in the bottom layer although it writes SQL: it takes the connection as a parameter
and imports no module of the package, so nothing it does is decided by what is above it.

Behaviour that varies is injected, not imported: `create_app(dsn, send, today=…, now=…)` takes the
message sender and the clock, which is why the API tests run against a real HTTP server with a fixed
date and a fake gateway and no monkeypatching of modules.

---

## Extension points

The LLD question is "what does it take to add one?" Each of these is one declaration.

**A new bank's statement layout** — one module in `paysleuth/banks/`, exporting `NAME`, `HEADER` (the
column names that identify the export) and `narration(text)`. Add it to the `BANKS` tuple. Nothing in
`statements.py` changes; `banks.detect()` picks it by header, and unknown layouts fall back to the
generic patterns.

```python
NAME = "Kotak"
HEADER = {"transactiondate", "narration", "chqrefno", "balance"}

def narration(text: str) -> dict | None:
    m = _UPI.match(text)
    return fields("UPI", m["utr"], m["name"], m["upi"], m["note"]) if m else None
```

**A new suggestion signal** — one `Signal` entry carrying its weight, when it fires and how it reads to
the owner, over a `Facts` object computed once per member:

```python
Signal("note_says_urgent", 50,
       fires=lambda f: "URGENT" in tokens(f.credit.get("note")),
       reason=lambda f: "the note says urgent")
```

`WEIGHTS` is derived from the table, so the surface the tuner uses stays in step automatically.
Mutually exclusive signals declare a `group`, so "equals the fee" cannot also score "a whole number of
months of the fee" for the same rupees. A signal receives facts rather than a connection — which is what
keeps the scoring pass at four queries however many signals exist. There is a test that declares a new
signal at runtime and watches the review queue score and explain it.

**A new message kind** — add it to the `outbox.kind` CHECK, enqueue it in the transaction that causes
it, and the relay delivers it with the retry, ordering and quiet-hours behaviour already in place.

**A new automatic matching rule** — one branch in `decide()`, which returns a `Decision(parts, rule)`
value object. Adding one does not touch recording, allocation or receipts.

What is deliberately *not* an extension point: the five automatic rules are a closed, ordered list
rather than a plugin registry. They decide where money goes without a human, so I want them readable
top to bottom on one screen and changed with intent, not discovered at import time.

---

## Patterns used, and patterns refused

| Pattern | Where | Why |
|---|---|---|
| Transactional outbox | `outbox.py` | A receipt row is inserted in the payment's own transaction, so a payment without its receipt is impossible. Then delivery happens outside any transaction, so a slow gateway never holds a lock |
| Strategy, as a data table | `matching.SIGNALS`, `banks.BANKS` | Each variant is one declaration instead of a branch in three functions |
| Value objects (frozen dataclasses) | `Credit`, `Decision`, `Facts`, `Signal`, `Bank`, `Continuity`, `StatementRow`, `Actor` | A fact from the bank should not be mutable after parsing |
| Append-only ledger with correction rows | `payments`, `allocations` | Money history is evidence. Corrections are new rows, enforced by triggers |
| Optimistic locking | `members.version` → ETag | Human edits are long-lived and rarely conflict; pessimistic locking across a form fill is wrong |
| Pessimistic row locks | money operations | Machine writes are short and *do* conflict |
| Claim-check queue | `FOR UPDATE SKIP LOCKED` + visibility timeout | A small queue with the properties of a real one, without another system to run |
| Context variable for the actor | `actors.current_actor` | The audit log records who acted without every money function taking a `who` parameter it does not use |
| Dependency injection by parameter | `create_app(dsn, send, today=…, now=…)` | Tests get a real server, a fixed clock and a fake gateway with no patching |

Refused, with reasons I would defend:

- **No ORM.** The hard parts here are `FOR UPDATE SKIP LOCKED`, `FILTER` aggregates, partial unique
  indexes, `INSERT ... ON CONFLICT`, row-level security and CTEs. Every one of those is something I would
  be fighting an ORM to express precisely, and the mapping layer would obscure the locking, which is the
  thing I most need to reason about.
- **No Kafka, no microservices.** A 500-student centre generates a few thousand rows a month. One
  PostgreSQL does transactional outbox, queueing, tenancy and reporting here. Choosing not to use Kafka
  and being able to say why is a better answer than using it.
- **No ML matching.** In money, a rule that can be explained to the owner beats a model that cannot. The
  review queue *ranks* with weights, but ranking is a suggestion shown to a person; the five automatic
  rules are deterministic and readable.
- **No stored balances, no denormalised counters.** Covered above: every cache needs invalidation, and
  invalidation under concurrency is where the money bug lives.
- **No soft-delete flag on money.** `payments` and `allocations` are append-only, so a correction is a
  new signed row rather than a hidden one.

---

## How correctness is established

Five layers, each catching what the one above cannot. 343 tests, none mocked — every one runs against a
real PostgreSQL in its own schema, because locks, constraints and row-level security are the subject.

**1. Unit and integration tests** — the cases I thought of. 19 files; `test_reports.py` is worth
singling out, because it cross-checks every report against the others and against
`allocation.balances()`: the ageing buckets form a partition that sums three ways, collection reconciles
per month, each member statement agrees with the balances it came from, and a half-paisa concession still
reconciles.

**2. A Hypothesis state machine** — the cases I did not. `RuleBasedStateMachine` chooses the operations
(bill, late fees, pay by code, pay from a known UPI ID, pay from a stranger, re-sent alert, assign,
reassign with splits, bank reversal, concession change, time passing), and after **every step** the books
must satisfy every invariant and the daily close must account for every credited rupee. The value over a
hand-written random loop is shrinking: reintroducing the scholarship-sibling bug used to fail at "seed 3,
step 41" and now reduces to one line.

```
state.pay_from_a_known_upi_id(amount='2500', family='verma@oksbi')
```

**3. Concurrency tests** — real threads, released together on a barrier, each on its own connection, one
per row of the table above. Where an interleaving matters, it is *forced* rather than hoped for.

**4. Equivalence testing** — parallel import against a serial baseline, at four worker counts, compared
by business key. The benchmark also hashes the whole set of books into a fingerprint and prints it per
worker count; one fingerprint across four rows is the property, and it stayed identical across the
performance pass, which is how I knew that pass changed no result.

**5. Mutation testing** — `tools/mutation_check.py` removes one safety mechanism at a time and requires
its test to go red. This is the layer that answers "do your tests actually test that?":

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
statement lines not checked against the books              caught
generic parser keeps a masked UPI ID                       caught
reversals skipped in the parallel import                   caught
statement check skipped when its import did not finish     caught
a line carrying money but no readable date is dropped      caught
printed UTR not normalised before the landed check         caught

22/22 mutations caught
```

One mechanism is deliberately *not* on that list. Swapping `hmac.compare_digest` for `==` answers
identically for every input and leaks only timing, so no behaviour test can catch it. It is a review
rule, not a tested one, and the code says so.

**6. Architecture tests** — the import graph assertions described above, so the layering is a fact rather
than a claim.

### Detecting absence, not just error

This deserves its own note because it is the hardest thing in the project and the thing I got wrong
twice. Three independent checks, each catching what the others cannot:

- *Within a statement.* Where the export carries a running balance, each line's balance must equal the
  previous plus its credit minus its debit. A dropped or altered line breaks the chain, and the alert
  names the line and the unaccounted amount.
- *Between statements.* Each file's opening balance must equal the previous file's closing balance for
  that account, or a page or a day is missing.
- *Into the books.* Every distinct (UTR, amount) the statement printed must have a `bank_credits` row
  with that UTR and that amount. This one asks the database rather than trusting the importer's own
  report, so a line lost to an unreadable narration, a rejected UTR, a UTR that came back with a
  different amount, or a bug in the import itself is caught the same way.

Then the daily close asks the other direction: every rupee that landed is applied to a bill, held as an
advance, waiting for review, or returned — and no more has been applied than was received.

### Bugs these layers caught, and how

Each of these fails a test by name if reintroduced. I list them because "what did your tests actually
find" is a better question than "what is your coverage".

| Bug | Found by |
|---|---|
| A sibling on a full scholarship given a Rs 0 share of a split | Hypothesis, shrunk to one step |
| A payment unable to return to a charge it had already part-paid, after another payment was moved away | Hypothesis |
| Siblings paying before their first month not split, because no fee was yet in force | Hypothesis |
| Grouping a parallel import by each credit's *first* candidate member | Serial-vs-parallel equivalence |
| A truncated or masked UPI ID (`sunita.sha`, `**sharma@oksbi`) kept as the payer's, which then failed validation downstream and dropped the whole credit line | Reviewing the README against the code |
| **The daily close summing only the credits that landed**, so a statement line that never became a row was absent from both sides of the arithmetic and the close reported "clean" | Reviewing the README against the code — the claim was written before the check existed |
| A row discarded as a summary footer on the shape of its date cell alone, before anything checked whether it carried money | Probing that fix |
| A statement whose import died partway never reconciled, and unable to be: the check was skipped on re-import and the close read a NULL as nothing-lost | Probing that fix |
| A UTR printed lowercase reported as missing, because the check looked it up as printed while the books store it normalised | Probing that fix |
| Exception tracebacks escaping the log masking, where both `log.exception` sites are in the money path | End-to-end review of the logging module |
| A NUL byte in any text field, and an out-of-range date, returning 500 instead of 422 | API fuzzing |
| `/metrics` unauthenticated, returning every institution's member count and money owed | API review |

The second one is the one I would raise myself in an interview. The feature's whole claim was *"notices
when data is missing, not just when it's wrong,"* and it was false in exactly its own case: ₹12,000 of
real credits vanished and the close said `clean`. I had written the sentence before building the check.
The fix asks the database instead of trusting the importer — and then three more holes turned up in the
fix, which is why the check now has four tests and two mutation cases pointed at it.

---

## Trade-offs, and what I would change at ten times the size

Decisions I would defend, with the cost stated:

| Decision | Cost I accepted |
|---|---|
| Derive balances instead of storing them | Every read is an aggregate. Bounded by scale, held by query-count tests |
| Append-only money with correction rows | More rows, and every reader must sum rather than select. Bought: no money record can be lost |
| Pessimistic row locks on the money path | Writes to one member serialise (183/s measured vs 520/s across members). Bought: no lost update, and a deadlock argument I can state in one sentence |
| Thread pool, not processes, for import | Bounded by the GIL for CPU work. Fine here, because the time is spent waiting on PostgreSQL |
| Grouping the whole statement in memory | A single upload must fit in memory. True at a few thousand lines; stated as a limit |
| Five closed matching rules, not a plugin registry | Adding a rule means editing a function. Deliberate: these spend money without a human |

At ten times the size I would keep the rule and change where it lives: partition by member id with each
partition handled in statement order, which is the same idea with groups decided up front instead of
discovered. What I would not change is the proof — the equivalence test keyed on business identity is
what let me make the thing faster twice without wondering whether the books had moved.

What I would reach for before anything else, in order: a read replica for reports (they are the only
unbounded reads); `NOTIFY`/`LISTEN` to wake the relay instead of a 5-second poll; and a second
institution's real statements, because the matching weights are currently tuned against synthetic data
and the honest number for auto-match rate is "unmeasured".

### Performance, measured

On a 2-core x86_64 VM with PostgreSQL on the same machine. Full output in
[bench/results/](bench/results/); the numbers are the size of a change, not precise constants.

Queries per credit, before and after the one performance pass. The "after" column is asserted by
`tests/test_performance.py`, so it cannot drift; the two starred figures were counted against code that
no longer exists.

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

API latency, 8 clients, 300 members: 1689 requests, 67/s
endpoint                              n   p50 ms   p95 ms   p99 ms
GET /members/{id}/statement         178       64      196      239
GET /reports/ageing                 173       59      190      254
GET /reports/collection             157       60      219      287
GET /review                         158      231      351      443
POST /bank-credits                  764      114      257      332
```

The speed-up flattens early because the machine has two cores shared with PostgreSQL. Load testing found
the one real performance bug: `GET /review` re-read every member, their dues, their fees and the payer's
family **for each waiting credit** — p50 2057ms, p95 3976ms. Reading them once per page took that to
231ms and 351ms, and throughput from 26/s to 67/s. Every credit on a page is still scored against every
active member, so the page is bounded (default 50, max 200) rather than letting the largest institution
set the slowest request.

---

## Running it

```bash
docker compose up -d db
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m tools.seed_demo          # a centre mid-month: billed, imported, two credits to review
uvicorn paysleuth.main:app &       # API docs at http://localhost:8000/docs
tools/demo.sh                      # or walk a month through the API from scratch
```

Of the sample statement's five credits, three match on their own and two cannot be matched safely. Real
output from the seeded demo's review queue:

```
Rs 2500.00 from LAKSHMI IYER: meera fees
     75  PS1005 Meera Iyer: payer name matches Lakshmi Iyer; payer has the same surname;
                            note mentions Meera
Rs 12000.00 from GREEN LEAF CAFE: rent june
     15  PS1001 Diya Sharma: amount is exactly 6 months of fees
     15  PS1002 Rohan Verma: amount is exactly 6 months of fees
```

The first is a parent whose UPI ID nobody registered; assigning it remembers `lakshmi.iyer@oksbi`, and
July's transfer from her matches by itself. The second is the centre's rent arriving, and the two equal
weak suggestions are what a 15-point amount coincidence looks like — the owner leaves it alone.

The five automatic rules, in order. Only these decide without a human:

1. Exactly one reference code (`PS1004`) in the note → that member.
2. Several codes, and the amount equals exactly what they owe together → split by what each owes.
3. A UPI ID registered to exactly one member → that member.
4. A UPI ID shared by siblings, and the amount equals what they owe together or their fees together →
   split. Never a Rs 0 share, so a sibling on a full scholarship sends it to review instead.
5. Anything else → the review queue, with ranked suggestions that are shown and never applied.

### Running a pilot on one real institution's statement

The matching weights are guesses until someone's own statement scores them, so the measuring path is a
tool rather than a plan:

```bash
python -m tools.pilot setup  --name "Sunrise PG" --members members.csv   # sample_data/ has the template
python -m tools.pilot import --institution 1 --statement june.csv --as-of 2026-06-30
python -m tools.pilot review --institution 1                             # one credit at a time
python -m tools.pilot measure --institution 1
```

`review` records, for every decision, which suggestions were on screen and where in that ranking the
answer came — so `measure` can report what share of credits matched with nobody involved, how often the
top suggestion was right, and each signal's precision. It refuses to tune the weights below 50 decisions
and prints how many more are needed, because a rate over six credits is not a rate.

Two rules about the data, and they are in the tool's own help. It runs against a local database that gets
dropped afterwards, and the statement is never committed (`.gitignore` covers `*_real.csv`, `real_*.csv`
and `pilot_data/`). And the statement is *not* anonymised first: payer names and UPI IDs are exactly what
matching is scored on, so anonymising before measuring would measure the anonymiser. `tools/anonymise_statement.py`
exists for the opposite job — turning a real export into a golden test fixture that can be committed.

### Deploying

```bash
docker build -t paysleuth .
docker run -e PAYSLEUTH_DATABASE_URL=... -p 8000:8000 paysleuth                  # API
docker run -e PAYSLEUTH_DATABASE_URL=... paysleuth python -m paysleuth.worker    # worker
```

One image serves both roles, so the API and the worker cannot drift apart. Settings:
`PAYSLEUTH_DATABASE_URL`, `PAYSLEUTH_MESSAGE_WEBHOOK`, `PAYSLEUTH_WEBHOOK_SECRET`,
`PAYSLEUTH_METRICS_TOKEN`, `PAYSLEUTH_POOL_SIZE`, `PAYSLEUTH_LOG_LEVEL`. The schema is applied on boot
under an advisory lock, so there is no migration step and two instances starting together cannot collide.

`render.yaml` is a blueprint for the free tier, and three of its decisions are about what free tiers
actually do rather than what they advertise:

- **The database is Neon, not Render's.** A free Render Postgres expires 30 days after creation — the
  clock starts at creation, not first use — and the data is deleted 14 days later. A demo with a fuse in
  it is worse than no demo, so the blueprint takes a connection string instead of provisioning a database.
- **No worker service**, because Render's free tier has none. Every job the worker runs is also an
  idempotent endpoint, so `.github/workflows/scheduled-jobs.yml` pokes them hourly in the right order
  (bill, remind, relay until it claims nothing, close last). Coarser than a 5-second poll, same work.
- **`.github/workflows/keep-warm.yml`** pings `/health` every 14 minutes between 06:00 and 00:00 IST,
  because a free service sleeps after 15 idle minutes and takes about a minute to wake. 18 hours a day is
  roughly 540 of the 750 free instance hours a month; round the clock would be about 720 and leave no
  margin, which is why the window is a window.

One change the hosted database forced on the code. The free services put a transaction-mode pooler in
front of PostgreSQL, which hands the next transaction a different backend — so a statement psycopg
prepared is not there the second time it runs, and the error (`prepared statement _pg3_0 already
exists`) appears only under load, after a query has been executed enough times to be promoted.
`make_pool` recognises a pooled DSN and turns preparation off for it. Nothing else about the design
minds a pooler: row-level security is set with `SET LOCAL` and the migration lock is transaction-scoped,
so both stay on one backend for exactly as long as they need to. `tests/test_deployment.py` holds the
detection and what it does to the connection.

Outgoing messages are signed `X-PaySleuth-Signature: t=<unix>,v1=<hex HMAC-SHA256>` over
`"<t>.<body bytes>"` — signing the timestamp *with* the body is what stops a captured message being
replayed with a fresh `t`. The verifier lives in the same module, is tested against the signer through a
real HTTP gateway, and can be copied into a gateway as it is. `/metrics` needs either the scrape token
(all institutions) or a staff key (that institution only). Logs mask phone numbers and UPI IDs
(`98****3210`, `su****@oksbi`) in a filter on the handler — including exception tracebacks — so a line
added later anywhere is masked without the author remembering.

---

## Limits

Stated plainly, because an unstated limit is a claim.

- **The matching numbers are unmeasured.** `GET /reports/matching` computes the auto-match rate and
  suggestion accuracy from an institution's own recorded decisions, and `tools/tune_weights.py` tunes the
  weights against them with 5-fold cross-validation, refusing to propose anything from fewer than 50
  decisions. No real institution has used this, so there is no real-world rate to quote and none is
  invented here. The current weights are informed guesses. `tools/pilot.py` is the path from here to a
  measured number; until someone walks it, this bullet stands.
- **The bank adapters are written against published export layouts**, and the golden statements in
  `tests/golden/` are synthetic. Each should be re-checked against a real anonymised export before it is
  trusted; `tools/anonymise_statement.py` prepares one.
- **SBI and Canara print UPI IDs truncated or masked, and Axis prints none**, so for those banks matching
  relies on reference codes or review. A truncated ID is never stored as if it were the payer's.
- **A change to a statement file's very first line** is visible only against the previous statement, not
  within the file. There is a test that says so.
- **A single upload must fit in memory** for the grouping pass (10 MB cap at the API).
- **No web UI.** The API and `/docs` are the interface.
