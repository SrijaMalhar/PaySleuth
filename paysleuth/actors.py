"""Who is acting. Set once per request, read by the audit log.

A context variable rather than a parameter on every function: the money
functions stay the same whether a person, the worker or a test calls them,
and the audit log still records who it was.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

current_actor: ContextVar[int | None] = ContextVar("paysleuth_actor", default=None)


@dataclass(frozen=True)
class Actor:
    institution_id: int
    staff_id: int
    role: str            # 'owner' or 'clerk'
    name: str


@contextmanager
def acting_as(staff_id: int | None):
    token = current_actor.set(staff_id)
    try:
        yield
    finally:
        current_actor.reset(token)
