"""The audit log: one row per change, written in the same transaction as the change.

Because the row is inserted inside the caller's transaction, an entry exists
if and only if the change it describes was committed.
"""

from psycopg.types.json import Jsonb

from .db import tenant_tx


def record(conn, institution_id: int, action: str, entity: str, entity_id: int | None,
           **details) -> None:
    conn.execute(
        "INSERT INTO audit_log (institution_id, action, entity, entity_id, details) "
        "VALUES (%s, %s, %s, %s, %s)",
        (institution_id, action, entity, entity_id, Jsonb(details)))


def recent(conn, institution_id: int, limit: int = 100) -> list[dict]:
    with tenant_tx(conn, institution_id):
        rows = conn.execute(
            "SELECT id, action, entity, entity_id, details, created_at FROM audit_log "
            "ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
    return [{**r, "created_at": r["created_at"].isoformat()} for r in rows]
