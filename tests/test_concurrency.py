"""Each race the design claims to handle, run for real against PostgreSQL.

tools/mutation_check.py removes the mechanism behind each of these one at a
time and checks that the matching test goes red.
"""

import random
import threading
import time
import uuid
from datetime import date

import psycopg

from conftest import DSN, add_member, charges_of, check_books, credit, money, receipts, together
from paysleuth.charges import apply_late_fees, run_billing
from paysleuth.db import apply_schema, lock_members, make_pool, tenant_tx
from paysleuth.errors import Conflict, PreconditionFailed
from paysleuth.members import get_member, update_member
from paysleuth.payments import assign_credit, reassign_credit, record_credit

JUNE = date(2026, 6, 1)


def code(member_id):
    return f"PS{1000 + member_id}"


def _errors(results):
    return [r for r in results if isinstance(r, Exception)]


def test_the_same_transfer_arriving_20_times_at_once_counts_once(pool, conn, inst):
    diya = add_member(conn, inst)
    run_billing(conn, inst, JUNE)
    results = together(pool, 20, lambda c, i: record_credit(c, inst, credit(note=code(diya))))
    assert _errors(results) == []
    assert sum(not r["duplicate"] for r in results) == 1
    assert money(conn, inst, diya)["paid"] == 200000
    assert len(receipts(conn, inst, diya)) == 1


def test_the_same_unknown_transfer_arriving_20_times_at_once_counts_once(pool, conn, inst):
    results = together(pool, 20, lambda c, i: record_credit(c, inst, credit(upi="stranger@ybl")))
    assert _errors(results) == [] and sum(not r["duplicate"] for r in results) == 1
    with tenant_tx(conn, inst):
        assert conn.execute("SELECT COUNT(*) AS n FROM bank_credits").fetchone()["n"] == 1


def test_20_payments_for_one_member_at_once_never_pay_a_fee_twice(pool, conn, inst):
    diya = add_member(conn, inst, start="2026-04")
    run_billing(conn, inst, JUNE)                                     # three months, Rs 6,000
    for round_ in range(3):
        results = together(pool, 20, lambda c, i: record_credit(
            c, inst, credit(utr=f"6154{round_:02d}{i:06d}", amount="500", note=code(diya))))
        assert _errors(results) == []
        assert check_books(conn, inst) == []
    assert [c["paid"] for c in charges_of(conn, inst, diya)] == [200000, 200000, 200000]
    assert money(conn, inst, diya)["credit"] == 3 * 20 * 50000 - 600000


def test_payments_for_different_members_do_not_wait_for_each_other(pool, conn, inst):
    diya = add_member(conn, inst)
    kabir = add_member(conn, inst, "Kabir")
    done = threading.Event()

    def pay_kabir():
        with pool.connection() as c:
            record_credit(c, inst, credit(note=code(kabir)))
        done.set()

    with tenant_tx(conn, inst):
        lock_members(conn, [diya])                                    # hold Diya's lock
        worker = threading.Thread(target=pay_kabir)
        worker.start()
        assert done.wait(timeout=10), "Kabir's payment waited for Diya's lock"
    worker.join()


def test_eight_billing_runs_at_once_bill_each_month_once(pool, conn, inst):
    members = [add_member(conn, inst, f"Child {i}", start="2026-01") for i in range(10)]
    results = together(pool, 8, lambda c, i: run_billing(c, inst, JUNE))
    assert _errors(results) == []
    for member_id in members:
        assert len(charges_of(conn, inst, member_id)) == 6
    assert check_books(conn, inst) == []


def _backend_waiting_on_a_lock(conn, pid) -> bool:
    row = conn.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()
    return bool(row and row["wait_event_type"] == "Lock")


def test_late_fee_is_rechecked_after_waiting_for_the_lock(pool, conn, inst):
    """Force the worst order every time: the job picks the member as unpaid, waits, and the payment lands."""
    diya = add_member(conn, inst, billing_day=5)
    run_billing(conn, inst, JUNE)
    with pool.connection() as job_conn, pool.connection() as watcher:
        job_pid = job_conn.info.backend_pid
        outcome = {}
        job = threading.Thread(target=lambda: outcome.setdefault(
            "late", apply_late_fees(job_conn, inst, date(2026, 6, 20))))
        with tenant_tx(conn, inst):
            lock_members(conn, [diya])
            job.start()
            deadline = time.monotonic() + 10
            while not _backend_waiting_on_a_lock(watcher, job_pid):
                assert time.monotonic() < deadline, "the late-fee job never reached the member lock"
                time.sleep(0.01)
            record_credit(conn, inst, credit(note=code(diya)))          # the payment lands while it waits
        job.join()
    assert outcome["late"] == []
    assert [c["kind"] for c in charges_of(conn, inst, diya)] == ["monthly_fee"]


