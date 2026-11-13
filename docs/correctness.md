# How correctness is established

The bugs they actually found, and how. Summarised in the
[README](../README.md#how-correctness-is-established).

Six layers, each catching what the one above cannot. 358 tests, none mocked — every one runs against a
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
