"""The transactional outbox: a small queue on PostgreSQL with the guarantees of a cloud queue.

    Never a payment without its receipt   The receipt row is inserted in the payment's transaction.
    No two workers send one row at once   The claim uses FOR UPDATE SKIP LOCKED.
    A crashed worker's messages still go  A claim older than VISIBILITY_TIMEOUT goes back to pending,
                                          so delivery is at-least-once and every payload carries
                                          message_id for the gateway to drop repeats.
    A member's receipts arrive in order   Only a member's oldest pending message of a kind is claimable.
    No transaction open while sending     Claim and commit, send outside any transaction, then record
                                          the result in a new short transaction.
"""

import uuid
from datetime import datetime, timedelta

from psycopg.types.json import Jsonb

from .db import tenant_tx
from .institutions import settings
from .money import to_rupees
from .periods import IST, utc_now
from .paging import limits

# 12 attempts over about two and a half hours: long enough to ride out a gateway
# outage, short enough that a genuinely undeliverable message is parked the same
# working day. The cap is reached on the last few waits, which is what makes it
# a real limit rather than dead code.
MAX_ATTEMPTS = 12
VISIBILITY_TIMEOUT = timedelta(seconds=60)


def enqueue(conn, inst: int, *, kind: str, member_id: int, payload: dict, charge_id: int | None = None,
            dedupe_key: str | None = None, quiet_hours: bool = False) -> str | None:
    """Queue a message inside the caller's transaction.

    Returns the message id, or None if dedupe_key was already queued. Receipts
    pass dedupe_key=None, and NULL never equals NULL in a UNIQUE constraint,
    so receipts never collide with each other.
    """
    message_id = str(uuid.uuid4())
    row = conn.execute(
        """INSERT INTO outbox (institution_id, message_id, kind, member_id, charge_id, dedupe_key,
                               payload, quiet_hours)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (institution_id, dedupe_key) DO NOTHING RETURNING id""",
        (inst, message_id, kind, member_id, charge_id, dedupe_key,
         Jsonb({"message_id": message_id, "kind": kind, **payload}), quiet_hours)).fetchone()
    return message_id if row else None


def enqueue_many(conn, inst: int, *, kind: str, rows) -> list[str]:
    """Queue several messages of one kind in one INSERT, inside the caller's transaction.

    For messages with no dedupe key, such as receipts: a sibling split queues
    one per child, and there is no reason for a round trip each.
    """
    rows = list(rows)
    if not rows:
        return []
    message_ids = [str(uuid.uuid4()) for _ in rows]
    values = ", ".join(["(%s, %s, %s, %s, %s)"] * len(rows))
    params = [v for (member_id, payload), message_id in zip(rows, message_ids)
              for v in (inst, message_id, kind, member_id,
                        Jsonb({"message_id": message_id, "kind": kind, **payload}))]
    conn.execute(f"INSERT INTO outbox (institution_id, message_id, kind, member_id, payload) "
                 f"VALUES {values}", params)
    return message_ids


def backoff(attempts: int) -> timedelta:
    """5, 10, 20, 40 ... seconds, capped at an hour (reached on attempt 11)."""
    return timedelta(seconds=min(5 * 2 ** (attempts - 1), 3600))


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end          # a window that wraps midnight, such as 21 to 8


def next_allowed(moment: datetime, end: int) -> datetime:
    """The next time the quiet window ends: today at end o'clock IST, or tomorrow if that has passed."""
    local = moment.astimezone(IST)
    allowed = local.replace(hour=end, minute=0, second=0, microsecond=0)
    if allowed <= local:
        allowed += timedelta(days=1)
    return allowed


def _claim(conn, inst: int, t: datetime, batch: int) -> list[dict]:
    with tenant_tx(conn, inst):
        conn.execute("UPDATE outbox SET status = 'pending', claimed_at = NULL "
                     "WHERE status = 'sending' AND claimed_at < %s", (t - VISIBILITY_TIMEOUT,))
        return conn.execute(
            """WITH ready AS (
                   SELECT o.id FROM outbox o
                    WHERE o.status = 'pending' AND o.next_attempt_at <= %(now)s
                      AND NOT EXISTS (SELECT 1 FROM outbox e
                                       WHERE e.member_id = o.member_id AND e.kind = o.kind
                                         AND e.status IN ('pending', 'sending') AND e.id < o.id)
                    ORDER BY o.id
                    LIMIT %(batch)s
                      FOR UPDATE SKIP LOCKED)
               UPDATE outbox o SET status = 'sending', claimed_at = %(now)s
                 FROM ready WHERE o.id = ready.id
            RETURNING o.*""", {"now": t, "batch": batch}).fetchall()


