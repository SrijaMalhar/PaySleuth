"""Shared fixtures. Every test gets its own empty schema in a real PostgreSQL.

Locks, constraints and row-level security are the point of this project, so
nothing here is mocked.
"""

import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime

import psycopg
import pytest

from paysleuth.allocation import balances
from paysleuth.db import make_pool, migrate, tenant_tx
from paysleuth.institutions import create_institution
from paysleuth.members import create_member
from paysleuth.money import to_paise
from paysleuth.periods import IST
from paysleuth.statements import Credit

DSN = os.environ.get("PAYSLEUTH_TEST_DATABASE_URL",
                     "postgresql://paysleuth:paysleuth@localhost:5432/paysleuth_test")
SUPERUSER_DSN = os.environ.get("PAYSLEUTH_TEST_SUPERUSER_URL")


@contextmanager
def fresh_pool(max_size=30):
    """A pool whose connections all use a brand-new schema, dropped afterwards."""
    schema = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")
    options = f"-c search_path={schema}"
    pool = make_pool(DSN, max_size=max_size, options=options)
    pool.schema = schema
    pool.options = options
    try:
        migrate(pool)
        yield pool
    finally:
        pool.close()
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def pool():
    with fresh_pool() as p:
        yield p


@pytest.fixture
def conn(pool):
    with pool.connection() as c:
        yield c


@pytest.fixture
def inst(conn):
    institution_id, _ = create_institution(conn, "Bright Minds Tuition", late_fee=10000, grace_days=3)
    return institution_id


def add_member(conn, inst, name="Diya", fee="2000", *, billing_day=5, start="2026-06",
               guardian=None, phone=None, upi_ids=(), concession=0) -> int:
    """Create a member and return its id. fee is in rupees."""
    return create_member(conn, inst, name=name, monthly_fee=to_paise(fee), billing_day=billing_day,
                         start_period=start, guardian_name=guardian, phone=phone,
                         concession_pct=concession, upi_ids=upi_ids)["id"]


def credit(utr="615412345601", amount="2000", *, upi=None, name=None, note=None,
           at=None, channel="UPI", line=0) -> Credit:
    """A bank credit as the statement parser would produce it. amount is in rupees."""
    return Credit(utr=utr, amount=to_paise(amount),
                  received_at=at or datetime(2026, 6, 10, 10, 0, tzinfo=IST), channel=channel,
                  payer_upi_id=upi, payer_name=name, note=note, line=line)


def receipts(conn, inst, member_id=None) -> list[dict]:
    """Queued payment receipts, oldest first."""
    with tenant_tx(conn, inst):
        return [r["payload"] for r in conn.execute(
            "SELECT payload FROM outbox WHERE kind = 'payment_receipt' "
            "AND (member_id = %s OR %s::bigint IS NULL) ORDER BY id", (member_id, member_id)).fetchall()]


def money(conn, inst, member_id) -> dict:
    """A member's balances in paise."""
    with tenant_tx(conn, inst):
        return balances(conn, [member_id])[member_id]


def charges_of(conn, inst, member_id) -> list[dict]:
    """Each charge with what has been paid towards it, oldest first."""
    with tenant_tx(conn, inst):
        return conn.execute(
            """SELECT c.period, c.kind, c.amount, COALESCE(SUM(a.amount), 0) AS paid
                 FROM charges c LEFT JOIN allocations a ON a.charge_id = c.id
                WHERE c.member_id = %s GROUP BY c.id ORDER BY c.due_date, c.id""",
            (member_id,)).fetchall()


# --- proof tools -----------------------------------------------------------------------

