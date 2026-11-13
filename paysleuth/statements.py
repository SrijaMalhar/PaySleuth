"""Turning a bank statement CSV into clean credit facts.

Pure Python: nothing here touches the database.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime

from .money import to_paise
from .periods import IST


@dataclass(frozen=True)
class Credit:
    """One credit line from the bank. Frozen: a fact from the bank never changes once parsed."""
    utr: str
    amount: int                       # paise
    received_at: datetime
    channel: str = "UPI"
    payer_upi_id: str | None = None
    payer_name: str | None = None
    note: str | None = None
    narration: str | None = None
    line: int = 0                     # position in the statement, for ties


@dataclass
class ParseResult:
    credits: list[Credit] = field(default_factory=list)   # not "= []": one list per instance
    skipped_debits: int = 0
    errors: list[dict] = field(default_factory=list)


# Anchored at both ends, so each narration can be split only one way. In
# "UPI-R K VERMA-rk-verma@okhdfcbank-HDFC0001234-512345678901-FEES" the UPI ID
# itself contains a hyphen; the IFSC and the 12-digit UTR must line up exactly
# at the end, which pins down where the UPI ID stops.
_SLASH_UPI = re.compile(
    r"^UPI/(?:CR/)?(?P<utr>\d{12})/(?P<name>[^/]*)/(?:(?P<bank>[A-Z]{4}[A-Z0-9]*)/)?"
    r"(?P<upi>[^/\s]+@[A-Za-z0-9.]+)(?:/(?P<note>.*))?$")
_HYPHEN_UPI = re.compile(
    r"^UPI-(?P<name>[^-]*)-(?P<upi>[^\s]+?@[A-Za-z0-9.]+)-(?P<ifsc>[A-Z]{4}0[A-Z0-9]{6})-"
    r"(?P<utr>\d{12})(?:-(?P<note>.*))?$")
_NEFT_IMPS = re.compile(
    r"^(?P<channel>NEFT|IMPS)[/-](?P<utr>[A-Z0-9]{8,22})[/-](?P<name>[^/-]*)(?:[/-](?P<note>.*))?$")


def _clean(value: str | None) -> str | None:
    value = " ".join((value or "").split())
    return value or None


def parse_narration(text: str | None) -> dict | None:
    """Pull channel, UTR, payer name, payer UPI ID and note out of a narration line."""
    narration = " ".join((text or "").split())
    if not narration:
        return None
    for pattern in (_SLASH_UPI, _HYPHEN_UPI):
        m = pattern.match(narration)
        if m:
            return {"channel": "UPI", "utr": m["utr"], "payer_name": _clean(m["name"]),
                    "payer_upi_id": m["upi"].lower(), "note": _clean(m["note"])}
    m = _NEFT_IMPS.match(narration)
    if m:
        return {"channel": m["channel"], "utr": m["utr"], "payer_name": _clean(m["name"]),
                "payer_upi_id": None, "note": _clean(m["note"])}
    return None


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


_ALIASES = {
    "date": ["txndate", "transactiondate", "trandate", "date", "postingdate", "valuedate", "valuedt"],
    "narration": ["narration", "description", "particulars", "transactionremarks", "remarks",
                  "transactiondetails", "details"],
    "credit": ["depositamt", "depositamount", "depositamountinr", "deposits", "credit",
               "creditamount", "creditamt", "cr", "creditinr"],
    "debit": ["withdrawalamt", "withdrawalamount", "withdrawalamountinr", "withdrawals", "debit",
              "debitamount", "debitamt", "dr", "debitinr"],
    "reference": ["chqrefno", "refnochequeno", "chequenumber", "chqno", "chequeno", "refno",
                  "reference", "referenceno", "utr", "utrno", "transactionid"],
    "balance": ["balance", "closingbalance", "balanceinr", "bal", "runningbalance"],
}


def _find_columns(header: list[str]) -> dict[str, int]:
    keys = [_key(h) for h in header]
    found = {}
    for column, aliases in _ALIASES.items():
        for alias in aliases:                 # earlier aliases win: "txn date" before "value date"
            if alias in keys:
                found[column] = keys.index(alias)
                break
    return found


_DATE_FORMATS = [
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d/%m/%y",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y", "%d-%m-%y",
    "%d %b %Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %y", "%d.%m.%Y",
]


def _parse_date(value: str) -> datetime:
    """A statement date as an aware datetime. A date with no time zone is Indian time."""
    text = " ".join((value or "").split())
    if not text:
        raise ValueError("missing date")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        moment = None
        for fmt in _DATE_FORMATS:
            try:
                moment = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if moment is None:
            raise ValueError(f"unrecognised date {value!r}") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=IST)


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def _blank_amount(text: str) -> bool:
    return text.replace(",", "").strip() in ("", "-", "0", "0.0", "0.00")


def parse_statement(text: str) -> ParseResult:
    """Parse a bank CSV. Debits are counted and skipped; a bad row is reported, the rest still import."""
    rows = list(csv.reader(io.StringIO(text)))
    while rows and not any(cell.strip() for cell in rows[0]):
        rows.pop(0)
    if not rows:
        raise ValueError("the statement is empty")
    columns = _find_columns(rows[0])
    missing = [c for c in ("date", "narration", "credit") if c not in columns]
    if missing:
        raise ValueError(f"the statement has no {', '.join(missing)} column")

    result = ParseResult()
    # Line 1 is the header, so numbering from 2 matches the row number the owner sees in Excel.
    for line, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        credit_text = _cell(row, columns["credit"])
        if _blank_amount(credit_text):
            if not _blank_amount(_cell(row, columns.get("debit"))):
                result.skipped_debits += 1
            continue
        try:
            amount = to_paise(credit_text)
            received_at = _parse_date(_cell(row, columns["date"]))
            narration = " ".join(_cell(row, columns["narration"]).split())
            parsed = parse_narration(narration)
            if parsed is None:
                reference = re.sub(r"\s+", "", _cell(row, columns.get("reference")))
                if not reference:
                    raise ValueError("narration not recognised and no reference number to use as the UTR")
                parsed = {"channel": "OTHER", "utr": reference, "payer_name": None,
                          "payer_upi_id": None, "note": None}
            result.credits.append(Credit(amount=amount, received_at=received_at,
                                         narration=narration or None, line=line, **parsed))
        except ValueError as error:
            result.errors.append({"line": line, "error": str(error)})
    return result
