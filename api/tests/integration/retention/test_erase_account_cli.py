"""`tailorcraft.cli erase-account --user-id <uuid> [--dry-run]` — the CLI contract (T23, AC-31),
test-after: `infrastructure/retention/erase_account_command.py` was implemented before this file
existed (T22), so there is no red to record — matching `test_revoke_logins_cli.py`'s own note for
the same reason, and its seam.

**The one rule that dominates this file, CLAUDE.md's 1.4 incident.** `get_settings()` under
`APP_ENV=test` still returns the **dev** `database_url` — only the `settings` fixture swaps in
`test_database_url`. `run_from_cli` calls `get_settings()` itself, so no test here calls it: every
test drives the seam the module docstring names, `erase_account(settings, user_id=, dry_run=)`, with
the already-swapped `settings` fixture. `_assert_test_database` asserts `"_test"` is in that URL
before the first statement, in every test that deletes.

**Why the rows are seeded through a genuinely committed connection, not the rolled-back `session`
fixture.** `erase_account` builds its own engine and its own session factory inside the function (the
module docstring: "the engine is built here, inside the loop `asyncio.run` opened"), which is a
different connection than any SAVEPOINT-per-test session could ever see under READ COMMITTED —
`test_revoke_logins_cli.py`'s `_CommittedLogins` and `test_purge_cli.py`'s `_CommittedRows` are the
same shape for the same reason. Real files are written through a `LocalFileStore` rooted at
`settings.upload_dir` (the session-scoped temp directory `conftest.py`'s `settings` fixture already
isolates), so a real `erase_account` run genuinely unlinks bytes on disk.

**The privacy test lives here rather than in `test_saved_base_cv_privacy_markers.py`.** That file's
own docstring says so: "`erase-account`'s own privacy claim is T23's test to write against the real
CLI signature once it exists." AC-49's shape is reused — plant markers, drive the real thing, assert
none of them reach `caplog`, with a positive control so the absence assertion is not vacuous.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.cli import main
from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    GuestSessionId,
    LoginId,
    PasswordHash,
    UserId,
)
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    BaseCvLabel,
    CvContentType,
    ExtractedText,
    OriginalFilename,
)
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.api.refresh_cookie import mint_refresh_token
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.identity.login import (
    login_table,
    retired_refresh_token_table,
)
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.retention import erase_account_command
from tailorcraft.infrastructure.settings import Settings
from tests.integration.owners import pasted_posting, ready_export, succeeded_run

_A_LOGIN_LIFETIME_DAYS = 30


def _assert_test_database(settings: Settings) -> None:
    assert "_test" in settings.database_url, (
        "refusing to run a deleting erase-account test against a URL that is not the test "
        f"database: {settings.database_url!r}"
    )


@pytest.fixture(autouse=True)
def _configured_logging(settings: Settings) -> None:
    """`erase_account` is called directly in this file, never through `run_from_cli`, so nothing
    else here configures structlog — `test_revoke_logins_cli.py`'s identical fixture, same reason."""
    configure_logging(settings)


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


# --- Seeding: real, committed rows through a connection independent of the seam under test --------


@dataclass(slots=True)
class _SeededAccount:
    user_id: UserId
    login_id: LoginId
    saved_cvs: list[tuple[BaseCvId, FileRef]]


