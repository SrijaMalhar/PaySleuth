"""Applying money to what is owed, oldest due date first."""

# A payment that has not been taken back: not a correction row, and no correction row points at it.
LIVE = "p.reverses_id IS NULL AND NOT EXISTS (SELECT 1 FROM payments v WHERE v.reverses_id = p.id)"


def _merge(payments: list[dict], charges: list[dict]) -> list[dict]:
    """Pair unspent money with unpaid charges, oldest first.

    The merge step of merge sort: two lists sorted by date, take the smaller
    remainder, advance whichever pointer reached zero. O(P + C), no queries.
    """
    created, pi, ci = [], 0, 0
    unspent = [p["unspent"] for p in payments]
    unpaid = [c["unpaid"] for c in charges]
    while pi < len(payments) and ci < len(charges):
        amount = min(unspent[pi], unpaid[ci])
        created.append({"member_id": payments[pi]["member_id"], "payment_id": payments[pi]["id"],
                        "charge_id": charges[ci]["id"], "period": charges[ci]["period"],
                        "kind": charges[ci]["kind"], "amount": amount})
        unspent[pi] -= amount
        unpaid[ci] -= amount
        if unspent[pi] == 0:
            pi += 1
        if unpaid[ci] == 0:
            ci += 1
    return created


def settle_members(conn, inst: int, member_ids) -> list[dict]:
    """Apply unspent money to unpaid charges for several members at once.

    The caller must already hold these members' locks. Two queries whatever
    the number of members: the unspent payments and the unpaid charges of all
    of them, each ordered so one pass splits them per member. The allocations
    then go in as one multi-row INSERT.

    Members are independent of each other here, because an allocation can only
    join a payment and a charge of the same member.
    """
    wanted = sorted(set(member_ids))
    if not wanted:
        return []
    payments = conn.execute(
        f"""SELECT p.member_id, p.id, p.amount - COALESCE(SUM(a.amount), 0) AS unspent
             FROM payments p LEFT JOIN allocations a ON a.payment_id = p.id
            WHERE p.member_id = ANY(%s) AND {LIVE} GROUP BY p.id
           HAVING p.amount - COALESCE(SUM(a.amount), 0) > 0
            ORDER BY p.member_id, p.received_at, p.id""", (wanted,)).fetchall()
    if not payments:
        return []
    with_money = sorted({p["member_id"] for p in payments})
    charges = conn.execute(
        """SELECT c.member_id, c.id, c.period, c.kind, c.amount - COALESCE(SUM(a.amount), 0) AS unpaid
             FROM charges c LEFT JOIN allocations a ON a.charge_id = c.id
            WHERE c.member_id = ANY(%s) GROUP BY c.id
           HAVING c.amount - COALESCE(SUM(a.amount), 0) > 0
            ORDER BY c.member_id, c.due_date, c.id""", (with_money,)).fetchall()
    if not charges:
        return []

    by_member = {member_id: [] for member_id in with_money}
    for charge in charges:
        if charge["member_id"] in by_member:
            by_member[charge["member_id"]].append(charge)
    created = []
    for member_id in with_money:
        created += _merge([p for p in payments if p["member_id"] == member_id], by_member[member_id])
    if created:
        # One INSERT, however many pairs: a round trip per pair was the cost here.
        # Always new rows. A payment can come back to a charge it part-paid earlier
        # (after another payment is moved away); that is simply a second row, and
        # every reader sums.
        values = ", ".join(["(%s, %s, %s, %s)"] * len(created))
        params = [value for a in created for value in (inst, a["payment_id"], a["charge_id"], a["amount"])]
        conn.execute(f"INSERT INTO allocations (institution_id, payment_id, charge_id, amount) "
                     f"VALUES {values}", params)
    return created


def settle_member(conn, inst: int, member_id: int) -> list[dict]:
    """Settle one member. The caller must already hold their lock."""
    return settle_members(conn, inst, [member_id])


def _balances(conn, member_ids, extra: str = "") -> dict[int, dict]:
    rows = conn.execute(
        f"""SELECT m.id{extra},
                   COALESCE((SELECT SUM(c.amount) FROM charges c WHERE c.member_id = m.id), 0) AS billed,
                   COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.member_id = m.id), 0) AS paid,
                   COALESCE((SELECT SUM(a.amount) FROM allocations a
                               JOIN payments p ON p.id = a.payment_id
                              WHERE p.member_id = m.id), 0) AS allocated
              FROM members m WHERE m.id = ANY(%s)""", (list(member_ids),)).fetchall()
    out = {}
    for row in rows:
        member_id, allocated = row.pop("id"), row.pop("allocated")
        out[member_id] = {**row, "outstanding": row["billed"] - allocated,
                          "credit": row["paid"] - allocated}
    return out


def balances(conn, member_ids) -> dict[int, dict]:
    """Billed, paid, outstanding and credit per member, computed from sums.

    outstanding = billed - allocated and credit = paid - allocated. Because
    settling always runs after money or charges change, at most one of the two
    is non-zero for any member.
    """
    return _balances(conn, member_ids)


def balances_with_details(conn, member_ids) -> dict[int, dict]:
    """The same, plus each member's reference code, name and phone.

    A receipt needs the money and the name together, and they are on the same
    row, so this is one query rather than two.
    """
    return _balances(conn, member_ids, extra=", m.ref_code, m.name, m.phone")


def outstanding(conn, member_id: int) -> int:
    return balances(conn, [member_id]).get(member_id, {"outstanding": 0})["outstanding"]
