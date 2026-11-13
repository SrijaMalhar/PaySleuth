"""Randomised histories: hand-written tests check the cases I thought of; this checks the ones I didn't.

After every single step check_books must return nothing. A failure message
carries the seed and the step, so it replays exactly.
"""

import random
from datetime import date, datetime, timedelta

import pytest

from conftest import check_books, credit
from paysleuth.charges import apply_late_fees, run_billing
from paysleuth.db import tenant_tx
from paysleuth.errors import Conflict, PreconditionFailed
from paysleuth.members import create_member, get_member, update_member
from paysleuth.payments import assign_credit, reassign_credit, record_credit
from paysleuth.periods import IST

FEES = [100000, 150000, 200000, 250000]


@pytest.mark.parametrize("seed", range(8))
def test_random_histories_keep_the_books_correct(conn, inst, seed):
    rng = random.Random(seed)
    members = []
    for i in range(8):
        upi = f"family{i // 2}@oksbi" if i < 6 else f"single{i}@oksbi"   # three sibling pairs
        members.append(create_member(conn, inst, name=f"Child {i}", monthly_fee=rng.choice(FEES),
                                     billing_day=rng.randint(1, 28), start_period=rng.choice(["2026-05", "2026-06"]),
                                     concession_pct=rng.choice([0, 0, 0, 25, 50, 100]), upi_ids=[upi])["id"])
    clock = datetime(2026, 5, 1, 9, 0, tzinfo=IST)
    day = date(2026, 5, 1)
    utrs = []

    for step in range(150):
        clock += timedelta(hours=rng.randint(1, 30))
        day = clock.date()
        op = rng.choice(["bill", "late", "code", "upi", "stranger", "replay", "assign", "reassign", "concession"])
        try:
            if op == "bill":
                run_billing(conn, inst, day)
            elif op == "late":
                apply_late_fees(conn, inst, day)
            elif op in ("code", "upi", "stranger"):
                utr = f"6{seed}{step:010d}"
                amount = str(rng.choice([500, 1000, 1500, 2000, 2500, 3500, 4000, 5000]))
                member = rng.choice(members)
                kwargs = {"code": {"note": f"PS{1000 + member}"},
                          "upi": {"upi": f"family{rng.randrange(3)}@oksbi"},
                          "stranger": {"upi": f"walkin{step}@ybl", "note": "fees"}}[op]
                record_credit(conn, inst, credit(utr=utr, amount=amount, at=clock, **kwargs))
                utrs.append((utr, amount, kwargs))
            elif op == "replay" and utrs:
                utr, amount, kwargs = rng.choice(utrs)
                record_credit(conn, inst, credit(utr=utr, amount=amount, at=clock, **kwargs))
            elif op in ("assign", "reassign"):
                status = "needs_review" if op == "assign" else "matched"
                with tenant_tx(conn, inst):
                    row = conn.execute("SELECT id, amount FROM bank_credits WHERE status = %s ORDER BY id",
                                       (status,)).fetchall()
                if row:
                    target = rng.choice(row)
                    if op == "assign":
                        assign_credit(conn, inst, target["id"], rng.choice(members))
                    else:
                        a, b = rng.sample(members, 2)
                        share = rng.randint(1, target["amount"] - 1) if target["amount"] > 1 else 0
                        parts = [(a, target["amount"])] if share == 0 or rng.random() < 0.5 else \
                            [(a, share), (b, target["amount"] - share)]
                        reassign_credit(conn, inst, target["id"], parts)
            elif op == "concession":
                member = rng.choice(members)
                view = get_member(conn, inst, member)
                update_member(conn, inst, member, view["version"],
                              changes={"concession_pct": rng.choice([0, 25, 50, 100])})
        except (Conflict, PreconditionFailed):
            pass
        broken = check_books(conn, inst)
        assert broken == [], f"seed={seed} step={step} op={op}: {broken}"
