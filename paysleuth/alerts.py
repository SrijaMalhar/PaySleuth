"""Alerts: things a person must look at, raised once however often they are detected."""

from psycopg.types.json import Jsonb

from .db import tenant_tx
from .errors import NotFound
from .paging import limits


def raise_alert(conn, inst: int, kind: str, message: str, dedupe_key: str, **details) -> int | None:
    """Call inside the caller's transaction. Returns the alert id, or None if it was already raised."""
    row = conn.execute(
        """INSERT INTO alerts (institution_id, kind, message, details, dedupe_key)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (institution_id, dedupe_key) DO NOTHING RETURNING id""",
        (inst, kind, message, Jsonb(details), dedupe_key)).fetchone()
    return row["id"] if row else None


def _view(row: dict) -> dict:
    return {**row, "created_at": row["created_at"].isoformat(),
            "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] else None}


def list_alerts(conn, inst: int, *, status: str | None = "open",
                limit: int | None = None, offset: int | None = None) -> list[dict]:
    size, start = limits(limit, offset)
    with tenant_tx(conn, inst):
        rows = conn.execute(
            "SELECT id, kind, message, details, status, created_at, resolved_at FROM alerts "
            "WHERE (%s::text IS NULL OR status = %s) ORDER BY id LIMIT %s OFFSET %s",
            (status, status, size, start)).fetchall()
    return [_view(r) for r in rows]


def resolve_alert(conn, inst: int, alert_id: int) -> dict:
    with tenant_tx(conn, inst):
        row = conn.execute(
            "UPDATE alerts SET status = 'resolved', resolved_at = COALESCE(resolved_at, now()) "
            "WHERE id = %s RETURNING id, kind, message, details, status, created_at, resolved_at",
            (alert_id,)).fetchone()
        if row is None:
            raise NotFound(f"alert {alert_id} not found")
    return _view(row)
