"""Recording bank credits exactly once, and assigning, splitting and moving them.

Locking order, used by every function in this module:

    1. The bank credit row (SELECT ... FOR UPDATE), for manual actions on an
       existing credit.
    2. Then member rows, in ascending id order (lock_members).

Because every operation takes locks in this order, no two operations can wait
on each other in a circle, so they cannot deadlock.
"""

import re

from psycopg.types.json import Jsonb

from . import audit, outbox
from .allocation import LIVE, balances_with_details, settle_members
from .db import lock_members, tenant_tx
from .errors import Conflict, NotFound
from .matching import candidate_members, decide, prepare, suggest
from .members import normalise_upi_id
from .money import to_rupees
from .periods import IST, utc_now
from .statements import Credit

_UTR = re.compile(r"^[A-Z0-9]{6,35}$")


def normalise_utr(utr: str) -> str:
    value = re.sub(r"\s+", "", utr or "").upper()
    if not _UTR.match(value):
        raise ValueError(f"UTR must be 6-35 letters or digits (got {utr!r})")
    return value


def credit_view(row: dict) -> dict:
    return {
        "id": row["id"], "utr": row["utr"], "channel": row["channel"], "amount": to_rupees(row["amount"]),
        "payer_upi_id": row["payer_upi_id"], "payer_name": row["payer_name"], "note": row["note"],
        "received_at": row["received_at"].astimezone(IST).isoformat(), "status": row["status"],
        "match_rule": row["match_rule"],
    }


def record_credit(conn, inst: int, credit: Credit) -> dict:
    """Record one bank credit exactly once, and apply it if a rule matches."""
    utr = normalise_utr(credit.utr)
    payer = normalise_upi_id(credit.payer_upi_id)
    if credit.amount <= 0:
        raise ValueError("amount must be positive")
    as_of = credit.received_at.astimezone(IST).date()

    with tenant_tx(conn, inst):
        # (1) The cheap path for a re-sent alert. This check is not the guarantee:
        # two requests can both see "no row" at the same moment.
        existing = conn.execute("SELECT * FROM bank_credits WHERE utr = %s", (utr,)).fetchone()
        if existing:
            return _replay(existing, credit.amount)

        # (2) Lock before deciding. A split depends on what each sibling owes
        # right now; holding their locks means that cannot change before we apply.
        members, source = candidate_members(conn, payer, credit.note)
        lock_members(conn, members)

        # (3) Decide while holding the locks, before the insert, so the credit row
        # is written once with the status it ends up in.
        decision = decide(conn, credit.amount, payer, credit.note, as_of, members=members, source=source)

        # (4) The constraint is the guarantee. A second transaction inserting the
        # same UTR waits on the unique index until the first commits, then DO
        # NOTHING returns no row and we re-read the winner's row.
        row = conn.execute(
            """INSERT INTO bank_credits (institution_id, utr, channel, amount, payer_upi_id, payer_name,
                                         note, narration, received_at, status, match_rule)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (institution_id, utr) DO NOTHING RETURNING *""",
            (inst, utr, credit.channel, credit.amount, payer, credit.payer_name, credit.note,
             credit.narration, credit.received_at,
             "matched" if decision.parts else "needs_review", decision.rule)).fetchone()
        if row is None:
            return _replay(conn.execute("SELECT * FROM bank_credits WHERE utr = %s", (utr,)).fetchone(),
                           credit.amount)
        applied = _apply_parts(conn, inst, row, decision.parts) if decision.parts else []
        return {"credit": credit_view(row), "duplicate": False, "applied": applied}


def _replay(existing: dict, amount: int) -> dict:
    if existing["amount"] != amount:
        # Same UTR, different amount: a data problem a person must look at.
        raise Conflict(f"UTR {existing['utr']} was already recorded with a different amount")
    return {"credit": credit_view(existing), "duplicate": True, "applied": []}


