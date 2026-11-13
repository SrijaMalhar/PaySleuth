"""Signing outgoing messages, so the gateway knows they came from PaySleuth.

A message tells an SMS or WhatsApp gateway to text a parent a receipt with
their name and what they owe. Anyone who can reach the gateway's URL could
post one, so each message carries a signature:

    X-PaySleuth-Signature: t=1781683200,v1=<hex HMAC-SHA256>

The signed string is "<t>.<exact bytes of the body>", not the body alone, so
a captured message cannot be replayed later with a fresh timestamp: changing
t changes what was signed. The gateway checks the signature and refuses a
message whose t is outside its tolerance.

The timestamp stops an old message from being accepted at all; message_id in
the body is what stops an accepted message from being acted on twice, since
delivery is at-least-once.

Both sides compare with hmac.compare_digest, which takes the same time
whether the first byte differs or only the last, so no timing difference
leaks how much of a guessed signature was right. That one is not in the
mutation check: swapping it for == gives the same answer to every input, so
no behaviour test can tell the difference. It is a review rule, not a
tested one.
"""

import hashlib
import hmac
import re
import time

ALGORITHM = "v1"                 # the scheme's name, so a future one can be added without ambiguity
DEFAULT_TOLERANCE = 300          # seconds a message stays acceptable

_HEADER = re.compile(r"^t=(?P<t>\d{1,20}),v1=(?P<v1>[0-9a-f]{64})$")


class BadSignature(Exception):
    """The message was not signed by the holder of the secret, or is too old."""


def signed_string(timestamp: int, body: bytes) -> bytes:
    return str(int(timestamp)).encode() + b"." + body


def sign(secret: str, body: bytes, timestamp: int | None = None) -> tuple[int, str]:
    """Return (timestamp, hex digest) for a request body."""
    if not secret:
        raise ValueError("a signing secret is required")
    moment = int(time.time() if timestamp is None else timestamp)
    digest = hmac.new(secret.encode(), signed_string(moment, body), hashlib.sha256).hexdigest()
    return moment, digest


def signature_header(secret: str, body: bytes, timestamp: int | None = None) -> str:
    moment, digest = sign(secret, body, timestamp)
    return f"t={moment},{ALGORITHM}={digest}"


def verify(secret: str, body: bytes, header: str | None, *, now: float | None = None,
           tolerance: int = DEFAULT_TOLERANCE) -> int:
    """Check a received message. Returns its timestamp, or raises BadSignature.

    This is the gateway's side. It is here so the check is tested against the
    signer in the same test run, and can be copied into a gateway as it is.
    """
    match = _HEADER.match((header or "").strip())
    if not match:
        raise BadSignature("missing or malformed X-PaySleuth-Signature header")
    timestamp = int(match["t"])
    moment = time.time() if now is None else now
    # Both directions: an old capture replayed, and a clock far enough ahead to
    # keep a message valid beyond the window.
    if abs(moment - timestamp) > tolerance:
        raise BadSignature(f"message is {abs(int(moment - timestamp))}s away from now, "
                           f"outside the {tolerance}s window")
    _, expected = sign(secret, body, timestamp)
    if not hmac.compare_digest(expected, match["v1"]):
        raise BadSignature("signature does not match")
    return timestamp
