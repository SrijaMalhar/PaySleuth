"""Liveness and metrics: the two endpoints that are not about one institution."""

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ..metrics import collect, render
from .deps import conn, now

router = APIRouter(tags=["system"])


@router.get("/health")
def health(c=Depends(conn)):
    """Touches the database, so a pass means the connection is alive too."""
    c.execute("SELECT 1")
    return {"status": "ok"}


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
def prometheus_metrics(request: Request, c=Depends(conn), at=Depends(now),
                       authorization: str | None = Header(None)):
    """Prometheus scrape endpoint.

    Counts are not personal data, but an institution's member count and the
    money it is owed are its own business, so this is not open: a scraper
    sends the token in PAYSLEUTH_METRICS_TOKEN and gets every institution,
    and a staff API key gets that institution only.
    """
    token = request.app.state.metrics_token
    if token and secrets.compare_digest((authorization or "").strip(), f"Bearer {token}"):
        return render(collect(c, at))
    who = request.state.actor
    if who is None:
        raise HTTPException(status_code=401,
                            detail="send Authorization: Bearer <metrics token> or X-API-Key")
    return render(collect(c, at, only=who.institution_id))
