"""The entropy the hashing decision rests on.

_hash is SHA-256 rather than a slow KDF, and that is correct only because an
API key is 256 bits of CSPRNG output rather than something a person chose. The
reasoning lives in the docstring; this is what stops the reasoning from
quietly becoming false.
"""

import hashlib
import math
import re

import pytest

from paysleuth.db import tenant_tx
from paysleuth.institutions import KEY_BYTES, KEY_PREFIX, actor_for_key, add_staff


def test_a_key_carries_enough_entropy_that_a_fast_hash_is_the_right_one(conn, inst):
    """If this ever drops, _hash has to change with it."""
    assert KEY_BYTES >= 32, "under 256 bits a work factor starts to matter"

    key = add_staff(conn, inst, "Asha", "owner")["api_key"]
    body = key.removeprefix(KEY_PREFIX)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", body)
    # token_urlsafe packs 6 bits per character, so the length is the proof.
    assert len(body) >= math.ceil(KEY_BYTES * 8 / 6)


def test_keys_are_not_guessable_from_each_other(conn, inst):
    keys = {add_staff(conn, inst, f"Clerk {n}", "clerk")["api_key"] for n in range(25)}
    assert len(keys) == 25
    bodies = [k.removeprefix(KEY_PREFIX) for k in keys]
    assert len({b[:8] for b in bodies}) == 25, "the first characters repeat"


def test_only_the_digest_is_stored(conn, inst):
    key = add_staff(conn, inst, "Bina", "clerk")["api_key"]
    with tenant_tx(conn, inst):
        rows = conn.execute("SELECT api_key_hash FROM staff").fetchall()
    stored = {r["api_key_hash"] for r in rows}
    assert key not in stored
    assert hashlib.sha256(key.encode()).hexdigest() in stored
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in stored)


def test_a_key_that_was_never_issued_identifies_nobody(conn, inst):
    add_staff(conn, inst, "Asha", "owner")
    assert actor_for_key(conn, KEY_PREFIX + "a" * 43) is None


@pytest.mark.parametrize("key", [None, "", "ps_", "not-a-key", " "])
def test_a_malformed_key_identifies_nobody(conn, inst, key):
    assert actor_for_key(conn, key) is None


def test_a_deactivated_key_stops_working(conn, inst):
    from paysleuth.institutions import add_staff as add, deactivate_staff
    owner = add(conn, inst, "Asha", "owner")
    clerk = add(conn, inst, "Bina", "clerk")
    assert actor_for_key(conn, clerk["api_key"]) is not None

    deactivate_staff(conn, inst, clerk["id"])
    assert actor_for_key(conn, clerk["api_key"]) is None
    assert actor_for_key(conn, owner["api_key"]) is not None, "the wrong key was turned off"


def test_the_lookup_is_by_digest_so_the_key_is_never_compared_in_python(conn, inst):
    """Timing: the comparison happens inside the index on a hash, not on the secret.

    A row is found by its digest, so there is no byte-by-byte comparison of the
    key itself for a caller to time.
    """
    import inspect

    source = inspect.getsource(actor_for_key)
    assert "api_key_hash = %s" in source
    assert "_hash(key)" in source
