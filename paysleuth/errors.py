"""Domain errors.

Business code raises these and knows nothing about HTTP. Only api.py maps them
to status codes (403, 404, 409, 412, 428), so the same functions run unchanged from
the API, the background worker and the tests.
"""


class NotFound(Exception):
    """The thing does not exist, or belongs to another institution."""


class Conflict(Exception):
    """The request contradicts what is already recorded."""


class Forbidden(Exception):
    """The person is known, but is not allowed to do this."""


class PreconditionFailed(Exception):
    """Someone else saved a newer version since this one was read."""


class PreconditionRequired(Exception):
    """An edit did not say which version it was based on."""
