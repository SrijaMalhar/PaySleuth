"""Matching payer names the way banks print them.

The bank prints "R K VERMA" or "MRS SUNITA SHAR"; the guardian was registered
as "Rajesh Kumar Verma". The score is the share of the payer's words found,
in order, in the guardian's name, halved when the surnames disagree.
"""

import re

_TITLES = {"MR", "MRS", "MS", "MISS", "DR", "SHRI", "SRI", "SMT", "KUMARI", "KU"}


def tokens(name: str | None) -> list[str]:
    words = re.findall(r"[A-Z]+", (name or "").upper())
    return [w for w in words if w not in _TITLES]


def first_name(name: str | None) -> str | None:
    words = tokens(name)
    return words[0] if words else None


def surname(name: str | None) -> str | None:
    words = tokens(name)
    return words[-1] if len(words) > 1 else None


def _token_matches(payer: str, guardian: str) -> bool:
    if payer == guardian:
        return True
    if len(payer) == 1:                                   # "R" for "RAJESH"
        return guardian.startswith(payer)
    # "SHAR" for "SHARMA" is a field the bank cut short; "AN" would match
    # ANIL, ANAND and ANKITA, so a prefix needs at least four letters.
    return len(payer) >= 4 and guardian.startswith(payer)


def similarity(payer_name: str | None, guardian_name: str | None) -> float:
    payer, guardian = tokens(payer_name), tokens(guardian_name)
    if not payer or not guardian:
        return 0.0
    matched, gi = 0, 0
    for p in payer:                       # in order, each guardian word used at most once
        for j in range(gi, len(guardian)):
            if _token_matches(p, guardian[j]):
                matched += 1
                gi = j + 1
                break
    score = matched / len(payer)
    if not _token_matches(payer[-1], guardian[-1]):       # surnames carry the most weight
        score *= 0.5
    return round(score, 2)
