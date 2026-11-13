"""Connections, migrations, tenant transactions and the member lock."""

from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.numeric import NumericLoader
from psycopg_pool import ConnectionPool

from .errors import NotFound

SCHEMA = Path(__file__).with_name("schema.sql").read_text()

# Any constant works, as long as every process that migrates uses the same one.
MIGRATION_LOCK = 0x5041_5953  # "PAYS"


class PaiseLoader(NumericLoader):
    """PostgreSQL widens SUM(bigint) to numeric. Turn it back into an exact int.

    A fraction of a paisa would mean a bug somewhere, so it raises instead of
    rounding quietly.
    """

    def load(self, data) -> int:
        value: Decimal = super().load(data)
        if value != value.to_integral_value():
            raise ValueError(f"expected whole paise from the database, got {value}")
        return int(value)


def _configure(conn) -> None:
    conn.adapters.register_loader("numeric", PaiseLoader)


def make_pool(dsn: str, *, min_size: int = 1, max_size: int = 10,
              options: str | None = None) -> ConnectionPool:
    """A pool of autocommit connections that return rows as dicts.

    Autocommit because every transaction in PaySleuth is opened explicitly;
    no connection can sit idle inside a forgotten transaction holding locks.
    """
    kwargs = {"row_factory": dict_row, "autocommit": True}
    if options:
        kwargs["options"] = options
    return ConnectionPool(dsn, min_size=min_size, max_size=max_size, kwargs=kwargs,
                          configure=_configure, open=True)


def check_role(conn) -> None:
    """Refuse to run as a role that row-level security does not apply to."""
    row = conn.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
                       ).fetchone()
    if row["rolsuper"] or row["rolbypassrls"]:
        raise RuntimeError(
            "the database role bypasses row-level security; connect as an ordinary role "
            "(see docker/initdb.sql) so tenants stay isolated")


def apply_schema(conn) -> None:
    # The API and the workers migrate at startup, possibly at the same instant,
    # and two concurrent CREATE POLICY statements fail. The advisory lock makes
    # them take turns and is released automatically at commit.
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        conn.execute(SCHEMA)


def migrate(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        check_role(conn)
        apply_schema(conn)


@contextmanager
def tenant_tx(conn, institution_id: int):
    """A transaction that can only see and write one institution's rows.

    set_config(..., true) makes the setting local to this transaction, so it
    can never leak to the next request that borrows this pooled connection.
    A nested call becomes a savepoint.
    """
    with conn.transaction():
        conn.execute("SELECT set_config('app.institution_id', %s, true)", (str(institution_id),))
        yield conn


def lock_members(conn, member_ids) -> list[int]:
    """Lock these members' rows until the transaction ends, always in ascending id order.

    If every transaction locks lower ids first, none can hold B while waiting
    for A, so a deadlock cycle cannot form. A member of another institution is
    invisible under row-level security, so it shows up here as missing.
    """
    wanted = sorted(set(member_ids))
    if not wanted:
        return []
    rows = conn.execute("SELECT id FROM members WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                        (wanted,)).fetchall()
    if len(rows) != len(wanted):
        missing = sorted(set(wanted) - {r["id"] for r in rows})
        raise NotFound(f"member(s) {missing} not found")
    return wanted
