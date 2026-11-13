"""Shared fixtures. Every test gets its own empty schema in a real PostgreSQL.

Locks, constraints and row-level security are the point of this project, so
nothing here is mocked.
"""

import os
import uuid
from contextlib import contextmanager

import psycopg
import pytest

from paysleuth.db import make_pool, migrate
from paysleuth.institutions import create_institution

DSN = os.environ.get("PAYSLEUTH_TEST_DATABASE_URL",
                     "postgresql://paysleuth:paysleuth@localhost:5432/paysleuth_test")
SUPERUSER_DSN = os.environ.get("PAYSLEUTH_TEST_SUPERUSER_URL")


@contextmanager
def fresh_pool(max_size=30):
    """A pool whose connections all use a brand-new schema, dropped afterwards."""
    schema = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")
    options = f"-c search_path={schema}"
    pool = make_pool(DSN, max_size=max_size, options=options)
    pool.schema = schema
    pool.options = options
    try:
        migrate(pool)
        yield pool
    finally:
        pool.close()
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def pool():
    with fresh_pool() as p:
        yield p


@pytest.fixture
def conn(pool):
    with pool.connection() as c:
        yield c


@pytest.fixture
def inst(conn):
    institution_id, _ = create_institution(conn, "Bright Minds Tuition", late_fee=10000, grace_days=3)
    return institution_id
