"""Canara Bank.

    Txn Date, Value Date, Cheque No., Description, Branch Code, Debit, Credit, Balance
    UPI/CR/615412345601/SUNITA SHARMA/SBIN/**sharma@oksbi/June fees//CNRB0001234
    NEFT CR-N156260012345-ANIL NAIR-PS1004 june fee

The UPI ID is partly masked, so it is not stored; the payer's name and the
note are kept.
"""

import re

from .common import fields

NAME = "Canara"
HEADER = {"txndate", "description", "branchcode"}

_UPI = re.compile(r"^UPI/CR/(?P<utr>\d{12})/(?P<name>[^/]*)/(?P<bank>[^/]*)/(?P<upi>[^/]*)"
                  r"(?:/(?P<note>[^/]*))?(?://.*)?$")
_NEFT = re.compile(r"^(?P<channel>NEFT|IMPS|RTGS) CR-(?P<utr>[A-Z0-9]{8,22})-(?P<name>[^-]*)(?:-(?P<note>.*))?$")


def narration(text: str) -> dict | None:
    m = _UPI.match(text)
    if m:
        return fields("UPI", m["utr"], m["name"], m["upi"], m["note"])
    m = _NEFT.match(text)
    if m:
        return fields(m["channel"], m["utr"], m["name"], None, m["note"])
    return None
