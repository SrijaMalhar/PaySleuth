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

WEIGHTS = {
    "amount_equals_outstanding": 40,
    "amount_equals_fee": 25,
    "amount_is_months_of_fee": 15,
    "payer_matches_guardian": 35,
    "payer_partly_matches_guardian": 15,
    "same_surname": 10,
    "note_names_member": 30,
    "upi_pays_for_family": 20,
}


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


def signals(member: dict, credit: dict, owed: int, fee: int, family: set[int]) -> dict[str, bool]:
    """Which suggestion signals fire for one member. Exclusive groups pick the strongest."""
    amount = credit["amount"]
    fired = dict.fromkeys(WEIGHTS, False)
    if owed > 0 and amount == owed:
        fired["amount_equals_outstanding"] = True
    elif fee > 0 and amount == fee:
        fired["amount_equals_fee"] = True
    elif fee > 0 and amount % fee == 0 and 2 <= amount // fee <= 6:
        fired["amount_is_months_of_fee"] = True
    payer = credit.get("payer_name")
    against = member["guardian_name"] or member["name"]
    score = similarity(payer, against)
    if score >= 0.8:
        fired["payer_matches_guardian"] = True
    elif score >= 0.5:
        fired["payer_partly_matches_guardian"] = True
    payer_surname = surname(payer)
    if payer_surname and payer_surname == surname(member["name"]):
        fired["same_surname"] = True
    first = first_name(member["name"])
    if first and first in tokens(credit.get("note")):
        fired["note_names_member"] = True
    if member["id"] in family:
        fired["upi_pays_for_family"] = True
    return fired


def _reasons(member: dict, credit: dict, fired: dict, owed: int, fee: int) -> list[str]:
    reasons = []
    if fired["amount_equals_outstanding"]:
        reasons.append(f"amount equals the Rs {to_rupees(owed)} outstanding")
    if fired["amount_equals_fee"]:
        reasons.append(f"amount equals the Rs {to_rupees(fee)} monthly fee")
    if fired["amount_is_months_of_fee"]:
        reasons.append(f"amount is exactly {credit['amount'] // fee} months of fees")
    who = member["guardian_name"] or member["name"]
    if fired["payer_matches_guardian"]:
        reasons.append(f"payer name matches {who}")
    if fired["payer_partly_matches_guardian"]:
        reasons.append(f"payer name partly matches {who}")
    if fired["same_surname"]:
        reasons.append("payer has the same surname")
    if fired["note_names_member"]:
        reasons.append(f"note mentions {first_name(member['name']).title()}")
    if fired["upi_pays_for_family"]:
        reasons.append("this UPI ID already pays for the family")
    return reasons


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
    owed, fees = data.owed, data.fees
    family = data.families.get(credit.get("payer_upi_id"), set())
    ranked = []
    for member in data.members:
        fee = fees[member["id"]]
        fired = signals(member, credit, owed[member["id"]], fee, family)
        score = sum(weights[name] for name, on in fired.items() if on)
        if score > 0:
            ranked.append({"member_id": member["id"], "ref_code": member["ref_code"],
                           "name": member["name"], "score": score,
                           "signals": [name for name, on in fired.items() if on],
                           "reasons": _reasons(member, credit, fired, owed[member["id"]], fee)})
    ranked.sort(key=lambda s: (-s["score"], s["member_id"]))      # ties stay stable
    return ranked if limit is None else ranked[:limit]
