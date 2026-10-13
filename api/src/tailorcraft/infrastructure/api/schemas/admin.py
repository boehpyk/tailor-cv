"""Wire shapes for `/api/admin/users` (slice 4.2, technical plan §4, ADR-0033) and an account's
content under it (slice 4.3, technical plan §3, ADR-0034), plus guest sessions and retention under
`/api/admin` (slice 4.4, technical plan §3, ADR-0035).

**No password hash, no login id, anywhere** (AC-15): a directory row and an account's detail carry
an id, an address, a role and two instants. Instants are ISO-8601 with an offset, whole seconds
(the `Clock` contract); `disabled_at` is `null` while the account is active.

None of these is a `UserResponse` subclass: `/api/auth/me` describes *your* account to you, these
describe someone else's to an administrator, and the two are free to drift (CLAUDE.md: shared shape
is not shared meaning).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tailorcraft.domain.identity.value_objects import Role
from tailorcraft.infrastructure.api.schemas.intake import BaseCvResponse, SavedBaseCvResponse
from tailorcraft.infrastructure.api.schemas.posting import JobPostingSummary
from tailorcraft.infrastructure.api.schemas.tailoring import TailoringRunSummary

# Slice 4.4's bounds (plan §3, §4): one purge batch, and one sweep, from `/admin`.
PURGE_LIMIT_MAX: Final = 100
PURGE_LIMIT_DEFAULT: Final = 100
SWEEP_LIMIT_MAX: Final = 100_000


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


# --- Slice 4.4: guest sessions ---------------------------------------------------------------------


class AdminGuestSessionSummary(BaseModel):
    """One live guest session and what it owns — counts only, **never `token_hash`** (AC-9)."""

    id: UUID
    created_at: datetime
    expires_at: datetime
    base_cvs: int
    job_postings: int
    tailoring_runs: int
    export_jobs: int


class AdminGuestSessionPageResponse(BaseModel):
    """One keyset page of live sessions, latest `expires_at` first; `next_cursor` is `null` on the
    last page (AC-9)."""

    items: list[AdminGuestSessionSummary]
    next_cursor: str | None


class AdminGuestSessionDetailResponse(BaseModel):
    """`GET /api/admin/guest-sessions/{sid}` (AC-11): the summary plus the guest's **own** list item
    shapes, byte-identical to what the guest's own list routes return."""

    session: AdminGuestSessionSummary
    base_cvs: list[BaseCvResponse]
    job_postings: list[JobPostingSummary]
    tailoring_runs: list[TailoringRunSummary]


class AdminGuestBaseCvResponse(BaseCvResponse):
    """One guest CV **with its extracted text** (`null` while not extracted) — AC-13.

    **Contradicts `BaseCvResponse` on purpose**, whose docstring says the text never crosses the
    wire: that shape describes a CV to the guest who uploaded it, this one shows an administrator
    what the session holds (ADR-0034, ADR-0035). 4.3's `AdminSavedBaseCvResponse` pattern.
    """

    extracted_text: str | None


class AdminGuestSessionErasureResponse(BaseModel):
    """`DELETE /api/admin/guest-sessions/{sid}` (AC-12). `unlink_failures` is a count; the type
    names stay in the log lines, and the bytes are orphans for the sweep."""

    files_unlinked: int
    unlink_failures: int


# --- Slice 4.4: retention --------------------------------------------------------------------------


class StorageBucketResponse(BaseModel):
    files: int
    bytes: int


class AdminStorageResponse(BaseModel):
    """`GET /api/admin/storage` (AC-15): the volume split five ways. No name, key or path."""

    scanned_at: datetime
    guest: StorageBucketResponse
    user: StorageBucketResponse
    unreferenced: StorageBucketResponse
    partial: StorageBucketResponse
    unrecognized: StorageBucketResponse


class AdminPurgeRequest(BaseModel):
    """`POST /api/admin/guest-purge` (AC-17): one batch of at most `limit` sessions."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(PURGE_LIMIT_DEFAULT, ge=1, le=PURGE_LIMIT_MAX)


class AdminPurgeResponse(BaseModel):
    """`PurgeReport`'s counts for one batch (AC-16, AC-17). `overdue` is the backlog **now** on a
    preview and **after** the batch on a run; `lock` is `null` on a preview (no lock is asked for)."""

    examined: int
    sessions_deleted: int
    sessions_skipped: int
    sessions_failed: int
    files_unlinked: int
    files_failed: int
    overdue: int
    dry_run: bool
    lock: Literal["acquired", "unavailable"] | None


class AdminSweepRequest(BaseModel):
    """`POST /api/admin/orphan-sweep` (AC-21): `null` is no limit."""

    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(None, ge=1, le=SWEEP_LIMIT_MAX)


class AdminSweepResponse(BaseModel):
    """`OrphanScanReport`'s counts (AC-21)."""

    scanned: int
    referenced: int
    too_young: int
    unrecognized: int
    reclaimed: int
    failed: int
    dry_run: bool
