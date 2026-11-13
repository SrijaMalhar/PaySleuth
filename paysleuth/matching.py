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


def _family_of(conn, payer_upi_id: str | None) -> set[int]:
    """Members this UPI ID already pays for, and their siblings (same guardian or a shared UPI ID)."""
    if not payer_upi_id:
        return set()
    rows = conn.execute(
        """WITH direct AS (SELECT member_id FROM member_upi_ids WHERE upi_id = %(u)s),
                linked AS (SELECT u2.member_id FROM member_upi_ids u1
                             JOIN member_upi_ids u2 ON u2.upi_id = u1.upi_id
                            WHERE u1.member_id IN (SELECT member_id FROM direct))
           SELECT member_id AS id FROM direct
           UNION SELECT member_id FROM linked
           UNION SELECT m2.id FROM members m1 JOIN members m2
                    ON lower(m2.guardian_name) = lower(m1.guardian_name)
                 WHERE m1.id IN (SELECT member_id FROM direct) AND m1.guardian_name IS NOT NULL""",
        {"u": payer_upi_id}).fetchall()
    return {r["id"] for r in rows}


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


def suggest(conn, credit: dict, as_of: date, limit: int = 3, weights: dict | None = None) -> list[dict]:
    """Rank active members for a credit in the review queue. Read-only; never applied."""
    weights = weights or WEIGHTS
    members = conn.execute("SELECT id, ref_code, name, guardian_name FROM members WHERE active ORDER BY id"
                           ).fetchall()
    if not members:
        return []
    ids = [m["id"] for m in members]
    owed = {m: b["outstanding"] for m, b in balances(conn, ids).items()}
    fees = current_fees(conn, ids, as_of)
    family = _family_of(conn, credit.get("payer_upi_id"))
    ranked = []
    for member in members:
        fee = fees[member["id"]]
        fired = signals(member, credit, owed[member["id"]], fee, family)
        score = sum(weights[name] for name, on in fired.items() if on)
        if score > 0:
            ranked.append({"member_id": member["id"], "ref_code": member["ref_code"],
                           "name": member["name"], "score": score,
                           "reasons": _reasons(member, credit, fired, owed[member["id"]], fee)})
    ranked.sort(key=lambda s: (-s["score"], s["member_id"]))      # ties stay stable
    return ranked[:limit]
