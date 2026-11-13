"""Measuring how well matching works, and tuning the suggestion weights against what people chose.

The weights in matching.WEIGHTS started as guesses. Every time a person
resolves a review-queue credit, review_outcomes keeps the ranked candidates
with the signals that fired for each, and who was actually chosen. That is
enough to replay any set of weights against real decisions and count how
often the top suggestion would have been right.
"""

from datetime import datetime

from .db import tenant_tx
from .matching import WEIGHTS

MIN_OUTCOMES_TO_TUNE = 50
GRID = range(0, 65, 5)


def _pct(part: int, whole: int) -> str | None:
    if whole == 0:
        return None                      # nothing measured yet: say so, rather than 0% or 100%
    tenths = (part * 1000 + whole // 2) // whole
    return f"{tenths // 10}.{tenths % 10}"


def load_outcomes(conn, inst: int) -> list[dict]:
    with tenant_tx(conn, inst):
        return conn.execute("SELECT bank_credit_id, chosen, rank_of_choice, candidates "
                            "FROM review_outcomes ORDER BY id").fetchall()


def top_pick(outcome: dict, weights: dict) -> int | None:
    """The member these weights would have suggested first, with the same tie-break as suggest()."""
    scored = [(-sum(weights.get(s, 0) for s in c["signals"]), c["member_id"]) for c in outcome["candidates"]]
    scored = [s for s in scored if s[0] < 0]
    return min(scored)[1] if scored else None


def top1_hits(outcomes: list[dict], weights: dict) -> int:
    return sum(top_pick(o, weights) in o["chosen"] for o in outcomes)


def signal_stats(outcomes: list[dict]) -> dict:
    """For each signal: how often it fired on the member who was chosen, and on one who was not."""
    stats = {name: {"fired_on_chosen": 0, "fired_on_others": 0} for name in WEIGHTS}
    for outcome in outcomes:
        for candidate in outcome["candidates"]:
            side = "fired_on_chosen" if candidate["member_id"] in outcome["chosen"] else "fired_on_others"
            for name in candidate["signals"]:
                if name in stats:
                    stats[name][side] += 1
    for name, s in stats.items():
        s["precision_pct"] = _pct(s["fired_on_chosen"], s["fired_on_chosen"] + s["fired_on_others"])
        s["weight"] = WEIGHTS[name]
    return stats


def tune(outcomes: list[dict], start: dict | None = None, rounds: int = 5) -> tuple[dict, int]:
    """Coordinate ascent: change one weight at a time to whichever grid value gets most top picks right.

    A weight only moves when that strictly improves the count, so with no
    evidence against the current weights they are left alone.
    """
    weights = dict(start or WEIGHTS)
    best = top1_hits(outcomes, weights)
    for _ in range(rounds):
        improved = False
        for name in weights:
            for value in GRID:
                trial = {**weights, name: value}
                hits = top1_hits(outcomes, trial)
                if hits > best:
                    weights, best, improved = trial, hits, True
        if not improved:
            break
    return weights, best


def cross_validate(outcomes: list[dict], folds: int = 5) -> dict:
    """Tune on four fifths, score on the held-out fifth, so the gain is not just memorised."""
    current = tuned = total = 0
    for k in range(folds):
        test = outcomes[k::folds]
        train = [o for i, o in enumerate(outcomes) if i % folds != k]
        weights, _ = tune(train)
        current += top1_hits(test, WEIGHTS)
        tuned += top1_hits(test, weights)
        total += len(test)
    return {"held_out": total, "current_top1_pct": _pct(current, total), "tuned_top1_pct": _pct(tuned, total)}


def matching_report(conn, inst: int, now: datetime) -> dict:
    """Auto-match rate, suggestion accuracy, and how usable payers' notes actually are."""
    with tenant_tx(conn, inst):
        c = conn.execute(
            r"""SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE match_rule IN ('ref_code', 'known_upi_id', 'sibling_split')) AS auto,
                       COUNT(*) FILTER (WHERE match_rule = 'manual') AS manual,
                       COUNT(*) FILTER (WHERE status = 'needs_review') AS waiting,
                       COUNT(*) FILTER (WHERE status = 'reversed') AS reversed,
                       COUNT(*) FILTER (WHERE note IS NULL OR btrim(note) = '') AS note_empty,
                       COUNT(*) FILTER (WHERE note ~* '\mPS[\s\-]?\d{4,7}\M') AS note_has_code,
                       COUNT(*) FILTER (WHERE payer_upi_id IS NULL) AS no_upi_id,
                       COUNT(*) FILTER (WHERE payer_name IS NULL) AS no_payer_name,
                       MIN(received_at) FILTER (WHERE status = 'needs_review') AS oldest_waiting
                  FROM bank_credits""").fetchone()
        by_rule = conn.execute("SELECT match_rule, COUNT(*) AS n FROM bank_credits "
                               "WHERE match_rule IS NOT NULL GROUP BY match_rule ORDER BY match_rule").fetchall()
    outcomes = load_outcomes(conn, inst)
    decided = len(outcomes)
    top1 = sum(o["rank_of_choice"] == 1 for o in outcomes)
    top3 = sum(o["rank_of_choice"] is not None and o["rank_of_choice"] <= 3 for o in outcomes)
    return {
        "credits": c["total"],
        "auto_matched": c["auto"], "resolved_by_a_person": c["manual"],
        "waiting_for_review": c["waiting"], "reversed": c["reversed"],
        "auto_match_rate_pct": _pct(c["auto"], c["total"]),
        "by_rule": {r["match_rule"]: r["n"] for r in by_rule},
        "oldest_waiting_hours": (None if c["oldest_waiting"] is None
                                 else round((now - c["oldest_waiting"]).total_seconds() / 3600, 1)),
        "suggestions": {
            "decisions_measured": decided,
            "top_suggestion_right_pct": _pct(top1, decided),
            "choice_in_top_3_pct": _pct(top3, decided),
            "choice_was_not_suggested": sum(o["rank_of_choice"] is None for o in outcomes),
        },
        # How far "just ask parents to write the fee in the note" would get.
        "what_payers_send": {
            "note_empty_pct": _pct(c["note_empty"], c["total"]),
            "note_has_reference_code_pct": _pct(c["note_has_code"], c["total"]),
            "no_usable_upi_id_pct": _pct(c["no_upi_id"], c["total"]),
            "no_payer_name_pct": _pct(c["no_payer_name"], c["total"]),
        },
        "signals": signal_stats(outcomes),
    }
