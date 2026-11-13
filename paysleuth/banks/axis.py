"""Axis Bank.

    Tran Date, CHQNO, PARTICULARS, DR, CR, BAL, SOL
    UPI/P2A/615412345601/SUNITA SHARMA/State Bank Of India/June fees
    NEFT/N156260012345/ANIL NAIR/STATE BANK OF INDIA/PS1004 june fee

The UPI line gives the payer's name and the note but no UPI ID, so matching
relies on a reference code in the note, or on review with name suggestions.
"""

import re

from .common import fields

NAME = "Axis"
HEADER = {"trandate", "particulars", "sol"}

_UPI = re.compile(r"^UPI/P2[AM]/(?P<utr>\d{12})/(?P<name>[^/]*)/(?P<bank>[^/]*)(?:/(?P<note>.*))?$")
_NEFT = re.compile(r"^(?P<channel>NEFT|IMPS|RTGS)/(?P<utr>[A-Z0-9]{8,22})/(?P<name>[^/]*)(?:/(?P<bank>[^/]*))?"
                   r"(?:/(?P<note>.*))?$")


def narration(text: str) -> dict | None:
    m = _UPI.match(text)
    if m:
        return fields("UPI", m["utr"], m["name"], None, m["note"])
    m = _NEFT.match(text)
    if m:
        return fields(m["channel"], m["utr"], m["name"], None, m["note"])
    return None
