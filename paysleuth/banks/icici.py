"""ICICI Bank.

    S No., Value Date, Transaction Date, Cheque Number, Transaction Remarks,
    Withdrawal Amount (INR ), Deposit Amount (INR ), Balance (INR )
    UPI/615412345601/June fees/sunita.sharma@oksbi/State Bank Of India/ICI0a1b2c3d
    NEFT-N156260012345-ANIL NAIR-PS1004 june fee

The UPI line gives the note and the full UPI ID but not the payer's name.
"""

import re

from .common import fields

NAME = "ICICI"
HEADER = {"transactionremarks", "depositamountinr", "withdrawalamountinr"}

_UPI = re.compile(r"^UPI/(?P<utr>\d{12})/(?P<note>[^/]*)/(?P<upi>[^/\s]+@[A-Za-z0-9.]+)/(?P<bank>[^/]*)(?:/.*)?$")
_NEFT = re.compile(r"^(?P<channel>NEFT|IMPS|RTGS)-(?P<utr>[A-Z0-9]{8,22})-(?P<name>[^-]*)(?:-(?P<note>.*))?$")


def narration(text: str) -> dict | None:
    m = _UPI.match(text)
    if m:
        return fields("UPI", m["utr"], None, m["upi"], m["note"])
    m = _NEFT.match(text)
    if m:
        return fields(m["channel"], m["utr"], m["name"], None, m["note"])
    return None
