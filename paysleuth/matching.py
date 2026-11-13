"""Who a credit belongs to: strict rules first, then ranked suggestions for a person.

In money, a wrong match is worse than no match: it marks the wrong family as
paid. Automatic decisions follow five strict rules, and everything else goes
to the review queue with suggestions that are shown, never applied.

    1. Exactly one reference code in the note           -> that member
    2. Several codes, amount = what they owe together    -> split by what each owes
    3. A UPI ID belonging to exactly one member          -> that member
    4. A UPI ID shared by siblings, amount = what they owe
       together or their monthly fees together          -> split
    5. Anything else                                     -> review queue
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable

from .allocation import balances
from .charges import concession_amount
from .money import to_rupees
from .names import first_name, similarity, surname, tokens
from .periods import period_of

# \b before PS stops "APS1001" from matching; [\s\-]? accepts "PS 1001" and "PS-1001".
_REF_CODE = re.compile(r"\bPS[\s\-]?(\d{4,7})\b", re.IGNORECASE)


@dataclass(frozen=True)
class Decision:
    parts: tuple            # ((member_id, amount_paise), ...); empty means "needs review"
    rule: str | None


REVIEW = Decision((), None)


def ref_codes_in(note: str | None) -> list[str]:
    codes = []
    for digits in _REF_CODE.findall(note or ""):
        code = f"PS{int(digits)}"
        if code not in codes:
            codes.append(code)
    return codes


def candidate_members(conn, payer_upi_id: str | None, note: str | None) -> tuple[list[int], str | None]:
    """The members a credit could be for, and where that came from.

    Reference codes win, but only if every code in the note exists for this
    institution; otherwise fall back to the members registered to the UPI ID.
    Read-only: call it inside the caller's tenant transaction.
    """
    codes = ref_codes_in(note)
    if codes:
        rows = conn.execute("SELECT id FROM members WHERE ref_code = ANY(%s)", (codes,)).fetchall()
        if len(rows) == len(codes):
            return sorted(r["id"] for r in rows), "ref_code"
    if payer_upi_id:
        rows = conn.execute("SELECT member_id FROM member_upi_ids WHERE upi_id = %s ORDER BY member_id",
                            (payer_upi_id,)).fetchall()
        if rows:
            return [r["member_id"] for r in rows], "upi_id"
    return [], None


def current_fees(conn, member_ids, as_of: date) -> dict[int, int]:
    """What each member pays per month at as_of, after concession.

    A payment that arrives before a member starts is an advance for their
    first month, so their first month's fee is used.
    """
    rows = conn.execute(
        """SELECT m.id, m.concession_pct,
                  (SELECT f.monthly_fee FROM member_fees f
                    WHERE f.member_id = m.id AND f.effective_period <= GREATEST(%s, m.start_period)
                    ORDER BY f.effective_period DESC LIMIT 1) AS fee
             FROM members m WHERE m.id = ANY(%s)""", (period_of(as_of), list(member_ids))).fetchall()
    return {r["id"]: r["fee"] - concession_amount(r["fee"], r["concession_pct"]) for r in rows}


def decide(conn, amount: int, payer_upi_id: str | None, note: str | None, as_of: date,
           members=None, source=None) -> Decision:
    """Apply the five rules. Pass members/source when the caller already locked them,
    so what each member owes cannot change between deciding and applying."""
    if members is None:
        members, source = candidate_members(conn, payer_upi_id, note)
    if not members:
        return REVIEW
    if len(members) == 1:
        return Decision(((members[0], amount),), "ref_code" if source == "ref_code" else "known_upi_id")
    owed = {m: b["outstanding"] for m, b in balances(conn, members).items()}
    if all(owed[m] > 0 for m in members) and sum(owed.values()) == amount:
        return Decision(tuple((m, owed[m]) for m in members), "sibling_split")
    if source == "upi_id":
        fees = current_fees(conn, members, as_of)
        # all(> 0): a sibling on a full scholarship must never get a Rs 0 share.
        if all(fees[m] > 0 for m in members) and sum(fees.values()) == amount:
            return Decision(tuple((m, fees[m]) for m in members), "sibling_split")
    return REVIEW


# --- suggestions for the review queue ------------------------------------------------

def families_of(conn, payer_upi_ids) -> dict[str, set[int]]:
    """For each UPI ID: the members it already pays for, and their siblings.

    Siblings are members sharing a UPI ID with one of those, or having the same
    guardian name. One query for the whole review queue, not one per credit.
    """
    wanted = sorted({u for u in payer_upi_ids if u})
    if not wanted:
        return {}
    rows = conn.execute(
        """WITH wanted AS (SELECT unnest(%(upis)s::text[]) AS upi_id),
                direct AS (SELECT w.upi_id, u.member_id FROM wanted w
                             JOIN member_upi_ids u ON u.upi_id = w.upi_id),
                linked AS (SELECT d.upi_id, u2.member_id FROM direct d
                             JOIN member_upi_ids u1 ON u1.member_id = d.member_id
                             JOIN member_upi_ids u2 ON u2.upi_id = u1.upi_id),
                same_guardian AS (SELECT d.upi_id, m2.id AS member_id FROM direct d
                                    JOIN members m1 ON m1.id = d.member_id
                                    JOIN members m2 ON lower(m2.guardian_name) = lower(m1.guardian_name)
                                   WHERE m1.guardian_name IS NOT NULL)
           SELECT upi_id, member_id FROM direct
           UNION SELECT upi_id, member_id FROM linked
           UNION SELECT upi_id, member_id FROM same_guardian""", {"upis": wanted}).fetchall()
    families: dict[str, set[int]] = {u: set() for u in wanted}
    for row in rows:
        families[row["upi_id"]].add(row["member_id"])
    return families


@dataclass(frozen=True)
class Facts:
    """What a signal is allowed to look at, worked out once per member.

    A signal gets facts rather than a connection, so adding one cannot add a
    query, and the whole scoring pass stays four queries however many signals
    there are.
    """
    member: dict
    credit: dict
    owed: int                       # what this member still owes
    fee: int                        # their monthly fee after concession
    family: set                     # members this payer's UPI ID already pays for
    amount: int
    months: int                     # amount / fee when it divides exactly, else 0
    name_score: float               # how well the payer's name matches the guardian's
    payer_surname: str | None
    first_name: str | None

    @classmethod
    def about(cls, member: dict, credit: dict, owed: int, fee: int, family: set) -> "Facts":
        amount = credit["amount"]
        payer = credit.get("payer_name")
        return cls(member=member, credit=credit, owed=owed, fee=fee, family=family, amount=amount,
                   months=amount // fee if fee > 0 and amount % fee == 0 else 0,
                   name_score=similarity(payer, member["guardian_name"] or member["name"]),
                   payer_surname=surname(payer), first_name=first_name(member["name"]))

    @property
    def who(self) -> str:
        return self.member["guardian_name"] or self.member["name"]


@dataclass(frozen=True)
class Signal:
    """One reason to think a credit belongs to a member.

    Everything about a signal is in one place: its weight, when it fires, and
    how it reads to the owner. Adding a reason to the review queue is one entry
    in SIGNALS, and nothing else in the file changes.

    group makes signals mutually exclusive: within a group only the first that
    fires counts, so "equals the fee" cannot also score "is a whole number of
    months of the fee" for the same rupees.
    """
    name: str
    weight: int
    fires: Callable[[Facts], bool]
    reason: Callable[[Facts], str]
    group: str | None = None


SIGNALS = (
    Signal("amount_equals_outstanding", 40, group="amount",
           fires=lambda f: f.owed > 0 and f.amount == f.owed,
           reason=lambda f: f"amount equals the Rs {to_rupees(f.owed)} outstanding"),
    Signal("amount_equals_fee", 25, group="amount",
           fires=lambda f: f.fee > 0 and f.amount == f.fee,
           reason=lambda f: f"amount equals the Rs {to_rupees(f.fee)} monthly fee"),
    Signal("amount_is_months_of_fee", 15, group="amount",
           fires=lambda f: 2 <= f.months <= 6,
           reason=lambda f: f"amount is exactly {f.months} months of fees"),
    Signal("payer_matches_guardian", 35, group="name",
           fires=lambda f: f.name_score >= 0.8,
           reason=lambda f: f"payer name matches {f.who}"),
    Signal("payer_partly_matches_guardian", 15, group="name",
           fires=lambda f: f.name_score >= 0.5,
           reason=lambda f: f"payer name partly matches {f.who}"),
    Signal("same_surname", 10,
           fires=lambda f: bool(f.payer_surname) and f.payer_surname == surname(f.member["name"]),
           reason=lambda f: "payer has the same surname"),
    Signal("note_names_member", 30,
           fires=lambda f: bool(f.first_name) and f.first_name in tokens(f.credit.get("note")),
           reason=lambda f: f"note mentions {f.first_name.title()}"),
    Signal("upi_pays_for_family", 20,
           fires=lambda f: f.member["id"] in f.family,
           reason=lambda f: "this UPI ID already pays for the family"),
)

# The tunable surface: name -> weight. tools/tune_weights.py proposes changes to
# these from recorded decisions, and evaluation.py replays any set of them.
WEIGHTS = {signal.name: signal.weight for signal in SIGNALS}


def fired_signals(facts: Facts) -> list[Signal]:
    """The signals that fire for one member, at most one per exclusive group."""
    out, groups_used = [], set()
    for signal in SIGNALS:
        if signal.group and signal.group in groups_used:
            continue
        if signal.fires(facts):
            out.append(signal)
            if signal.group:
                groups_used.add(signal.group)
    return out


@dataclass(frozen=True)
class Suggesting:
    """Everything suggest() needs that does not depend on the individual credit.

    Four queries, built once for a whole review queue instead of once per
    credit: with 300 members and 50 waiting credits, that is the difference
    between 4 queries and 200.
    """
    members: tuple
    owed: dict
    fees: dict
    families: dict


def prepare(conn, as_of: date, payer_upi_ids=()) -> Suggesting:
    members = conn.execute("SELECT id, ref_code, name, guardian_name FROM members WHERE active ORDER BY id"
                           ).fetchall()
    ids = [m["id"] for m in members]
    return Suggesting(members=tuple(members),
                      owed={m: b["outstanding"] for m, b in balances(conn, ids).items()} if ids else {},
                      fees=current_fees(conn, ids, as_of) if ids else {},
                      families=families_of(conn, payer_upi_ids))


def suggest(conn, credit: dict, as_of: date, limit: int | None = 3, weights: dict | None = None,
            prepared: Suggesting | None = None) -> list[dict]:
    """Rank active members for a credit in the review queue. Read-only; never applied."""
    weights = weights or WEIGHTS
    data = prepared or prepare(conn, as_of, [credit.get("payer_upi_id")])
    if not data.members:
        return []
    family = data.families.get(credit.get("payer_upi_id"), set())
    ranked = []
    for member in data.members:
        facts = Facts.about(member, credit, data.owed[member["id"]], data.fees[member["id"]], family)
        fired = fired_signals(facts)
        score = sum(weights.get(signal.name, 0) for signal in fired)
        if score > 0:
            ranked.append({"member_id": member["id"], "ref_code": member["ref_code"],
                           "name": member["name"], "score": score,
                           "signals": [signal.name for signal in fired],
                           "reasons": [signal.reason(facts) for signal in fired]})
    ranked.sort(key=lambda s: (-s["score"], s["member_id"]))      # ties stay stable
    return ranked if limit is None else ranked[:limit]
