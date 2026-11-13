"""Where messages go: a JSON-lines file for demos and tests, or a signed HTTP webhook to an SMS/WhatsApp gateway."""

import json
import os
import threading
import urllib.request

from .webhooks import signature_header


def file_sender(path: str):
    """Append one JSON line per message. The lock keeps parallel sends from interleaving."""
    lock = threading.Lock()

    def send(payload: dict) -> None:
        line = json.dumps(payload, sort_keys=True)
        with lock, open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    return send


def webhook_sender(url: str, *, secret: str | None = None, timeout: float = 10.0):
    """POST each message as JSON, signed if a secret is set.

    The signature covers the exact bytes that are sent, so the body is built
    once and both signed and posted. urlopen raises on 4xx, 5xx and timeouts,
    which the relay retries with backoff.
    """

    def send(payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        headers = {"Content-Type": "application/json", "X-Message-Id": payload["message_id"]}
        if secret:
            headers["X-PaySleuth-Signature"] = signature_header(secret, body)
        request = urllib.request.Request(url, data=body, method="POST", headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()

    return send


def sender_from_env():
    url = os.environ.get("PAYSLEUTH_MESSAGE_WEBHOOK")
    if url:
        return webhook_sender(url, secret=os.environ.get("PAYSLEUTH_WEBHOOK_SECRET"))
    return file_sender(os.environ.get("PAYSLEUTH_MESSAGE_FILE", "messages_sent.jsonl"))
