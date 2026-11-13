"""Turning a bank statement CSV into clean credit facts.

Pure Python: nothing here touches the database.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import banks
from .banks.common import full_upi_id
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


@dataclass(frozen=True)
class Reversal:
    """A debit line where the bank took an earlier credit back."""
    utr: str                          # the UTR of the credit being returned
    amount: int
    reversed_at: datetime
    narration: str | None = None
    line: int = 0


@dataclass(frozen=True)
class StatementRow:
    """Any money line, credit or debit, as printed: used to check the running balance."""
    line: int
    at: datetime
    debit: int
    credit: int
    balance: int | None
    narration: str = ""


@dataclass
class ParseResult:
    credits: list[Credit] = field(default_factory=list)   # not "= []": one list per instance
    reversals: list[Reversal] = field(default_factory=list)
    rows: list[StatementRow] = field(default_factory=list)
    bank: str | None = None           # which bank adapter recognised the layout, if any
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


# A returned credit shows up as a debit that says so and quotes the original UTR:
# "UPI/REV/615412345601/SUNITA SHARMA", "REV-UPI-615412345601", "UPI RETURN 615412345601".
_REVERSAL_WORD = re.compile(r"(?<![A-Z])(?:REV|REVERSAL|RVSL|RETURN|RETURNED|RET)(?![A-Z])", re.IGNORECASE)
_UTR_IN_TEXT = re.compile(r"(?<!\d)(\d{12})(?!\d)")


def parse_reversal(narration: str | None) -> str | None:
    """The UTR a debit narration says it is returning, or None if it is an ordinary debit."""
    text = narration or ""
    if not _REVERSAL_WORD.search(text):
        return None
    utrs = _UTR_IN_TEXT.findall(text)
    return utrs[0] if len(utrs) == 1 else None


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
            # full_upi_id, not .lower(): a bank that prints the ID masked
            # ("**sharma@oksbi") or cut short must yield no ID rather than a
            # broken one, which would otherwise fail validation and lose the line.
            return {"channel": "UPI", "utr": m["utr"], "payer_name": _clean(m["name"]),
                    "payer_upi_id": full_upi_id(m["upi"]), "note": _clean(m["note"])}
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


HEADER_SEARCH_LINES = 40

# Something that is trying to be a date ("31/02/2026" included, so a bad date is still reported).
_DATE_LIKE = re.compile(r"\d{1,4}[/\-. ][0-9A-Za-z]{1,9}[/\-. ]\d{2,4}")

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


def _amount_or_zero(text: str) -> int:
    return 0 if _blank_amount(text) else to_paise(text)


def _signed_paise(text: str) -> int | None:
    """A balance, which may be blank, negative (overdrawn) or carry a Cr/Dr suffix."""
    value = text.replace(",", "").strip().upper()
    if not value:
        return None
    sign = 1
    if value.endswith("DR"):
        sign = -1
    value = re.sub(r"\s*(CR|DR)$", "", value)
    if value.startswith("-"):
        sign, value = -sign, value[1:]
    if value in ("0", "0.0", "0.00"):
        return 0
    return sign * to_paise(value)


def _ledger_row(row: list[str], columns: dict, line: int) -> StatementRow | None:
    try:
        return StatementRow(
            line=line, at=_parse_date(_cell(row, columns["date"])),
            debit=_amount_or_zero(_cell(row, columns.get("debit"))),
            credit=_amount_or_zero(_cell(row, columns["credit"])),
            balance=_signed_paise(_cell(row, columns.get("balance"))),
            narration=" ".join(_cell(row, columns["narration"]).split()))
    except ValueError:
        return None          # the continuity check will notice the hole this leaves


def parse_statement(text: str) -> ParseResult:
    """Parse a bank CSV. Debits are counted and skipped; a bad row is reported, the rest still import."""
    rows = list(csv.reader(io.StringIO(text)))
    if not any(cell.strip() for row in rows for cell in row):
        raise ValueError("the statement is empty")
    # Banks put the account holder's details above the table, so find the header row first.
    header_at, columns = None, {}
    for index, row in enumerate(rows[:HEADER_SEARCH_LINES]):
        columns = _find_columns(row)
        if all(c in columns for c in ("date", "narration", "credit")):
            header_at = index
            break
    if header_at is None:
        first = next(row for row in rows if any(cell.strip() for cell in row))
        missing = [c for c in ("date", "narration", "credit") if c not in _find_columns(first)]
        raise ValueError(f"the statement has no {', '.join(missing)} column")
    bank = banks.detect(_key(h) for h in rows[header_at])

    result = ParseResult(bank=bank.name if bank else None)
    # Numbered as in the file, so an error names the row the owner sees in Excel.
    for line, row in enumerate(rows[header_at + 1:], start=header_at + 2):
        if not any(cell.strip() for cell in row):
            continue
        date_text = _cell(row, columns["date"])
        if date_text and not _DATE_LIKE.search(date_text):
            continue                  # a summary or legend line under the table, not a transaction
        ledger = _ledger_row(row, columns, line)
        if ledger is not None:
            result.rows.append(ledger)
        credit_text = _cell(row, columns["credit"])
        if _blank_amount(credit_text):
            debit_text = _cell(row, columns.get("debit"))
            if not _blank_amount(debit_text):
                narration = " ".join(_cell(row, columns["narration"]).split())
                returned_utr = parse_reversal(narration)
                try:
                    if returned_utr is None:
                        raise ValueError
                    result.reversals.append(Reversal(
                        utr=returned_utr, amount=to_paise(debit_text), narration=narration, line=line,
                        reversed_at=_parse_date(_cell(row, columns["date"]))))
                except ValueError:
                    result.skipped_debits += 1
            continue
        try:
            amount = to_paise(credit_text)
            received_at = _parse_date(_cell(row, columns["date"]))
            narration = " ".join(_cell(row, columns["narration"]).split())
            parsed = (bank.narration(narration) if bank else None) or parse_narration(narration)
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