def together(pool, n, fn):
    """Run fn(conn, i) on n threads, released at the same instant. Returns results or exceptions by i.

    Each thread takes its own connection before the barrier, so they all reach
    PostgreSQL together instead of queueing for the pool.
    """
    barrier = threading.Barrier(n)
    out = [None] * n

    def run(i):
        with pool.connection() as c:
            barrier.wait()
            try:
                out[i] = fn(c, i)
            except Exception as error:
                out[i] = error

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def check_books(conn, inst) -> list[str]:
    """The rules that must always hold. Returns the broken ones; empty means healthy."""
    broken = []
    with tenant_tx(conn, inst):
        def rows(sql):
            return conn.execute(sql).fetchall()

        for r in rows("""SELECT c.id, c.amount, SUM(a.amount) AS paid FROM charges c
                           JOIN allocations a ON a.charge_id = c.id GROUP BY c.id
                         HAVING SUM(a.amount) > c.amount"""):
            broken.append(f"charge {r['id']} is paid {r['paid']} of {r['amount']}")
        for r in rows("""SELECT p.id, p.amount, SUM(a.amount) AS spent FROM payments p
                           JOIN allocations a ON a.payment_id = p.id GROUP BY p.id
                         HAVING SUM(a.amount) > p.amount"""):
            broken.append(f"payment {r['id']} is spent {r['spent']} of {r['amount']}")
        for r in rows("""SELECT a.id FROM allocations a JOIN payments p ON p.id = a.payment_id
                           JOIN charges c ON c.id = a.charge_id WHERE p.member_id <> c.member_id"""):
            broken.append(f"allocation {r['id']} pays another member's charge")
        for r in rows("""SELECT b.id, b.status, b.amount, COALESCE(SUM(p.amount), 0) AS applied
                           FROM bank_credits b LEFT JOIN payments p ON p.bank_credit_id = b.id
                          GROUP BY b.id
                         HAVING (b.status = 'matched' AND COALESCE(SUM(p.amount), 0) <> b.amount)
                             OR (b.status <> 'matched' AND COALESCE(SUM(p.amount), 0) <> 0)"""):
            broken.append(f"credit {r['id']} ({r['status']}) of {r['amount']} has {r['applied']} applied")
        for r in rows("""WITH unspent AS (
                             SELECT p.member_id FROM payments p LEFT JOIN allocations a ON a.payment_id = p.id
                              WHERE p.reverses_id IS NULL
                                AND NOT EXISTS (SELECT 1 FROM payments v WHERE v.reverses_id = p.id)
                              GROUP BY p.id HAVING p.amount - COALESCE(SUM(a.amount), 0) > 0),
                           unpaid AS (
                             SELECT c.member_id FROM charges c LEFT JOIN allocations a ON a.charge_id = c.id
                              GROUP BY c.id HAVING c.amount - COALESCE(SUM(a.amount), 0) > 0)
                         SELECT DISTINCT member_id FROM unspent WHERE member_id IN (SELECT member_id FROM unpaid)"""):
            broken.append(f"member {r['member_id']} has unused money while owing")
        for r in rows("""SELECT member_id, period, kind FROM charges GROUP BY member_id, period, kind
                         HAVING COUNT(*) > 1"""):
            broken.append(f"member {r['member_id']} billed twice for {r['period']} {r['kind']}")
        for r in rows("""SELECT p.id FROM payments p LEFT JOIN allocations a ON a.payment_id = p.id
                          WHERE p.reverses_id IS NOT NULL
                             OR EXISTS (SELECT 1 FROM payments v WHERE v.reverses_id = p.id)
                          GROUP BY p.id HAVING COALESCE(SUM(a.amount), 0) <> 0"""):
            broken.append(f"voided payment {r['id']} still pays for something")
        for r in rows("""SELECT p.bank_credit_id, p.member_id FROM payments p
                          WHERE p.reverses_id IS NULL
                            AND NOT EXISTS (SELECT 1 FROM payments v WHERE v.reverses_id = p.id)
                          GROUP BY p.bank_credit_id, p.member_id HAVING COUNT(*) > 1"""):
            broken.append(f"credit {r['bank_credit_id']} has two live payments for member {r['member_id']}")
    return broken