def _apply_parts(conn, inst: int, credit_row: dict, parts) -> list[dict]:
    """Insert each member's payment, settle it, and queue its receipt, all in the caller's transaction.

    The caller holds the members' locks. Payment and receipt commit together or not at all.
    """
    parts = sorted(parts)
    # One query each for the payments, the settling, the balances and the member
    # details, whatever the number of siblings sharing the credit.
    values = ", ".join(["(%s, %s, %s, %s, %s)"] * len(parts))
    params = [v for member_id, amount in parts
              for v in (inst, credit_row["id"], member_id, amount, credit_row["received_at"])]
    payment_ids = {r["member_id"]: r["id"] for r in conn.execute(
        f"INSERT INTO payments (institution_id, bank_credit_id, member_id, amount, received_at) "
        f"VALUES {values} RETURNING id, member_id", params).fetchall()}
    members = [member_id for member_id, _ in parts]
    settled = settle_members(conn, inst, members)
    after = balances_with_details(conn, members)

    applied, receipts = [], []
    for member_id, amount in parts:
        payment_id = payment_ids[member_id]
        member = after[member_id]
        covered = [{"period": a["period"], "kind": a["kind"], "amount": to_rupees(a["amount"])}
                   for a in settled if a["payment_id"] == payment_id]
        receipt = {
            "member_id": member_id, "ref_code": member["ref_code"], "member_name": member["name"],
            "phone": member["phone"], "utr": credit_row["utr"], "amount": to_rupees(amount),
            "received_at": credit_row["received_at"].astimezone(IST).isoformat(), "covered": covered,
            "credit_after": to_rupees(member["credit"]),
            "outstanding_after": to_rupees(member["outstanding"]),
        }
        receipts.append((member_id, receipt))
        applied.append({"member_id": member_id, "ref_code": member["ref_code"], "payment_id": payment_id,
                        "amount": to_rupees(amount), "covered": covered,
                        "credit_after": receipt["credit_after"],
                        "outstanding_after": receipt["outstanding_after"]})
    outbox.enqueue_many(conn, inst, kind="payment_receipt", rows=receipts)
    return applied


# --- manual actions on the review queue ------------------------------------------------

def _lock_credit(conn, credit_id: int) -> dict:
    row = conn.execute("SELECT * FROM bank_credits WHERE id = %s FOR UPDATE", (credit_id,)).fetchone()
    if row is None:
        raise NotFound(f"bank credit {credit_id} not found")
    return row


def _normalise_parts(parts) -> tuple:
    return tuple(sorted((int(member_id), int(amount)) for member_id, amount in parts))


def _check_parts(parts: tuple, total: int) -> None:
    if not parts:
        raise ValueError("say which member(s) the credit is for")
    members = [m for m, _ in parts]
    if len(set(members)) != len(members):
        raise ValueError("a member appears twice in the split")
    if any(amount <= 0 for _, amount in parts):
        raise ValueError("every share must be more than zero")
    if sum(amount for _, amount in parts) != total:
        raise ValueError(f"the shares add up to Rs {to_rupees(sum(a for _, a in parts))}, "
                         f"but the credit is Rs {to_rupees(total)}")


def _current_parts(conn, credit_id: int) -> tuple:
    rows = conn.execute(f"SELECT p.member_id, p.amount FROM payments p WHERE p.bank_credit_id = %s "
                        f"AND {LIVE} ORDER BY p.member_id", (credit_id,)).fetchall()
    return tuple((r["member_id"], r["amount"]) for r in rows)


def void_payments(conn, inst: int, credit_id: int) -> list[int]:
    """Take back every live payment of a credit by inserting correction rows. Nothing is deleted.

    For each payment: one negative allocation per charge it paid (reopening the
    charge), then one negative payment row pointing at it. The caller holds the
    credit's and the members' locks. Returns the members affected.
    """
    live = [r["id"] for r in conn.execute(
        f"SELECT p.id FROM payments p WHERE p.bank_credit_id = %s AND {LIVE} ORDER BY p.id",
        (credit_id,)).fetchall()]
    if not live:
        return []
    # Two statements whatever the number of siblings. Both are INSERT ... SELECT,
    # so the amounts come from the rows themselves and are never carried through
    # Python, where a stale read could write back the wrong number.
    conn.execute(
        """INSERT INTO allocations (institution_id, payment_id, charge_id, amount)
           SELECT %s, payment_id, charge_id, -SUM(amount) FROM allocations
            WHERE payment_id = ANY(%s) GROUP BY payment_id, charge_id HAVING SUM(amount) <> 0""",
        (inst, live))
    members = conn.execute(
        """INSERT INTO payments (institution_id, bank_credit_id, member_id, amount, reverses_id, received_at)
           SELECT institution_id, bank_credit_id, member_id, -amount, id, received_at
             FROM payments WHERE id = ANY(%s) ORDER BY id RETURNING member_id""", (live,)).fetchall()
    return sorted({r["member_id"] for r in members})


