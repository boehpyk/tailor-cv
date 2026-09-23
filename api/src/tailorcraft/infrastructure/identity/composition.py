"""The CLI's identity bindings — `LoginRepository` and the break-glass use case (AC-13, OQ-5).

`python -m tailorcraft.cli revoke-logins --all` is its own composition root, like `purge-guests`
(`infrastructure/retention/purge_command.py` says why a CLI does not borrow the worker's). It needs
exactly one port, so this module binds exactly one: the command that deletes every login must not be
able to reach a hasher, a signing key or anything else it has no use for.

The worker and beat bind **nothing** from identity (`tasks/container.py`) — no task needs auth.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.identity.revoke_all_logins import RevokeAllLogins
from tailorcraft.domain.identity.ports import LoginRepository


def build_login_repository(session: AsyncSession) -> LoginRepository:
    """Binds `LoginRepository` -> `SqlAlchemyLoginRepository` on a session the caller owns (and
    commits). Deferred import, for the mapper-configuration reason `deps.get_base_cv_repository`
    documents: the repository module reads mapped attributes at import time, which exist only once
    `configure_mappings()` has run."""
    from tailorcraft.infrastructure.persistence.repositories.identity.login import (
        SqlAlchemyLoginRepository,
    )

    return SqlAlchemyLoginRepository(session)


def build_revoke_all_logins(session: AsyncSession) -> RevokeAllLogins:
    """The break-glass use case over `session`. The caller owns the transaction and commits it, so
    the command decides — once, visibly — what "done" means."""
    return RevokeAllLogins(build_login_repository(session))
