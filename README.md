# PaySleuth

[![CI](https://github.com/SrijaMalhar/PaySleuth/actions/workflows/ci.yml/badge.svg)](https://github.com/SrijaMalhar/PaySleuth/actions/workflows/ci.yml)

Fee reconciliation for tuition centres, PGs and housing societies. It reads the bank statement they
already get, decides which member each UPI transfer belongs to, applies it to what they owe, sends
receipts and reminders — and tells the owner when the statement itself is missing a line. It never moves
money and never touches a payment network.

The CRUD here is an afternoon's work. The design problem is that the input is partial, repeated and
adversarially messy, the output is **money attributed to a named family**, and **a wrong attribution is
worse than no attribution** — the family that paid gets a reminder they do not deserve, and the family
that did not stops paying. Three real statement lines, and what each one costs:

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

## It runs

`python -m tools.walkthrough` — billing, an import that decides what it can and refuses what it cannot,
the queue a person works, one credit delivered twenty times at once, and a statement line that never
became a row. Real output, against a real PostgreSQL, into a throwaway schema.

![A recorded run of tools/walkthrough.py](docs/walkthrough.gif)

The last beat is the one to watch. The bank printed ₹12,000, nothing in the books corresponds to it, and
summing what landed would have balanced perfectly — because a lost line is absent from *both* sides of
the arithmetic. Catching that needs a third question, asked of the database rather than of the importer.

### What is proven, not claimed

| | |
|---|---|
| **347 tests, zero mocks** | every one against a real PostgreSQL in its own schema, because locks, constraints and row-level security *are* the subject |
| **22/22 mutations caught** | each safety mechanism deleted one at a time; the test that covers it must go red |
| **9 concurrency races** | real threads released on a barrier — and where an interleaving matters it is *forced* by polling `pg_stat_activity`, not hoped for |
| **1 fingerprint, 4 worker counts** | parallel import produces books that hash identically to a serial import; equivalence tested at 1, 4, 8 and 16 workers |
| **12 bugs, listed by name** | including the one where a feature's headline claim in this README was false |
| **10 layers, asserted** | `test_architecture.py` reads the AST import graph: acyclic, pure layer closed, HTTP confined to one module |

Python 3.12 · FastAPI · PostgreSQL 16 · psycopg 3 · no ORM · 4,300 lines of application code, a 320-line
schema, 4,200 lines of tests.

**Deeper than this page goes:** [concurrency](docs/concurrency.md) ·
[how correctness is established](docs/correctness.md) ·
[parallel import equivalence](docs/parallel-import.md) · [running a pilot and deploying](docs/deploying.md)

---

## The design problem

Four properties of the input drive every decision in this codebase:

| Property of the input | What it forces in the design |
|---|---|
| The same credit arrives more than once (bank alert, then the statement, then a re-import) | Every write has to be idempotent on a key the *bank* chose, not one we generate |
| One transfer can pay for several people | Attribution is a decision over a set, and the money has to split by what each owes *at that instant* |
| The payer identifies themselves inconsistently or not at all | Matching needs tiers of confidence, and an explicit "a person must decide" state |
| Lines go missing, repeat, or arrive out of order | The system has to detect absence, which is harder than detecting error |

And one constraint the asymmetry above imposes on the output: every automatic decision has to be
explainable in one sentence to a tuition centre owner. That rules out a model, and it is why the five
rules that spend money without a human are a closed ordered list rather than a plugin registry.

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
money between two families — an incremented counter is where the bug lives. Deriving the number means a
lost update can corrupt *a row*, but it cannot corrupt *the answer*: the sum is recomputed from rows that
are themselves constrained.

What it costs: every balance read is an aggregate. The mitigation is that the aggregate is cheap at this
scale, indexed for it, and never done in a loop — `settle_members()` and `balances_with_details()` take a
list of members and answer in one query each. `tests/test_performance.py` holds the query count per
credit to a constant so a per-member loop cannot creep back in.

### Money is integer paise, and months are strings

Amounts are parsed once with `Decimal` at the edge, stored as `bigint`, and formatted back to a rupee
string at the edge. No float ever touches an amount, because `int(0.29 * 100)` is 28. Three decimal
places are *refused*, not rounded — a fraction of a paisa is bad input, not an amount. Amounts cross the
API as strings (`"2000.50"`), never JSON numbers, because many clients parse those as floats.

A billing month is `"2026-06"`, not a date. Zero-padded, so `"2026-09" < "2026-10"` is a plain string
comparison and PostgreSQL can index and `GROUP BY` it directly. A `CHECK` regex keeps the column honest.
`due_date` is derived, and clamps: billing day 31 in February becomes the 28th or 29th.

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

Everything a constraint *can* express is a constraint. The three that cannot are the three the Hypothesis
property test re-checks after every single operation.

The tenancy mechanism is worth a note. Every tenant query runs inside `tenant_tx(conn, institution_id)`,
which does `SET LOCAL app.institution_id` via `set_config(..., true)` — **transaction-local**, so it
cannot leak to the next request that borrows the same pooled connection. With nothing set, the policy
matches no rows: it fails closed, not open. And `check_role()` refuses to start as a role with
`SUPERUSER` or `BYPASSRLS`, because either would silently skip every policy. Two tables are deliberately
outside it — `institutions` and `staff`, because an API key must be looked up *before* the institution it
belongs to is known. Those two are isolated by an explicit `WHERE institution_id = %s` on every query,
which is weaker (a missing clause there is a bug, not a no-op) and is exactly why nothing else is written
that way.

## State machines

**A bank credit** is the only state that matters to money.

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

**An outbox message** carries the delivery contract in its states.

```
pending ──claim (FOR UPDATE SKIP LOCKED)──> sending ──┬──> sent
   ▲                                          │       ├──> skipped   (no longer needed at send time)
   │ claim older than 60s, or send failed     │       └──> dead      (12 attempts, ~2h25m)
   └──────────────────────────────────────────┘
```

**An approval request** is `pending -> approved | rejected`, decided by someone other than the requester,
with `CHECK ((status = 'pending') = (decided_by IS NULL))` so there is no half-decided row.

**A charge's payment state** is *derived*, not stored: `unpaid`, `partially_paid`, `paid` are read from
`amount` against `SUM(allocations)`. Same reasoning as balances — a state column would be a cache.

## Concurrency: one lock order

Every money operation takes locks in the same order: the `approval_requests` row, then the
`bank_credits` row, then member rows **in ascending id order**.

**Ascending id order is the whole deadlock argument.** If every transaction takes the lower id first, no
transaction can hold B while waiting for A, so a wait cycle cannot form. The alternative — locking "from,
then to" when moving money — deadlocks the moment one clerk moves money from Diya to Kabir while another
moves different money from Kabir to Diya. `lock_members()` is the only place that takes a member lock,
and it sorts; there is no second path to get it wrong.

A second ordering decision: **decide while holding the locks, then write.** A sibling split divides by
what each child owes *right now*, so the candidate members are locked, then the rules run, then the credit
row is inserted with the status it ends up in.

Nine races have a test each, and where an interleaving matters it is forced rather than hoped for — the
late-fee test polls `pg_stat_activity` until the job is observably blocked on the member lock, *then*
lands the payment, so the worst order happens on every run. Parallel import is grouped by a union-find
over (UTR, member) nodes so that it is not merely safe but **deterministic**: the same upload must text a
parent the same thing twice. → [docs/concurrency.md](docs/concurrency.md)

## Idempotency: seven keys for seven questions

"Make it idempotent" is not one decision. Each repeated action has its own natural key, and choosing the
wrong one is how a system double-charges.

| Repeated action | Key | Whose key is it |
|---|---|---|
| The same credit arrives again | `(institution_id, utr)` | **The bank's.** Ours would differ between the alert and the statement |
| The same statement file is uploaded again | `(institution_id, file_sha256)` | Content, so a renamed file is still the same file |
| A month is billed again | `(member_id, period, kind)` | The domain's own identity for a charge |
| A reminder stage is queued again | `reminder:<charge>:<stage>` | Composite, so a job that did not run for a week sends one reminder, not three |
| A message is delivered twice | `message_id` in the payload | **The gateway's** problem to dedupe; delivery is at-least-once by design |
| A member edit is submitted twice | `version`, as an HTTP ETag | Optimistic locking: the second gets 412, not a silent overwrite |
| A correction is applied twice | `payments.reverses_id` unique | Money can be taken back once |

Two details worth defending. `record_credit` does a cheap `SELECT` before the `INSERT`, but **that lookup
is not the guarantee** — two requests can both see "no row". The unique constraint is the guarantee; the
lookup only saves the work of locking members for the common re-import case. And the same UTR arriving
with a *different amount* is not idempotent and not an error to swallow: it is a 409 for a person to look
at.

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

Three rules hold this shape, and `tests/test_architecture.py` reads the import graph out of the source and
asserts each one, because a layering that exists only in a README stops being true on the first busy
afternoon:

1. **No cycles.** Every module is understandable, and testable, without its callers.
2. **The pure layer is closed.** Money, time, name matching, statement parsing and the bank adapters
   depend only on each other — never on the database. So a statement can be parsed and balance-checked
   with no connection, no institution and no fixtures.
3. **HTTP lives only in `api.py`.** Business code raises `NotFound`, `Conflict`, `PreconditionFailed`,
   `Forbidden`; `api.py` maps them to 404, 409, 412, 403 in one place. A test asserts no module outside
   `api.py` raises `HTTPException`. That is what lets the worker, the CLI and the tests call the same
   functions with no request in sight.

Behaviour that varies is injected, not imported: `create_app(dsn, send, today=…, now=…)` takes the message
sender and the clock, which is why the API tests run against a real HTTP server with a fixed date and a
fake gateway and no monkeypatching of modules.

## Extension points

The LLD question is "what does it take to add one?" Each of these is one declaration.

**A new bank's statement layout** — one module in `paysleuth/banks/` exporting `NAME`, `HEADER` and
`narration(text)`, added to the `BANKS` tuple. Nothing in `statements.py` changes; `banks.detect()` picks
it by header, and unknown layouts fall back to the generic patterns.

**A new suggestion signal** — one `Signal` entry carrying its weight, when it fires and how it reads to the
owner, over a `Facts` object computed once per member:

```python
Signal("note_says_urgent", 50,
       fires=lambda f: "URGENT" in tokens(f.credit.get("note")),
       reason=lambda f: "the note says urgent")
```

`WEIGHTS` is derived from the table, so the surface the tuner uses stays in step automatically. Mutually
exclusive signals declare a `group`, so "equals the fee" cannot also score "a whole number of months of the
fee" for the same rupees. A signal receives facts rather than a connection — which is what keeps the
scoring pass at four queries however many signals exist. There is a test that declares a new signal at
runtime and watches the review queue score and explain it.

**A new message kind** — add it to the `outbox.kind` CHECK and enqueue it in the transaction that causes it;
the relay delivers it with the retry, ordering and quiet-hours behaviour already in place.

What is deliberately *not* an extension point: the five automatic rules are a closed, ordered list rather
than a plugin registry. They decide where money goes without a human, so they should be readable top to
bottom on one screen and changed with intent, not discovered at import time.

## Patterns used, and patterns refused

| Pattern | Where | Why |
|---|---|---|
| Transactional outbox | `outbox.py` | A receipt row is inserted in the payment's own transaction, so a payment without its receipt is impossible. Delivery then happens outside any transaction, so a slow gateway never holds a lock |
| Strategy, as a data table | `matching.SIGNALS`, `banks.BANKS` | Each variant is one declaration instead of a branch in three functions |
| Value objects (frozen dataclasses) | `Credit`, `Decision`, `Facts`, `Signal`, `Bank`, `Continuity`, `StatementRow`, `Actor` | A fact from the bank should not be mutable after parsing |
| Append-only ledger with correction rows | `payments`, `allocations` | Money history is evidence. Corrections are new rows, enforced by triggers |
| Optimistic locking | `members.version` → ETag | Human edits are long-lived and rarely conflict; pessimistic locking across a form fill is wrong |
| Pessimistic row locks | money operations | Machine writes are short and *do* conflict |
| Claim-check queue | `FOR UPDATE SKIP LOCKED` + visibility timeout | A small queue with the properties of a real one, without another system to run |
| Context variable for the actor | `actors.current_actor` | The audit log records who acted without every money function taking a `who` parameter it does not use |

Refused, with reasons I would defend:

- **No ORM.** The hard parts here are `FOR UPDATE SKIP LOCKED`, `FILTER` aggregates, partial unique
  indexes, `INSERT ... ON CONFLICT`, row-level security and CTEs. Every one is something I would be
  fighting an ORM to express precisely, and the mapping layer would obscure the locking, which is the
  thing I most need to reason about.
- **No Kafka, no microservices.** A 500-student centre generates a few thousand rows a month. One
  PostgreSQL does transactional outbox, queueing, tenancy and reporting here. Choosing not to use Kafka
  and being able to say why is a better answer than using it.
- **No ML matching.** In money, a rule that can be explained to the owner beats a model that cannot. The
  review queue *ranks* with weights, but ranking is a suggestion shown to a person; the five automatic
  rules are deterministic and readable.
- **No stored balances, no denormalised counters.** Every cache needs invalidation, and invalidation
  under concurrency is where the money bug lives.
- **No soft-delete flag on money.** `payments` and `allocations` are append-only, so a correction is a new
  signed row rather than a hidden one.

## How correctness is established

Six layers, each catching what the one above cannot:

1. **Unit and integration tests** — the cases I thought of. `test_reports.py` cross-checks every report
   against the others and against `allocation.balances()`.
2. **A Hypothesis state machine** — the cases I did not. It chooses the operations and re-checks every
   invariant after **every step**. The value over a random loop is shrinking: a bug that used to fail at
   "seed 3, step 41" now reduces to one line.
3. **Concurrency tests** — real threads on a barrier, each on its own connection, with the bad
   interleaving *forced*.
4. **Equivalence testing** — parallel import against a serial baseline at four worker counts, compared by
   business key, with the whole set of books hashed to one fingerprint.
5. **Mutation testing** — `tools/mutation_check.py` removes one safety mechanism at a time and requires its
   test to go red. 22/22. This is the layer that answers "do your tests actually test that?"
6. **Architecture tests** — the import graph assertions above, so the layering is a fact rather than a
   claim.

One mechanism is deliberately *not* mutation-tested. Swapping `hmac.compare_digest` for `==` answers
identically for every input and leaks only timing, so no behaviour test can catch it. It is a review rule,
not a tested one, and the code says so.

→ **[docs/correctness.md](docs/correctness.md)** has all 22 mutations and the 12 real bugs these layers
found, each named with the layer that caught it.

### The one I would raise myself

The daily close's whole claim was *"PaySleuth notices when data is missing, not just when it's wrong"* —
and it was false in exactly its own case. It summed `bank_credits`, so a statement line that never became
a row was absent from both sides of the arithmetic: ₹12,000 of real credits vanished and the close
reported `clean`. I had written the sentence before building the check.

The fix asks the database instead of trusting the importer: every distinct (UTR, amount) the statement
printed must have a row. Then three more holes turned up in the fix itself — a footer misread as money, a
statement whose import died never re-checked, a UTR compared unnormalised — which is why that check now
has four tests and two mutation cases pointed at it. It is the last beat of the walkthrough above.

## Trade-offs, and what I would change at ten times the size

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
discovered. What I would not change is the proof — the equivalence test keyed on business identity is what
let me make the thing faster twice without wondering whether the books had moved.

What I would reach for first, in order: a read replica for reports (the only unbounded reads);
`NOTIFY`/`LISTEN` to wake the relay instead of a 5-second poll; and a second institution's real
statements, because the honest number for auto-match rate is still "unmeasured".

### Performance, measured

On a 2-core x86_64 VM with PostgreSQL on the same machine. Full output in [bench/results/](bench/results/).

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

## Running it

```bash
docker compose up -d db
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

python -m tools.walkthrough        # the five beats in the GIF above
python -m tools.seed_demo          # a centre mid-month, for clicking through /docs
uvicorn paysleuth.main:app &       # API docs at http://localhost:8000/docs
pytest -q                          # 347 tests
python tools/mutation_check.py     # 22 safety mechanisms, each deleted and caught
```

The five automatic rules, in order. Only these decide without a human:

1. Exactly one reference code (`PS1004`) in the note → that member.
2. Several codes, and the amount equals exactly what they owe together → split by what each owes.
3. A UPI ID registered to exactly one member → that member.
4. A UPI ID shared by siblings, and the amount equals what they owe together or their fees together →
   split. Never a ₹0 share, so a sibling on a full scholarship goes to review instead.
5. Anything else → the review queue, with ranked suggestions that are shown and never applied.

Outgoing messages are signed `X-PaySleuth-Signature: t=<unix>,v1=<hex HMAC-SHA256>` over
`"<t>.<body bytes>"` — signing the timestamp *with* the body is what stops a captured message being
replayed with a fresh `t`. `/metrics` needs either the scrape token or a staff key. Logs mask phone
numbers and UPI IDs (`98****3210`, `su****@oksbi`) in a filter on the handler — including exception
tracebacks — so a line added later anywhere is masked without the author remembering.

→ **[docs/deploying.md](docs/deploying.md)** for the deploy, including the two things a managed PostgreSQL
forces on the code: its owner role carries `BYPASSRLS`, and its pooler breaks server-side prepared
statements.

## Limits

Stated plainly, because an unstated limit is a claim.

- **The matching numbers are unmeasured.** `GET /reports/matching` computes the auto-match rate and
  suggestion accuracy from an institution's own recorded decisions, and `tools/tune_weights.py` tunes the
  weights against them with 5-fold cross-validation, refusing to propose anything from fewer than 50
  decisions. No real institution has used this, so there is no real-world rate to quote and none is
  invented here. The current weights are informed guesses, and `tools/pilot.py` is the path from here to a
  measured number.
- **The bank adapters are written against published export layouts**, and the golden statements in
  `tests/golden/` are synthetic. Each should be re-checked against a real anonymised export before it is
  trusted; `tools/anonymise_statement.py` prepares one.
- **SBI and Canara print UPI IDs truncated or masked, and Axis prints none**, so for those banks matching
  relies on reference codes or review. A truncated ID is never stored as if it were the payer's.
- **A change to a statement file's very first line** is visible only against the previous statement, not
  within the file. There is a test that says so.
- **No rate limiting.** An API key can call any endpoint as fast as it likes. For a money API that is a
  real gap, not a style choice.
- **A single upload must fit in memory** for the grouping pass (10 MB cap at the API).
- **No web UI.** The API and `/docs` are the interface.
