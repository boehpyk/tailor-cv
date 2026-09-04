"""The root of the domain's exception hierarchy.

Domain code raises *domain* errors. It never raises `HTTPException`, never returns a status code,
and never knows that HTTP exists — translating a `DomainError` into a response is the API layer's
job, and it is the only layer that can do it correctly for its own protocol. The same error has to
survive being raised inside a Celery task, where there is no request to fail.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every error that expresses a broken business rule.

    Catching `DomainError` at a boundary is how an adapter distinguishes "the user asked for
    something the business does not allow" from "the process is broken" — the first is a 4xx and a
    message, the second is a 5xx and a page.
    """


class InvariantViolated(DomainError):
    """An aggregate was asked to enter a state its rules forbid."""