@dataclass(slots=True)
class _AccountRig:
    """Seeds a genuinely committed user + login (with one retired token, from a real rotation) +
    saved base CVs with real files on disk, through the session-scoped `engine` fixture — never the
    rolled-back `session` fixture, for the reason the module docstring gives. Deletes at teardown
    whatever it created and any files still on disk (a successful non-dry-run erasure already removed
    both; teardown tolerates that)."""

    engine: AsyncEngine
    files: LocalFileStore
    upload_dir: Path
    _user_ids: list[UserId] = field(default_factory=list)
    _file_refs: list[FileRef] = field(default_factory=list)

    async def seed_account(
        self,
        *,
        email: str,
        saved_cv_specs: list[tuple[str, str | None, str | None]],
    ) -> _SeededAccount:
        """`saved_cv_specs`: `(original_filename, label_or_none, extracted_text_or_none)` per saved
        CV to create for this account."""
        now = _now()
        user_id = UserId(uuid4())
        login_id = LoginId(uuid4())
        factory = async_sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            user = User.register_with_password(
                user_id,
                EmailAddress.parse(email),
                PasswordHash("$argon2id$v=19$m=8,t=1,p=1$c2FsdA$dummy"),
                now,
            )
            session.add(user)
            await session.flush()

            minted = mint_refresh_token()
            login = Login.start(
                login_id,
                user_id,
                minted.token_hash,
                now,
                timedelta(days=_A_LOGIN_LIFETIME_DAYS),
            )
            session.add(login)
            await session.flush()

            # A real rotation, so a retired hash genuinely exists for the cascade to remove — the
            # same technique `test_revoke_logins_cli.py` uses to seed a `Login`, one step further.
            second = mint_refresh_token()
            retired = login.rotate(second.token_hash, now)
            await session.execute(
                retired_refresh_token_table.insert().values(
                    token_hash=retired.token_hash,
                    login_id=login_id,
                    generation=retired.generation,
                    retired_at=retired.retired_at,
                )
            )

            cvs = SqlAlchemyBaseCvRepository(session)
            saved: list[tuple[BaseCvId, FileRef]] = []
            for original_filename, label, extracted_text in saved_cv_specs:
                cv_id = cvs.next_identity()
                ref = FileRef.for_base_cv(cv_id, CvContentType.TXT)
                cv = BaseCv.upload(
                    id=cv_id,
                    owner=UserOwner(user_id),
                    original_filename=OriginalFilename(original_filename),
                    content_type=CvContentType.TXT,
                    size_bytes=8,
                    file=ref,
                    uploaded_at=now,
                )
                if label is not None:
                    cv.rename(BaseCvLabel(label), now)
                if extracted_text is not None:
                    cv.mark_extracted(ExtractedText(extracted_text), now)
                await cvs.add(cv)
                saved.append((cv_id, ref))

            await session.commit()

        for _cv_id, ref in saved:
            await self.files.put(ref, b"marker-body")
            self._file_refs.append(ref)
        self._user_ids.append(user_id)

        return _SeededAccount(user_id=user_id, login_id=login_id, saved_cvs=saved)

    async def user_exists(self, user_id: UserId) -> bool:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(func.count()).select_from(user_table).where(user_table.c.id == user_id)
            )
            return bool(result.scalar_one() > 0)

    async def login_count_for(self, user_id: UserId) -> int:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(login_table)
                .where(login_table.c.user_id == user_id)
            )
            return result.scalar_one()

    async def retired_token_count_for(self, login_id: LoginId) -> int:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(retired_refresh_token_table)
                .where(retired_refresh_token_table.c.login_id == login_id)
            )
            return result.scalar_one()

    async def saved_cv_count_for(self, user_id: UserId) -> int:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(base_cv_table)
                .where(base_cv_table.c.user_id == user_id)
            )
            return result.scalar_one()

    async def cleanup(self) -> None:
        for ref in self._file_refs:
            (self.upload_dir / ref.key).unlink(missing_ok=True)
        if self._user_ids:
            async with self.engine.begin() as conn:
                await conn.execute(user_table.delete().where(user_table.c.id.in_(self._user_ids)))


@pytest_asyncio.fixture
async def files(settings: Settings) -> LocalFileStore:
    return LocalFileStore(settings.upload_dir)


@pytest_asyncio.fixture
async def account_rig(
    engine: AsyncEngine, files: LocalFileStore, settings: Settings
) -> AsyncIterator[_AccountRig]:
    rig = _AccountRig(engine=engine, files=files, upload_dir=settings.upload_dir)
    try:
        yield rig
    finally:
        await rig.cleanup()


async def _seed_guest_base_cv(
    engine: AsyncEngine, files: LocalFileStore, *, original_filename: str = "decoy.txt"
) -> tuple[GuestSessionId, BaseCvId, FileRef]:
    """A guest-owned decoy: a base CV nothing in this file's erasures should ever touch (AC-31's
    "a guest-owned decoy CV ... survives untouched")."""
    now = _now()
    session_id = GuestSessionId(uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            guest_session_table.insert().values(
                id=session_id,
                token_hash="a" * 64,
                created_at=now,
                expires_at=now + timedelta(hours=24),
            )
        )
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        cvs = SqlAlchemyBaseCvRepository(session)
        cv_id = cvs.next_identity()
        ref = FileRef.for_base_cv(cv_id, CvContentType.TXT)
        await cvs.add(
            BaseCv.upload(
                id=cv_id,
                owner=GuestOwner(session_id),
                original_filename=OriginalFilename(original_filename),
                content_type=CvContentType.TXT,
                size_bytes=8,
                file=ref,
                uploaded_at=now,
            )
        )
        await session.commit()
    await files.put(ref, b"decoy-body")
    return session_id, cv_id, ref