def _prepare(conn, inst: int, msg: dict, t: datetime, cfg: dict):
    """Decide what to do with a claimed message: ("send", payload), ("skip", reason) or ("later", when)."""
    payload = dict(msg["payload"])
    if msg["kind"] == "overdue_reminder":
        with tenant_tx(conn, inst):
            later = conn.execute("SELECT 1 FROM outbox WHERE charge_id = %s AND kind = 'overdue_reminder' "
                                 "AND id > %s LIMIT 1", (msg["charge_id"], msg["id"])).fetchone()
            if later:
                return "skip", "a later reminder for this fee exists"
            unpaid = conn.execute(
                """SELECT c.amount - COALESCE(SUM(a.amount), 0) AS unpaid
                     FROM charges c LEFT JOIN allocations a ON a.charge_id = c.id
                    WHERE c.id = %s GROUP BY c.id""", (msg["charge_id"],)).fetchone()["unpaid"]
        if unpaid <= 0:
            return "skip", "the fee has been paid"
        payload["amount_due"] = to_rupees(unpaid)
    if msg["quiet_hours"] and in_quiet_hours(t.astimezone(IST).hour, cfg["quiet_start"], cfg["quiet_end"]):
        return "later", next_allowed(t, cfg["quiet_end"])
    return "send", payload


def _guarded(conn, inst: int, msg: dict, sql: str, params: tuple) -> bool:
    """Update a message only if this worker still owns its claim.

    A slow worker whose claim already expired and was taken by another worker
    must not overwrite the new owner's state.
    """
    with tenant_tx(conn, inst):
        row = conn.execute(sql + " WHERE id = %s AND status = 'sending' AND claimed_at = %s RETURNING id",
                           (*params, msg["id"], msg["claimed_at"])).fetchone()
    return row is not None


def _finish(conn, inst: int, msg: dict, status: str, t: datetime, note: str | None = None) -> bool:
    return _guarded(conn, inst, msg, "UPDATE outbox SET status = %s, sent_at = %s, claimed_at = NULL, "
                    "last_error = %s", (status, t if status == "sent" else None, note))


def relay_once(conn, inst: int, send, *, now=utc_now, batch: int = 50) -> dict:
    """Claim a batch, then deliver each message outside any transaction."""
    t = now()
    claimed = _claim(conn, inst, t, batch)          # the claim is committed here
    summary = {"claimed": len(claimed), "sent": 0, "skipped": 0, "deferred": 0, "failed": 0, "dead": 0}
    if not claimed:
        return summary
    cfg = settings(conn, inst)
    for msg in sorted(claimed, key=lambda m: m["id"]):
        action, value = _prepare(conn, inst, msg, t, cfg)
        if action == "skip":
            summary["skipped"] += _finish(conn, inst, msg, "skipped", t, value)
            continue
        if action == "later":
            summary["deferred"] += _guarded(conn, inst, msg, "UPDATE outbox SET status = 'pending', "
                                            "claimed_at = NULL, next_attempt_at = %s", (value,))
            continue
        try:
            send(value)
        except Exception as error:                      # any failure is retried
            attempts = msg["attempts"] + 1
            dead = attempts >= MAX_ATTEMPTS
            _guarded(conn, inst, msg,
                     "UPDATE outbox SET status = %s, attempts = %s, next_attempt_at = %s, "
                     "claimed_at = NULL, last_error = %s",
                     ("dead" if dead else "pending", attempts, t + backoff(attempts),
                      f"{type(error).__name__}: {error}"[:500]))
            summary["dead" if dead else "failed"] += 1
            continue
        summary["sent"] += _finish(conn, inst, msg, "sent", t)
    return summary


def dead_messages(conn, inst: int, limit: int | None = None,
                  offset: int | None = None) -> list[dict]:
    size, start = limits(limit, offset)
    with tenant_tx(conn, inst):
        rows = conn.execute("SELECT id, message_id, kind, member_id, attempts, last_error, created_at "
                            "FROM outbox WHERE status = 'dead' ORDER BY id LIMIT %s OFFSET %s",
                            (size, start)).fetchall()
    return [{**r, "message_id": str(r["message_id"]), "created_at": r["created_at"].isoformat()}
            for r in rows]
