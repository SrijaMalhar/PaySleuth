"""Billing months and the clock.

A billing month is the string "YYYY-MM", not a date. Zero-padded strings sort
in calendar order, so "2026-09" < "2026-10" is a plain comparison, and
PostgreSQL stores them as text guarded by a CHECK regex.

today_ist() and utc_now() are the only two places that read the clock; every
function that cares about time takes the moment as a parameter so tests can
move it.
"""

import calendar
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def check_period(period: str) -> str:
    if not isinstance(period, str) or not _PERIOD.match(period):
        raise ValueError(f"billing month must look like 2026-06 (got {period!r})")
    return period


def _split(period: str) -> tuple[int, int]:
    check_period(period)
    return int(period[:4]), int(period[5:])


def period_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def next_period(period: str) -> str:
    year, month = _split(period)
    # month // 12 is 1 only for December, so December rolls into January with no if.
    return f"{year + month // 12:04d}-{month % 12 + 1:02d}"


def periods_from(first: str, last: str) -> list[str]:
    """Every month from first to last inclusive; empty if first is after last."""
    check_period(first)
    check_period(last)
    months = []
    current = first
    while current <= last:
        months.append(current)
        current = next_period(current)
    return months


def due_date(period: str, billing_day: int) -> date:
    """The day a month's fee falls due. Day 31 in February becomes the 28th or 29th."""
    year, month = _split(period)
    if not 1 <= billing_day <= 31:
        raise ValueError("billing day must be between 1 and 31")
    return date(year, month, min(billing_day, calendar.monthrange(year, month)[1]))


def today_ist() -> date:
    return datetime.now(IST).date()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
