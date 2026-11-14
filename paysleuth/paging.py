"""One page bound, in one place.

Every list in this system is per-institution, so its size is set by somebody
else's data: the largest institution would otherwise decide how slow the
smallest one's request is. That was a real bug once -- the review queue read
every member for every waiting credit -- and the fix was to page it. These are
the numbers that fix chose, lifted out so the other lists cannot each pick
their own.

The clamp lives here rather than only in the HTTP layer because the CLI, the
worker and the tests call the same list functions, and a bound that only one
caller applies is not a bound.
"""

DEFAULT = 50
MAX = 200


def limits(limit: int | None = None, offset: int | None = None) -> tuple[int, int]:
    """Clamp a page request. Returns (limit, offset), both safe to pass to SQL.

    A missing limit is DEFAULT, not "everything": a caller that forgot to say
    should get a page, not the whole table. Anything above MAX is MAX rather
    than an error, because a client asking for too much wants as much as it can
    have, and failing the request would only make it ask again.
    """
    size = DEFAULT if limit is None else max(1, min(int(limit), MAX))
    start = 0 if offset is None else max(0, int(offset))
    return size, start
