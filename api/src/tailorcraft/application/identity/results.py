"""`Authenticated`: what every use case that signs somebody in hands back to the route.

Shared by three use cases — `RegisterUser`, `LogIn` and `RefreshLogin` — which is why it lives in its
own module rather than beside any one of them: each of the three ends in the same place (a user, the
login that now speaks for them, and an access token minted for this instant), and the route that
answers all three builds the same body and the same `Set-Cookie` from it.

**What it does not carry is the point.** No refresh token: the route minted the plaintext and handed
this layer only its `TokenHash` (ADR-0010's pattern, AC-10), so the route already holds the one value
it must put in the cookie, and this layer never had it to return. The login's own
`current_token_hash` is reachable through `login`, and is redacted by `TokenHash.__repr__`.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import IssuedAccessToken


@dataclass(frozen=True, slots=True)
class Authenticated:
    """A signed-in outcome: the `user`, the `login` (refresh-token family) now current for this
    device, and the `access_token` issued at the use case's single `clock.now()`.

    The aggregates themselves rather than a narrower DTO, for `StartGuestSession`'s reason: the route
    needs `user.id` and `user.email` for the body and `login.expires_at` for the cookie's `Max-Age`,
    and a bespoke shape would only mirror those facts. `IssuedAccessToken` redacts its own `repr`, so
    an accidental `log.info("%r", result)` prints no bearer token.
    """

    user: User
    login: Login
    access_token: IssuedAccessToken
