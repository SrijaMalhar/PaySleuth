"""Anonymise a real bank statement so its layout can be kept as a golden test file.

    python tools/anonymise_statement.py real.csv > tests/golden/sbi.csv

Keeps what the parser and the continuity check need: the layout, dates,
amounts and balances. Replaces what identifies people: every long number
(UTRs, account numbers) with other digits, every UPI ID, every payer name the
parser finds, the words of every note except reference codes, and everything
above the table. The same input always maps to the same output, so a UTR that
appears twice still matches itself.

READ THE OUTPUT BEFORE SHARING IT. A bank can print a name somewhere this
tool does not know to look.
"""

import csv
import hashlib
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paysleuth import banks  # noqa: E402
from paysleuth.statements import HEADER_SEARCH_LINES, _find_columns, _key, parse_narration  # noqa: E402

KEEP_WORDS = {"FEE", "FEES", "ADVANCE", "PART", "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG",
              "SEP", "OCT", "NOV", "DEC", "JANUARY", "FEBRUARY", "MARCH", "APRIL", "JUNE", "JULY",
              "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"}


_UPI_ID = re.compile(r"([A-Za-z0-9._*]+(?:-[A-Za-z0-9._*]+)*)@([A-Za-z][A-Za-z0-9.]+)")


_LOOSE_UPI_ID = re.compile(r"(?<![A-Za-z0-9._*])(?!payer\d{5}@)([A-Za-z0-9._*]+)@([A-Za-z][A-Za-z0-9.]+)")


def _digest(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest(), 16)


def _digits(match: re.Match) -> str:
    original = match.group(0)
    core = original.lstrip("0")           # HDFC pads the UTR with zeros in the reference column
    return str(_digest(core))[-len(core):].rjust(len(core), "1").rjust(len(original), "0")


def _upi(match: re.Match) -> str:
    return f"payer{_digest(match.group(0).lower()) % 100000:05d}@{match.group(2)}"


def _note(note: str) -> str:
    def word(m: re.Match) -> str:
        w = m.group(0)
        return w if w.upper() in KEEP_WORDS or re.fullmatch(r"PS\d{4,7}", w, re.IGNORECASE) else "note"
    return re.sub(r"[A-Za-z][A-Za-z0-9]*", word, note)


def anonymise(text: str) -> str:
    rows = list(csv.reader(io.StringIO(text)))
    header_at = next((i for i, row in enumerate(rows[:HEADER_SEARCH_LINES])
                      if all(c in _find_columns(row) for c in ("date", "narration", "credit"))), None)
    if header_at is None:
        raise SystemExit("no header row with date, narration and credit columns was found")
    columns = _find_columns(rows[header_at])
    bank = banks.detect(_key(h) for h in rows[header_at])

    out = [[row[0], "REDACTED"] if len(row) > 1 else row for row in rows[:header_at]]
    out.append(rows[header_at])
    for row in rows[header_at + 1:]:
        row = list(row)
        index = columns["narration"]
        if index < len(row):
            narration = " ".join(row[index].split())
            parsed = (bank.narration(narration) if bank else None) or parse_narration(narration) or {}
            if parsed.get("note"):
                narration = narration.replace(parsed["note"], _note(parsed["note"]))
            if parsed.get("payer_upi_id"):          # the exact ID first: it may contain a hyphen
                narration = re.sub(re.escape(parsed["payer_upi_id"]),
                                   lambda m: _upi(_UPI_ID.fullmatch(m.group(0))), narration, flags=re.IGNORECASE)
            narration = _LOOSE_UPI_ID.sub(_upi, narration)     # any other ID the parser did not pick out
            if parsed.get("payer_name"):            # last, so the pseudonym is never mistaken for part of an ID
                narration = narration.replace(parsed["payer_name"],
                                              f"PAYER {_digest(parsed['payer_name'].upper()) % 10000:04d}")
            row[index] = narration
        money = {columns.get(c) for c in ("debit", "credit", "balance", "date")}
        row = [cell if i in money else re.sub(r"\d{9,}", _digits, cell) for i, cell in enumerate(row)]
        out.append(row)
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(out)
    return buffer.getvalue()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    sys.stdout.write(anonymise(Path(sys.argv[1]).read_text(encoding="utf-8-sig")))
    print("Read the output before sharing it: check that no real name, number or UPI ID is left.",
          file=sys.stderr)
