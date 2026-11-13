"""Institutions (tenants) and their API keys."""

import hashlib
import secrets

from . import audit
from .db import tenant_tx
from .errors import NotFound


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_institution(conn, name: str, *, late_fee: int = 0, grace_days: int = 0,
                       quiet_start: int = 21, quiet_end: int = 8) -> tuple[int, str]:
    """Create a tenant and return (id, api_key).

    Only the key's SHA-256 digest is stored, so a leaked database leaks no
    working keys. The key is returned here once and can never be read again.
    """
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("institution name is required")
    key = "ps_" + secrets.token_urlsafe(32)
    with conn.transaction():
        institution_id = conn.execute(
            "INSERT INTO institutions (name, api_key_hash, late_fee, grace_days, quiet_start, quiet_end) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (name, _hash(key), late_fee, grace_days, quiet_start, quiet_end)).fetchone()["id"]
        with tenant_tx(conn, institution_id):
            audit.record(conn, institution_id, "institution.created", "institution", institution_id,
                         name=name)
    return institution_id, key


def institution_for_key(conn, key: str | None) -> int | None:
    if not key:
        return None
    row = conn.execute("SELECT id FROM institutions WHERE api_key_hash = %s", (_hash(key),)).fetchone()
    return row["id"] if row else None


def settings(conn, institution_id: int) -> dict:
    row = conn.execute("SELECT id, name, late_fee, grace_days, quiet_start, quiet_end "
                       "FROM institutions WHERE id = %s", (institution_id,)).fetchone()
    if row is None:
        raise NotFound(f"institution {institution_id} not found")
    return row


def all_institution_ids(conn) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM institutions ORDER BY id").fetchall()]
