"""The audit log: one row per change, written in the same transaction as the change.

Because the row is inserted inside the caller's transaction, an entry exists
if and only if the change it describes was committed. Each row records who
made the change (actors.current_actor), or NULL for the system.
"""

from psycopg.types.json import Jsonb

from .actors import current_actor
from .db import tenant_tx
from .paging import limits


def record(conn, institution_id: int, action: str, entity: str, entity_id: int | None,
           **details) -> None:
    conn.execute(
        "INSERT INTO audit_log (institution_id, actor_id, action, entity, entity_id, details) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (institution_id, current_actor.get(), action, entity, entity_id, Jsonb(details)))


def recent(conn, institution_id: int, limit: int | None = None,
           offset: int | None = None) -> list[dict]:
    size, start = limits(limit, offset)
    with tenant_tx(conn, institution_id):
        rows = conn.execute(
            """SELECT a.id, a.action, a.entity, a.entity_id, a.details, a.created_at,
                      a.actor_id, s.name AS actor
                 FROM audit_log a LEFT JOIN staff s ON s.id = a.actor_id
                ORDER BY a.id DESC LIMIT %s OFFSET %s""", (size, start)).fetchall()
    return [{**r, "actor": r["actor"] or "system", "created_at": r["created_at"].isoformat()} for r in rows]