def _learn_upi_id(conn, inst: int, upi_id: str | None, member_ids) -> None:
    """Remember the payer's UPI ID, so next month's payment matches by rule 3 or 4."""
    if not upi_id:
        return
    for member_id in sorted(member_ids):
        conn.execute("INSERT INTO member_upi_ids (institution_id, upi_id, member_id, source) "
                     "VALUES (%s, %s, %s, 'learned') ON CONFLICT DO NOTHING", (inst, upi_id, member_id))


def _record_outcome(conn, inst: int, credit_row: dict, parts: tuple) -> None:
    """Keep what was suggested next to what the person chose, before the choice changes the balances."""
    ranked = suggest(conn, credit_row, credit_row["received_at"].astimezone(IST).date(), limit=None)
    chosen = [m for m, _ in parts]
    ranks = [i for i, s in enumerate(ranked, start=1) if s["member_id"] in chosen]
    candidates = [{"member_id": s["member_id"], "signals": s["signals"]} for s in ranked[:10]]
    candidates += [{"member_id": m, "signals": []} for m in chosen
                   if m not in {c["member_id"] for c in candidates}]
    conn.execute(
        "INSERT INTO review_outcomes (institution_id, bank_credit_id, chosen, rank_of_choice, candidates) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (bank_credit_id) DO NOTHING",
        (inst, credit_row["id"], Jsonb(chosen), min(ranks) if ranks else None, Jsonb(candidates)))


def split_credit(conn, inst: int, credit_id: int, parts, remember_upi_id: bool = True) -> dict:
    """Resolve a review-queue credit by saying which members it pays for, and how much each."""
    parts = _normalise_parts(parts)
    with tenant_tx(conn, inst):
        row = _lock_credit(conn, credit_id)     # two clerks cannot resolve it at once
        _check_parts(parts, row["amount"])
        if row["status"] == "reversed":
            raise Conflict(f"bank credit {credit_id} was reversed by the bank")
        if row["status"] == "matched":
            if _current_parts(conn, credit_id) == parts:
                return {"credit": credit_view(row), "duplicate": True, "applied": []}   # a retried click
            raise Conflict(f"bank credit {credit_id} is already assigned; use reassign to move it")
        lock_members(conn, [m for m, _ in parts])
        _record_outcome(conn, inst, row, parts)
        row = conn.execute("UPDATE bank_credits SET status = 'matched', match_rule = 'manual' "
                           "WHERE id = %s RETURNING *", (credit_id,)).fetchone()
        if remember_upi_id:
            _learn_upi_id(conn, inst, row["payer_upi_id"], [m for m, _ in parts])
        applied = _apply_parts(conn, inst, row, parts)
        audit.record(conn, inst, "credit.assigned", "bank_credit", credit_id,
                     parts=[list(p) for p in parts], learned_upi_id=bool(remember_upi_id and row["payer_upi_id"]))
        return {"credit": credit_view(row), "duplicate": False, "applied": applied}


def assign_credit(conn, inst: int, credit_id: int, member_id: int, remember_upi_id: bool = True) -> dict:
    """Give the whole credit to one member."""
    with tenant_tx(conn, inst):
        row = _lock_credit(conn, credit_id)
        # The inner tenant_tx becomes a savepoint, and re-locking a row we already hold is free.
        return split_credit(conn, inst, credit_id, [(member_id, row["amount"])], remember_upi_id)


def reassign_credit(conn, inst: int, credit_id: int, parts) -> dict:
    """Move an assigned credit to different members.

    Old and new members are locked in one sorted call: clerk A moving money
    from Diya to Kabir while clerk B moves other money from Kabir to Diya
    would deadlock with "from, then to" locking, and cannot with sorted ids.
    """
    parts = _normalise_parts(parts)
    with tenant_tx(conn, inst):
        row = _lock_credit(conn, credit_id)
        if row["status"] == "reversed":
            raise Conflict(f"bank credit {credit_id} was reversed by the bank")
        if row["status"] != "matched":
            raise Conflict(f"bank credit {credit_id} is not assigned yet; assign it instead")
        _check_parts(parts, row["amount"])
        old = _current_parts(conn, credit_id)
        if old == parts:
            return {"credit": credit_view(row), "changed": False, "applied": []}
        affected = [m for m, _ in old] + [m for m, _ in parts]       # "from", then "to"...
        lock_members(conn, affected)                                  # ...but locked in id order
        void_payments(conn, inst, credit_id)           # correction rows, not deletes
        row = conn.execute("UPDATE bank_credits SET match_rule = 'manual' WHERE id = %s RETURNING *",
                           (credit_id,)).fetchone()
        applied = _apply_parts(conn, inst, row, parts)
        # The old owner may have other unspent credit that can now pay what reopened.
        settle_members(conn, inst, affected)
        audit.record(conn, inst, "credit.reassigned", "bank_credit", credit_id,
                     before=[list(p) for p in old], after=[list(p) for p in parts])
        return {"credit": credit_view(row), "changed": True, "applied": applied}


