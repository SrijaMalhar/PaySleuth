"""Stateful property test: any sequence of operations must leave the books correct.

Hand-written tests check the cases I thought of; this checks the ones I
didn't. Hypothesis drives a RuleBasedStateMachine: it picks operations and
their arguments, runs check_books and the daily close after every step, and
when something breaks it shrinks the run to the shortest sequence of steps
that still fails, printed as code that can be pasted into a test.

Profiles (PAYSLEUTH_HYPOTHESIS_PROFILE):
    ci       the default: derandomised, so CI is repeatable
    explore  random and longer; run it locally to look for new bugs
"""

import os
from datetime import date, datetime, timedelta

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from conftest import check_books, credit, fresh_pool
from paysleuth.charges import apply_late_fees, run_billing
from paysleuth.close import run_close
from paysleuth.db import tenant_tx
from paysleuth.errors import Conflict, PreconditionFailed
from paysleuth.institutions import create_institution
from paysleuth.members import create_member, get_member, update_member
from paysleuth.payments import assign_credit, reassign_credit, record_credit, reverse_credit
from paysleuth.periods import IST

settings.register_profile("ci", max_examples=40, stateful_step_count=40, deadline=None, derandomize=True,
                          suppress_health_check=list(HealthCheck))
settings.register_profile("explore", max_examples=300, stateful_step_count=60, deadline=None,
                          suppress_health_check=list(HealthCheck))
settings.load_profile(os.environ.get("PAYSLEUTH_HYPOTHESIS_PROFILE", "ci"))

# (name, fee in paise, concession %, UPI ID). Two sibling pairs, one child on a full scholarship.
MEMBERS = [("Diya", 200000, 0, "sharma@oksbi"), ("Kabir", 150000, 0, "sharma@oksbi"),
           ("Rohan", 250000, 0, "verma@oksbi"), ("Asha", 200000, 100, "verma@oksbi"),
           ("Meera", 100000, 25, "iyer@oksbi"), ("Arjun", 180000, 0, None)]
AMOUNTS = st.sampled_from(["500", "1000", "1500", "2000", "2500", "3500", "4000", "5000"])
MEMBER = st.integers(0, len(MEMBERS) - 1)


class Books(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self._pool_context = fresh_pool(max_size=2)
        self.pool = self._pool_context.__enter__()
        self.conn = self.pool.getconn()
        self.clock = datetime(2026, 5, 1, 9, 0, tzinfo=IST)
        self.utrs = []                      # (utr, amount, kwargs) of every credit sent so far
        self.next_utr = 0

    def teardown(self):
        self.pool.putconn(self.conn)
        self._pool_context.__exit__(None, None, None)

    @initialize()
    def institution(self):
        self.inst, _ = create_institution(self.conn, "Property Test Centre", late_fee=10000, grace_days=3)
        self.members = [
            create_member(self.conn, self.inst, name=name, monthly_fee=fee, billing_day=5,
                          start_period="2026-06" if name == "Kabir" else "2026-05",
                          concession_pct=concession, upi_ids=[upi] if upi else [])["id"]
            for name, fee, concession, upi in MEMBERS]

    def _send(self, amount, **kwargs):
        self.next_utr += 1
        utr = f"61541{self.next_utr:07d}"
        self.utrs.append((utr, amount, kwargs))
        record_credit(self.conn, self.inst, credit(utr=utr, amount=amount, at=self.clock, **kwargs))

    def _credits(self, status):
        with tenant_tx(self.conn, self.inst):
            return self.conn.execute("SELECT id, amount FROM bank_credits WHERE status = %s ORDER BY id",
                                     (status,)).fetchall()

    # --- operations ---------------------------------------------------------------------

    @rule(days=st.integers(1, 20))
    def time_passes(self, days):
        self.clock += timedelta(days=days)

    @rule()
    def bill(self):
        run_billing(self.conn, self.inst, self.clock.date())

    @rule()
    def late_fees(self):
        apply_late_fees(self.conn, self.inst, self.clock.date())

    @rule(member=MEMBER, amount=AMOUNTS)
    def pay_with_a_reference_code(self, member, amount):
        self._send(amount, note=f"fees PS{1000 + self.members[member]}")

    @rule(family=st.sampled_from(["sharma@oksbi", "verma@oksbi", "iyer@oksbi"]), amount=AMOUNTS)
    def pay_from_a_known_upi_id(self, family, amount):
        self._send(amount, upi=family)

    @rule(amount=AMOUNTS)
    def pay_from_a_stranger(self, amount):
        self._send(amount, upi=f"walkin{self.next_utr}@ybl", note="fees")

    @precondition(lambda self: self.utrs)
    @rule(pick=st.integers(0, 10**6))
    def the_bank_sends_an_alert_again(self, pick):
        utr, amount, kwargs = self.utrs[pick % len(self.utrs)]
        record_credit(self.conn, self.inst, credit(utr=utr, amount=amount, at=self.clock, **kwargs))

    @rule(pick=st.integers(0, 10**6), member=MEMBER)
    def a_person_assigns_a_waiting_credit(self, pick, member):
        waiting = self._credits("needs_review")
        if waiting:
            assign_credit(self.conn, self.inst, waiting[pick % len(waiting)]["id"], self.members[member])

    @rule(pick=st.integers(0, 10**6), first=MEMBER, second=MEMBER, share=st.integers(1, 99))
    def a_person_moves_an_assigned_credit(self, pick, first, second, share):
        matched = self._credits("matched")
        if not matched:
            return
        target = matched[pick % len(matched)]
        part = target["amount"] * share // 100
        parts = [(self.members[first], target["amount"])]
        if first != second and 0 < part < target["amount"]:
            parts = [(self.members[first], part), (self.members[second], target["amount"] - part)]
        reassign_credit(self.conn, self.inst, target["id"], parts)

    @rule(pick=st.integers(0, 10**6))
    def the_bank_returns_a_credit(self, pick):
        live = self._credits("matched") + self._credits("needs_review")
        if live:
            reverse_credit(self.conn, self.inst, live[pick % len(live)]["id"])

    @rule(member=MEMBER, concession=st.sampled_from([0, 25, 50, 100]))
    def a_concession_changes(self, member, concession):
        view = get_member(self.conn, self.inst, self.members[member])
        try:
            update_member(self.conn, self.inst, view["id"], view["version"],
                          changes={"concession_pct": concession})
        except (Conflict, PreconditionFailed):
            pass

    # --- what must hold after every step ----------------------------------------------------

    @invariant()
    def the_books_are_correct(self):
        if hasattr(self, "inst"):
            assert check_books(self.conn, self.inst) == []

    @invariant()
    def every_credited_rupee_is_accounted_for(self):
        if hasattr(self, "inst"):
            close = run_close(self.conn, self.inst, date(2026, 5, 1))
            assert close["books_balance"], close


TestBooks = Books.TestCase
