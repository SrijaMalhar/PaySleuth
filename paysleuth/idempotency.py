"""Client-supplied retry keys, for the one thing the domain keys cannot do.

The seven idempotency keys elsewhere are the domain's, and they are the better
mechanism: a repeated credit is a no-op because the bank's UTR is unique, not
because anybody remembered to send a header. But they only cover operations
that have a natural identity. `POST /members` does not: two members called
Diya Sharma are two members, so the server cannot tell a retry from a second
student.

A client that times out cannot tell a lost request from a lost response. This
gives it a way to say "this is the same request I sent before", and get the
first reply back rather than a second student.

The protocol is the usual one:

    reserve -> we own the key, run the handler, record the reply
            -> somebody else owns it and finished   -> replay their reply
            -> somebody else owns it, still running -> 409, try again shortly
            -> same key, different request          -> 422, that is a bug

A reservation whose request never finished is taken over after
TAKEOVER_AFTER, on the same reasoning as the outbox's visibility timeout: a
process that died holding a claim must not wedge the key forever.

The cost of that takeover, stated rather than hidden: the work can run twice.
The handler's own transaction commits before this key record does, so a crash
in between leaves work done and unrecorded, and the retry redoes it. The
transport key cannot close that window -- only a domain key can, by making the
second attempt a no-op at the database. Which is the actual argument for the
seven of those: this header is a convenience for clients, and the unique
constraints are the safety. Where both exist, as on POST /bank-credits, a
takeover is harmless.
"""

import hashlib
import json
from datetime import timedelta

from .db import tenant_tx
from .errors import Conflict, PreconditionFailed

TAKEOVER_AFTER = timedelta(seconds=60)
KEEP_FOR = timedelta(days=1)
MAX_KEY_LENGTH = 255


class Replay(Exception):
    """A completed request under this key: send its stored reply again."""

    def __init__(self, status_code: int, body):
        super().__init__("replayed")
        self.status_code, self.body = status_code, body


def fingerprint(method: str, path: str, body: bytes) -> str:
    """What makes two requests 'the same request'.

    The body is hashed rather than stored: a request body can hold a phone
    number, and this table is not where those should accumulate.
    """
    digest = hashlib.sha256()
    digest.update(method.encode())
    digest.update(b"\0")
    digest.update(path.encode())
    digest.update(b"\0")
    digest.update(body or b"")
    return digest.hexdigest()


def reserve(conn, inst: int, key: str, method: str, path: str, body: bytes) -> None:
    """Claim the key, or raise: Replay to repeat a reply, Conflict if in flight.

    Raises PreconditionFailed when the key was used for a different request,
    which is a client bug rather than a race, and silently letting it through
    would mean a retry key that does not identify a retry.
    """
    if len(key) > MAX_KEY_LENGTH:
        raise PreconditionFailed(f"Idempotency-Key must be at most {MAX_KEY_LENGTH} characters")
    mine = fingerprint(method, path, body)
    with tenant_tx(conn, inst):
        row = conn.execute(
            """INSERT INTO idempotency_keys (institution_id, key, fingerprint, method, path)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (institution_id, key) DO UPDATE
                   -- Take over a reservation nobody finished in time. DO UPDATE
                   -- rather than DO NOTHING so this is one statement and one
                   -- row lock: two retries racing cannot both take it over.
                   SET reserved_at = now(), fingerprint = EXCLUDED.fingerprint
                 WHERE idempotency_keys.finished_at IS NULL
                   AND idempotency_keys.reserved_at < now() - %s
                   AND idempotency_keys.fingerprint = EXCLUDED.fingerprint
               RETURNING finished_at""",
            (inst, key, mine, method, path, TAKEOVER_AFTER)).fetchone()
        if row is not None:
            return                                  # inserted, or taken over: it is ours

        existing = conn.execute(
            "SELECT fingerprint, status_code, response, finished_at FROM idempotency_keys "
            "WHERE key = %s", (key,)).fetchone()

    if existing is None:                            # vanished between the two statements
        raise Conflict("that Idempotency-Key is in use; retry shortly")
    if existing["fingerprint"] != mine:
        raise PreconditionFailed(
            "that Idempotency-Key was used for a different request")
    if existing["finished_at"] is None:
        raise Conflict("a request with that Idempotency-Key is still running; retry shortly")
    raise Replay(existing["status_code"], existing["response"])


def finish(conn, inst: int, key: str, status_code: int, body) -> None:
    """Record the reply, so the next retry replays it instead of acting again."""
    with tenant_tx(conn, inst):
        conn.execute(
            "UPDATE idempotency_keys SET status_code = %s, response = %s, finished_at = now() "
            "WHERE key = %s AND finished_at IS NULL",
            (status_code, json.dumps(body) if body is not None else None, key))


def release(conn, inst: int, key: str) -> None:
    """Drop an unfinished reservation, so a failed request can be retried at once.

    Only unfinished ones: a handler that raised after its own transaction
    committed is a different problem, and silently dropping a completed key
    would let the retry act twice.
    """
    with tenant_tx(conn, inst):
        conn.execute("DELETE FROM idempotency_keys WHERE key = %s AND finished_at IS NULL", (key,))


def forget_old(conn, inst: int, keep_for: timedelta = KEEP_FOR) -> int:
    """Drop keys older than the window a client could plausibly still retry in."""
    with tenant_tx(conn, inst):
        return conn.execute("DELETE FROM idempotency_keys WHERE reserved_at < now() - %s",
                            (keep_for,)).rowcount
