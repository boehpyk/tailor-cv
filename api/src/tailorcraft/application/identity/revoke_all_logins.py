"""The `RevokeAllLogins` use case: the break-glass that signs everybody out (AC-13, OQ-5).

Entry point: `python -m tailorcraft.cli revoke-logins --all [--dry-run]`, which prints the count.
**`dry_run` is a call argument, not constructor configuration** — unlike the purge's, whose dry-run
is fixed per run by the scheduler — because this has one caller that decides per invocation, and a
rehearsal (`--dry-run`) followed by the real pull is the intended sequence against one container.

**No event, and no clock.** A mass revocation has no aggregate to record it (the logins are deleted
in one statement, not loaded), and an event per login would be a flood of `LoggedOut`s nobody chose.
The CLI's own log line, built from the returned count, is the record.
"""

from __future__ import annotations

from tailorcraft.domain.identity.ports import LoginRepository


class RevokeAllLogins:
    """Delete every `Login` and return how many there were; with `dry_run=True`, delete nothing and
    return how many there are (`LoginRepository.count_all`). Idempotent: a second run returns 0.
    Constructor argument: the `logins` port."""

    def __init__(self, logins: LoginRepository) -> None:
        self._logins = logins

    async def __call__(self, dry_run: bool) -> int:
        if dry_run:
            return await self._logins.count_all()
        return await self._logins.remove_all()
