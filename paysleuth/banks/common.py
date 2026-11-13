"""Helpers shared by the bank adapters."""

import re

_FULL_UPI_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,255}@[a-z][a-z0-9.]{1,63}$")


def clean(value: str | None) -> str | None:
    value = " ".join((value or "").split())
    return value or None


def full_upi_id(value: str | None) -> str | None:
    """The UPI ID if the bank printed all of it; None if it was cut short or masked.

    A truncated or masked ID ("sunita.sha", "**sharma@oksbi") must not be
    stored as if it were the payer's ID: it would never match, or worse, match
    someone else.
    """
    text = (value or "").strip().lower()
    return text if _FULL_UPI_ID.match(text) else None


def fields(channel: str, utr: str, name=None, upi=None, note=None) -> dict:
    return {"channel": channel, "utr": utr, "payer_name": clean(name),
            "payer_upi_id": full_upi_id(upi), "note": clean(note)}
