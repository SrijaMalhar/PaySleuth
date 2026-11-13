"""Money is integer paise from the edge of the system to the database.

Rupee strings such as "2,000.50" exist only where a person types or reads an
amount. Everything in between, including every column in PostgreSQL, is a
bigint number of paise, so no amount ever passes through a float.
"""

import re
from decimal import Decimal

_RUPEES = re.compile(r"^\d{1,10}(\.\d{1,2})?$")


def check_text(value, what: str) -> str:
    """Text on its way to the database. A NUL byte cannot be stored in a text column,
    so it is the caller's bad input, not a server error waiting to happen."""
    text = "" if value is None else str(value)
    if "\x00" in text:
        raise ValueError(f"{what} cannot contain a NUL byte")
    return text


def to_paise(rupees) -> int:
    """Parse a positive rupee amount into paise: "2,000.50" -> 200050.

    Three decimals are refused rather than rounded, because a fraction of a
    paisa does not exist: "12.345" is bad input, not an amount.
    """
    text = str(rupees).replace(",", "").strip()
    if not _RUPEES.match(text):
        raise ValueError(f"amount must be rupees with at most 2 decimals (got {rupees!r})")
    paise = int(Decimal(text) * 100)
    if paise <= 0:
        raise ValueError("amount must be greater than zero")
    return paise


def to_rupees(paise: int) -> str:
    """Format paise as a rupee string: 200050 -> "2000.50", -5 -> "-0.05"."""
    sign = "-" if paise < 0 else ""
    rupees, rest = divmod(abs(paise), 100)
    return f"{sign}{rupees}.{rest:02d}"
