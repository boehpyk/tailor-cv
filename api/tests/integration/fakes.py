"""In-memory fakes shared by the `integration` suite's application-layer tests.

Lifted out of `tests/integration/intake/test_upload_base_cv.py` (T9) rather than duplicated,
because a second hand-written copy of `FakeBaseCvRepository` or `FakeGuestSessionRepository` is
exactly how a test suite starts lying: the two copies drift, one gets a bug fixed and the other
does not, and nobody notices until a test that "should" catch a regression passes against the
stale fake. Every class here still satisfies its Protocol exactly (`domain/intake/ports.py`,
`domain/identity/ports.py`, `domain/shared/files.py`, `domain/shared/events.py`) — moving a fake to
a shared module changes nothing about what it does.

Used by:
- `tests/integration/intake/test_upload_base_cv.py` (T9/T10 — `UploadBaseCv`)
- `tests/integration/intake/test_read_base_cvs.py` (T12 — `GetBaseCvForSession`,
  `ListBaseCvsForSession`)
- `tests/integration/identity/test_start_guest_session.py` (T12 — `StartGuestSession`)
- `tests/integration/posting/test_capture_job_posting.py` (T9/T10 — `CaptureJobPosting`)
- `tests/integration/posting/test_read_job_postings.py` (T12/T13 — `GetJobPostingForSession`,
  `ListJobPostingsForSession`)
- `tests/integration/tailoring/test_request_tailoring_run.py` (T9/T10 — `RequestTailoringRun`).
  `FakeLlm` and `FakeTailoringQueue` are added in this same commit even though this file does not
  yet exercise them — T12 (`ExecuteTailoringRun`) and the T29 API tests need both, and a fake added
  in the commit that first uses its sibling port is how this file avoids ever growing a second,
  drifting copy of one.
- `tests/integration/tailoring/test_execute_tailoring_run.py` (T12 — `ExecuteTailoringRun`).
  `FakeTailoringRunRepository.save_calls` and `FakeLlm.on_call` are added in this commit: T12 needs
  to observe the repository's state at the exact moment the LLM is invoked, to prove `running` is
  saved before the call rather than after (technical-plan.md's "Step 4 — two commits").
- `tests/integration/tailoring/test_revise_tailored_document.py` (T6 — `ReviseTailoredDocument`),
  `test_execute_tailoring_run.py` (AC-8) and `test_abandon_stale_tailoring_runs.py` (AC-9).
  `FakeTailoringRunRepository.conflict_on_save` and `.saved` are added in this commit (ADR-0015 §3):
  a repository built with `conflict_on_save=N` raises `TailoringRunConcurrentlyModified` on its next
  `N` calls to `save`, decrementing each time, before reverting to its ordinary behaviour — the
  in-memory stand-in for the database race a `version_id_col` mismatch reports as `StaleDataError`.
  `.saved` records every run a *successful* `save` call persisted, in order, which is what lets a
  test on the rejection paths assert nothing was written (`saved == []`) without caring whether
  `save` was even reached.
- `tests/integration/export/` (T6 — the seven `export` use cases). `FakeExportJobRepository` is
  `FakeTailoringRunRepository`'s shape (`next_identity`, `add`, `get`-raises, `find`-returns-`None`,
  `conflict_on_save`, `saved`, `.all()`) plus the three lookups `ExportJobRepository` adds that no
  earlier port needed — `list_for_run`, `find_latest_for_key` and `list_stale_rendering` — because
  `ExportJob` is the first aggregate in this codebase looked up by a business key instead of only by
  id. `FakeDocumentRenderer` and `FakeExportQueue` mirror `FakeLlm` and `FakeTailoringQueue`'s shape
  (one instance, one configured outcome, a `.calls` / `.enqueued` log); `MissingFileStore` is a third
  `FileStorePort` stand-in beside the pre-existing `InMemoryFileStore` and `AlwaysFailingFileStore`,
  for the one case neither covers — a `ready` job's `get` finding nothing (X-47).
- `tests/integration/identity/{test_register_user,test_log_in,test_refresh_login,test_log_out,
  test_get_current_user,test_revoke_all_logins}.py` (T13 — the six slice-2.1 use cases).
  `FakeUserRepository` and `FakeLoginRepository` are `add`-is-the-uniqueness-check and
  revocation-is-deletion respectively (technical plan §0.4, ADR-0020), mirroring
  `FakeGuestSessionRepository`'s shape one context over. `FakeLoginRepository.
  conflict_on_save_rotation` is `FakeTailoringRunRepository.conflict_on_save`'s pattern applied to
  `save_rotation` (I-25) — a stand-in for the real `UPDATE … WHERE version = :v` race, whose actual
  truth is T31's, against real Postgres. `RecordingPasswordHasher`, `FakeAccessTokenPort` and
  `RecordingFailedLoginObserver` are one-instance-per-test recorders in `FakeLlm`'s shape: a
  configured outcome plus a full argument log, because AC-9 needs to assert not just how many times
  `verify` ran but *what* it was called with (`against=None` on the unknown-email path). The
  pre-existing `RecordingEventPublisher` below (added for the `export`/`tailoring` suites) is reused
  as-is — its `repo` parameter is simply left `None` here, since no T13 test needs the
  publish-after-save snapshot. `CountingClock` wraps a `FixedClock` and counts `.now()` calls, which
  `FixedClock` itself does not track — what T13's "one `clock.now()` per use-case call" assertion
  needs.
- `tests/integration/intake/{test_upload_base_cv,test_list_saved_base_cvs,test_rename_saved_base_cv,
  test_delete_saved_base_cv}.py`, `tests/integration/retention/
  test_erase_account.py`, `tests/integration/identity/{test_resolve_existing_user,
  test_delete_own_account}.py` (T9, slice 2.2 — AC-7…AC-12). `FakeBaseCvRepository.list_for_user`/
  `count_for_user`/`save_label`/`remove` and `FakeUserRepository` already existed (T7/T13); this
  commit's additions are `InMemoryFileStore.get` raising `StoredFileMissing` instead of a bare
  `KeyError` on a missing key, `delete_calls`/`repo_size_at_delete`/`fail_delete`/`fail_delete_keys`
  on the same class (AC-10's order and failure assertions, and `EraseAccount`'s per-key unlink
  failures), `RecordingPasswordHasher.verify_raises` (`DeleteOwnAccount`'s AC-12 "a
  `PasswordHashingFailed` propagates exactly" test) and `FakeAccountDataPort` (`EraseAccount`'s
  `AccountDataPort`, `domain/retention/ports.py`'s three methods reproduced rather than stubbed —
  see its own docstring for `race_delete_account_result`, the one behaviour `files_by_user` alone
  cannot model).
- Slice 2.3's application tests (T10 — AC-7…AC-15): `InMemoryTailoringHistoryQuery`
  (`ListTailoringHistory`), `RecordingHistoryEntryData` (`EraseHistoryEntry`), the test-only
  `discard` on the run, export-job and posting fakes (the rows those two model going), and
  `FakeAccountDataPort`'s `export_files_by_user` / `history_by_user` (`EraseAccount`'s widened
  report). Every addition is backward-compatible: 2.2's callers pass none of the new arguments.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import NamedTuple, Protocol
from uuid import UUID, uuid4

from tailorcraft.domain.export.errors import (
    DocumentRenderFailed,
    ExportJobConcurrentlyModified,
    ExportJobNotFound,
    ExportNotQueued,
)
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobId, ExportJobStatus
from tailorcraft.domain.identity.claim import ClaimedGuestWork
from tailorcraft.domain.identity.errors import (
    AccessTokenInvalid,
    EmailAlreadyRegistered,
    GuestSessionNotFound,
    LoginConcurrentlyRotated,
    UserNotFound,
)
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    AccessTokenRefusal,
    EmailAddress,
    GuestSessionId,
    IssuedAccessToken,
    LoginId,
    Password,
    PasswordHash,
    PasswordVerdict,
    RetiredRefreshToken,
    TokenHash,
    UserId,
)
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import BaseCvNotFound, CvExtractionFailed
from tailorcraft.domain.intake.saved_base_cv_summary import SavedBaseCvSummary
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType, ExtractedText
from tailorcraft.domain.posting.errors import JobPostingFetchFailed, JobPostingNotFound
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import (
    FetchedPosting,
    JobPostingId,
    JobPostingText,
    SourceUrl,
)
from tailorcraft.domain.retention.errors import AccountNotFound
from tailorcraft.domain.retention.value_objects import AccountCounts, DeletedHistoryEntry
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable, StoredFileMissing
from tailorcraft.domain.tailoring.errors import (
    TailoringFailed,
    TailoringNotQueued,
    TailoringRunConcurrentlyModified,
    TailoringRunNotFound,
)
from tailorcraft.domain.tailoring.history import (
    HistoryBaseCv,
    HistoryCursor,
    HistoryPage,
    HistoryPageSize,
    HistoryPosting,
    TailoringHistoryEntry,
)
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    TailoredDocumentKind,
    TailoredDraft,
    TailoringRunId,
    TailoringRunStatus,
)
from tailorcraft.infrastructure.clock import FixedClock

# A `NULL`-`started_at` stand-in for `FakeTailoringRunRepository.list_stale_running`'s sort key:
# older than any real instant, so a `None` sorts first exactly as `NULLS FIRST` would in SQL.
_EPOCH = datetime.min.replace(tzinfo=UTC)


# --- Fakes -------------------------------------------------------------------------------------


class FakeBaseCvRepository:
    """In-memory `BaseCvRepository`."""

    def __init__(self) -> None:
        self._by_id: dict[BaseCvId, BaseCv] = {}

    def next_identity(self) -> BaseCvId:
        return BaseCvId(value=uuid4())

    async def add(self, cv: BaseCv) -> None:
        self._by_id[cv.id] = cv

    async def get(self, cv_id: BaseCvId) -> BaseCv:
        try:
            return self._by_id[cv_id]
        except KeyError:
            raise BaseCvNotFound(str(cv_id)) from None

    async def list_for_session(self, sid: GuestSessionId) -> Sequence[BaseCv]:
        return [cv for cv in self._by_id.values() if cv.owner == GuestOwner(sid)]

    async def count_for_session(self, sid: GuestSessionId) -> int:
        return len(await self.list_for_session(sid))

    async def list_for_user(self, uid: UserId) -> Sequence[SavedBaseCvSummary]:
        """Mirrors the real adapter's contract (T13b): a `SavedBaseCvSummary` per aggregate, never
        the aggregate itself, newest first. `character_count` comes from `ExtractedText.
        character_count` — the same code-point count the real adapter's `char_length` computes in
        SQL — and is `None` whenever there is no extracted text to count."""
        matches = [cv for cv in self._by_id.values() if cv.owner == UserOwner(uid)]
        newest_first = sorted(matches, key=lambda cv: cv.uploaded_at, reverse=True)
        return [
            SavedBaseCvSummary(
                id=cv.id,
                label=cv.label,
                original_filename=cv.original_filename,
                content_type=cv.content_type,
                size_bytes=cv.size_bytes,
                status=cv.status,
                character_count=(
                    cv.extracted_text.character_count if cv.extracted_text is not None else None
                ),
                failure_reason=cv.failure_reason,
                uploaded_at=cv.uploaded_at,
            )
            for cv in newest_first
        ]

    async def count_for_user(self, uid: UserId) -> int:
        return len(await self.list_for_user(uid))

    async def save_label(self, cv: BaseCv) -> None:
        if cv.id not in self._by_id:
            raise BaseCvNotFound(str(cv.id))
        self._by_id[cv.id] = cv

    async def remove(self, cv_id: BaseCvId, owner: UserOwner) -> None:
        existing = self._by_id.get(cv_id)
        if existing is None or existing.owner != owner:
            raise BaseCvNotFound(str(cv_id))
        del self._by_id[cv_id]

    def all(self) -> list[BaseCv]:
        """Test-only inspection, not part of `BaseCvRepository`."""
        return list(self._by_id.values())


class FakeGuestSessionRepository:
    """In-memory `GuestSessionRepository`."""

    def __init__(self) -> None:
        self._by_id: dict[GuestSessionId, GuestSession] = {}

    def next_identity(self) -> GuestSessionId:
        return GuestSessionId(value=uuid4())

    async def add(self, session: GuestSession) -> None:
        self._by_id[session.id] = session

    async def get(self, session_id: GuestSessionId) -> GuestSession:
        try:
            return self._by_id[session_id]
        except KeyError:
            raise GuestSessionNotFound(str(session_id)) from None

    async def find_by_token_hash(self, token_hash: str) -> GuestSession | None:
        for session in self._by_id.values():
            if session.token_hash == token_hash:
                return session
        return None


class FakeUserRepository:
    """In-memory `UserRepository` (slice 2.1, T13).

    **`add` is the uniqueness check** (technical plan §0.4, I-5, I-6): it raises
    `EmailAlreadyRegistered` for a second user whose *normalized* email matches an existing one —
    never a `find_by_email` first, mirroring the real unique index rather than a look-up-then-insert
    race a fake could get away with pretending is safe.
    """

    def __init__(self) -> None:
        self._by_id: dict[UserId, User] = {}

    def next_identity(self) -> UserId:
        return UserId(value=uuid4())

    async def add(self, user: User) -> None:
        for existing in self._by_id.values():
            if existing.email == user.email:
                raise EmailAlreadyRegistered()
        self._by_id[user.id] = user

    async def get(self, user_id: UserId) -> User:
        try:
            return self._by_id[user_id]
        except KeyError:
            raise UserNotFound(str(user_id)) from None

    async def find_by_email(self, email: EmailAddress) -> User | None:
        for user in self._by_id.values():
            if user.email == email:
                return user
        return None

    async def save(self, user: User) -> None:
        self._by_id[user.id] = user

    async def get_for_update(self, user_id: UserId) -> User:
        """`get`; a single-threaded fake has no row lock to take (slice 2.5, T11)."""
        return await self.get(user_id)

    async def confirm_credential_unchanged(self, user_id: UserId, seen: PasswordHash) -> bool:
        """True iff the stored hash is still exactly `seen`; False when the user is gone. Faithful,
        so a test can make it answer False by saving a reset before the call (slice 2.5, T11)."""
        user = self._by_id.get(user_id)
        return user is not None and user.password_hash == seen

    def all(self) -> list[User]:
        """Test-only inspection, not part of `UserRepository`."""
        return list(self._by_id.values())


class FakeLoginRepository:
    """In-memory `LoginRepository` (slice 2.1, T13). **Revocation is deletion** (ADR-0020): `remove`
    is idempotent — popping an id already gone is success, never an error, exactly like the real
    port's contract for a logout racing a reuse revocation (AC-11).

    `conflict_on_save_rotation` mirrors `FakeTailoringRunRepository.conflict_on_save` above: a
    repository built with `conflict_on_save_rotation=N` raises `LoginConcurrentlyRotated` on its next
    `N` calls to `save_rotation`, decrementing each time, then reverts to its ordinary behaviour —
    the in-memory stand-in for two concurrent rotations racing the real `version` column (I-25),
    without this fake pretending to reimplement `UPDATE … WHERE version = :v` honestly (that truth
    is T31's, against real Postgres). Genuinely honoured for free, because it costs nothing to be
    honest about: a `retired` token already present at that hash is refused the same way a second
    `INSERT` at one primary key really would be.
    """

    def __init__(self, *, conflict_on_save_rotation: int = 0) -> None:
        self._by_id: dict[LoginId, Login] = {}
        self._retired_by_hash: dict[TokenHash, tuple[LoginId, int]] = {}
        self._conflict_on_save_rotation = conflict_on_save_rotation
        self.removed: list[LoginId] = []

    def next_identity(self) -> LoginId:
        return LoginId(value=uuid4())

    async def add(self, login: Login) -> None:
        self._by_id[login.id] = login

    async def find_by_current_token_hash(self, token_hash: TokenHash) -> Login | None:
        for login in self._by_id.values():
            if login.current_token_hash == token_hash:
                return login
        return None

    async def find_by_retired_token_hash(self, token_hash: TokenHash) -> tuple[Login, int] | None:
        entry = self._retired_by_hash.get(token_hash)
        if entry is None:
            return None
        login_id, generation = entry
        login = self._by_id.get(login_id)
        if login is None:
            return None
        return login, generation

    async def save_rotation(self, login: Login, retired: RetiredRefreshToken) -> None:
        if self._conflict_on_save_rotation > 0:
            self._conflict_on_save_rotation -= 1
            raise LoginConcurrentlyRotated()
        if retired.token_hash in self._retired_by_hash:
            raise LoginConcurrentlyRotated()
        self._by_id[login.id] = login
        self._retired_by_hash[retired.token_hash] = (login.id, retired.generation)

    async def remove(self, login_id: LoginId) -> None:
        self._by_id.pop(login_id, None)
        self.removed.append(login_id)

    async def count_all(self) -> int:
        return len(self._by_id)

    async def remove_all(self) -> int:
        count = len(self._by_id)
        self._by_id.clear()
        return count

    async def remove_all_for_user(self, user_id: UserId) -> int:
        """Delete every login of `user_id` and its retired tokens; another user's are untouched."""
        gone = [login_id for login_id, login in self._by_id.items() if login.user_id == user_id]
        for login_id in gone:
            del self._by_id[login_id]
        self._retired_by_hash = {
            h: entry for h, entry in self._retired_by_hash.items() if entry[0] not in gone
        }
        return len(gone)

    def all(self) -> list[Login]:
        """Test-only inspection, not part of `LoginRepository`."""
        return list(self._by_id.values())


class RecordingPasswordHasher:
    """In-memory `PasswordHasherPort` (slice 2.1, T13). Records every call instead of hashing
    anything real — AC-8/AC-9's assertions ("zero hasher calls", "exactly one `verify(password,
    None)` call") are call-log assertions, not cryptographic ones; a real argon2 adapter earns its
    own tests at T25.

    One instance per test, configured with the `verify` outcome that test is about — `FakeLlm`'s
    shape, one instance and one configured outcome. `hash_calls` and `verify_calls` are full argument
    logs, not counts: AC-9 needs to assert `against is None` on the unknown-email path and that the
    wrong-password path was verified against the real stored hash, which a count cannot distinguish.

    `verify_raises` (T9, slice 2.2): when set, `verify` raises it instead of returning
    `verify_result` — `PasswordHasherPort`'s own floor (`PasswordHashingFailed`), for
    `DeleteOwnAccount`'s AC-12 "propagates exactly, nothing deleted" test. The call is still
    recorded first, so a test can tell "the hasher was asked and then failed" from "never asked".
    """

    def __init__(
        self,
        *,
        verify_result: PasswordVerdict = PasswordVerdict.MATCH,
        hash_result: PasswordHash | None = None,
        verify_raises: Exception | None = None,
    ) -> None:
        self._verify_result = verify_result
        self._hash_result = hash_result or PasswordHash(
            value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZWhhc2g"
        )
        self._verify_raises = verify_raises
        self.hash_calls: list[Password] = []
        self.verify_calls: list[tuple[Password, PasswordHash | None]] = []

    async def hash(self, password: Password) -> PasswordHash:
        self.hash_calls.append(password)
        return self._hash_result

    async def verify(self, password: Password, against: PasswordHash | None) -> PasswordVerdict:
        self.verify_calls.append((password, against))
        if self._verify_raises is not None:
            raise self._verify_raises
        return self._verify_result


class FakeAccessTokenPort:
    """In-memory `AccessTokenPort` (slice 2.1, T13). `issue` mints a distinct token string per call
    and records `(user_id, at)`; `verify` looks up the id that was returned for that exact string,
    refusing an unrecognized one. None of T13's six use cases call `verify` — only `issue`, from
    `RegisterUser`, `LogIn` and `RefreshLogin` — but the fake still needs a working `verify` to
    satisfy `AccessTokenPort`'s `Protocol` under `mypy --strict`; the real adapter's own decode/claim
    rules (I-32 … I-40) are T25's.
    """

    def __init__(self, *, lifetime: timedelta = timedelta(minutes=15)) -> None:
        self._lifetime = lifetime
        self._issued: dict[str, UserId] = {}
        self._counter = 0
        self.issue_calls: list[tuple[UserId, datetime]] = []

    def issue(self, user_id: UserId, at: datetime) -> IssuedAccessToken:
        self.issue_calls.append((user_id, at))
        self._counter += 1
        token = f"fake-access-token-{self._counter}"
        self._issued[token] = user_id
        return IssuedAccessToken(token=token, expires_in=self._lifetime)

    def verify(self, token: str, at: datetime) -> UserId:
        try:
            return self._issued[token]
        except KeyError:
            raise AccessTokenInvalid(AccessTokenRefusal.MALFORMED) from None


class RecordingFailedLoginObserver:
    """In-memory `FailedLoginObserver` (slice 2.1, T13). Records which of the two methods `LogIn`
    called and with what, so I-9 and I-10 can be told apart at the port even though the
    `InvalidCredentials` it raises carries nothing itself (AC-9)."""

    def __init__(self) -> None:
        self.unknown_email_calls = 0
        self.wrong_password_calls: list[UserId] = []

    def unknown_email(self) -> None:
        self.unknown_email_calls += 1

    def wrong_password(self, user_id: UserId) -> None:
        self.wrong_password_calls.append(user_id)


class CountingClock:
    """Wraps a `FixedClock` and counts `.now()` calls (slice 2.1, T13) — what "one `clock.now()`
    call per use-case call" (technical plan §2) needs and `FixedClock` itself does not track
    (`infrastructure/clock.py`). Seed test fixtures through the wrapped `FixedClock` directly (its
    `.now()` does not count), and pass only this wrapper to the use case under test — otherwise
    seeding inflates the call count the assertion is trying to pin.
    """

    def __init__(self, inner: FixedClock) -> None:
        self._inner = inner
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        return self._inner.now()

    def advance(self, seconds: int) -> None:
        self._inner.advance(seconds)


class FakeJobPostingRepository:
    """In-memory `JobPostingRepository`. Same shape as `FakeBaseCvRepository`, deliberately: the two
    ports are the same shape (`domain/posting/ports.py`'s docstring says so explicitly), so the fakes
    are too."""

    def __init__(self) -> None:
        self._by_id: dict[JobPostingId, JobPosting] = {}

    def next_identity(self) -> JobPostingId:
        return JobPostingId(value=uuid4())

    async def add(self, posting: JobPosting) -> None:
        self._by_id[posting.id] = posting

    async def get(self, posting_id: JobPostingId) -> JobPosting:
        try:
            return self._by_id[posting_id]
        except KeyError:
            raise JobPostingNotFound(str(posting_id)) from None

    async def list_for_session(self, sid: GuestSessionId) -> Sequence[JobPosting]:
        return [posting for posting in self._by_id.values() if posting.owner == GuestOwner(sid)]

    async def count_for_owner(self, owner: Owner) -> int:
        return len([posting for posting in self._by_id.values() if posting.owner == owner])

    async def list_recent_for_user(self, user_id: UserId, limit: int) -> Sequence[JobPosting]:
        postings = [
            posting for posting in self._by_id.values() if posting.owner == UserOwner(user_id)
        ]
        postings.sort(key=lambda posting: posting.created_at, reverse=True)
        return postings[:limit]

    def all(self) -> list[JobPosting]:
        """Test-only inspection, not part of `JobPostingRepository`."""
        return list(self._by_id.values())

    def discard(self, posting_id: JobPostingId) -> None:
        """Test-only removal, not part of `JobPostingRepository` — the row going the way a database
        `DELETE` would take it (a history-entry erasure's third statement, an account cascade), for
        `RecordingHistoryEntryData` below. Idempotent, like the `DELETE` it models."""
        self._by_id.pop(posting_id, None)


class InMemoryFileStore:
    """In-memory `FileStorePort`, backed by a plain dict.

    Optionally takes a `FakeBaseCvRepository` the use case is also given, purely so a test can
    prove the **ordering** the technical plan requires (step 5 before step 6/8, ADR-0006 §2): the
    file is written while the repository is still empty. `repo_size_at_put` snapshots
    `len(repo.all())` at the moment `put` runs, so the assertion is positive ("the repo held zero
    rows when the file landed") rather than only checking the end state.

    **T9 additions (slice 2.2), backward-compatible with every existing caller:**

    - `get` on a missing key now raises `StoredFileMissing` rather than a bare `KeyError` —
      the real port's documented exception is the one a missing key raises, and no existing test
      relies on the old `KeyError` (every prior caller always `put`s
      before it `get`s the same key).
    - `delete_calls` records every `FileRef` handed to `delete`, in order — `DeleteSavedBaseCv`'s
      AC-10 needs to prove `delete` is the *last* thing that happens, never `delete_partial`.
    - `repo_size_at_delete` mirrors `repo_size_at_put`, aimed at `delete` instead of `put`: with
      `repo=cvs`, it snapshots `len(repo.all())` at the moment `delete` is first called, so a test can
      assert the row was already gone from the repository (the count dropped) before the file was
      touched — the same technique, aimed at the other end of AC-10's order.
    - `fail_delete`, optionally scoped to `fail_delete_keys`: `delete` raises this exception instead
      of removing the key, for AC-10's "a failing unlink returns the type name" and AC-11's "some
      unlinks fail" (`EraseAccount`, S-45) — `fail_delete_keys=None` fails every `delete` call;
      naming a subset fails only those keys, leaving the rest to succeed.
    """

    def __init__(
        self,
        repo: FakeBaseCvRepository | None = None,
        *,
        fail_delete: Exception | None = None,
        fail_delete_keys: set[str] | None = None,
    ) -> None:
        self._repo = repo
        self.data: dict[str, bytes] = {}
        self.repo_size_at_put: int | None = None
        self.repo_size_at_delete: int | None = None
        self.delete_calls: list[FileRef] = []
        self.delete_partial_calls: list[FileRef] = []
        self._fail_delete = fail_delete
        self._fail_delete_keys = fail_delete_keys

    async def put(self, ref: FileRef, data: bytes) -> None:
        if self._repo is not None:
            self.repo_size_at_put = len(self._repo.all())
        self.data[ref.key] = data

    async def get(self, ref: FileRef) -> bytes:
        try:
            return self.data[ref.key]
        except KeyError:
            raise StoredFileMissing(ref.key) from None

    async def delete(self, ref: FileRef) -> None:
        self.delete_calls.append(ref)
        if self._repo is not None and self.repo_size_at_delete is None:
            self.repo_size_at_delete = len(self._repo.all())
        if self._fail_delete is not None and (
            self._fail_delete_keys is None or ref.key in self._fail_delete_keys
        ):
            raise self._fail_delete
        self.data.pop(ref.key, None)

    async def delete_partial(self, ref: FileRef) -> None:
        """No test using this fake exercises the orphan sweep's partial branch (that is
        `_RecordingFileStorePort`'s job, below) — this fake has no `.part` concept at all, so the
        method is a no-op rather than a guard-rail `AssertionError`: nothing here claims to cover a
        reached-but-unexpected call, only "this fake cannot express a `.part` file". Recorded in
        `delete_partial_calls` regardless, so `DeleteSavedBaseCv`'s AC-10 ("never `delete_partial` —
        a row's key is always a final key") has something positive to assert `== []` against."""
        self.delete_partial_calls.append(ref)


class AlwaysFailingFileStore:
    """`FileStorePort` that fails every write, simulating F-14 (`ENOSPC` / `EACCES`).

    `get` raises `FileStoreUnavailable` too — added in this commit (T6) for
    `DownloadExportFile`'s X-48 (`EIO`, permissions: the store answers, but not with the file).
    Before 1.5 nothing in this codebase ever called `get` at all (`StoredFileMissing`'s own
    docstring says so), so widening it from the original guard-rail `AssertionError` changes no
    existing test's behaviour — 1.1's upload test never reaches this method either way. `delete`
    keeps the `AssertionError`: nothing here needs it to fail, and it stays a guard against a test
    reaching a method it does not claim to cover.
    """

    async def put(self, ref: FileRef, data: bytes) -> None:
        raise FileStoreUnavailable("simulated storage failure")

    async def get(self, ref: FileRef) -> bytes:
        raise FileStoreUnavailable("simulated storage failure")

    async def delete(self, ref: FileRef) -> None:
        raise AssertionError("delete() should not be reached in this scenario")

    async def delete_partial(self, ref: FileRef) -> None:
        raise AssertionError("delete_partial() should not be reached in this scenario")


class FakeExtractor:
    """`CvTextExtractorPort` that either returns a fixed `ExtractedText` or raises a fixed
    `CvExtractionFailed` — one instance per test, configured with exactly the outcome that test is
    about."""

    def __init__(self, outcome: ExtractedText | CvExtractionFailed) -> None:
        self._outcome = outcome

    async def extract(self, content_type: CvContentType, data: bytes) -> ExtractedText:
        if isinstance(self._outcome, CvExtractionFailed):
            raise self._outcome
        return self._outcome


class FakeJobPostingFetcher:
    """`JobPostingFetcherPort` that either returns a fixed `FetchedPosting` or raises a fixed
    `JobPostingFetchFailed` — one instance per test, configured with exactly the outcome that test
    is about. Mirrors `FakeExtractor`'s shape, with one addition: `calls` counts invocations, so a
    paste-path test can assert the fetcher was never reached (`fetcher.calls == 0`) rather than only
    trusting that a value it never used happened not to matter."""

    def __init__(self, outcome: FetchedPosting | JobPostingFetchFailed) -> None:
        self._outcome = outcome
        self.calls = 0

    async def fetch(self, url: SourceUrl) -> FetchedPosting:
        self.calls += 1
        if isinstance(self._outcome, JobPostingFetchFailed):
            raise self._outcome
        return self._outcome


class FakeTailoringRunRepository:
    """In-memory `TailoringRunRepository`.

    Same shape as `FakeBaseCvRepository` and `FakeJobPostingRepository` — `next_identity`, `add`,
    `get`-raises, `list_for_session`, `count_for_session` — extended with the two methods this port
    adds because `TailoringRun` is the codebase's first aggregate that is loaded, mutated and
    persisted by a second process: `save` (an unconditional overwrite here, same as `add`, since an
    in-memory dict has no concept of "the row already existed") and `find` (the worker's
    `get`-returns-`None` counterpart, so a fake exercising `ExecuteTailoringRun` can hand back
    `MISSING` for a purged id without raising).

    `save_calls` records the `status` recorded by every `save()` call, in order — added for T12's
    "`running` is committed before the LLM is called" test (technical-plan.md's "Step 4 — two
    commits"). That test needs to observe the repository's state at the exact moment `LlmPort.tailor`
    is invoked (via `FakeLlm.on_call`, below), and a plain end-state assertion cannot do that: by the
    time the use case returns, `save` has already been called again with the terminal outcome, so
    only a call log — not the current row — can prove `running` was saved *before* the model was
    ever asked. A count alone would not do either, since `1` is consistent with "saved before the
    call" and "saved after, coincidentally also once"; recording the status distinguishes them.

    `list_stale_running`, added for V5b (the stale-run sweep, G-25'), is now a member of
    `TailoringRunRepository` (`domain/tailoring/ports.py`, GREEN, V5c) — it landed on the Protocol
    once every implementer had it, and this class was its first implementation, ahead of the SQL
    adapter (`infrastructure/persistence/repositories/tailoring/tailoring_run.py`). The contract,
    matching the SQL adapter's: `RUNNING` rows whose `started_at` is before `started_before`, **or**
    `started_at is None` — the same fold `TailoringRun.is_stale` makes, expressed as a filter —
    oldest first with a `NULL` counted as oldest (`NULLS FIRST`), ties on `started_at` broken by id,
    at most `limit`, no locking.

    `conflict_on_save` (T6, ADR-0015 §3) is the in-memory stand-in for the database race the mapping
    reports as `StaleDataError` once `version_id_col` is wired: a repository built with
    `conflict_on_save=N` raises `TailoringRunConcurrentlyModified` on its next `N` calls to `save`,
    decrementing on each raise, then behaves exactly as before. It says nothing about *which* run —
    the real adapter's `WHERE version = :loaded` can't target one either, it fails whichever `UPDATE`
    it is asked to run next — so a test needing a conflict on one specific run out of several (AC-9)
    composes a small local subclass instead, rather than stretching this counter to do a job it
    cannot honestly do.

    `saved` (T6) records every run a *successful* `save` persisted, in order — as distinct from
    `save_calls`, which records only the `status` of each successful save. A rejection-path test
    (E-5 … E-9) asserts `saved == []` without having to know whether `save` was even reached.
    """

    def __init__(self, conflict_on_save: int = 0) -> None:
        self._by_id: dict[TailoringRunId, TailoringRun] = {}
        self.save_calls: list[TailoringRunStatus] = []
        self.saved: list[TailoringRun] = []
        self._conflict_on_save = conflict_on_save

    def next_identity(self) -> TailoringRunId:
        return TailoringRunId(value=uuid4())

    async def add(self, run: TailoringRun) -> None:
        self._by_id[run.id] = run

    async def save(self, run: TailoringRun) -> None:
        if self._conflict_on_save > 0:
            self._conflict_on_save -= 1
            raise TailoringRunConcurrentlyModified(run.id)
        self.save_calls.append(run.status)
        self.saved.append(run)
        self._by_id[run.id] = run

    async def get(self, run_id: TailoringRunId) -> TailoringRun:
        try:
            return self._by_id[run_id]
        except KeyError:
            raise TailoringRunNotFound(str(run_id)) from None

    async def find(self, run_id: TailoringRunId) -> TailoringRun | None:
        return self._by_id.get(run_id)

    async def list_for_session(self, sid: GuestSessionId) -> Sequence[TailoringRun]:
        runs = [run for run in self._by_id.values() if run.owner == GuestOwner(sid)]
        return sorted(runs, key=lambda run: run.requested_at, reverse=True)

    async def count_for_owner(self, owner: Owner) -> int:
        return len([run for run in self._by_id.values() if run.owner == owner])

    async def find_active_for_owner(self, owner: Owner) -> TailoringRun | None:
        active_statuses = (TailoringRunStatus.QUEUED, TailoringRunStatus.RUNNING)
        for run in self._by_id.values():
            if run.owner == owner and run.status in active_statuses:
                return run
        return None

    async def list_stale_running(
        self, started_before: datetime, limit: int
    ) -> Sequence[TailoringRun]:
        """`AbandonStaleTailoringRuns`' lookup (V5b, G-25'). See the class docstring for the
        contract this implements — the same one `TailoringRunRepository.list_stale_running` now
        states on the Protocol itself."""
        candidates = [
            run
            for run in self._by_id.values()
            if run.status is TailoringRunStatus.RUNNING
            and (run.started_at is None or run.started_at < started_before)
        ]

        def _sort_key(run: TailoringRun) -> tuple[bool, datetime, UUID]:
            # `started_at is None` sorts first (`False < True`); ties on `started_at` break on id.
            return (run.started_at is not None, run.started_at or _EPOCH, run.id.value)

        candidates.sort(key=_sort_key)
        return candidates[:limit]

    def all(self) -> list[TailoringRun]:
        """Test-only inspection, not part of `TailoringRunRepository`."""
        return list(self._by_id.values())

    def discard(self, run_id: TailoringRunId) -> None:
        """Test-only removal, not part of `TailoringRunRepository` (which has no delete on purpose —
        a history entry is erased through retention's port, technical plan §0.6). Models the row
        going under a Core `DELETE`, for `RecordingHistoryEntryData` and for tests that need a run
        to vanish between two reads. Idempotent."""
        self._by_id.pop(run_id, None)


class FakeLlm:
    """`LlmPort` that either returns a fixed `TailoredDraft` or raises a fixed `TailoringFailed` —
    one instance per test, configured with exactly the outcome that test is about. Same constructor
    shape as `FakeExtractor` and `FakeJobPostingFetcher` on purpose, so all three fakes read alike.

    `calls` records the exact `(cv, posting)` argument pairs `tailor` was invoked with, not merely a
    count: that is what lets a test count attempts (AC-8/AC-10) and assert the port received the CV
    text and the posting text **and nothing else** (AC-24) — a count alone could not distinguish
    "called once with the right text" from "called once with someone else's".

    `delay_seconds` sleeps before producing the configured outcome, on every call. It is not used by
    `RequestTailoringRun`'s tests (this port is never reached from there) but exists here rather than
    being bolted on later, so that `ExecuteTailoringRun`'s per-attempt and total-deadline timeout
    tests can drive this fake past a configured budget without a real network call.

    `on_call`, added for T12's "`running` is committed before the LLM is called" test, is a
    synchronous hook invoked the instant `tailor()` starts — before the delay, before the outcome is
    produced or raised. A test wires it to snapshot `FakeTailoringRunRepository.save_calls` at that
    exact moment, which is what turns "was the run already saved as `running` when the model was
    asked?" into a plain list-equality assertion rather than a guess based on the end state.

    `aclose` is a recording no-op, added at verify-round-1 (V4): `tasks/container.py::
    tailoring_use_case` now closes whatever `GeminiLlm(settings)` produced in its own `finally`, and
    every test that monkeypatches that factory to return a `FakeLlm` runs that same `finally` — so
    the fake needs the method just to keep those tests from raising `AttributeError`, and recording
    that it ran is what lets a test assert the container actually closed its adapter rather than
    merely surviving the call. Not on `LlmPort` itself: adapter lifecycle is not domain language, and
    a port that grew `aclose` would make every fake implement a lifecycle the business model has no
    word for (`GeminiLlm.aclose`'s own docstring).
    """

    def __init__(
        self,
        outcome: TailoredDraft | TailoringFailed,
        *,
        delay_seconds: float = 0.0,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self._outcome = outcome
        self._delay_seconds = delay_seconds
        self._on_call = on_call
        self.calls: list[tuple[ExtractedText, JobPostingText]] = []
        self.closed = False

    async def tailor(self, cv: ExtractedText, posting: JobPostingText) -> TailoredDraft:
        self.calls.append((cv, posting))
        if self._on_call is not None:
            self._on_call()
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if isinstance(self._outcome, TailoringFailed):
            raise self._outcome
        return self._outcome

    async def aclose(self) -> None:
        self.closed = True


class FakeTailoringQueue:
    """`TailoringQueuePort` that records every id it was asked to enqueue, or raises a fixed
    `TailoringNotQueued` instead — one instance per test, configured with exactly the outcome that
    test is about, mirroring `FakeExtractor`'s and `FakeJobPostingFetcher`'s shape. The default
    (`outcome=None`) is the broker accepting the publish; passing a `TailoringNotQueued` instance is
    G-14's broker-down variant.

    Not exercised by `RequestTailoringRun`'s tests — that use case never enqueues, per its own
    docstring — but added in this commit for the same reason `FakeLlm` is: T12 and the T29 API tests
    need it, and this is where a fake belongs so it never grows a second copy.
    """

    def __init__(self, outcome: TailoringNotQueued | None = None) -> None:
        self._outcome = outcome
        self.enqueued: list[TailoringRunId] = []

    async def enqueue(self, run_id: TailoringRunId) -> None:
        if self._outcome is not None:
            raise self._outcome
        self.enqueued.append(run_id)


class FakeExportJobRepository:
    """In-memory `ExportJobRepository`.

    `FakeTailoringRunRepository`'s shape (`next_identity`, `add`, `get`-raises, `find`-returns-
    `None`, `conflict_on_save`, `saved`, `.all()`), for the identical reason: `ExportJob` is the
    second aggregate in this codebase loaded, mutated and re-saved by a second process (the worker),
    so it needs the same optimistic-concurrency stand-in. `conflict_on_save` (T6, ADR-0015 §3
    applied a second time) raises `ExportJobConcurrentlyModified` on its next `N` calls to `save`,
    decrementing each time, then behaves normally — the in-memory version of the `StaleDataError` a
    real `version_id_col` mismatch reports.

    Three methods have no counterpart in `FakeTailoringRunRepository`, because `ExportJobRepository`
    is the first port in this codebase with lookups keyed on something other than an id alone:

    - `list_for_run` — every job for a run, **newest first** (`requested_at` descending, ties
      broken by id descending — the same total order `find_latest_for_key` needs for the identical
      reason, since the whole-second `Clock` makes ties ordinary rather than rare).
    - `find_latest_for_key` — the most recent job for a (run, document, format) triple, or `None`.
      `RequestExport`'s idempotency check (X-16, X-17).
    - `list_stale_rendering` — `RENDERING` jobs whose `started_at` is before `started_before`, **or**
      has no `started_at` at all (the same `NULLS FIRST` fold `ExportJob.is_stale` makes, expressed
      as a filter), oldest first, ties broken by id, bounded by `limit`. `AbandonStaleExportJobs`'s
      lookup (X-29).
    """

    def __init__(self, conflict_on_save: int = 0) -> None:
        self._by_id: dict[ExportJobId, ExportJob] = {}
        self.saved: list[ExportJob] = []
        self._conflict_on_save = conflict_on_save

    def next_identity(self) -> ExportJobId:
        return ExportJobId(value=uuid4())

    async def add(self, job: ExportJob) -> None:
        self._by_id[job.id] = job

    async def save(self, job: ExportJob) -> None:
        if self._conflict_on_save > 0:
            self._conflict_on_save -= 1
            raise ExportJobConcurrentlyModified(job.id)
        self.saved.append(job)
        self._by_id[job.id] = job

    async def get(self, job_id: ExportJobId) -> ExportJob:
        try:
            return self._by_id[job_id]
        except KeyError:
            raise ExportJobNotFound(str(job_id)) from None

    async def find(self, job_id: ExportJobId) -> ExportJob | None:
        return self._by_id.get(job_id)

    async def list_for_run(self, run_id: TailoringRunId) -> Sequence[ExportJob]:
        jobs = [job for job in self._by_id.values() if job.tailoring_run_id == run_id]
        jobs.sort(key=lambda job: (job.requested_at, job.id.value), reverse=True)
        return jobs

    async def count_for_run(self, run_id: TailoringRunId) -> int:
        return len([job for job in self._by_id.values() if job.tailoring_run_id == run_id])

    async def find_latest_for_key(
        self, run_id: TailoringRunId, document: TailoredDocumentKind, format: ExportFormat
    ) -> ExportJob | None:
        candidates = [
            job
            for job in self._by_id.values()
            if job.tailoring_run_id == run_id and job.document == document and job.format == format
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda job: (job.requested_at, job.id.value), reverse=True)
        return candidates[0]

    async def count_for_session(self, sid: GuestSessionId) -> int:
        return len([job for job in self._by_id.values() if job.owner == GuestOwner(sid)])

    async def list_stale_rendering(
        self, started_before: datetime, limit: int
    ) -> Sequence[ExportJob]:
        candidates = [
            job
            for job in self._by_id.values()
            if job.status is ExportJobStatus.RENDERING
            and (job.started_at is None or job.started_at < started_before)
        ]

        def _sort_key(job: ExportJob) -> tuple[bool, datetime, UUID]:
            # `started_at is None` sorts first (`False < True`); ties on `started_at` break on id.
            return (job.started_at is not None, job.started_at or _EPOCH, job.id.value)

        candidates.sort(key=_sort_key)
        return candidates[:limit]

    def all(self) -> list[ExportJob]:
        """Test-only inspection, not part of `ExportJobRepository`."""
        return list(self._by_id.values())

    def discard(self, job_id: ExportJobId) -> None:
        """Test-only removal, not part of `ExportJobRepository` — `FakeTailoringRunRepository.
        discard`'s reason, for `RecordingHistoryEntryData`. Idempotent."""
        self._by_id.pop(job_id, None)


class FakeDocumentRenderer:
    """`DocumentRendererPort` that either returns fixed bytes or raises a fixed
    `DocumentRenderFailed`, mirroring `FakeLlm`'s and `FakeExtractor`'s shape: one instance,
    configured with exactly the outcome a test is about.

    `calls` records the exact `(markdown, document, format)` triple `render` was invoked with — not
    merely a count — because `render`'s two keyword-only parameters (`document`, `format`) are the
    two facts a test needs to assert the use case selected the right source and asked for the right
    format, the same argument `FakeLlm.calls` makes for `(cv, posting)`.

    `delay_seconds` sleeps before producing the outcome, on every call — present for the same reason
    `FakeLlm.delay_seconds` is, even though no T6 test drives it past a budget: a later timeout test
    needs it and this is where it must live to avoid a second, drifting copy of this fake.
    """

    def __init__(
        self,
        outcome: bytes | DocumentRenderFailed,
        *,
        delay_seconds: float = 0.0,
    ) -> None:
        self._outcome = outcome
        self._delay_seconds = delay_seconds
        self.calls: list[tuple[str, TailoredDocumentKind, ExportFormat]] = []

    async def render(
        self, markdown: str, *, document: TailoredDocumentKind, format: ExportFormat
    ) -> bytes:
        self.calls.append((markdown, document, format))
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if isinstance(self._outcome, DocumentRenderFailed):
            raise self._outcome
        return self._outcome


class FakeExportQueue:
    """`ExportQueuePort` that records every id it was asked to enqueue, or raises a fixed
    `ExportNotQueued` instead — one instance per test, configured with exactly the outcome that test
    is about, mirroring `FakeTailoringQueue`'s shape exactly (the default `outcome=None` is the
    broker accepting the publish; passing an `ExportNotQueued` instance is X-22's broker-down
    variant).

    Not exercised by any test in this commit — `RequestExport` never enqueues, per its own docstring,
    and no other T6 use case reaches this port — but added here now for `FakeTailoringQueue`'s own
    reason: the HTTP-contract tests (I17) need it, and this is where a fake belongs so it never grows
    a second, drifting copy.
    """

    def __init__(self, outcome: ExportNotQueued | None = None) -> None:
        self._outcome = outcome
        self.enqueued: list[ExportJobId] = []

    async def enqueue(self, job_id: ExportJobId) -> None:
        if self._outcome is not None:
            raise self._outcome
        self.enqueued.append(job_id)


class MissingFileStore:
    """`FileStorePort` whose `get` always raises `StoredFileMissing` — the file a `ready` row names
    has been deleted from under it (X-47). `put` and `delete` raise `AssertionError`, the
    `AlwaysFailingFileStore` convention: this fake exists for `DownloadExportFile`'s read path only,
    and a test reaching either write method has drifted from what it claims to cover.
    """

    async def put(self, ref: FileRef, data: bytes) -> None:
        raise AssertionError("put() should not be reached in this scenario")

    async def get(self, ref: FileRef) -> bytes:
        raise StoredFileMissing("simulated missing file")

    async def delete(self, ref: FileRef) -> None:
        raise AssertionError("delete() should not be reached in this scenario")

    async def delete_partial(self, ref: FileRef) -> None:
        raise AssertionError("delete_partial() should not be reached in this scenario")


class AccountHistory(NamedTuple):
    """What an account's tailoring history holds, for `FakeAccountDataPort` (slice 2.3, AC-15):
    the rows the erasure's cascade takes that name no file of their own. The export jobs are not
    here — each one names a file, so the fake counts them from `export_files_by_user` instead, and
    the two can never disagree."""

    tailoring_runs: int
    job_postings: int


class FakeAccountDataPort:
    """In-memory `AccountDataPort` (slice 2.2, T9; widened in slice 2.3, T10) — `EraseAccount`'s two
    reads and one write, `domain/retention/ports.py`'s shape reproduced honestly rather than reduced
    to a stub.

    Seeded with `files_by_user`: every account this fake knows about, and its **saved base CVs'**
    `FileRef`s. An id **not** in that mapping, or one already erased, raises `AccountNotFound` from
    `files_of_account` — the ordinary "unknown or already-gone account" case, and what makes **a
    second `EraseAccount(user_id)` call for the same user** raise `AccountNotFound` (AC-11): the first
    call's `delete_account` marks the id erased, and the second call's `files_of_account` sees that
    and refuses before anything else runs.

    **Slice 2.3 (AC-15).** `export_files_by_user` holds each account's export files — one per export
    job, since every job has a queued format and its key is derived from `(id, format)` — and
    `history_by_user` its tailoring runs and job postings. `files_of_account` returns the CV keys
    **and** the export keys, as the widened port docstring says; `count_account` answers every field
    of the widened `AccountCounts` from the same data, so a use case may read its counts from either
    method and get one consistent story. Both new arguments default to "no history", which is what
    keeps 2.2's callers meaning exactly what they meant.

    `files_of_account_calls` / `delete_account_calls` are ordered call logs (not just counts) — what
    AC-11's "`files_of_account` before `delete_account`, and every unlink after `delete_account`
    returns" ordering assertion needs.

    `race_delete_account_result`, set once, makes the **next** `delete_account` call return that
    value regardless of the mapping — the narrower race `EraseAccount`'s own docstring names
    separately from "called twice": `files_of_account` already succeeded (a stale-but-true read) and
    then a concurrent erasure's `delete_account` wins first, so this account's own `delete_account`
    must return `False` without this fake's ordinary "already erased" bookkeeping ever having reason
    to fire. Consumed on use, so it affects exactly one call.
    """

    def __init__(
        self,
        files_by_user: dict[UserId, Sequence[FileRef]] | None = None,
        *,
        logins_by_user: dict[UserId, int] | None = None,
        export_files_by_user: dict[UserId, Sequence[FileRef]] | None = None,
        history_by_user: dict[UserId, AccountHistory] | None = None,
    ) -> None:
        self._files_by_user = dict(files_by_user or {})
        self._logins_by_user = dict(logins_by_user or {})
        self._export_files_by_user = dict(export_files_by_user or {})
        self._history_by_user = dict(history_by_user or {})
        self._erased: set[UserId] = set()
        self._race_delete_account_result: bool | None = None
        self.files_of_account_calls: list[UserId] = []
        self.delete_account_calls: list[UserId] = []

    def force_next_delete_account_result(self, result: bool) -> None:
        """Arms the one-shot race override `delete_account` consumes on its next call."""
        self._race_delete_account_result = result

    def _exists(self, user_id: UserId) -> bool:
        return user_id in self._files_by_user and user_id not in self._erased

    async def files_of_account(self, user_id: UserId) -> Sequence[FileRef]:
        self.files_of_account_calls.append(user_id)
        if not self._exists(user_id):
            raise AccountNotFound(str(user_id))
        return [*self._files_by_user[user_id], *self._export_files_by_user.get(user_id, ())]

    async def delete_account(self, user_id: UserId) -> bool:
        self.delete_account_calls.append(user_id)
        if self._race_delete_account_result is not None:
            result = self._race_delete_account_result
            self._race_delete_account_result = None
            return result
        if not self._exists(user_id):
            return False
        self._erased.add(user_id)
        return True

    async def count_account(self, user_id: UserId) -> AccountCounts | None:
        if not self._exists(user_id):
            return None
        cv_files = self._files_by_user[user_id]
        export_files = self._export_files_by_user.get(user_id, ())
        history = self._history_by_user.get(user_id, AccountHistory(0, 0))
        return AccountCounts(
            base_cvs=len(cv_files),
            files=len(cv_files) + len(export_files),
            logins=self._logins_by_user.get(user_id, 0),
            tailoring_runs=history.tailoring_runs,
            job_postings=history.job_postings,
            export_jobs=len(export_files),
        )


class InMemoryTailoringHistoryQuery:
    """In-memory `TailoringHistoryQuery` (slice 2.3, T10, AC-13) — technical plan §0.5's statement,
    re-expressed over the three in-memory repositories so `ListTailoringHistory` can be driven
    without a database. **The SQL is T15's and is proven against Postgres in T17**; this double
    exists so the use case's own obligations (resolve the user, pass `after` and `size` through
    unchanged, hand back the page) are observable, and so the history *contract* has one executable
    statement before the adapter exists.

    Faithful to the query clause by clause, because a lenient double would let a use-case bug hide:

    - `WHERE r.user_id = :u` — only `UserOwner(user_id)`'s runs; a guest's run never appears.
    - `ORDER BY requested_at DESC, id DESC` — `UUID` compares by its 128-bit integer, which is
      PostgreSQL's byte-wise `uuid` order, so a same-second tie breaks the way the index does.
    - `(requested_at, id) < (:cursor_at, :cursor_id)` — **strictly** after the cursor.
    - `LIMIT size + 1` — the extra row decides `next_cursor` and is never returned.
    - Both `LEFT JOIN`s carry the owner (`c.user_id = r.user_id`): a CV or posting that is gone,
      **or owned by anyone else**, yields `None` rather than lending its label to this history.
    - `left(p.text, 140)` for the preview; `edited` from the two revision instants.

    `calls` records every `(user_id, after, size)` in order.
    """

    def __init__(
        self,
        runs: FakeTailoringRunRepository,
        cvs: FakeBaseCvRepository,
        postings: FakeJobPostingRepository,
    ) -> None:
        self._runs = runs
        self._cvs = cvs
        self._postings = postings
        self.calls: list[tuple[UserId, HistoryCursor | None, HistoryPageSize]] = []

    async def page_for_user(
        self, user_id: UserId, after: HistoryCursor | None, size: HistoryPageSize
    ) -> HistoryPage:
        self.calls.append((user_id, after, size))
        owner = UserOwner(user_id)
        mine = [run for run in self._runs.all() if run.owner == owner]
        mine.sort(key=lambda run: (run.requested_at, run.id.value), reverse=True)
        if after is not None:
            bound = (after.requested_at, after.tailoring_run_id.value)
            mine = [run for run in mine if (run.requested_at, run.id.value) < bound]
        window = mine[: size.value + 1]
        entries = tuple(self._entry(run) for run in window[: size.value])
        next_cursor = (
            HistoryCursor(
                requested_at=entries[-1].requested_at,
                tailoring_run_id=entries[-1].tailoring_run_id,
            )
            if len(window) > size.value
            else None
        )
        return HistoryPage(entries=entries, next_cursor=next_cursor)

    def _entry(self, run: TailoringRun) -> TailoringHistoryEntry:
        cv = next(
            (c for c in self._cvs.all() if c.id == run.base_cv_id and c.owner == run.owner), None
        )
        posting = next(
            (
                p
                for p in self._postings.all()
                if p.id == run.job_posting_id and p.owner == run.owner
            ),
            None,
        )
        return TailoringHistoryEntry(
            tailoring_run_id=run.id,
            status=run.status,
            failure_reason=run.failure_reason,
            requested_at=run.requested_at,
            completed_at=run.completed_at,
            version=run.version,
            edited=run.cv_edited_at is not None or run.cover_letter_edited_at is not None,
            base_cv_id=run.base_cv_id,
            base_cv=(
                HistoryBaseCv(
                    base_cv_id=cv.id,
                    label=cv.label.value if cv.label is not None else None,
                    original_filename=cv.original_filename.value,
                )
                if cv is not None
                else None
            ),
            posting=(
                HistoryPosting(
                    job_posting_id=posting.id,
                    source=posting.source,
                    title=posting.title.value if posting.title is not None else None,
                    source_url=posting.source_url.value if posting.source_url is not None else None,
                    preview=posting.text.value[:140],
                )
                if posting is not None
                else None
            ),
        )


class RecordingHistoryEntryData:
    """In-memory `HistoryEntryDataPort` (slice 2.3, T10, AC-14) — technical plan §0.6's three
    `DELETE`s re-expressed over the in-memory repositories, so a second call really does find the run
    gone (`GetTailoringRun` then raises `TailoringRunNotFound`) instead of a flag pretending it is.

    1. The run, **only if** `UserOwner(user_id)` owns it; otherwise `None` and nothing else touched
       (the loser of a concurrent deletion, H-44).
    2. The run's export jobs owned by the same user; each job's key comes back **derived**
       (`job.storage_ref`, i.e. `FileRef.for_export(id, format)`) — never read off a `file_key`.
    3. The posting, only if it is the user's and **no remaining run** references it.

    `calls` records every `(user_id, run_id)` in order — `run_id` a bare `UUID`, as the port takes it.
    Rows only; this fake never touches a file store, exactly as the port promises.
    """

    def __init__(
        self,
        runs: FakeTailoringRunRepository,
        jobs: FakeExportJobRepository,
        postings: FakeJobPostingRepository,
    ) -> None:
        self._runs = runs
        self._jobs = jobs
        self._postings = postings
        self.calls: list[tuple[UserId, UUID]] = []

    async def delete_history_entry(
        self, user_id: UserId, run_id: UUID
    ) -> DeletedHistoryEntry | None:
        self.calls.append((user_id, run_id))
        owner = UserOwner(user_id)
        run = await self._runs.find(TailoringRunId(value=run_id))
        if run is None or run.owner != owner:
            return None
        self._runs.discard(run.id)

        jobs = [
            job for job in self._jobs.all() if job.tailoring_run_id == run.id and job.owner == owner
        ]
        for job in jobs:
            self._jobs.discard(job.id)

        posting_deleted = False
        still_referenced = any(
            other.job_posting_id == run.job_posting_id for other in self._runs.all()
        )
        if not still_referenced:
            for posting in self._postings.all():
                if posting.id == run.job_posting_id and posting.owner == owner:
                    self._postings.discard(posting.id)
                    posting_deleted = True

        return DeletedHistoryEntry(
            export_files=tuple(job.storage_ref for job in jobs),
            export_jobs=len(jobs),
            posting_deleted=posting_deleted,
        )


class _HasAll(Protocol):
    """Structural type for "a fake repository with a test-only `.all()` inspector" — satisfied by
    both `FakeBaseCvRepository` and `FakeJobPostingRepository` without either needing to share a
    base class with the other (CLAUDE.md: shared shape is not shared behaviour; this is a
    test-fixture convenience typed narrowly enough to stay honest about that)."""

    def all(self) -> Sequence[object]: ...


class RecordingEventPublisher:
    """`EventPublisherPort` that records what it was handed, for AC-13-style assertions on the
    published events' field sets and content.

    Optionally takes a repository-like fake exposing `.all()`, purely so a test can prove
    **publish-after-save** ordering — the same technique `InMemoryFileStore(repo=cvs)` uses for
    T9's file-before-row proof, aimed the other way. `repo_size_at_first_publish` snapshots
    `len(repo.all())` at the moment `publish` is first called, so an assertion that it is nonzero is
    positive evidence the save already happened by then, rather than only an end-state check that
    would pass even if publish ran first.
    """

    def __init__(self, repo: _HasAll | None = None) -> None:
        self._repo = repo
        self.published: list[DomainEvent] = []
        self.repo_size_at_first_publish: int | None = None

    async def publish(self, *events: DomainEvent) -> None:
        if self._repo is not None and self.repo_size_at_first_publish is None:
            self.repo_size_at_first_publish = len(self._repo.all())
        self.published.extend(events)


# --- Shared test helpers -------------------------------------------------------------------------


async def create_active_session(
    sessions: FakeGuestSessionRepository,
    clock: FixedClock,
    *,
    token_hash: str = "a" * 64,
    ttl_hours: int = 24,
) -> GuestSession:
    """Start and persist a `GuestSession` that has not expired at `clock.now()`.

    `token_hash` defaults to a fixed dummy hash — fine when a test only ever needs one session, but
    a test asserting the T12 authorization rule (two sessions, each owning some CVs) must pass a
    distinct `token_hash` per session, since `FakeGuestSessionRepository.find_by_token_hash` looks
    sessions up by that value.
    """
    session = GuestSession.start(
        id=sessions.next_identity(),
        token_hash=token_hash,
        at=clock.now(),
        ttl_hours=ttl_hours,
    )
    await sessions.add(session)
    return session


class RecordingGuestWorkClaim:
    """Recording `GuestWorkClaimPort` (slice 2.4, T13). `calls` is the ordered log of what the use
    case did to the port: `("lock_session", token_hash)` and `("transfer", session_id, user_id)`.
    A test can share `order` with a recording file store to prove `transfer` comes before every
    unlink. `lock_session` answers `session` (None = no such row); `transfer` answers `claimed`, or
    raises `transfer_error` (the user erased between the use case's read and the re-key)."""

    def __init__(
        self,
        session: GuestSession | None,
        claimed: ClaimedGuestWork | None = None,
        *,
        transfer_error: Exception | None = None,
        order: list[str] | None = None,
    ) -> None:
        self._session = session
        self._claimed = claimed
        self._transfer_error = transfer_error
        self._order = order
        self.calls: list[tuple[object, ...]] = []

    async def lock_session(self, token_hash: str) -> GuestSession | None:
        self.calls.append(("lock_session", token_hash))
        if self._order is not None:
            self._order.append("lock_session")
        return self._session

    async def transfer(self, session_id: GuestSessionId, user_id: UserId) -> ClaimedGuestWork:
        self.calls.append(("transfer", session_id, user_id))
        if self._order is not None:
            self._order.append("transfer")
        if self._transfer_error is not None:
            raise self._transfer_error
        assert self._claimed is not None, "this double was built without a transfer outcome"
        return self._claimed
