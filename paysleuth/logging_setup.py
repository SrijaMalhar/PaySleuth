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

import logging
import re

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
        return True


def configure(level: str | int = logging.INFO, stream=None) -> None:
    """Set up logging for a process, with masking on the handler.

    On the handler rather than on one logger, so records from libraries and
    from any logger name are masked too.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(MaskPersonalData())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
