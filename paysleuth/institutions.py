"""Institutions (tenants), their staff and API keys."""

import hashlib
import secrets

from . import audit
from .actors import Actor
from .db import tenant_tx
from .errors import Conflict, NotFound
from .paging import limits

ROLES = ("owner", "clerk")


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _clean(name: str | None, what: str) -> str:
    name = " ".join((name or "").split())
    if not name or len(name) > 120:
        raise ValueError(f"{what} is required, at most 120 characters")
    return name


def _add_staff(conn, institution_id: int, name: str, role: str) -> tuple[int, str]:
    # Only the key's SHA-256 digest is stored, so a leaked database leaks no working keys.
    key = "ps_" + secrets.token_urlsafe(32)
    staff_id = conn.execute(
        "INSERT INTO staff (institution_id, name, role, api_key_hash) VALUES (%s, %s, %s, %s) RETURNING id",
        (institution_id, name, role, _hash(key))).fetchone()["id"]
    return staff_id, key


def create_institution(conn, name: str, *, late_fee: int = 0, grace_days: int = 0,
                       quiet_start: int = 21, quiet_end: int = 8, approval_threshold: int = 0,
                       owner_name: str = "Owner") -> tuple[int, str]:
    """Create a tenant with its first owner and return (id, owner_api_key).

    The key is returned here once and can never be read again.
    """
    name = _clean(name, "institution name")
    owner_name = _clean(owner_name, "owner name")
    with conn.transaction():
        institution_id = conn.execute(
            "INSERT INTO institutions (name, late_fee, grace_days, quiet_start, quiet_end, approval_threshold) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (name, late_fee, grace_days, quiet_start, quiet_end, approval_threshold)).fetchone()["id"]
        _, key = _add_staff(conn, institution_id, owner_name, "owner")
        with tenant_tx(conn, institution_id):
            audit.record(conn, institution_id, "institution.created", "institution", institution_id,
                         name=name)
    return institution_id, key


def actor_for_key(conn, key: str | None) -> Actor | None:
    if not key:
        return None
    row = conn.execute("SELECT id, institution_id, role, name FROM staff WHERE api_key_hash = %s AND active",
                       (_hash(key),)).fetchone()
    return Actor(row["institution_id"], row["id"], row["role"], row["name"]) if row else None


def institution_for_key(conn, key: str | None) -> int | None:
    actor = actor_for_key(conn, key)
    return actor.institution_id if actor else None


def add_staff(conn, institution_id: int, name: str, role: str) -> dict:
    """Add a staff member and return their details with the API key, shown once."""
    name = _clean(name, "staff name")
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    with tenant_tx(conn, institution_id):
        staff_id, key = _add_staff(conn, institution_id, name, role)
        audit.record(conn, institution_id, "staff.added", "staff", staff_id, name=name, role=role)
    return {"id": staff_id, "name": name, "role": role, "active": True, "api_key": key}


def list_staff(conn, institution_id: int, limit: int | None = None,
               offset: int | None = None) -> list[dict]:
    size, start = limits(limit, offset)
    return conn.execute("SELECT id, name, role, active FROM staff WHERE institution_id = %s "
                        "ORDER BY id LIMIT %s OFFSET %s", (institution_id, size, start)).fetchall()


def deactivate_staff(conn, institution_id: int, staff_id: int) -> dict:
    """Turn a staff member's key off. The last active owner cannot be removed."""
    with tenant_tx(conn, institution_id):
        # Lock the institution's staff rows so two owners cannot remove each other at once.
        rows = conn.execute("SELECT id, name, role, active FROM staff WHERE institution_id = %s "
                            "ORDER BY id FOR UPDATE", (institution_id,)).fetchall()
        target = next((r for r in rows if r["id"] == staff_id), None)
        if target is None:
            raise NotFound(f"staff member {staff_id} not found")
        owners = [r for r in rows if r["role"] == "owner" and r["active"]]
        if target["active"] and target["role"] == "owner" and len(owners) == 1:
            raise Conflict("the last active owner cannot be deactivated")
        conn.execute("UPDATE staff SET active = false WHERE id = %s", (staff_id,))
        audit.record(conn, institution_id, "staff.deactivated", "staff", staff_id)
    return {**target, "active": False}


def settings(conn, institution_id: int) -> dict:
    row = conn.execute("SELECT id, name, late_fee, grace_days, quiet_start, quiet_end, approval_threshold "
                       "FROM institutions WHERE id = %s", (institution_id,)).fetchone()
    if row is None:
        raise NotFound(f"institution {institution_id} not found")
    return row


def all_institution_ids(conn) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM institutions ORDER BY id").fetchall()]
