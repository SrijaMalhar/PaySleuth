"""What every route needs, resolved once.

These were closures over create_app's arguments when the whole API was one
module. Routers are built at import time and cannot close over anything, so
the per-app values live on app.state and these read them back. That is the
only change the split forced on the design: everything injectable is still
injected, just one indirection later.
"""

from datetime import date, datetime

from fastapi import Depends, Header, HTTPException, Query, Request

from ..actors import Actor
from ..errors import Forbidden
from ..paging import DEFAULT as PAGE_DEFAULT, MAX as PAGE_MAX


def conn(request: Request):
    """A pooled connection for the length of one request."""
    with request.app.state.pool.connection() as c:
        yield c


def today(request: Request) -> date:
    """The current Indian date, from the app's clock so tests can fix it."""
    return request.app.state.today()


def now(request: Request) -> datetime:
    return request.app.state.now()


def actor(request: Request, x_api_key: str | None = Header(None)) -> Actor:
    """Who is calling. The middleware already looked the key up exactly once."""
    who = request.state.actor
    if who is None:
        raise HTTPException(status_code=401, detail="missing or unknown X-API-Key")
    return who


def institution(who: Actor = Depends(actor)) -> int:
    return who.institution_id


def owner(who: Actor = Depends(actor)) -> Actor:
    if who.role != "owner":
        raise Forbidden("only an owner can do this")
    return who


def page(limit: int = Query(PAGE_DEFAULT, ge=1, le=PAGE_MAX),
         offset: int = Query(0, ge=0)) -> tuple[int, int]:
    """The page bound, declared once instead of on ten signatures."""
    return limit, offset
