"""State Bank of India.

    Txn Date, Value Date, Description, Ref No./Cheque No., Debit, Credit, Balance
    BY TRANSFER-UPI/CR/615412345601/SUNITA S/SBIN/sunita.sha/June fees--
    BY TRANSFER-NEFT*HDFC0001234*N156260012345*ANIL NAIR--

SBI cuts the payer's name and UPI ID short, so the UPI ID is usually unusable
and matching relies on a reference code in the note, or on review.
"""

import re

from .common import fields

NAME = "SBI"
HEADER = {"txndate", "valuedate", "description", "refnochequeno"}

_UPI = re.compile(r"^BY TRANSFER-UPI/CR/(?P<utr>\d{12})/(?P<name>[^/]*)/(?P<bank>[^/]*)/(?P<upi>[^/]*)"
                  r"(?:/(?P<note>.*?))?-*$")
_NEFT = re.compile(r"^BY TRANSFER-(?P<channel>NEFT|IMPS|RTGS)\*(?P<ifsc>[A-Z0-9]+)\*(?P<utr>[A-Z0-9]{8,22})"
                   r"\*(?P<name>[^*]*?)(?:\*(?P<note>.*?))?-*$")


def narration(text: str) -> dict | None:
    m = _UPI.match(text)
    if m:
        return fields("UPI", m["utr"], m["name"], m["upi"], m["note"])
    m = _NEFT.match(text)
    if m:
        return fields(m["channel"], m["utr"], m["name"], None, m["note"])
    return None
