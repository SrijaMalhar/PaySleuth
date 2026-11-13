---
title: PaySleuth
emoji: 🧾
colorFrom: indigo
colorTo: green
sdk: docker
app_port: 8000
pinned: false
---

# PaySleuth

A fee reconciliation service for tuition centres, PGs and housing societies. It reads the bank
statement they already get, decides which member each UPI transfer belongs to, applies it to what they
owe, sends receipts and reminders, and tells the owner when the statement itself is missing a line. It
never moves money and never touches a payment network.

**Source, and the design write-up: https://github.com/SrijaMalhar/PaySleuth**

This Space runs the API. The interactive documentation is at [`/docs`](/docs), and `/health` needs no
key. Everything else needs an API key in an `X-API-Key` header, because each request is scoped to one
institution by row-level security.

The demo institution is a tuition centre mid-month: a month billed, a statement imported, three credits
matched automatically and two left in the review queue because matching them would have been a guess.

```
GET  /review                 credits waiting for a person, with ranked suggestions and the reason for each
GET  /reports/ageing         what is owed, bucketed by how late it is
GET  /members/{id}/statement one family's charges, payments and running balance
POST /bank-credits           record a credit; idempotent on the bank's UTR
```

The five rules that assign money without a human, in order:

1. Exactly one reference code (`PS1004`) in the note → that member.
2. Several codes, and the amount equals exactly what they owe together → split by what each owes.
3. A UPI ID registered to exactly one member → that member.
4. A UPI ID shared by siblings, and the amount matches their combined dues or fees → split. Never a
   zero-rupee share, so a sibling on a full scholarship goes to review instead.
5. Anything else → the review queue, where suggestions are shown and never applied.

A wrong attribution is worse than no attribution: the family that paid gets a reminder they do not
deserve, and the family that did not stops paying. So the system refuses rather than guesses.