def reverse_credit(conn, inst: int, credit_id: int, *, reason: str | None = None,
                   reference: str | None = None, reversed_at=None) -> dict:
    """The bank returned this credit: take its money back out of the books.

    The reversal is recorded as a new row. The credit's payments are voided
    with correction rows, which reopens the charges they paid; each member is
    settled again in case other credit can cover what reopened; and each
    member is told, through the outbox, in the same transaction.
    """
    with tenant_tx(conn, inst):
        row = _lock_credit(conn, credit_id)
        if row["status"] == "reversed":
            return {"credit": credit_view(row), "duplicate": True, "reopened": []}
        was = _current_parts(conn, credit_id)
        lock_members(conn, [m for m, _ in was])
        void_payments(conn, inst, credit_id)
        conn.execute(
            "INSERT INTO credit_reversals (institution_id, bank_credit_id, amount, reference, reason, reversed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (inst, credit_id, row["amount"], reference, reason, reversed_at or utc_now()))
        row = conn.execute("UPDATE bank_credits SET status = 'reversed' WHERE id = %s RETURNING *",
                           (credit_id,)).fetchone()
        members = [m for m, _ in was]
        settle_members(conn, inst, members)
        after = balances_with_details(conn, members)
        reopened, notices = [], []
        for member_id, amount in was:
            member = after[member_id]
            notice = {"member_id": member_id, "ref_code": member["ref_code"], "member_name": member["name"],
                      "phone": member["phone"], "utr": row["utr"], "amount": to_rupees(amount),
                      "outstanding_after": to_rupees(member["outstanding"]),
                      "credit_after": to_rupees(member["credit"])}
            notices.append((member_id, notice))
            reopened.append({k: notice[k] for k in ("member_id", "ref_code", "amount", "outstanding_after")})
        outbox.enqueue_many(conn, inst, kind="payment_reversed", rows=notices)
        audit.record(conn, inst, "credit.reversed", "bank_credit", credit_id,
                     was=[list(p) for p in was], reason=reason, reference=reference)
        return {"credit": credit_view(row), "duplicate": False, "reopened": reopened}


def get_credit(conn, inst: int, credit_id: int) -> dict:
    with tenant_tx(conn, inst):
        row = conn.execute("SELECT * FROM bank_credits WHERE id = %s", (credit_id,)).fetchone()
        if row is None:
            raise NotFound(f"bank credit {credit_id} not found")
        rows = conn.execute(
            f"""SELECT p.id, p.member_id, m.ref_code, p.amount, p.reverses_id, p.created_at, {LIVE} AS live
                  FROM payments p JOIN members m ON m.id = p.member_id
                 WHERE p.bank_credit_id = %s ORDER BY p.id""", (credit_id,)).fetchall()
        return {**credit_view(row),
                "parts": [{"member_id": p["member_id"], "ref_code": p["ref_code"],
                           "amount": to_rupees(p["amount"])}
                          for p in sorted(rows, key=lambda r: r["member_id"]) if p["live"]],
                # Every row ever written for this credit, corrections included.
                "history": [{"payment_id": p["id"], "member_id": p["member_id"], "ref_code": p["ref_code"],
                             "amount": to_rupees(p["amount"]), "corrects_payment_id": p["reverses_id"],
                             "recorded_at": p["created_at"].isoformat()} for p in rows]}


def review_queue(conn, inst: int, as_of, limit: int = 50) -> list[dict]:
    """The credits waiting for a person, oldest first, each with ranked suggestions.

    The members, their dues, their fees and the payers' families are read once
    for the page, not once per credit. The page is limited because every credit
    on it is scored against every active member: a person works through a
    queue a screenful at a time, and an unbounded page would make the slowest
    institution the slowest request.
    """
    with tenant_tx(conn, inst):
        rows = conn.execute("SELECT * FROM bank_credits WHERE status = 'needs_review' "
                            "ORDER BY received_at, id LIMIT %s", (limit,)).fetchall()
        if not rows:
            return []
        prepared = prepare(conn, as_of, [r["payer_upi_id"] for r in rows])
        return [{**credit_view(r), "suggestions": suggest(conn, r, as_of, prepared=prepared)} for r in rows]
