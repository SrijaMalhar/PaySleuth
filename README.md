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
| **358 tests, zero mocks** | every one against a real PostgreSQL in its own schema, because locks, constraints and row-level security *are* the subject |
| **22/22 mutations caught** | each safety mechanism deleted one at a time; the test that covers it must go red |
| **9 concurrency races** | real threads released on a barrier — and where an interleaving matters it is *forced* by polling `pg_stat_activity`, not hoped for |
| **1 fingerprint, 4 worker counts** | parallel import produces books that hash identically to a serial import; equivalence tested at 1, 4, 8 and 16 workers |
| **12 bugs, listed by name** | including the one where a feature's headline claim in this README was false |
| **10 layers, asserted** | `test_architecture.py` reads the AST import graph: acyclic, pure layer closed, HTTP confined to one module |

Python 3.12 · FastAPI · PostgreSQL 16 · psycopg 3 · no ORM · 4,300 lines of application code, a 320-line
schema, 4,200 lines of tests.

**The design, in order:** [domain model](#domain-model) · [object model](#the-object-model) ·
[invariants and where each is enforced](#invariants-and-where-they-live) ·
[the critical path, step by step](#the-critical-path) · [state machines](#state-machines) ·
[the lock order](#concurrency-one-lock-order) · [seven idempotency keys](#idempotency-seven-keys-for-seven-questions) ·
[layering](#module-structure) · [extension points](#extension-points) ·
[principles](#design-principles-and-the-line-of-code-each-one-is)

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

Eighteen tables. The ones money moves through, and the direction each relationship points:

```mermaid
erDiagram
    INSTITUTIONS  ||--o{ STAFF            : "API key -> owner | clerk"
    INSTITUTIONS  ||--o{ MEMBERS          : has
    MEMBERS       ||--o{ MEMBER_FEES      : "fee history, effective_period -> amount"
    MEMBERS       }o--o{ MEMBER_UPI_IDS   : "siblings share a parent's ID"
    MEMBERS       ||--o{ CHARGES          : "owes, unique (member, period, kind)"
    INSTITUTIONS  ||--o{ BANK_CREDITS     : "unique (institution, utr)"
    BANK_CREDITS  ||--o{ PAYMENTS         : "one member's share, append-only"
    PAYMENTS      ||--o{ ALLOCATIONS      : "signed, append-only"
    CHARGES       ||--o{ ALLOCATIONS      : "settled by"
    BANK_CREDITS  ||--o| CREDIT_REVERSALS : "the bank took it back"
    BANK_CREDITS  ||--o| REVIEW_OUTCOMES  : "what a person chose, vs what was suggested"
    BANK_CREDITS  }o--|| STATEMENT_IMPORTS: "the file it came from"
```

**There is no edge from `members` to a balance**, and that absence is the design. `outstanding` is
`SUM(charges) - SUM(allocations)`, computed on demand. Everything below follows from it.

`payments` and `allocations` are the only append-only tables, enforced by triggers rather than by
convention. A correction is a new row with a negative amount and a `reverses_id` pointing at what it
undoes — so the history of a disputed payment is readable in the table itself, which is what you want the
first time a parent says "I did pay in May".

## The object model

The pure layer — everything that can be unit-tested with no database — is six frozen dataclasses and two
functions. Frozen because a fact from the bank does not change after parsing, and a `Decision` that could
be mutated between being made and being written is a bug waiting for a maintainer.

```mermaid
classDiagram
    class Credit {
        <<frozen>>
        +str utr
        +int amount "paise, never float"
        +datetime received_at
        +str|None payer_upi_id
        +str|None payer_name
        +str|None note
        +int line
    }
    class Decision {
        <<frozen>>
        +tuple parts "member_id and paise pairs"
        +str|None rule "empty parts == needs_review"
    }
    class Facts {
        <<frozen>>
        +dict member
        +dict credit
        +int owed
        +about(member, credit, context)$ Facts
    }
    class Signal {
        <<frozen>>
        +str name
        +int weight
        +Callable~Facts,bool~ fires
        +Callable~Facts,str~ reason
        +str|None group "mutually exclusive"
    }
    class StatementRow {
        <<frozen>>
        +int line
        +int debit
        +int credit
        +int|None balance
    }
    class Continuity {
        <<frozen>>
        +str status
        +int|None opening
        +int|None closing
    }

    Signal ..> Facts : reads
    Facts ..> Credit : wraps
    Decision <.. Signal : "ranks, never decides"
    Continuity <.. StatementRow : "checks the running balance"
```

`Signal` is the one to look at. A reason to believe a credit belongs to a member is **one value**
carrying its weight, its predicate and its explanation together — not a method on a class, not a branch
in a function. `SIGNALS` is a tuple of them and `WEIGHTS` is derived from it, so a new reason is one
entry and the tuner's surface stays in step automatically.

`Facts` exists to keep a promise about complexity: a signal receives facts, never a connection. That is
what holds the scoring pass at four queries however many signals exist, and it is enforced structurally —
there is no `conn` in scope for a signal to reach for.

### Service boundaries

Not classes. Each module is a namespace of functions over `(conn, institution_id, …)`, because the unit
of consistency here is a transaction, not an object graph, and a service object holding a connection
would invite a second caller to use it outside one.

```python
# payments.py — the money path
def record_credit(conn, inst: int, credit: Credit) -> dict
def assign_credit(conn, inst: int, credit_id: int, member_id: int, *, remember_upi_id=True) -> dict
def split_credit(conn, inst: int, credit_id: int, parts) -> dict
def reverse_credit(conn, inst: int, credit_id: int, *, reason=None, reference=None) -> dict
def review_queue(conn, inst: int, as_of: date, limit: int = 50) -> list[dict]

# matching.py — pure decision, then ranked advice
def decide(conn, amount: int, payer_upi_id, note, as_of: date, *, members, source) -> Decision
def suggest(conn, credit: dict, as_of: date, limit: int | None = 3, weights=None) -> list[dict]

# allocation.py — set-based, never per-member
def settle_members(conn, inst: int, member_ids) -> list[dict]
def balances_with_details(conn, member_ids) -> dict[int, dict]

# db.py — the two primitives every money operation is built from
@contextmanager
def tenant_tx(conn, institution_id: int)        # SET LOCAL app.institution_id; fails closed
def lock_members(conn, member_ids) -> list[int] # sorts ascending: the deadlock argument
```

Three things are deliberate in those signatures. `settle_members` takes a **list** so that settling forty
members is two queries and one multi-row insert rather than forty round trips — the plural is the API, and
`settle_member` is a one-line wrapper over it, not the other way round. `decide` returns a `Decision`
rather than writing anything, so the rules are testable without a credit row existing. And `tenant_tx` is a
context manager rather than a parameter, because row-level security has to be set on the connection for
the whole transaction and nothing else must be able to forget it.

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
| A tenant sees only its own rows | `FORCE`d row-level security on 16 tables, keyed on `app.institution_id` | A forgotten `WHERE` clause becomes a no-op rather than a leak |
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

## The critical path

One credit arriving is the whole system in miniature: tenancy, idempotency, locking, the decision, the
allocation and the outgoing receipt. The order of the first four steps is the design.

```mermaid
sequenceDiagram
    participant API as api/credits.py
    participant P as payments.record_credit
    participant DB as PostgreSQL
    participant M as matching.decide
    participant A as allocation.settle_members
    participant O as outbox

    API->>P: Credit(utr, amount, payer_upi_id, note)
    activate P
    P->>DB: BEGIN + SET LOCAL app.institution_id
    Note over P,DB: every later query is inside row-level security

    P->>DB: SELECT ... WHERE utr = ?
    DB-->>P: nothing (the cheap path, not the guarantee)

    P->>DB: candidate_members(payer_upi_id, note)
    P->>DB: SELECT ... WHERE id = ANY(?) ORDER BY id FOR UPDATE
    Note over P,DB: ascending id: no wait cycle can form

    P->>M: decide(amount, upi, note, as_of, members)
    M-->>P: Decision(parts, rule)
    Note over M: decided while the locks are held,<br/>so dues cannot move underneath it

    P->>DB: INSERT ... ON CONFLICT (institution_id, utr) DO NOTHING
    alt no row returned
        DB-->>P: another transaction won the UTR
        P->>DB: re-read the winner's row
        P-->>API: {duplicate: true}
    else inserted
        P->>A: settle_members(member_ids)
        A->>DB: 2 queries + 1 multi-row INSERT
        A-->>P: allocations
        P->>O: enqueue(receipt)
        Note over P,O: same transaction as the payment:<br/>a payment without its receipt is impossible
        P->>DB: COMMIT
        P-->>API: {applied: [...]}
    end
    deactivate P

    Note over O: delivery happens later, outside any transaction,<br/>so a slow gateway never holds a lock
```

Four decisions in that order, each of which would be a bug reversed:

1. **Tenancy before anything.** `SET LOCAL` is transaction-local, so it cannot leak to the next request
   that borrows this pooled connection. With nothing set, the policy matches no rows — it fails closed.
2. **Lock before deciding.** A sibling split divides by what each child owes *right now*. Deciding first
   and locking second would let a concurrent payment change the answer between the two.
3. **Decide before inserting.** The credit row is written once, with the status it ends up in. Insert-then-
   update would make `needs_review` a transient state that a reader could observe.
4. **The constraint is the guarantee.** The `SELECT` at the top is an optimisation for the common re-import;
   two transactions can both see "no row". `ON CONFLICT DO NOTHING` returning no row is the real
   duplicate detection, and the second caller re-reads the winner.

## State machines

**A bank credit** — the only state that matters to money.

```mermaid
stateDiagram-v2
    [*] --> matched : a rule fired
    [*] --> needs_review : no rule was confident
    needs_review --> matched : a person assigned or split it
    matched --> matched : reassigned (correction rows, never a delete)
    matched --> reversed : the bank took it back
    needs_review --> reversed : the bank took it back
    reversed --> [*]

    note right of needs_review
        a first-class state, not an error state:
        the only one a human can act on
    end note
```

**An outbox message** — the delivery contract, written as states.

```mermaid
stateDiagram-v2
    [*] --> pending : enqueued in the caller's transaction
    pending --> sending : claimed, FOR UPDATE SKIP LOCKED
    sending --> sent
    sending --> skipped : no longer needed at send time
    sending --> pending : send failed, or the claim aged past 60s
    sending --> dead : 12 attempts, backoff 5s to 3600s (~2h25m)
    sent --> [*]
```

The 60-second visibility timeout is what makes a crashed worker recoverable without a separate reaper: a
claim older than that is simply claimable again. Backoff reaching its cap before the attempt limit is
deliberate — with eight attempts the 3600s ceiling was unreachable, which made it a lie in the config.

**An approval request** is `pending -> approved | rejected`, decided by someone other than the requester,
with `CHECK ((status = 'pending') = (decided_by IS NULL))` so there is no half-decided row.

**A charge's payment state** is *derived*, not stored: `unpaid`, `partially_paid`, `paid` are read from
`amount` against `SUM(allocations)`. Same reasoning as balances — a state column would be a cache, and a
cache needs invalidation.

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

```mermaid
graph TD
    subgraph L10[" "]
        main["main · ASGI entry"]
    end
    subgraph L9[" "]
        api["api/ · the only package that knows HTTP exists · 9 modules, 37 endpoints"]
    end
    subgraph L8[" "]
        importer
        approvals
    end
    subgraph L7[" "]
        payments
        worker
        evaluation
    end
    subgraph L6[" "]
        close
        matching
        reminders
    end
    subgraph L5[" "]
        charges
        outbox
        metrics
        continuity
        cli
    end
    subgraph L4[" "]
        institutions
        members
        statements
    end
    subgraph L3[" "]
        alerts
        audit
        reports
        banks
    end
    subgraph L2[" "]
        db
        notify
        adapters["banks/hdfc · sbi · icici · axis · canara · kotak"]
    end
    subgraph L1["the pure layer — no database in scope"]
        money
        periods
        names
        errors
        actors
        webhooks
        allocation
    end

    main --> api --> L8 --> L7 --> L6 --> L5 --> L4 --> L3 --> L2 --> L1

    style L1 fill:#0d1117,stroke:#2ea043,color:#c9d1d9
```

Three rules hold this shape, and `tests/test_architecture.py` reads the import graph out of the source and
asserts each one, because a layering that exists only in a README stops being true on the first busy
afternoon:

1. **No cycles.** Every module is understandable, and testable, without its callers.
2. **The pure layer is closed.** Money, time, name matching, statement parsing and the bank adapters
   depend only on each other — never on the database. So a statement can be parsed and balance-checked
   with no connection, no institution and no fixtures.
3. **HTTP lives only in `api/`.** Business code raises `NotFound`, `Conflict`, `PreconditionFailed`,
   `Forbidden`; the package maps them to 404, 409, 412, 403 in one place. A test asserts no module
   outside it raises `HTTPException`, and a second asserts nothing outside it is ever imported *by* the
   domain — the package is a sink. That is what lets the worker, the CLI and the tests call the same
   functions with no request in sight.

   The package is routers by resource (`members`, `credits`, `jobs`, `staff`, `reports`, `system`) over
   shared `deps` and `schemas`; what stays in `__init__` is what is true of every request — who is
   calling, how fast they may go, whether this is a retry, and how a domain error becomes a status code.
   Routers are built at import time and cannot close over `create_app`'s arguments, so the clock, the
   message sender and the pool live on `app.state` and `deps` reads them back. That indirection is the
   only thing the split cost.

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

## Design principles, and the line of code each one is

Named, because a reviewer should not have to infer them — and tied to something checkable, because a
principle you cannot point at is a principle you are not following.

| Principle | Where it is | What would break without it |
|---|---|---|
| **Single responsibility** | One module per concern, ten acyclic layers, asserted from the AST by `test_architecture.py` | The layering stops being true on the first busy afternoon, and nobody notices |
| **Open/closed** | `SIGNALS` is a tuple of `Signal` values; `BANKS` a tuple of adapters. A new reason or a new bank is one entry — no existing function changes | Every new bank adds a branch to the parser, and the parser becomes the file nobody will touch |
| **Liskov substitution** | Every bank adapter satisfies the same three names (`NAME`, `HEADER`, `narration`), and the generic fallback is one of them | `detect()` would need to know which adapters are "real", and the fallback would be a special case forever |
| **Interface segregation** | A signal receives `Facts`, not a connection. There is no `conn` in scope for it to reach for | A signal could issue a query, and the scoring pass would go from 4 queries to 4 + N |
| **Dependency inversion** | `create_app(dsn, send, today=…, now=…)` injects the gateway and the clock onto `app.state`, and `api/deps.py` reads them back. Business code raises `NotFound`/`Conflict`; only the api package knows those are 404 and 409 | Tests monkeypatch modules, and the worker and CLI cannot call the same functions |
| **Fail closed** | RLS with nothing set matches no rows; `check_role()` refuses to start as a role that bypasses it | A forgotten `WHERE` becomes a leak instead of a no-op |
| **Make illegal states unrepresentable** | `CHECK ((status = 'pending') = (decided_by IS NULL))`; `CHECK (decided_by <> requested_by)`; append-only triggers | A half-decided approval, or a self-approved correction, is reachable from any code path |
| **Primitive obsession, avoided** | Paise as `int` with `Decimal` only at the edge; periods as zero-padded `str`; `Credit`, `Decision`, `Facts` as frozen types | `int(0.29 * 100)` is 28, and `"2026-9" < "2026-10"` is false |

Two places this code deliberately departs from the textbook, and the reasons I would give:

**No service classes.** Each module is a namespace of functions over `(conn, institution_id, …)` rather
than a `PaymentService` holding a connection. The unit of consistency here is a transaction, and an object
that owns a connection invites a second caller to use it outside one — which is exactly how a lock gets
held across a network call. Passing the connection makes the transaction boundary visible at every call
site.

**No repository layer.** The hard parts are `FOR UPDATE SKIP LOCKED`, partial unique indexes,
`INSERT … ON CONFLICT`, `FILTER` aggregates and row-level security. A repository abstraction over those
would either leak them all or hide the locking, and the locking is the thing I most need to reason about.
The cost I accept: SQL is spread across modules rather than centralised, and swapping databases would be
a rewrite rather than a reimplementation. For a system whose correctness *is* PostgreSQL's concurrency
semantics, that trade is the right way round.

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

- **No ORM, no repository layer.** Covered above: the mapping layer would obscure the locking.
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
python -m tools.seed_history       # four months, 40 members, four banks' layouts
uvicorn paysleuth.main:app &       # API docs at http://localhost:8000/docs
pytest -q                          # 358 tests
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
replayed with a fresh `t`. `/metrics` needs either the scrape token or a staff key.

Logs are one JSON object per line, because the place they end up has a query box rather than a reader
(`PAYSLEUTH_LOG_FORMAT=text` for when that reader is your own terminal). Phone numbers and UPI IDs are
masked (`98****3210`, `su****@oksbi`) in a filter on the *handler*, so a line added later anywhere is
masked without the author remembering — including exception tracebacks, and including the structured
fields a caller attaches with `extra=`. That last one was not a gap until the JSON formatter existed:
the text format never printed those, so nothing masked them. Changing how logs are written changed what
counts as a leak, which is the sort of thing a format migration quietly does.

Every caller gets a token bucket: 120 requests a minute, burst 30, refused with `429` and a `Retry-After`
a client can trust. It is in process rather than in PostgreSQL — a database-backed limiter would be exact
across instances and would add a write to every request, including the ones it exists to make cheap. With
N instances the effective limit is N times the configured one. `/health` is exempt, because it is what
the uptime check and the scheduled jobs poll.

A client may send `Idempotency-Key` on any mutating request and get the first reply back instead of
acting twice — the one thing the seven domain keys cannot do, since `POST /members` has no natural
identity and two students called Diya Sharma are two students.

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
