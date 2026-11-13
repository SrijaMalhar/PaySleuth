"""One small adapter per bank.

A bank's CSV export differs from another's in three ways: the column names
(handled by the alias list in statements.py), lines before and after the
table (handled by searching for the header row), and how the narration is
laid out. An adapter covers the third: it recognises its bank from the header
and turns that bank's narration into channel, UTR, payer name, payer UPI ID
and note.

Honest status: these layouts are modelled on each bank's published export
format and on narrations as they commonly appear. The files in tests/golden
are synthetic. Each adapter should be re-checked against a real, anonymised
export from that bank, and its golden file replaced, before it is trusted.
"""

from dataclasses import dataclass
from typing import Callable

from . import axis, canara, hdfc, icici, sbi


@dataclass(frozen=True)
class Bank:
    name: str
    header: frozenset                       # normalised column names that identify this bank's export
    narration: Callable[[str], dict | None]


BANKS = [Bank(m.NAME, frozenset(m.HEADER), m.narration) for m in (sbi, hdfc, icici, axis, canara)]


def detect(header_keys) -> Bank | None:
    """The bank whose identifying columns are all present, or None for an unknown layout."""
    keys = set(header_keys)
    for bank in BANKS:
        if bank.header <= keys:
            return bank
    return None
