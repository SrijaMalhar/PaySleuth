"""Where messages go: a JSON-lines file for demos and tests, or an HTTP webhook to an SMS/WhatsApp gateway."""

import json
import os
import threading
import urllib.request


def file_sender(path: str):
    """Append one JSON line per message. The lock keeps parallel sends from interleaving."""
    lock = threading.Lock()

    def send(payload: dict) -> None:
        line = json.dumps(payload, sort_keys=True)
        with lock, open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    return send


def webhook_sender(url: str, *, timeout: float = 10.0):
    """POST each message as JSON. urlopen raises on 4xx, 5xx and timeouts, which the relay retries."""

    def send(payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "X-Message-Id": payload["message_id"]})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()

    return send


def sender_from_env():
    url = os.environ.get("PAYSLEUTH_MESSAGE_WEBHOOK")
    if url:
        return webhook_sender(url)
    return file_sender(os.environ.get("PAYSLEUTH_MESSAGE_FILE", "messages_sent.jsonl"))