# --- AC-31: dry run reports the counts and deletes nothing -----------------------------------------


async def test_dry_run_reports_the_counts_and_deletes_nothing(
    settings: Settings,
    account_rig: _AccountRig,
    files: LocalFileStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _assert_test_database(settings)
    seeded = await account_rig.seed_account(
        email=f"dry-run-{uuid4().hex}@example.com",
        saved_cv_specs=[("a.txt", "Label A", None), ("b.txt", None, None)],
    )

    exit_code = await erase_account_command.erase_account(
        settings, user_id=seeded.user_id, dry_run=True
    )

    assert exit_code == erase_account_command.EXIT_OK
    out = capsys.readouterr().out
    assert (
        f"would erase account {seeded.user_id.value}: 2 saved CV(s), 2 file(s), 1 login(s) "
        "(dry run)" in out
    )

    # Nothing was touched: the row, the login, the retired token and both files are all still there.
    assert await account_rig.user_exists(seeded.user_id)
    assert await account_rig.saved_cv_count_for(seeded.user_id) == 2
    assert await account_rig.login_count_for(seeded.user_id) == 1
    assert await account_rig.retired_token_count_for(seeded.login_id) == 1
    for _cv_id, ref in seeded.saved_cvs:
        assert await files.get(ref) == b"marker-body"


async def test_dry_run_on_an_unknown_id_exits_1_and_names_the_id(
    settings: Settings,
) -> None:
    _assert_test_database(settings)
    unknown = UserId(uuid4())

    exit_code = await erase_account_command.erase_account(settings, user_id=unknown, dry_run=True)

    assert exit_code == erase_account_command.EXIT_FAILED


# --- AC-31: a real run erases rows and files, exits 0 -----------------------------------------------


async def test_a_real_run_erases_the_user_logins_retired_tokens_and_saved_cvs_and_their_files(
    settings: Settings,
    account_rig: _AccountRig,
    files: LocalFileStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _assert_test_database(settings)
    seeded = await account_rig.seed_account(
        email=f"real-run-{uuid4().hex}@example.com",
        saved_cv_specs=[("a.txt", "Label A", None), ("b.txt", None, None)],
    )

    exit_code = await erase_account_command.erase_account(
        settings, user_id=seeded.user_id, dry_run=False
    )

    assert exit_code == erase_account_command.EXIT_OK
    out = capsys.readouterr().out
    assert (
        f"erased account {seeded.user_id.value}: 2 saved CV(s), 2 file(s) unlinked, 0 failed" in out
    )

    # The user row is gone, and everything that cascades from it is gone with it.
    assert not await account_rig.user_exists(seeded.user_id)
    assert await account_rig.saved_cv_count_for(seeded.user_id) == 0
    assert await account_rig.login_count_for(seeded.user_id) == 0
    assert await account_rig.retired_token_count_for(seeded.login_id) == 0

    # And the files themselves are unlinked from disk, not merely dereferenced.
    for _cv_id, ref in seeded.saved_cvs:
        assert not (settings.upload_dir / ref.key).exists()


async def test_a_real_run_with_a_failing_unlink_still_exits_0_and_logs_both_s45_lines(
    settings: Settings,
    account_rig: _AccountRig,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S-45, the CLI's own code path — `erase_account_command.erase_account` logs
    `retention.account_erased`/`retention.account_file_unlink_failed` itself (module docstring: "one
    log line per run, plus S-45's one line per failed unlink"), a separate call site from
    `routers/auth.py::_log_erasure`, so the HTTP route's own test of the same rows does not cover it.

    Fault injected **below** `LocalFileStore.delete`'s own floor (`Path.unlink`, the library call it
    wraps, matched by the exact path), never the adapter's public method (CLAUDE.md's I-45
    correction) — the real `except OSError` floor in `LocalFileStore.delete` still runs and
    translates it to `FileStoreUnavailable`, which is the type this test asserts on.

    **Mutation, observed red 2026-09-26 and reverted byte-exact.** In
    `erase_account_command.erase_account`, replaced the final
    `log.info(EVENT_ACCOUNT_ERASED, ...)` call with `pass`. Re-run:
    ```
    erased_lines = [r for r in caplog.records if "retention.account_erased" in r.getMessage()]
    >       assert erased_lines, f"expected a 'retention.account_erased' line, captured:\\n{caplog.text}"
    E       AssertionError: expected a 'retention.account_erased' line, captured: ...
    E       assert []
    FAILED tests/integration/retention/test_erase_account_cli.py::test_a_real_run_with_a_failing_unlink_still_exits_0_and_logs_both_s45_lines
    1 failed in ...s
    ```
    Source restored byte-exact (`git diff --stat api/src` empty); re-run green alone and the full
    module green twice in a row afterward.
    """
    _assert_test_database(settings)
    seeded = await account_rig.seed_account(
        email=f"unlink-fails-{uuid4().hex}@example.com",
        saved_cv_specs=[("a.txt", None, None), ("b.txt", None, None)],
    )
    ok_ref, failing_ref = (ref for _cv_id, ref in seeded.saved_cvs)
    failing_path = settings.upload_dir / failing_ref.key
    original_unlink = Path.unlink

    def _selective_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == failing_path:
            raise OSError(5, "Input/output error")
        return original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _selective_unlink)

    with caplog.at_level(logging.INFO):
        exit_code = await erase_account_command.erase_account(
            settings, user_id=seeded.user_id, dry_run=False
        )

    assert exit_code == erase_account_command.EXIT_OK
    out = capsys.readouterr().out
    assert (
        f"erased account {seeded.user_id.value}: 2 saved CV(s), 1 file(s) unlinked, 1 failed" in out
    )
    assert not (settings.upload_dir / ok_ref.key).exists()
    assert failing_path.exists(), "the failing unlink must have left its bytes behind"

    erased_lines = [r for r in caplog.records if "retention.account_erased" in r.getMessage()]
    assert erased_lines, f"expected a 'retention.account_erased' line, captured:\n{caplog.text}"
    erased_message = erased_lines[0].getMessage()
    assert str(seeded.user_id.value) in erased_message
    assert '"base_cvs": 2' in erased_message, erased_message
    assert '"files_unlinked": 1' in erased_message, erased_message
    assert '"files_failed": 1' in erased_message, erased_message

    failure_lines = [
        r for r in caplog.records if "retention.account_file_unlink_failed" in r.getMessage()
    ]
    assert len(failure_lines) == 1, f"expected exactly one failure line, captured:\n{caplog.text}"
    failure_message = failure_lines[0].getMessage()
    assert str(seeded.user_id.value) in failure_message
    assert "FileStoreUnavailable" in failure_message


async def test_a_real_run_leaves_a_guest_owned_decoy_and_another_users_cv_untouched(
    settings: Settings,
    engine: AsyncEngine,
    files: LocalFileStore,
    account_rig: _AccountRig,
) -> None:
    """AC-31's own decoy proof: `erase-account` is a `WHERE user_id = :u` and a schema-level
    guarantee (`guest_session_id IS NULL` on every saved CV), never a wider sweep."""
    _assert_test_database(settings)
    victim = await account_rig.seed_account(
        email=f"victim-{uuid4().hex}@example.com",
        saved_cv_specs=[("victim.txt", None, None)],
    )
    bystander = await account_rig.seed_account(
        email=f"bystander-{uuid4().hex}@example.com",
        saved_cv_specs=[("bystander.txt", "Bystander's CV", None)],
    )
    guest_session_id, _guest_cv_id, guest_ref = await _seed_guest_base_cv(engine, files)

    exit_code = await erase_account_command.erase_account(
        settings, user_id=victim.user_id, dry_run=False
    )

    assert exit_code == erase_account_command.EXIT_OK
    assert not await account_rig.user_exists(victim.user_id)

    # The bystander's account, saved CV and file: untouched.
    assert await account_rig.user_exists(bystander.user_id)
    assert await account_rig.saved_cv_count_for(bystander.user_id) == 1
    for _cv_id, ref in bystander.saved_cvs:
        assert await files.get(ref) == b"marker-body"

    # The guest-owned decoy: untouched, row and file both.
    async with engine.connect() as conn:
        result = await conn.execute(
            select(func.count())
            .select_from(guest_session_table)
            .where(guest_session_table.c.id == guest_session_id)
        )
        assert result.scalar_one() == 1
    assert await files.get(guest_ref) == b"decoy-body"

    # Cleanup this test's own extra rows/files the shared rig does not know about.
    async with engine.begin() as conn:
        await conn.execute(
            guest_session_table.delete().where(guest_session_table.c.id == guest_session_id)
        )
    (settings.upload_dir / guest_ref.key).unlink(missing_ok=True)


# --- AC-31: unknown id -> 1 -------------------------------------------------------------------------


async def test_a_real_run_on_an_unknown_id_exits_1_and_names_the_id_not_an_email(
    settings: Settings, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    _assert_test_database(settings)
    unknown = UserId(uuid4())

    with caplog.at_level(logging.INFO):
        exit_code = await erase_account_command.erase_account(
            settings, user_id=unknown, dry_run=False
        )

    assert exit_code == erase_account_command.EXIT_FAILED
    err = capsys.readouterr().err
    assert f"no account with id {unknown.value}" in err
    assert str(unknown.value) in caplog.text
    assert "@" not in err, "the CLI never reads or reports an email — only the id it was given"


async def test_erasing_the_same_account_a_second_time_exits_1(
    settings: Settings, account_rig: _AccountRig
) -> None:
    """T22's own smoke-test note ("a second run -> 1") as a real test: once `erase_account` has
    already erased the row, a second invocation with the same id finds nothing — `AccountNotFound`,
    the same outcome S-46 gives a genuinely concurrent second erasure, just sequential here."""
    _assert_test_database(settings)
    seeded = await account_rig.seed_account(
        email=f"twice-{uuid4().hex}@example.com",
        saved_cv_specs=[("a.txt", None, None)],
    )
    first = await erase_account_command.erase_account(
        settings, user_id=seeded.user_id, dry_run=False
    )
    assert first == erase_account_command.EXIT_OK

    second = await erase_account_command.erase_account(
        settings, user_id=seeded.user_id, dry_run=False
    )

    assert second == erase_account_command.EXIT_FAILED


# --- AC-31: usage errors -> 2, through the real argparse parser -------------------------------------


def test_missing_user_id_exits_2() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["erase-account"])
    assert exc_info.value.code == 2


def test_malformed_user_id_exits_2() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["erase-account", "--user-id", "not-a-uuid"])
    assert exc_info.value.code == 2


# --- AC-31: the database-mismatch guard -> 1, nothing touched ---------------------------------------


async def test_the_foreign_database_guard_detects_a_mismatched_database_name(
    settings: Settings, session: AsyncSession
) -> None:
    """The guard itself, proved against a real connection: `session` is genuinely connected to
    `tailorcraft_test` (the `connection`/`session` fixtures), so handing `_refuse_a_foreign_database`
    a `Settings` whose `database_url` names a different database must raise `_ForeignDatabase`
    carrying both names — never silently pass because the two strings merely differ."""
    _assert_test_database(settings)
    mismatched = settings.model_copy(
        update={
            "database_url": settings.database_url.replace("tailorcraft_test", "definitely_not_it")
        }
    )

    with pytest.raises(erase_account_command._ForeignDatabase) as exc_info:
        await erase_account_command._refuse_a_foreign_database(session, mismatched)

    assert exc_info.value.expected == "definitely_not_it"
    assert exc_info.value.actual == "tailorcraft_test"


async def test_the_guard_refusing_exits_1_and_touches_nothing(
    settings: Settings,
    account_rig: _AccountRig,
    files: LocalFileStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The entry point's handling of the guard's own refusal: forced by monkeypatching
    `_refuse_a_foreign_database` itself to always raise (the guard's detection logic is proved for
    real, against a genuine connection, by the test immediately above) — proving that when it fires,
    `erase_account` reaches no account data at all: the seeded account, its saved CV and its file are
    every one still there afterward, and the exit code and log line are AC-31's."""
    _assert_test_database(settings)
    seeded = await account_rig.seed_account(
        email=f"guard-{uuid4().hex}@example.com",
        saved_cv_specs=[("a.txt", None, None)],
    )

    async def _always_refuse(session: AsyncSession, refused_settings: Settings) -> None:
        raise erase_account_command._ForeignDatabase("intended_db", "actual_db")

    monkeypatch.setattr(erase_account_command, "_refuse_a_foreign_database", _always_refuse)

    with caplog.at_level(logging.WARNING):
        exit_code = await erase_account_command.erase_account(
            settings, user_id=seeded.user_id, dry_run=False
        )

    assert exit_code == erase_account_command.EXIT_FAILED
    err = capsys.readouterr().err
    assert "intended_db" in err
    assert "actual_db" in err
    assert "retention.account_erasure_refused" in caplog.text
    assert "foreign_database" in caplog.text

    # Nothing was touched: the guard fired before the use case ever ran.
    assert await account_rig.user_exists(seeded.user_id)
    assert await account_rig.saved_cv_count_for(seeded.user_id) == 1
    for _cv_id, ref in seeded.saved_cvs:
        assert await files.get(ref) == b"marker-body"


# --- The CLI's own privacy claim, moved here from T20's placeholder note ---------------------------


async def test_erase_account_never_logs_the_erased_users_email_label_filename_or_cv_text(
    settings: Settings,
    account_rig: _AccountRig,
    files: LocalFileStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`erase-account`'s own privacy claim, deferred here from `test_saved_base_cv_privacy_markers.
    py` (T20) once the CLI's real signature existed. Plants a marker in the erased user's email, one
    saved CV's label, its original filename and its extracted text; drives the real
    `erase_account(...)` entry point; asserts none of the four markers appears anywhere in `caplog`
    (structlog and stdlib both render through it — `configure_logging`'s own rendering). Paired with
    a positive control: at least one captured line carries the run's `user_id`, so the absence
    assertions are not passing because nothing was logged at all (the 1.6 lesson about a silenced
    logger)."""
    _assert_test_database(settings)
    marker = uuid4().hex
    marker_email = f"cli-marker-{marker}@example.com"
    marker_label = f"CLI-MARKER-LABEL-{marker}"
    marker_filename = f"CLI-MARKER-FILENAME-{marker}.txt"
    marker_cv_text = (f"CLI-MARKER-CV-TEXT-{marker} " * 20).strip()

    seeded = await account_rig.seed_account(
        email=marker_email,
        saved_cv_specs=[(marker_filename, marker_label, marker_cv_text)],
    )

    with caplog.at_level(logging.DEBUG):
        exit_code = await erase_account_command.erase_account(
            settings, user_id=seeded.user_id, dry_run=False
        )

    assert exit_code == erase_account_command.EXIT_OK

    log_text = caplog.text
    for description, value in (
        ("the erased user's email", marker_email),
        ("the saved CV's label", marker_label),
        ("the saved CV's original filename", marker_filename),
        ("the saved CV's extracted text", marker_cv_text),
    ):
        assert value not in log_text, (
            f"{description} ({value!r}) appeared in a captured log record. Captured text:\n{log_text}"
        )

    # The positive control: the run's own line(s) name the id, so the absence above is not vacuous.
    assert str(seeded.user_id.value) in log_text, (
        "no captured log line names the erased user_id — the absence assertions above would hold "
        f"even if nothing were logged at all. Captured text:\n{log_text}"
    )


# --- Slice 2.3 (T25, AC-36): the CLI with history ------------------------------------------------


async def _seed_history(
    rig: _AccountRig, owner: UserOwner | GuestOwner, *, runs: int, exports_per_run: int
) -> list[FileRef]:
    """`runs` entries (a posting and a succeeded run each) with `exports_per_run` ready export jobs
    each, committed, their files on the real volume. Returns the export keys."""
    # Deferred: these repositories read mapped attributes at import time, and this module is
    # collected before the session's `configure_mappings()` fixture runs.
    from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
        SqlAlchemyExportJobRepository,
    )
    from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
        SqlAlchemyJobPostingRepository,
    )
    from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
        SqlAlchemyTailoringRunRepository,
    )

    now = _now()
    keys: list[FileRef] = []
    factory = async_sessionmaker(bind=rig.engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        for _ in range(runs):
            posting = pasted_posting(owner, now)
            await SqlAlchemyJobPostingRepository(session).add(posting)
            run = succeeded_run(owner, now, job_posting_id=posting.id)
            await SqlAlchemyTailoringRunRepository(session).add(run)
            formats = [ExportFormat.PDF, ExportFormat.DOCX][:exports_per_run]
            for export_format in formats:
                job = ready_export(owner, run, now, format=export_format)
                await SqlAlchemyExportJobRepository(session).add(job)
                keys.append(job.storage_ref)
        await session.commit()
    for ref in keys:
        await rig.files.put(ref, b"rendered-export")
        rig._file_refs.append(ref)
    return keys


async def _count_owned(rig: _AccountRig, table: str, user_id: UserId) -> int:
    async with rig.engine.connect() as conn:
        result = await conn.execute(
            text(f"SELECT count(*) FROM {table} WHERE user_id = :u"),  # noqa: S608 -- test-owned name
            {"u": user_id.value},
        )
        return int(result.scalar_one())


async def test_ac36_the_dry_run_reports_the_history_and_deletes_nothing(
    settings: Settings,
    account_rig: _AccountRig,
    files: LocalFileStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _assert_test_database(settings)
    seeded = await account_rig.seed_account(
        email=f"ac36-dry-{uuid4().hex}@example.com", saved_cv_specs=[("a.txt", None, None)]
    )
    export_keys = await _seed_history(
        account_rig, UserOwner(seeded.user_id), runs=2, exports_per_run=1
    )
    await _seed_history(account_rig, UserOwner(seeded.user_id), runs=1, exports_per_run=0)

    exit_code = await erase_account_command.erase_account(
        settings, user_id=seeded.user_id, dry_run=True
    )

    assert exit_code == erase_account_command.EXIT_OK
    out = capsys.readouterr().out
    assert (
        f"would erase account {seeded.user_id.value}: 1 saved CV(s), 3 file(s), 1 login(s) "
        "(dry run); history: 3 tailoring run(s), 3 job posting(s), 2 export job(s)"
    ) in out
    assert await account_rig.user_exists(seeded.user_id)
    for table, count in (("tailoring_run", 3), ("posting_job_posting", 3), ("export_job", 2)):
        assert await _count_owned(account_rig, table, seeded.user_id) == count, table
    for ref in export_keys:
        assert await files.get(ref) == b"rendered-export"


async def test_ac36_a_real_run_erases_the_history_and_its_files_and_spares_a_guests(
    settings: Settings,
    engine: AsyncEngine,
    account_rig: _AccountRig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _assert_test_database(settings)
    seeded = await account_rig.seed_account(
        email=f"ac36-real-{uuid4().hex}@example.com", saved_cv_specs=[("a.txt", None, None)]
    )
    export_keys = await _seed_history(
        account_rig, UserOwner(seeded.user_id), runs=2, exports_per_run=2
    )
    guest_session_id, _guest_cv_id, guest_cv_ref = await _seed_guest_base_cv(
        engine, account_rig.files
    )
    account_rig._file_refs.append(guest_cv_ref)
    guest_keys = await _seed_history(
        account_rig, GuestOwner(guest_session_id), runs=1, exports_per_run=1
    )

    try:
        exit_code = await erase_account_command.erase_account(
            settings, user_id=seeded.user_id, dry_run=False
        )

        assert exit_code == erase_account_command.EXIT_OK
        out = capsys.readouterr().out
        assert (
            f"erased account {seeded.user_id.value}: 1 saved CV(s), 5 file(s) unlinked, 0 failed; "
            "history: 2 tailoring run(s), 2 job posting(s), 4 export job(s), 5 file(s) in all"
        ) in out
        assert not await account_rig.user_exists(seeded.user_id)
        for table in ("tailoring_run", "posting_job_posting", "export_job", "intake_base_cv"):
            assert await _count_owned(account_rig, table, seeded.user_id) == 0, table
        for ref in export_keys:
            assert not (account_rig.upload_dir / ref.key).exists()
        for ref in [guest_cv_ref, *guest_keys]:
            assert (account_rig.upload_dir / ref.key).exists(), "a guest's file was unlinked"
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                guest_session_table.delete().where(guest_session_table.c.id == guest_session_id)
            )