def test_two_people_resolving_the_same_credit_differently(pool, conn, inst):
    diya = add_member(conn, inst)
    kabir = add_member(conn, inst, "Kabir")
    for round_ in range(10):
        credit_id = record_credit(conn, inst, credit(utr=f"61541234{round_:04d}", upi="walkin@ybl"))[
            "credit"]["id"]
        results = together(pool, 2, lambda c, i: assign_credit(c, inst, credit_id, [diya, kabir][i],
                                                               remember_upi_id=False))
        assert sum(isinstance(r, Conflict) for r in results) == 1, results
        assert sum(isinstance(r, dict) for r in results) == 1
    assert check_books(conn, inst) == []
    assert money(conn, inst, diya)["paid"] + money(conn, inst, kabir)["paid"] == 10 * 200000


def test_opposite_reassignments_at_once_never_deadlock(pool, conn, inst):
    diya = add_member(conn, inst)
    kabir = add_member(conn, inst, "Kabir")
    run_billing(conn, inst, JUNE)
    first = record_credit(conn, inst, credit(utr="615412345601", note=code(diya)))["credit"]["id"]
    second = record_credit(conn, inst, credit(utr="615412345602", note=code(kabir)))["credit"]["id"]
    owners = {first: diya, second: kabir}
    for _ in range(25):
        moves = [(first, kabir if owners[first] == diya else diya),
                 (second, kabir if owners[second] == diya else diya)]
        results = together(pool, 2, lambda c, i: reassign_credit(c, inst, moves[i][0], [(moves[i][1], 200000)]))
        assert _errors(results) == []
        owners = dict(moves)
    assert check_books(conn, inst) == []


def test_ten_people_saving_the_same_member_at_once(pool, conn, inst):
    diya = add_member(conn, inst)
    results = together(pool, 10, lambda c, i: update_member(c, inst, diya, 1,
                                                            changes={"guardian_name": f"Guardian {i}"}))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, PreconditionFailed) for r in results) == 9
    assert get_member(conn, inst, diya)["version"] == 2


def test_mixed_load_keeps_the_books_correct(pool, conn, inst):
    families = []
    for i in range(6):
        upi = f"parent{i}@oksbi"
        families.append([add_member(conn, inst, f"Child {i}a", start="2026-05", upi_ids=[upi]),
                         add_member(conn, inst, f"Child {i}b", fee="1500", start="2026-05", upi_ids=[upi])])
    run_billing(conn, inst, JUNE)
    members = [m for f in families for m in f]

    def work(c, i):
        rng = random.Random(i)
        for step in range(25):
            roll = rng.random()
            utr = f"6{i:03d}{step:08d}"
            try:
                if roll < 0.35:
                    record_credit(c, inst, credit(utr=utr, amount=rng.choice(["500", "2000", "3500"]),
                                                  note=code(rng.choice(members))))
                elif roll < 0.6:
                    record_credit(c, inst, credit(utr=utr, amount="3500", upi=f"parent{rng.randrange(6)}@oksbi"))
                elif roll < 0.7:
                    run_billing(c, inst, date(2026, 7, 1))
                elif roll < 0.8:
                    apply_late_fees(c, inst, date(2026, 6, 20))
                else:
                    queued = record_credit(c, inst, credit(utr=utr, amount="2000", upi=f"walkin{i}@ybl"))
                    credit_id = queued["credit"]["id"]
                    assign_credit(c, inst, credit_id, rng.choice(members), remember_upi_id=False)
                    reassign_credit(c, inst, credit_id, [(rng.choice(members), 200000)])
            except Conflict:
                pass
        return "ok"

    results = together(pool, 8, work)
    assert results == ["ok"] * 8, _errors(results)
    assert check_books(conn, inst) == []


def test_processes_starting_together_can_all_migrate():
    for _ in range(3):
        schema = "t_" + uuid.uuid4().hex[:12]
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {schema}")
        pool = make_pool(DSN, max_size=8, options=f"-c search_path={schema}")
        try:
            results = together(pool, 8, lambda c, i: apply_schema(c))
            assert _errors(results) == []
        finally:
            pool.close()
            with psycopg.connect(DSN, autocommit=True) as c:
                c.execute(f"DROP SCHEMA {schema} CASCADE")
