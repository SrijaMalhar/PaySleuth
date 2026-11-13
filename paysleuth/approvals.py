"""Two-person approval (maker-checker) for large reassignments.

Moving a credit from one family to another is how money would be hidden, so a
reassignment above the institution's threshold does not happen when it is
asked for. It waits until a second person approves it, and at least one of
the two must be an owner.

Locking order: the approval row, then the bank credit, then members.
"""

from psycopg.types.json import Jsonb

from . import audit
from .actors import Actor
from .db import tenant_tx
from .errors import Conflict, Forbidden, NotFound
from .institutions import settings
from .money import to_rupees
from .payments import _check_parts, _current_parts, _lock_credit, _normalise_parts, reassign_credit


def request_reassign(conn, inst: int, actor: Actor, credit_id: int, parts) -> dict:
    """Reassign now if the credit is small enough, otherwise queue it for a second person."""
    parts = _normalise_parts(parts)
    threshold = settings(conn, inst)["approval_threshold"]
    with tenant_tx(conn, inst):
        row = _lock_credit(conn, credit_id)
        if threshold == 0 or row["amount"] <= threshold:
            return {"status": "applied", **reassign_credit(conn, inst, credit_id, parts)}
        # Validate now, so the approver is never shown a request that could not be applied.
        if row["status"] != "matched":
            raise Conflict(f"bank credit {credit_id} is not assigned; there is nothing to reassign")
        _check_parts(parts, row["amount"])
        before = _current_parts(conn, credit_id)
        if before == parts:
            return {"status": "applied", **reassign_credit(conn, inst, credit_id, parts)}
        known = conn.execute("SELECT COUNT(*) AS n FROM members WHERE id = ANY(%s)",
                             ([m for m, _ in parts],)).fetchone()["n"]
        if known != len(parts):
            raise NotFound("a member in the request was not found")
        request = conn.execute(
            """INSERT INTO approval_requests (institution_id, action, bank_credit_id, amount, payload, requested_by)
               VALUES (%s, 'reassign_credit', %s, %s, %s, %s)
               ON CONFLICT (bank_credit_id) WHERE status = 'pending' DO NOTHING RETURNING id""",
            (inst, credit_id, row["amount"],
             Jsonb({"before": [list(p) for p in before], "after": [list(p) for p in parts]}),
             actor.staff_id)).fetchone()
        if request is None:
            raise Conflict(f"bank credit {credit_id} already has a reassignment waiting for approval")
        audit.record(conn, inst, "approval.requested", "approval", request["id"], bank_credit_id=credit_id,
                     amount=row["amount"], after=[list(p) for p in parts])
        return {"status": "pending_approval", "approval_id": request["id"],
                "reason": f"Rs {to_rupees(row['amount'])} is above the Rs {to_rupees(threshold)} limit; "
                          f"a second person must approve"}


def decide(conn, inst: int, actor: Actor, approval_id: int, *, approve: bool, note: str | None = None) -> dict:
    with tenant_tx(conn, inst):
        request = conn.execute("SELECT * FROM approval_requests WHERE id = %s FOR UPDATE",
                               (approval_id,)).fetchone()
        if request is None:
            raise NotFound(f"approval {approval_id} not found")
        if request["status"] != "pending":
            raise Conflict(f"approval {approval_id} was already {request['status']}")
        if actor.staff_id == request["requested_by"]:
            raise Forbidden("the person who asked for a reassignment cannot be the one who approves it")
        requester = conn.execute("SELECT role FROM staff WHERE id = %s", (request["requested_by"],)).fetchone()
        if "owner" not in (actor.role, requester["role"]):
            raise Forbidden("one of the two people must be an owner")

        result = None
        if approve:
            if [list(p) for p in _current_parts(conn, request["bank_credit_id"])] != request["payload"]["before"]:
                raise Conflict("the credit changed after this was requested; reject it and ask again")
            result = reassign_credit(conn, inst, request["bank_credit_id"],
                                     [tuple(p) for p in request["payload"]["after"]])
        status = "approved" if approve else "rejected"
        conn.execute("UPDATE approval_requests SET status = %s, decided_by = %s, decided_at = now(), note = %s "
                     "WHERE id = %s", (status, actor.staff_id, note, approval_id))
        audit.record(conn, inst, f"approval.{status}", "approval", approval_id,
                     bank_credit_id=request["bank_credit_id"], requested_by=request["requested_by"])
        return {"approval_id": approval_id, "status": status, "result": result}


def list_approvals(conn, inst: int, status: str | None = "pending") -> list[dict]:
    with tenant_tx(conn, inst):
        rows = conn.execute(
            """SELECT r.id, r.action, r.bank_credit_id, b.utr, r.amount, r.payload, r.status, r.note,
                      r.requested_by, q.name AS requested_by_name, r.decided_by, d.name AS decided_by_name,
                      r.created_at, r.decided_at
                 FROM approval_requests r JOIN bank_credits b ON b.id = r.bank_credit_id
                 JOIN staff q ON q.id = r.requested_by LEFT JOIN staff d ON d.id = r.decided_by
                WHERE %s::text IS NULL OR r.status = %s ORDER BY r.id""", (status, status)).fetchall()
    return [{**r, "amount": to_rupees(r["amount"]), "created_at": r["created_at"].isoformat(),
             "decided_at": r["decided_at"].isoformat() if r["decided_at"] else None} for r in rows]
