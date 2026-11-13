"""The transactional outbox: messages queued with the change they report."""

import uuid

from psycopg.types.json import Jsonb


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
