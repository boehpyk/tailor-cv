"""Errors the `identity` bounded context raises about a guest session."""

from __future__ import annotations

from tailorcraft.domain.shared.errors import DomainError


class GuestSessionNotFound(DomainError):
    """No `GuestSession` exists for the token hash presented — an unrecognized or forged cookie."""


class GuestSessionExpired(DomainError):
    """The session exists but `expires_at` has passed (ADR-0006 §1). A GET on a base CV refuses an
    expired session rather than minting a new one (F-19); a POST is more forgiving and starts a
    fresh session instead (F-17/F-18) — that asymmetry lives in the use cases, not here."""
