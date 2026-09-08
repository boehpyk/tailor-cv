"""The `StartGuestSession` use case: mint a new `GuestSession` for a hashed cookie token.

A guest session is **not a login and not a weak one** (ADR-0008). It carries no password, no
identity claim and no upgrade path; it exists only so an anonymous upload has *something* to be
linked to, and so the 1.6 purge job has an `expires_at` to sweep by (ADR-0006 §1).

The plaintext token **never reaches this layer**. `infrastructure/api/guest_session.py` mints the
random cookie value (`secrets.token_urlsafe(32)`, ADR-0010) and hashes it before this use case ever
sees it — the parameter here is `token_hash`, not `token`, because holding the plaintext in
`application/` would be one accidental log line away from handing out a working session credential.
"""

from __future__ import annotations

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ports import GuestSessionRepository
from tailorcraft.domain.shared.clock import Clock


class StartGuestSession:
    """Mint and persist a new `GuestSession` bound to `token_hash`.

    Called from the cookie-resolution dependency (`resolve_or_start_guest_session` in
    `infrastructure/api/deps.py`) whenever a request arrives with no cookie, an unrecognized one, or
    one whose session has expired (F-17/F-18) — in every case the answer is the same: start a fresh
    session and let the caller set it as the new cookie.

    Returns the created `GuestSession` itself, not a narrower DTO: the caller (the API layer, which
    builds the `Set-Cookie` header and the response body) needs both `id` — to stamp new rows and to
    resolve future requests — and `expires_at` — to be echoed in `BaseCvResponse` so the 24-hour
    promise is visible in the payload as well as the UI (technical-plan.md, API contract). Returning
    the aggregate rather than a bespoke result type avoids inventing a second shape that would only
    ever mirror `GuestSession`'s own two public facts.
    """

    def __init__(
        self,
        sessions: GuestSessionRepository,
        clock: Clock,
        retention_hours: int,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._retention_hours = retention_hours

    async def __call__(self, token_hash: str) -> GuestSession:
        session_id = self._sessions.next_identity()
        session = GuestSession.start(
            id=session_id,
            token_hash=token_hash,
            at=self._clock.now(),
            ttl_hours=self._retention_hours,
        )
        await self._sessions.add(session)
        return session
