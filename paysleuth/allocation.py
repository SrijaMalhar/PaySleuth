"""Applying money to what is owed, oldest due date first."""

# A payment that has not been taken back: not a correction row, and no correction row points at it.
LIVE = "p.reverses_id IS NULL AND NOT EXISTS (SELECT 1 FROM payments v WHERE v.reverses_id = p.id)"


def settle_member(conn, inst: int, member_id: int) -> list[dict]:
    """Apply unspent money to unpaid charges, oldest due date first.

    The caller must already hold this member's lock. This is the merge step of
    merge sort: two lists sorted by date, take the smaller remainder, advance
    whichever pointer reached zero. O(P + C) after the two queries.
    """
    payments = conn.execute(
        f"""SELECT p.id, p.amount - COALESCE(SUM(a.amount), 0) AS unspent
             FROM payments p LEFT JOIN allocations a ON a.payment_id = p.id
            WHERE p.member_id = %s AND {LIVE} GROUP BY p.id
           HAVING p.amount - COALESCE(SUM(a.amount), 0) > 0
            ORDER BY p.received_at, p.id""", (member_id,)).fetchall()
    if not payments:
        return []
    charges = conn.execute(
        """SELECT c.id, c.period, c.kind, c.amount - COALESCE(SUM(a.amount), 0) AS unpaid
             FROM charges c LEFT JOIN allocations a ON a.charge_id = c.id
            WHERE c.member_id = %s GROUP BY c.id
           HAVING c.amount - COALESCE(SUM(a.amount), 0) > 0
            ORDER BY c.due_date, c.id""", (member_id,)).fetchall()

    created, pi, ci = [], 0, 0
    unspent = [p["unspent"] for p in payments]
    unpaid = [c["unpaid"] for c in charges]
    while pi < len(payments) and ci < len(charges):
        amount = min(unspent[pi], unpaid[ci])
        # Always a new row. A payment can come back to a charge it part-paid
        # earlier (after another payment is moved away); that is simply a
        # second row, and every reader sums.
        conn.execute(
            "INSERT INTO allocations (institution_id, payment_id, charge_id, amount) VALUES (%s, %s, %s, %s)",
            (inst, payments[pi]["id"], charges[ci]["id"], amount))
        created.append({"payment_id": payments[pi]["id"], "charge_id": charges[ci]["id"],
                        "period": charges[ci]["period"], "kind": charges[ci]["kind"],
                        "amount": amount})
        unspent[pi] -= amount
        unpaid[ci] -= amount
        if unspent[pi] == 0:
            pi += 1
        if unpaid[ci] == 0:
            ci += 1
    return created


def balances(conn, member_ids) -> dict[int, dict]:
    """Billed, paid, outstanding and credit per member, computed from sums.

    outstanding = billed - allocated and credit = paid - allocated. Because
    settle_member always runs after money or charges change, at most one of
    the two is non-zero for any member.
    """
    rows = conn.execute(
        """SELECT m.id,
                  COALESCE((SELECT SUM(c.amount) FROM charges c WHERE c.member_id = m.id), 0) AS billed,
                  COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.member_id = m.id), 0) AS paid,
                  COALESCE((SELECT SUM(a.amount) FROM allocations a
                              JOIN payments p ON p.id = a.payment_id
                             WHERE p.member_id = m.id), 0) AS allocated
             FROM members m WHERE m.id = ANY(%s)""", (list(member_ids),)).fetchall()
    return {r["id"]: {"billed": r["billed"], "paid": r["paid"],
                      "outstanding": r["billed"] - r["allocated"],
                      "credit": r["paid"] - r["allocated"]} for r in rows}


def outstanding(conn, member_id: int) -> int:
    return balances(conn, [member_id]).get(member_id, {"outstanding": 0})["outstanding"]
