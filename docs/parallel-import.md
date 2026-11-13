# How I made parallel import give the same result as serial import

A tuition centre uploads one CSV: a month of bank statement, a few thousand lines. Each credit line
needs a handful of queries (look up the payer, lock the members, record the payment, allocate it,
queue a receipt), and almost all of that time is spent waiting for PostgreSQL. Done one line at a
time, a 5,000-line statement took 19 seconds on my machine. Done on eight threads, 10 seconds.

The interesting part is not the speed. It is that the fast version had to produce *exactly* the same
books as the slow one, and proving that turned out to be the whole design.

## Why "the locks make it safe" was not enough

Every write already takes the member's row lock in ascending id order, so no two threads can corrupt
each other. I could have handed credits to threads at random and the books would still have been
valid: no charge paid twice, no money invented.

Valid, but not the same every time. Two of Diya's payments arriving in one statement is the simple
case:

- Rs 1,500 at 10:00, then Rs 1,000 at 14:00, against a Rs 2,000 June fee.
- Serial: the 10:00 payment pays Rs 1,500 of June; the 14:00 payment pays the last Rs 500 and holds
  Rs 500 as credit.
- Reversed: the 14:00 payment pays Rs 1,000 of June; the 10:00 payment pays the remaining Rs 1,000
  and holds Rs 500.

Same balances, different allocation rows, so a different receipt goes to the parent: one says
"Rs 500 towards June, Rs 500 held", the other "Rs 1,000 towards June". Two uploads of the same file
could text a parent two different things. For a sibling split it is worse, because what each child
owes at that moment decides how the money is divided, and that depends on which credits landed first.

A money system people trust has to give the same answer twice. So the requirement became: **parallel
import must produce byte-identical books to a line-by-line import, every time.**

## The rule

> Parallel across members, in statement order within a member.

Two credits may run at the same time only if there is no way for one to affect the other's outcome.
A credit affects another when they touch the same member, because what a member owes is what the
rules read. It also affects another when they are the same credit: a line the bank printed twice must
be recorded once, and the second copy has to see the first.

## Grouping with union-find

Before anything is written, each credit is turned into nodes:

- its UTR, as `("utr", "615412345601")`
- every member it could possibly belong to, as `("member", 41)`

A credit joins its UTR node to each of its candidate members. Then every group of connected nodes is
one bucket of credits, and buckets cannot affect each other.

```
    Rs 3,500 from rk.verma@okhdfcbank ──┬── ("member", 41)  Rohan
                                        └── ("member", 42)  Kabir
    Rs 1,500 "PS1042" ──────────────────────┘                  joined, so same group
    Rs 2,000 "PS1055" ───── ("member", 55)  Diya               separate group
```

The Verma family's joint transfer and Kabir's separate one end up in one group because they share
Kabir. Diya's credit shares nothing, so it runs on another thread. A repeated line shares its UTR
node with the original, so the copy always runs after it and is recorded as a plain duplicate.

Each group is then sorted by `(received_at, line)` and handed to one worker, which processes it in
that order on its own pooled connection. Groups are sorted by their first credit, so the work is also
handed out in a fixed order.

Union-find is the right structure because the joins arrive one credit at a time and the question is
only ever "are these two already connected?". With path halving and union by size it is effectively
constant time per operation, so grouping 5,000 credits costs nothing next to the database work.

Nodes are tagged tuples (`("utr", x)` and `("member", 41)`) so a UTR can never collide with a member
id. That is the sort of bug that would show up as one family's money landing in another's account.

## What the grouping is, and is not

The grouping is a **determinism and performance layer**. The locks are the **safety layer**. If the
grouping were wrong tomorrow, the books would still be valid, because every write still takes member
locks in id order; the result would merely depend on timing again. Keeping those two jobs separate
matters: it means a bug in the fast path cannot lose money, only reproducibility.

## Proving it, rather than asserting it

The test builds the same institution twice, in two schemas, from the same seed. It imports one
generated statement line by line into the first, and in parallel into the second, then compares
everything that matters:

- every bank credit, by UTR, with its status and matching rule
- every payment, keyed by UTR and reference code, never by row id
- every allocation, keyed by UTR, reference code, period and kind
- every credit the bank returned in the same file
- every queued message payload, receipts and reversal notices alike

Keying on UTRs and reference codes rather than row ids is deliberate: serial numbers come out in a
different order under parallel import, and an id-based comparison would fail for a reason nobody
cares about while hiding the ones they do.

The generated statement is made to be awkward on purpose: joint sibling payments, the same family
also paying separately, partial payments, advances, strangers, lines the bank printed twice, one UTR
that comes back with a different amount, and a debit. The comparison runs for several seeds and for
1, 4, 8 and 16 workers, all of which must produce the one result.

The benchmark adds a second check: it hashes the whole set of books into a fingerprint and prints it
on every row of the scaling table.

```
workers  seconds  lines/s speed-up  fingerprint
      1     14.5      323    1.00x  39467cfe985a
      2      9.3      502    1.55x  39467cfe985a
      4      9.1      511    1.58x  39467cfe985a
      8      8.4      558    1.73x  39467cfe985a
```

One fingerprint, four worker counts. When I later rewrote the hot path to cut round trips, the
fingerprint came out unchanged, which said in one line that a performance change had altered no result
anywhere. Its value moved only when I widened the comparison itself to cover returned credits.

## The bug this caught

My first grouping joined each credit's UTR to its *first* candidate member only. It is an easy
mistake: with one member per credit it is identical, and it passes any test where each family pays
once.

It is wrong as soon as a family both pays jointly and pays for one child separately. The joint
transfer's first candidate is Rohan, so it lands in Rohan's group; Kabir's separate payment lands in
Kabir's. Two threads, both touching Kabir, and which one gets there first decides how the joint
transfer is split.

The locks kept the books valid, so nothing looked broken. The equivalence test failed. I made the
generated statement produce exactly that pattern, and now putting the bug back fails the test every
time it runs. It is one of the cases in `tools/mutation_check.py`:

```
union-find replaced by "first candidate"                   caught
```

That is the part I would want to be asked about: the test did not catch a crash, it caught two
correct-looking answers that disagreed.

## What I would do differently at ten times the size

At a few thousand lines a statement, grouping in memory on one machine is right, and the speed-up
stops at the core count anyway (this box has two). If a single upload were millions of lines I would keep the same rule
and change where the grouping lives: partition by member id, with each partition's credits handled in
statement order, which is the same idea with the groups decided up front instead of discovered.

What I would not change is the proof. The equivalence test, keyed on UTRs rather than row ids, is
what made it possible to make the thing faster twice without wondering whether the books had moved.
