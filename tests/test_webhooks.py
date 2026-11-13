"""Signed webhooks: the gateway can tell a PaySleuth message from anyone else's, and refuse an old one."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from paysleuth.notify import webhook_sender
from paysleuth.webhooks import BadSignature, signature_header, verify

SECRET = "whsec_1dd3f8b1c0a74e9f8b2d6a5c4e3f2a19"
BODY = b'{"amount":"2000.00","kind":"payment_receipt","message_id":"m-1"}'
NOW = 1781683200          # a fixed moment, so these tests do not depend on the clock


def test_a_signed_message_verifies():
    header = signature_header(SECRET, BODY, NOW)
    assert header.startswith(f"t={NOW},v1=") and len(header.split("v1=")[1]) == 64
    assert verify(SECRET, BODY, header, now=NOW) == NOW
    assert verify(SECRET, BODY, header, now=NOW + 299) == NOW      # inside the window


def test_a_message_signed_with_another_secret_is_refused():
    header = signature_header("whsec_someone_else", BODY, NOW)
    with pytest.raises(BadSignature, match="does not match"):
        verify(SECRET, BODY, header, now=NOW)


def test_a_changed_body_is_refused():
    header = signature_header(SECRET, BODY, NOW)
    tampered = BODY.replace(b'"2000.00"', b'"9000.00"')
    with pytest.raises(BadSignature, match="does not match"):
        verify(SECRET, tampered, header, now=NOW)
    # Even reordering the bytes breaks it: the signature covers the body exactly as sent.
    with pytest.raises(BadSignature):
        verify(SECRET, b" " + BODY, header, now=NOW)


def test_an_old_message_sent_again_is_refused():
    header = signature_header(SECRET, BODY, NOW)
    with pytest.raises(BadSignature, match="outside the 300s window"):
        verify(SECRET, BODY, header, now=NOW + 301)
    with pytest.raises(BadSignature):                              # and a clock far ahead
        verify(SECRET, BODY, header, now=NOW - 301)
    assert verify(SECRET, BODY, header, now=NOW + 900, tolerance=1200) == NOW   # a gateway may allow more


def test_a_captured_signature_cannot_be_reused_with_a_fresh_timestamp():
    captured = signature_header(SECRET, BODY, NOW)
    digest = captured.split("v1=")[1]
    forged = f"t={NOW + 600},v1={digest}"                          # same digest, newer timestamp
    with pytest.raises(BadSignature, match="does not match"):
        verify(SECRET, BODY, forged, now=NOW + 600)


@pytest.mark.parametrize("header", [
    None, "", "t=1781683200", f"v1={'a' * 64}", "t=abc,v1=" + "a" * 64,
    f"t={NOW},v1=" + "a" * 63, f"t={NOW},v2=" + "a" * 64, f"t={NOW},v1=" + "A" * 64,
])
def test_a_malformed_header_is_refused_not_crashed(header):
    with pytest.raises(BadSignature):
        verify(SECRET, BODY, header, now=NOW)


def test_surrounding_whitespace_in_the_header_is_tolerated():
    header = signature_header(SECRET, BODY, NOW)
    assert verify(SECRET, BODY, f"  {header}\n", now=NOW) == NOW


def test_signing_needs_a_secret():
    with pytest.raises(ValueError):
        signature_header("", BODY, NOW)


@pytest.fixture
def gateway():
    """A gateway that verifies the signature and returns 401 if it does not check out."""
    received, rejected = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            try:
                verify(SECRET, body, self.headers.get("X-PaySleuth-Signature"))
            except BadSignature as error:
                rejected.append(str(error))
                self.send_response(401)
                self.end_headers()
                return
            received.append({"id": self.headers["X-Message-Id"], "body": json.loads(body)})
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/messages", received, rejected
    server.shutdown()


def test_the_sender_and_a_real_gateway_agree(gateway):
    url, received, rejected = gateway
    webhook_sender(url, secret=SECRET)({"message_id": "m-1", "kind": "payment_receipt",
                                        "amount": "2000.00", "member_name": "Diya Sharma"})
    assert received[0]["id"] == "m-1" and received[0]["body"]["member_name"] == "Diya Sharma"
    assert rejected == []


def test_the_gateway_turns_away_an_unsigned_or_wrongly_signed_message(gateway):
    url, received, rejected = gateway
    with pytest.raises(Exception):                                 # 401, which the relay will retry
        webhook_sender(url)({"message_id": "m-2", "kind": "payment_receipt"})
    with pytest.raises(Exception):
        webhook_sender(url, secret="whsec_wrong")({"message_id": "m-3", "kind": "payment_receipt"})
    assert received == []
    assert rejected == ["missing or malformed X-PaySleuth-Signature header", "signature does not match"]


def test_the_same_body_signed_twice_in_the_same_second_gives_the_same_signature():
    """Signing is a pure function of (secret, timestamp, body): no nonce, no state.

    This is what lets the gateway verify by recomputing, and it is why replay
    protection has to come from the timestamp and message_id rather than from
    the signature being unique.
    """
    assert signature_header(SECRET, BODY, NOW) == signature_header(SECRET, BODY, NOW)
    assert signature_header(SECRET, BODY, NOW) != signature_header(SECRET, BODY, NOW + 1)
    assert signature_header(SECRET, BODY, NOW) != signature_header(SECRET + "x", BODY, NOW)
