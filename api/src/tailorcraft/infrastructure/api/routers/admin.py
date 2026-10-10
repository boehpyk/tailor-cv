"""The admin area's router (slice 4.1, technical plan §0.4-§0.6, §3 "New router", §4).

Built red-first (sdlc.md §2): **SKELETON** (T14), **RED** (T15, `qa`), **GREEN** (T16).

**The firewall is the router, not the route** (§0.6): `require_admin` is a router-level dependency,
so every route added under `/api/admin` inherits it. A handler that needs the administrator's `User`
takes `AdminDep` as well; FastAPI caches a dependency per request, so that is the same read.

The OpenAPI `responses` list 401 and 503 only. **404 is deliberately not documented**: a non-admin
gets a 404 byte-identical to an unmatched path, and documenting it would describe the refusal.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from tailorcraft.infrastructure.api.deps import require_admin
from tailorcraft.infrastructure.api.routers._me_responses import NOT_SIGNED_IN, SERVICE_UNAVAILABLE

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
