"""Logging with phone numbers and UPI IDs masked.

Receipts and reminders carry a parent's phone number and UPI ID. Logs are
copied into aggregators, shared in screenshots and kept long after the
conversation that produced them, so the masking happens in a filter on the
log record rather than at each call site: a log line added later, anywhere,
is masked without the author having to remember.

    9876543210            -> 98****3210
    sunita.sharma@oksbi   -> su****@oksbi

Enough is left to tell two parents apart in a support conversation, and not
enough to contact either.
"""

import json
import logging
import re
from datetime import UTC, datetime

# A 10-digit Indian mobile number, with an optional +91 or 0 in front, a space or
# hyphen allowed where people group the digits, and not part of a longer number.
_PHONE = re.compile(r"(?<![\d@])(?:\+91[\s-]?|0)?([6-9]\d{4})[\s-]?(\d{5})(?!\d)")
_UPI_ID = re.compile(r"(?<![\w.@-])([\w.-]{1,256})@([a-z][a-z0-9.]{1,63})(?![\w.])", re.IGNORECASE)


def mask_phone(number: str) -> str:
    return f"{number[:2]}****{number[-4:]}"


def _mask_phones(text: str) -> str:
    return _PHONE.sub(lambda m: mask_phone(m.group(1) + m.group(2)), text)


def _mask_upi_ids(text: str) -> str:
    # [:2] on a one-character local part leaves that character, which is all there
    # is; nothing is revealed that the domain did not already give away.
    return _UPI_ID.sub(lambda m: f"{m.group(1)[:2]}****@{m.group(2)}", text)


def mask(text: str) -> str:
    """Mask phone numbers and UPI IDs in one string."""
    return _mask_upi_ids(_mask_phones(text))


# Everything logging puts on a record itself. Anything else was passed as
# `extra=` by a caller, which is exactly the kind of field that holds a phone
# number -- and which nothing printed until the JSON formatter arrived.
_STANDARD = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime", "taskName"}


def _mask_value(value, depth: int = 0):
    """Mask strings anywhere in a small structure, and leave the rest alone."""
    if isinstance(value, str):
        return mask(value)
    if depth < 3:
        if isinstance(value, dict):
            return {k: _mask_value(v, depth + 1) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_mask_value(v, depth + 1) for v in value]
    return value


def extras(record: logging.LogRecord) -> dict:
    """The fields a caller attached with extra=."""
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD}


class MaskPersonalData(logging.Filter):
    """Masks the formatted message, and the traceback of an exception, on each record.

    The formatted message is masked rather than the format string, so an
    interpolated phone number is caught whether it arrives as %s or inside a
    dict. The traceback matters as much: log.exception is how a failure in the
    money path is recorded, and the error it carries often quotes the payer.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True                      # a broken format string is logging's problem, not ours
        masked = mask(message)
        if masked != message:
            record.msg = masked
            record.args = ()
        if record.exc_info or record.exc_text:
            # Render the traceback here and mask it, so the formatter has nothing
            # left to append unmasked.
            text = record.exc_text or logging.Formatter().formatException(record.exc_info)
            record.exc_text = mask(text)
            record.exc_info = None
        if record.stack_info:
            record.stack_info = mask(record.stack_info)
        # Structured fields too. The text formatter never printed these, so this
        # was not a gap until the JSON one started emitting them -- changing how
        # logs are written changed what a leak is.
        for key, value in extras(record).items():
            masked_value = _mask_value(value)
            if masked_value != value:
                setattr(record, key, masked_value)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for a log that is queried rather than read.

    Keys are fixed and short so a collector can index them, and whatever the
    caller attached with extra= rides alongside -- already masked, because the
    filter runs before any formatter.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "at": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_text:
            entry["exception"] = record.exc_text
        if record.stack_info:
            entry["stack"] = record.stack_info
        entry.update(extras(record))
        # default=str so an unexpected object logs as its repr instead of
        # raising inside logging, which would lose the line entirely.
        return json.dumps(entry, default=str, ensure_ascii=False)


TEXT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure(level: str | int = logging.INFO, stream=None, fmt: str = "json") -> None:
    """Set up logging for a process, with masking on the handler.

    On the handler rather than on one logger, so records from libraries and
    from any logger name are masked too.

    JSON by default, because the place these lines end up is a collector with a
    query box. `fmt="text"` gives the readable one line per record, which is
    what you want when the collector is your own terminal.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(TEXT))
    handler.addFilter(MaskPersonalData())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    take_over(SERVER_LOGGERS)


# uvicorn and gunicorn install their own handlers and set propagate=False, so
# their lines never reach the root handler: not formatted as JSON, and -- the
# part that matters -- never passed through the mask. The access log is the
# highest-volume thing either of them writes and the most likely to carry a
# path with somebody's identifier in it.
SERVER_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn", "gunicorn.error",
                  "gunicorn.access")


def take_over(names) -> None:
    """Make these loggers propagate to the root handler instead of their own."""
    for name in names:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
