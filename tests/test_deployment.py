"""Deployment-shaped concerns that only show up on a hosted database.

The free hosted Postgres services put a transaction-mode pooler in front of
the database. Nothing in the design minds that -- row-level security is set
with SET LOCAL and the migration lock is transaction-scoped, so both stay on
one backend -- but server-side prepared statements do, and the failure appears
only after a query has been executed enough times to be promoted. So the
detection is a test, not a comment.
"""

import pytest

from conftest import DSN
from paysleuth.db import make_pool, pooler_in_front

POOLED = [
    "postgresql://u:p@ep-cool-dawn-123456-pooler.ap-southeast-1.aws.neon.tech/paysleuth",
    "postgresql://u:p@db.abcdefgh.supabase.co:5432/postgres?pgbouncer=true",
]
DIRECT = [
    "postgresql://u:p@ep-cool-dawn-123456.ap-southeast-1.aws.neon.tech/paysleuth",
    "postgresql://paysleuth:paysleuth@localhost:5432/paysleuth",
    "postgresql://u:p@dpg-abcdefg-a.singapore-postgres.render.com/paysleuth",
]


@pytest.mark.parametrize("dsn", POOLED)
def test_a_pooled_endpoint_is_recognised(dsn):
    assert pooler_in_front(dsn)


@pytest.mark.parametrize("dsn", DIRECT)
def test_a_direct_endpoint_is_not_mistaken_for_a_pooled_one(dsn):
    assert not pooler_in_front(dsn)


def test_prepared_statements_are_off_behind_a_pooler_and_on_without_one():
    """The whole point of the detection: what it does to the connection."""
    with make_pool(DSN, max_size=1) as direct, direct.connection() as conn:
        assert conn.prepare_threshold is not None

    with make_pool(DSN, max_size=1, prepare=False) as pooled, pooled.connection() as conn:
        assert conn.prepare_threshold is None


def test_a_repeated_query_still_answers_with_preparation_disabled():
    """Past psycopg's promotion threshold, which is where the pooler error lands."""
    with make_pool(DSN, max_size=1, prepare=False) as pool, pool.connection() as conn:
        for n in range(12):
            assert conn.execute("SELECT %s::int AS n", (n,)).fetchone()["n"] == n
