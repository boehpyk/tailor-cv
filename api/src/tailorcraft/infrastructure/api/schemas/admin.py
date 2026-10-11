"""Wire shapes for `/api/admin/users` (slice 4.2, technical plan §4, ADR-0033) and an account's
content under it (slice 4.3, technical plan §3, ADR-0034).

**No password hash, no login id, anywhere** (AC-15): a directory row and an account's detail carry
an id, an address, a role and two instants. Instants are ISO-8601 with an offset, whole seconds
(the `Clock` contract); `disabled_at` is `null` while the account is active.

None of these is a `UserResponse` subclass: `/api/auth/me` describes *your* account to you, these
describe someone else's to an administrator, and the two are free to drift (CLAUDE.md: shared shape
is not shared meaning).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from tailorcraft.domain.identity.value_objects import Role
from tailorcraft.infrastructure.api.schemas.intake import SavedBaseCvResponse


class AdminUserSummary(BaseModel):
    """One directory row: `{"id", "email", "role", "created_at", "disabled_at"}` (AC-17)."""

    id: UUID
    email: str
    role: Role
    created_at: datetime
    disabled_at: datetime | None


class AdminUserPageResponse(BaseModel):
    """One keyset page of the directory, newest first. `next_cursor` is an opaque string to send
    back as `?cursor=`, or `null` on the last page. No match is `{"items": [], "next_cursor":
    null}` and a 200."""

    items: list[AdminUserSummary]
    next_cursor: str | None


class AdminAccountCounts(BaseModel):
    """What erasing the account would delete (`AccountCounts`, the `erase-account --dry-run`
    numbers). Counts only."""

    base_cvs: int
    files: int
    logins: int
    tailoring_runs: int
    job_postings: int
    export_jobs: int
    tracked_applications: int


class AdminUserDetailResponse(BaseModel):
    """`GET /api/admin/users/{id}` (AC-20): the summary's fields plus `counts`."""

    id: UUID
    email: str
    role: Role
    created_at: datetime
    disabled_at: datetime | None
    counts: AdminAccountCounts


class AccountStatusChangeResponse(BaseModel):
    """`POST …/disable` and `POST …/enable` (AC-21, AC-22). `disabled_at` is the state afterwards;
    `changed` is `false` for a no-op; `logins_revoked` is always 0 for an enable."""

    user_id: UUID
    disabled_at: datetime | None
    changed: bool
    logins_revoked: int


class AccountErasureResponse(BaseModel):
    """`DELETE /api/admin/users/{id}` (AC-23): `AccountErasureReport`'s counts.

    `unlink_failures` is an **integer** — how many unlinks the store refused. The report's exception
    type names stay in the log lines (`retention.account_file_unlink_failed`), never on the wire.
    """

    base_cvs: int
    files: int
    files_unlinked: int
    unlink_failures: int
    tailoring_runs: int
    job_postings: int
    export_jobs: int
    tracked_applications: int


class AdminSavedBaseCvResponse(SavedBaseCvResponse):
    """One saved CV **with its extracted text** — `GET /api/admin/users/{id}/base-cvs/{cv_id}`.

    **Contradicts `SavedBaseCvResponse` on purpose**, whose docstring promises "no text and no
    bytes": that shape describes a CV to its owner's list, this one shows an administrator what the
    account holds (ADR-0034; slice 4.3 plan §0.6). A subclass, because it is exactly that shape plus
    one field, and a test pins the parent unchanged (AC-24) so the text cannot leak back into the
    user's routes. `null` while the CV is not extracted.
    """

    extracted_text: str | None


class AdminBaseCvDeletionResponse(BaseModel):
    """`DELETE /api/admin/users/{id}/base-cvs/{cv_id}` — the row is gone; `file_unlinked: false`
    means the bytes are an orphan for the sweep (the user's twin answers 204 and logs the same)."""

    file_unlinked: bool


class AdminHistoryEntryErasureResponse(BaseModel):
    """`DELETE /api/admin/users/{id}/tailoring-runs/{run_id}` — what `EraseHistoryEntry` removed:
    rows committed, then files; `unlink_failures` left for the orphan sweep."""

    tailoring_runs: int
    job_postings: int
    export_jobs: int
    tracked_applications: int
    files_unlinked: int
    unlink_failures: int
