"""The admin area's router (slice 4.1, technical plan §0.4-§0.6, §3 "New router", §4).

Built red-first (sdlc.md §2): **SKELETON** (T14), **RED** (T15, `qa`), **GREEN** (T16).

**The firewall is the router, not the route** (§0.6): `require_admin` is a router-level dependency,
so every route added under `/api/admin` inherits it. A handler that needs the administrator's `User`
takes `AdminDep` as well; FastAPI caches a dependency per request, so that is the same read.

The OpenAPI `responses` list 401 and 503 only. **404 is deliberately not documented**: a non-admin
gets a 404 byte-identical to an unmatched path, and documenting it would describe the refusal.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from tailorcraft.application.shared.read_demand_counters import ReadDemandCountersQuery
from tailorcraft.domain.shared.counters import DemandCounter
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.infrastructure.api.deps import ReadDemandCountersDep, require_admin
from tailorcraft.infrastructure.api.errors import domain_error_to_http_exception
from tailorcraft.infrastructure.api.routers._me_responses import NOT_SIGNED_IN, SERVICE_UNAVAILABLE
from tailorcraft.infrastructure.api.schemas.admin import (
    DemandCountersResponse,
    DemandCountsResponse,
    DemandDayResponse,
)
from tailorcraft.infrastructure.api.schemas.intake import ErrorResponse

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get(
    "/access",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={**NOT_SIGNED_IN, **SERVICE_UNAVAILABLE},
)
async def access(response: Response) -> None:
    """204, empty, `Cache-Control: no-store`: the signed-in user may enter the admin area."""
    response.headers["Cache-Control"] = "no-store"


@router.get(
    "/demand-counters",
    response_model=DemandCountersResponse,
    responses={
        **NOT_SIGNED_IN,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": "validation_error (not an ISO date) | invalid_counter_range "
            "(`to` before `from`, or more than 92 days).",
        },
        **SERVICE_UNAVAILABLE,
    },
)
async def read_demand_counters(
    response: Response,
    from_day: Annotated[date, Query(alias="from")],
    to_day: Annotated[date, Query(alias="to")],
    read_counters: ReadDemandCountersDep,
) -> DemandCountersResponse:
    """Slice 5.2 (ADR-0043): every day in `[from, to]` with zeros filled, and the totals."""
    response.headers["Cache-Control"] = "no-store"
    try:
        counters = await read_counters(ReadDemandCountersQuery(from_day=from_day, to_day=to_day))
    except DomainError as exc:
        refused = domain_error_to_http_exception(exc)
        refused.headers = {**(refused.headers or {}), "Cache-Control": "no-store"}
        raise refused from None
    # By alias: `from` is a Python keyword, so the fields are `from_day` / `to_day` on the model.
    return DemandCountersResponse.model_validate(
        {
            "from": from_day,
            "to": to_day,
            "days": [DemandDayResponse(day=d.day, counts=_counts(d.counts)) for d in counters.days],
            "totals": _counts(counters.totals),
        }
    )


def _counts(counts: Mapping[DemandCounter, int]) -> DemandCountsResponse:
    """Every member, zero when absent: the response's keys are the enum's values."""
    return DemandCountsResponse.model_validate({c.value: counts.get(c, 0) for c in DemandCounter})
