"""The REST API, and the only part of this package that knows HTTP exists.

Routes are grouped by resource into the modules beside this one; what stays
here is everything that is true of every request: how a caller is identified,
how fast they may go, how a retry is recognised, and how a domain error
becomes a status code.

Endpoints are plain `def`, not `async def`: psycopg calls block, and FastAPI
runs plain `def` endpoints in a thread pool, so a slow query never blocks the
event loop.

Money in and out is always a string such as "2000.50", never a JSON number,
because many clients parse JSON numbers as floats.
"""

import json
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from psycopg.errors import DataError

from .. import idempotency
from ..actors import current_actor
from ..db import make_pool, migrate
from ..errors import Conflict, Forbidden, NotFound, PreconditionFailed, PreconditionRequired
from ..institutions import actor_for_key
from ..periods import today_ist, utc_now
from ..ratelimit import DEFAULT_BURST, DEFAULT_RATE, RateLimiter
from . import credits, jobs, members, reports, staff, system

__all__ = ["create_app"]

ROUTERS = (system.router, members.router, credits.router, jobs.router,
           staff.router, reports.router)

ERRORS = ((Forbidden, 403), (NotFound, 404), (Conflict, 409), (PreconditionFailed, 412),
          (PreconditionRequired, 428), (ValueError, 422),
          # Anything the database refuses as malformed input is the caller's
          # problem, not a server fault: a NUL byte in a text field, a
          # timestamp outside its range.
          (DataError, 422))

MUTATING = ("POST", "PATCH", "PUT", "DELETE")


def create_app(dsn: str, send, *, today=today_ist, now=utc_now, pool_options: str | None = None,
               pool_size: int = 20, metrics_token: str | None = None,
               rate: int = DEFAULT_RATE, burst: int = DEFAULT_BURST, clock=time.monotonic) -> FastAPI:
    """Build the app in a function, so tests can pass a fake sender, a fixed date and a test schema."""
    pool = make_pool(dsn, max_size=pool_size, options=pool_options)
    migrate(pool)

    @asynccontextmanager
    async def lifespan(app):
        yield
        pool.close()

    app = FastAPI(title="PaySleuth", version="1.0",
                  description="Matches UPI bank-statement credits to the members they pay for.",
                  lifespan=lifespan)
    # Routers are built at import time, so what varies per app lives here and the
    # dependencies in deps.py read it back.
    app.state.pool = pool
    app.state.pool_size = pool_size
    app.state.send = send
    app.state.today = today
    app.state.now = now
    app.state.metrics_token = metrics_token
    # One bucket per caller. The clock is injected so the tests can run a minute
    # of traffic without waiting a minute.
    app.state.limiter = RateLimiter(rate=rate, burst=burst, clock=clock)

    def _error(status: int):
        def handler(request: Request, exc: Exception):
            return JSONResponse(status_code=status, content={"detail": str(exc)})
        return handler

    for error, status in ERRORS:
        app.add_exception_handler(error, _error(status))

    def _identify(key: str):
        with pool.connection() as c:
            return actor_for_key(c, key)

    # Declared before `identify` so it ends up *inside* it: Starlette makes the
    # last middleware added the outermost one, and this needs the actor that
    # `identify` resolves.
    @app.middleware("http")
    async def idempotent(request: Request, call_next):
        key = request.headers.get("idempotency-key")
        who = getattr(request.state, "actor", None)
        if not key or who is None or request.method not in MUTATING:
            return await call_next(request)

        inst, body = who.institution_id, await request.body()

        def _reserve():
            with pool.connection() as c:
                idempotency.reserve(c, inst, key, request.method, request.url.path, body)

        try:
            await run_in_threadpool(_reserve)
        except idempotency.Replay as again:
            # The reply the first request got, verbatim, with a header saying so
            # -- a client comparing the two should see no difference but this.
            return JSONResponse(status_code=again.status_code, content=again.body,
                                headers={"Idempotency-Replayed": "true"})
        except Conflict as busy:
            return JSONResponse(status_code=409, content={"detail": str(busy)})
        except PreconditionFailed as wrong:
            return JSONResponse(status_code=412, content={"detail": str(wrong)})

        response = await call_next(request)
        payload = b"".join([chunk async for chunk in response.body_iterator])

        def _record():
            with pool.connection() as c:
                if 200 <= response.status_code < 300:
                    idempotency.finish(c, inst, key, response.status_code,
                                       json.loads(payload) if payload else None)
                else:
                    # A failed request is not a result worth replaying: the client
                    # should be free to fix it and retry under the same key.
                    idempotency.release(c, inst, key)

        await run_in_threadpool(_record)
        return Response(content=payload, status_code=response.status_code,
                        headers=dict(response.headers), media_type=response.media_type)

    @app.middleware("http")
    async def identify(request: Request, call_next):
        # Look the key up once, here, and set who is acting for everything this
        # request does: the context is copied into the thread that runs the endpoint,
        # so the audit log records the person without every function passing them along.
        key = request.headers.get("x-api-key")
        who = await run_in_threadpool(_identify, key) if key else None
        request.state.actor = who

        # Limit after identifying, so the bucket belongs to the caller rather than
        # to whichever address they happen to be behind. /health is exempt: it is
        # what an uptime check and the scheduled jobs poll, and refusing it would
        # make a busy institution look down.
        if request.url.path != "/health":
            caller = (f"staff:{who.staff_id}" if who
                      else f"ip:{request.client.host if request.client else 'unknown'}")
            retry_after = app.state.limiter.take(caller)
            if retry_after is not None:
                return JSONResponse(
                    status_code=429, headers={"Retry-After": str(retry_after)},
                    content={"detail": f"too many requests; retry in {retry_after}s"})

        token = current_actor.set(who.staff_id if who else None)
        try:
            return await call_next(request)
        finally:
            current_actor.reset(token)

    for router in ROUTERS:
        app.include_router(router)
    return app
