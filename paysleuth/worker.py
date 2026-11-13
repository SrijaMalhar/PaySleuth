"""The background worker: relay messages every few seconds, run the daily jobs every hour.

Every job is idempotent (billing has UNIQUE charges, reminders have dedupe
keys, the relay claims with SKIP LOCKED), so running several workers is safe.

    python -m paysleuth.worker
"""

import logging
import os
import time

from .charges import apply_late_fees, run_billing
from .close import run_close
from .db import make_pool, migrate
from .institutions import all_institution_ids
from .logging_setup import configure
from .notify import sender_from_env
from .outbox import relay_once
from .periods import today_ist
from .reminders import queue_reminders

log = logging.getLogger("paysleuth.worker")

RELAY_EVERY = 5.0
JOBS_EVERY = 3600.0


def run_jobs(pool, today) -> None:
    with pool.connection() as conn:
        for inst in all_institution_ids(conn):
            try:
                billed = run_billing(conn, inst, today)
                late = apply_late_fees(conn, inst, today)
                reminders = queue_reminders(conn, inst, today)
                closed = run_close(conn, inst, today)
                log.info("jobs institution=%s billed=%s late_fees=%s reminders=%s close=%s",
                         inst, billed["charges_created"], len(late), reminders, closed["status"])
            except Exception:
                log.exception("daily jobs failed for institution %s", inst)


def relay_all(pool, send) -> int:
    sent = 0
    with pool.connection() as conn:
        for inst in all_institution_ids(conn):
            try:
                while True:
                    summary = relay_once(conn, inst, send)
                    sent += summary["sent"]
                    if summary["claimed"] == 0:
                        break
            except Exception:
                log.exception("relay failed for institution %s", inst)
    return sent


def run_forever(pool, send, *, today=today_ist, clock=time.monotonic, sleep=time.sleep,
                iterations: int | None = None) -> None:
    last_jobs = None
    count = 0
    while iterations is None or count < iterations:
        count += 1
        if last_jobs is None or clock() - last_jobs >= JOBS_EVERY:
            run_jobs(pool, today())
            last_jobs = clock()
        relay_all(pool, send)
        sleep(RELAY_EVERY)


def main() -> None:
    configure(os.environ.get("PAYSLEUTH_LOG_LEVEL", "INFO"))
    dsn = os.environ.get("PAYSLEUTH_DATABASE_URL", "postgresql://paysleuth:paysleuth@localhost:5432/paysleuth")
    pool = make_pool(dsn, max_size=4)
    migrate(pool)
    log.info("worker started")
    run_forever(pool, sender_from_env())


if __name__ == "__main__":
    main()
