"""HDFC Bank.

    Date, Narration, Chq./Ref.No., Value Dt, Withdrawal Amt., Deposit Amt., Closing Balance
    UPI-SUNITA SHARMA-SUNITA.SHARMA@OKSBI-SBIN0001234-615412345601-JUNE FEES
    NEFT CR-SBIN0001234-ANIL NAIR-BRIGHT MINDS TUITION-N156260012345

The UPI line carries the full UPI ID and is anchored on the IFSC and the
12-digit UTR, so a hyphen inside the name or the ID cannot shift the fields.
"""

import re

from .common import fields

NAME = "HDFC"
HEADER = {"narration", "chqrefno", "valuedt", "closingbalance"}

_UPI = re.compile(r"^UPI-(?P<name>.*?)-(?P<upi>[^\s]+?@[A-Za-z0-9.]+)-(?P<ifsc>[A-Z]{4}0[A-Z0-9]{6})-"
                  r"(?P<utr>\d{12})(?:-(?P<note>.*))?$")
_NEFT = re.compile(r"^(?P<channel>NEFT|RTGS) CR-(?P<ifsc>[A-Z]{4}0[A-Z0-9]{6})-(?P<name>[^-]*)-.*-"
                   r"(?P<utr>[A-Z0-9]{8,22})$")
_IMPS = re.compile(r"^IMPS-(?P<utr>\d{12})-(?P<name>[^-]*)-(?P<bank>[^-]*)-(?P<account>[^-]*)(?:-(?P<note>.*))?$")


def narration(text: str) -> dict | None:
    m = _UPI.match(text)
    if m:
        return fields("UPI", m["utr"], m["name"], m["upi"], m["note"])
    m = _NEFT.match(text)
    if m:
        return fields(m["channel"], m["utr"], m["name"])
    m = _IMPS.match(text)
    if m:
        return fields("IMPS", m["utr"], m["name"], None, m["note"])
    return None
