"""What to watch: the numbers that say whether PaySleuth is doing its job.

Exposed at /metrics in Prometheus text format, computed from the database
rather than kept in memory, so every process reports the same figures and a
restart loses nothing.

The four that matter, and why:

    auto-match rate        the product's whole claim; if it falls, matching broke
    review-queue age       a credit nobody has looked at is a parent not credited
    outbox lag             the oldest message still waiting; if it grows, receipts
                           and reminders are not going out
    dead messages          delivery gave up after 8 attempts; somebody must look
    open alerts            a statement is missing a page, or the books do not add up

The counts are per institution, which is what an operator needs to see, and
the number of institutions on one of these is small.
"""

from datetime import datetime

from .db import tenant_tx
from .institutions import all_institution_ids

# name, type, help
SERIES = [
    ("paysleuth_credits_total", "counter", "Bank credits recorded, by status"),
    ("paysleuth_credits_matched_total", "counter", "Bank credits matched, by rule"),
    ("paysleuth_review_queue_depth", "gauge", "Credits waiting for a person"),
    ("paysleuth_review_queue_oldest_seconds", "gauge", "Age of the oldest credit waiting for a person"),
    ("paysleuth_outbox_depth", "gauge", "Messages not yet sent, by status"),
    ("paysleuth_outbox_oldest_pending_seconds", "gauge", "Age of the oldest message still waiting"),
    ("paysleuth_outbox_dead_total", "gauge", "Messages delivery gave up on"),
    ("paysleuth_alerts_open", "gauge", "Alerts waiting for a person, by kind"),
    ("paysleuth_approvals_pending", "gauge", "Reassignments waiting for a second person"),
    ("paysleuth_members_active", "gauge", "Active members"),
    ("paysleuth_outstanding_paise", "gauge", "Money billed and not yet paid"),
    ("paysleuth_last_close_clean", "gauge", "1 if the most recent daily close balanced, else 0"),
]


def _seconds_since(moment: datetime | None, now: datetime) -> float | None:
    return None if moment is None else max(0.0, (now - moment).total_seconds())


def collect(conn, now: datetime) -> list[tuple[str, dict, float]]:
    """Every sample as (name, labels, value). One query per institution."""
    samples: list[tuple[str, dict, float]] = []
    for inst in all_institution_ids(conn):
        tenant = {"institution": str(inst)}
        with tenant_tx(conn, inst):
            credits = conn.execute("SELECT status, COUNT(*) AS n FROM bank_credits GROUP BY status").fetchall()
            rules = conn.execute("SELECT match_rule, COUNT(*) AS n FROM bank_credits "
                                 "WHERE match_rule IS NOT NULL GROUP BY match_rule").fetchall()
            queue = conn.execute("SELECT COUNT(*) AS n, MIN(received_at) AS oldest FROM bank_credits "
                                 "WHERE status = 'needs_review'").fetchone()
            outbox = conn.execute("SELECT status, COUNT(*) AS n, MIN(created_at) AS oldest FROM outbox "
                                  "GROUP BY status").fetchall()
            alerts = conn.execute("SELECT kind, COUNT(*) AS n FROM alerts WHERE status = 'open' "
                                  "GROUP BY kind").fetchall()
            other = conn.execute(
                """SELECT (SELECT COUNT(*) FROM approval_requests WHERE status = 'pending') AS approvals,
                          (SELECT COUNT(*) FROM members WHERE active)                       AS members,
                          (SELECT COALESCE(SUM(c.amount), 0) FROM charges c)
                            - (SELECT COALESCE(SUM(a.amount), 0) FROM allocations a)        AS outstanding,
                          (SELECT status FROM daily_closes ORDER BY day DESC LIMIT 1)        AS last_close
                """).fetchone()

        for row in credits:
            samples.append(("paysleuth_credits_total", {**tenant, "status": row["status"]}, row["n"]))
        for row in rules:
            samples.append(("paysleuth_credits_matched_total", {**tenant, "rule": row["match_rule"]}, row["n"]))
        samples.append(("paysleuth_review_queue_depth", tenant, queue["n"]))
        oldest = _seconds_since(queue["oldest"], now)
        if oldest is not None:
            samples.append(("paysleuth_review_queue_oldest_seconds", tenant, oldest))

        pending_oldest = None
        for row in outbox:
            samples.append(("paysleuth_outbox_depth", {**tenant, "status": row["status"]}, row["n"]))
            if row["status"] in ("pending", "sending"):
                age = _seconds_since(row["oldest"], now)
                pending_oldest = age if pending_oldest is None else max(pending_oldest, age)
            if row["status"] == "dead":
                samples.append(("paysleuth_outbox_dead_total", tenant, row["n"]))
        if pending_oldest is not None:
            samples.append(("paysleuth_outbox_oldest_pending_seconds", tenant, pending_oldest))

        for row in alerts:
            samples.append(("paysleuth_alerts_open", {**tenant, "kind": row["kind"]}, row["n"]))
        samples.append(("paysleuth_approvals_pending", tenant, other["approvals"]))
        samples.append(("paysleuth_members_active", tenant, other["members"]))
        samples.append(("paysleuth_outstanding_paise", tenant, other["outstanding"]))
        if other["last_close"] is not None:
            samples.append(("paysleuth_last_close_clean", tenant, 1 if other["last_close"] == "clean" else 0))
    return samples


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(samples) -> str:
    """Prometheus text format. TYPE and HELP once per series, before its samples."""
    by_name: dict[str, list] = {}
    for name, labels, value in samples:
        by_name.setdefault(name, []).append((labels, value))
    lines = []
    for name, kind, description in SERIES:
        if name not in by_name:
            continue
        lines.append(f"# HELP {name} {description}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in by_name[name]:
            pairs = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
            number = int(value) if float(value).is_integer() else value
            lines.append(f"{name}{{{pairs}}} {number}")
    return "\n".join(lines) + "\n"
