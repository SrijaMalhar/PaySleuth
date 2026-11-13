import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege

from conftest import SUPERUSER_DSN
from paysleuth.db import check_role, tenant_tx
from paysleuth.institutions import create_institution, institution_for_key


def _raw_member(conn, institution_id, ref_code="PS9001"):
    with tenant_tx(conn, institution_id):
        return conn.execute(
            "INSERT INTO members (institution_id, ref_code, name, billing_day, start_period) "
            "VALUES (%s, %s, 'Diya', 5, '2026-06') RETURNING id", (institution_id, ref_code)
        ).fetchone()["id"]


# --- tenancy ------------------------------------------------------------------------

def test_another_institution_cannot_see_members_even_without_a_where_clause(conn, inst):
    _raw_member(conn, inst)
    other, _ = create_institution(conn, "Other Centre")
    with tenant_tx(conn, inst):
        assert conn.execute("SELECT COUNT(*) AS n FROM members").fetchone()["n"] == 1
    with tenant_tx(conn, other):
        assert conn.execute("SELECT COUNT(*) AS n FROM members").fetchone()["n"] == 0


def test_with_no_institution_set_nothing_is_visible(conn, inst):
    _raw_member(conn, inst)
    assert conn.execute("SELECT COUNT(*) AS n FROM members").fetchone()["n"] == 0
    with conn.transaction():          # a transaction that never set the tenant
        assert conn.execute("SELECT COUNT(*) AS n FROM members").fetchone()["n"] == 0


def test_cannot_write_rows_for_another_institution(conn, inst):
    other, _ = create_institution(conn, "Other Centre")
    with pytest.raises(InsufficientPrivilege):
        with tenant_tx(conn, other):
            conn.execute("INSERT INTO audit_log (institution_id, action, entity) "
                         "VALUES (%s, 'x', 'y')", (inst,))


def test_api_keys_are_stored_only_as_hashes(conn):
    institution_id, key = create_institution(conn, "Bright Minds Tuition")
    assert key.startswith("ps_")
    assert institution_for_key(conn, key) == institution_id
    assert institution_for_key(conn, key + "x") is None
    assert institution_for_key(conn, None) is None
    stored = conn.execute("SELECT api_key_hash FROM institutions WHERE id = %s",
                          (institution_id,)).fetchone()["api_key_hash"]
    assert len(stored) == 64 and all(ch in "0123456789abcdef" for ch in stored)
    assert key not in stored


@pytest.mark.skipif(not SUPERUSER_DSN, reason="set PAYSLEUTH_TEST_SUPERUSER_URL to run")
def test_refuses_to_run_as_a_role_that_bypasses_row_level_security():
    with psycopg.connect(SUPERUSER_DSN, row_factory=psycopg.rows.dict_row) as su:
        with pytest.raises(RuntimeError, match="bypasses row-level security"):
            check_role(su)


def test_the_app_role_is_accepted(conn):
    check_role(conn)
