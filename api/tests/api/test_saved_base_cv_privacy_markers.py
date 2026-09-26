"""AC-49 / AC-50's planted-marker privacy test for `intake-saved-base-cvs` (slice 2.2, T20).

Follows `test_auth_privacy_markers.py`'s (2.1, AC-46) and `test_purge_privacy_log_markers.py`'s
(1.6, AC-38) structure and their measured reason for using `caplog` rather than
`structlog.testing.capture_logs()` (see either module's docstring — `configure_logging()` rebuilds
the processor chain from scratch per `create_app()` call, and a module-level logger cached from an
earlier test keeps its own stale reference `capture_logs()` cannot reach; `caplog` reads the
already-flattened string every `logging.Logger` call produces, regardless of which chain rendered
it).

**Hardened at `/verify` round 1, now that GREEN has landed for every route this flow drives.** The
file's first cut was deliberately written "soft" — every step ran only `if _ok(previous)`, and every
AC-50 assertion sat inside `if fake_llm_request is not None:` — because at the time it was written
`upload_saved_base_cv`, `rename_saved_base_cv`, `copy_saved_base_cv` and `delete_account` were still
`NotImplementedError` bodies, and the whole point of that file was to keep planting every later
marker "worth exercising for real the moment each skeleton goes GREEN" without needing an edit once
it did. It did; this is that edit. Every step below now asserts its own exact status code, in the
order the spec's flow describes it — a step that stops returning 2xx must fail this test loudly, on
the assertion that step's own contract promises, never disappear into "nothing further ran". The
`client` fixture below still overrides `raise_app_exceptions` (see its own docstring for why that
default still earns its place even though nothing here is expected to raise any more), but the
`_ok`-gated continuation shape is gone.

**`erase-account` (T22/T23) is exercised in `test_erase_account_cli.py`, not here.** That file's own
`test_erase_account_never_logs_the_erased_users_email_label_filename_or_cv_text` is AC-49's
planted-marker claim for the CLI's own composition root, against the CLI's real signature. This file
plants a second marker user (registered, never erased here) purely so that a *second* account's email
is present in the run for the CLI test to build on — it does not itself drive `erase-account`.

**A purge and an orphan sweep, over 2.2's own shapes, live in the second test below** — added at
`/verify` round 1, because AC-49 names both and neither existed here before. They cannot reuse this
file's own HTTP-driven data: `client`/`app`/`session` bind every request to one `AsyncSession` whose
commits are SAVEPOINTs released against the test's own outer, never-committed transaction
(`conftest.py`), and `purge_command._purge_guests`/`_reclaim_orphans` open their own, genuinely
separate engine — which under READ COMMITTED sees none of that. `test_registered_data_survives_purge_
and_sweep.py` (AC-15…AC-17) and `test_purge_privacy_log_markers.py` (AC-38) are the two references
this second test's harness follows: real, committed rows through the session-scoped `engine`, a
private `tmp_path` root (never the session-scoped `settings.upload_dir` every other file shares —
`test_registered_data_survives_purge_and_sweep.py`'s own docstring records what sharing it did to an
earlier draft of *that* file), and the real `purge_command._purge_guests`/`_reclaim_orphans` entry
points production actually runs.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    GuestSessionId,
    PasswordHash,
)
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import (
    CvContentType,
    ExtractedText,
    OriginalFilename,
)
from tailorcraft.domain.posting.value_objects import JobPostingText
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.retention import purge_command
from tailorcraft.infrastructure.settings import Settings

REGISTER_URL = "/api/auth/register"
ME_BASE_CVS_URL = "/api/me/base-cvs"
COPIES_URL = "/api/base-cvs/copies"
DELETE_ACCOUNT_URL = "/api/auth/delete-account"
JOB_POSTINGS_URL = "/api/job-postings"
TAILORING_RUNS_URL = "/api/tailoring-runs"

_MARKER_PREFIX: Final = "QA49MARKER"


def _marker(label: str) -> str:
    return f"{_MARKER_PREFIX}-{label}-{uuid4().hex}"


def _marker_email(label: str) -> str:
    return f"{_marker(label).lower()}@example.com"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Shadows `conftest.py`'s `client` fixture — `test_intake.py`'s/`test_auth.py`'s reason:
    `ASGITransport`'s default `raise_app_exceptions=True` re-raises an unhandled handler exception
    as a bare Python exception rather than a real response. Nothing this test drives is expected to
    raise any more (every route is GREEN), so this is defensive symmetry with every sibling test
    file in this package rather than a load-bearing requirement here: if a regression ever did
    reintroduce an unhandled exception mid-flow, this keeps the failure a readable
    `assert 500 == 201` on the specific step's own assertion, rather than a raw traceback that aborts
    the test before the later markers are ever planted."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Every step below can touch a rate limiter; the database rollback never reaches Redis."""


async def test_no_marker_leaks_across_the_full_saved_cv_flow_and_a_line_names_every_id(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-49. Drives register -> upload to account -> list -> rename -> copy -> a tailoring run on
    the copy (fake LLM, AC-50) -> delete the saved CV -> delete the account -> a second marker user's
    registration. Then: no marker anywhere in `caplog`, in any response body outside its one
    legitimate channel, in any Redis key — and, per step, at least one captured line names the id
    that step's own contract says gets logged.

    **Mutation, observed red 2026-09-26 and reverted byte-exact.** In
    `routers/intake.py::copy_saved_base_cv`, inserted `raise HTTPException(409, detail={"error":
    {"code": "too_many_base_cvs", "message": "mutation"}})` as the first statement of the function
    body, before the real flow runs. Re-run:
    ```
    >       assert copied.status_code == 201, copied.text
    E       assert 409 == 201
    E        +  where 409 = <Response [409 Conflict]>.status_code
    FAILED tests/api/test_saved_base_cv_privacy_markers.py::test_no_marker_leaks_across_the_full_saved_cv_flow_and_a_line_names_every_id
    1 failed, 1 deselected in 0.72s
    ```
    (The same run's captured output also confirms the upload step's own positive-control assertion
    works for the right reason: `BaseCvUploaded`'s `domain_event` line, carrying `base_cv_id`, is
    genuinely present before the mutated line is ever reached.) Source restored byte-exact
    (`git diff --stat api/src` empty); re-run green alone and the full module green twice in a row
    afterward.
    """
    marker_email = _marker_email("email")
    marker_filename = _marker("original-filename") + ".txt"
    marker_label = _marker("label")
    marker_cv_text = _marker("cv-body-text") * 20  # comfortably over the 200-char extraction floor
    marker_posting_text = _marker("posting-text") * 20

    responses: list[Response] = []
    ids: dict[str, str] = {}
    line_counts_before: dict[str, int] = {}

    def _mark(step: str) -> None:
        line_counts_before[step] = len(caplog.records)

    def _lines_since(step: str) -> list[logging.LogRecord]:
        return caplog.records[line_counts_before[step] :]

    with caplog.at_level(logging.DEBUG):
        # --- register --------------------------------------------------------------------------
        register = await client.post(
            REGISTER_URL,
            json={"email": marker_email, "password": "correct horse battery staple 9"},
            headers={"Origin": settings.public_base_url},
        )
        responses.append(register)
        assert register.status_code == 201, register.text
        body = register.json()
        token = str(body["access_token"])
        ids["user_id"] = str(body["user"]["id"])

        # --- upload to the account ---------------------------------------------------------------
        _mark("upload")
        uploaded = await client.post(
            ME_BASE_CVS_URL,
            files={"file": (marker_filename, marker_cv_text.encode(), "text/plain")},
            headers=_bearer(token),
        )
        responses.append(uploaded)
        assert uploaded.status_code == 201, uploaded.text
        saved_cv_id = str(uploaded.json()["id"])
        ids["base_cv_id"] = saved_cv_id
        upload_lines = _lines_since("upload")
        assert any(saved_cv_id in r.getMessage() for r in upload_lines), (
            "the upload records BaseCvUploaded (AC-6) — no captured line names the new "
            f"base_cv_id. Captured:\n{caplog.text}"
        )

        # --- list ----------------------------------------------------------------------------------
        listed = await client.get(ME_BASE_CVS_URL, headers=_bearer(token))
        responses.append(listed)
        assert listed.status_code == 200, listed.text

        # --- rename (plants the label marker) -------------------------------------------------------
        # No positive-control line is asserted for this step: AC-4 records no event for a rename on
        # purpose ("a label is user text; events carry ids") and the router's own success path logs
        # nothing either (S-14/S-16 only log on a *refusal*, which this call is not) — there is
        # nothing here that would log, by design, so asserting one would be asserting a defect.
        renamed = await client.patch(
            f"{ME_BASE_CVS_URL}/{saved_cv_id}",
            json={"label": marker_label},
            headers=_bearer(token),
        )
        responses.append(renamed)
        assert renamed.status_code == 200, renamed.text

        # --- copy into the workspace ------------------------------------------------------------------
        _mark("copy")
        copied = await client.post(
            COPIES_URL, json={"saved_base_cv_id": saved_cv_id}, headers=_bearer(token)
        )
        responses.append(copied)
        assert copied.status_code == 201, copied.text
        working_copy_id = str(copied.json()["id"])
        ids["working_copy_id"] = working_copy_id
        copy_lines = _lines_since("copy")
        assert any(working_copy_id in r.getMessage() for r in copy_lines), (
            "the copy records BaseCvCopied (AC-3) — no captured line names the working copy's "
            f"base_cv_id. Captured:\n{caplog.text}"
        )

        # --- a tailoring run on the working copy, fake LLM (AC-50) --------------------------------------
        posting = await client.post(
            JOB_POSTINGS_URL, json={"source": "pasted", "text": marker_posting_text}
        )
        responses.append(posting)
        assert posting.status_code == 201, posting.text

        cv_sent, posting_sent = await _tailor(
            client, app, session, settings, working_copy_id, str(posting.json()["id"])
        )
        assert marker_email not in cv_sent.value
        assert ids["user_id"] not in cv_sent.value
        assert saved_cv_id not in cv_sent.value
        assert marker_label not in cv_sent.value
        assert marker_cv_text in cv_sent.value, (
            "the working copy's own text IS the legitimate channel"
        )
        assert marker_email not in posting_sent.value
        assert ids["user_id"] not in posting_sent.value

        # --- delete the saved CV -----------------------------------------------------------------------
        _mark("delete")
        deleted_cv = await client.delete(f"{ME_BASE_CVS_URL}/{saved_cv_id}", headers=_bearer(token))
        responses.append(deleted_cv)
        assert deleted_cv.status_code == 204, deleted_cv.text
        delete_lines = _lines_since("delete")
        assert any(saved_cv_id in r.getMessage() for r in delete_lines), (
            "the delete records BaseCvDeleted (AC-5) — no captured line names the deleted "
            f"base_cv_id. Captured:\n{caplog.text}"
        )

        # --- delete the account -------------------------------------------------------------------------
        _mark("delete_account")
        deleted_account = await client.post(
            DELETE_ACCOUNT_URL,
            json={"password": "correct horse battery staple 9"},
            headers={**_bearer(token), "Origin": settings.public_base_url},
        )
        responses.append(deleted_account)
        assert deleted_account.status_code == 204, deleted_account.text
        erasure_lines = _lines_since("delete_account")
        assert any(ids["user_id"] in r.getMessage() for r in erasure_lines), (
            "the erasure records retention.account_erased — no captured line names the erased "
            f"user_id. Captured:\n{caplog.text}"
        )

        # --- a second marker user, registered so its email is a marker present in the run too;
        # erase-account's own privacy claim is a separate test, against the CLI's real entry point,
        # in test_erase_account_cli.py (module docstring). ------------------------------------------
        second_marker_email = _marker_email("second-user-email")
        second_register = await client.post(
            REGISTER_URL,
            json={"email": second_marker_email, "password": "correct horse battery staple 9"},
            headers={"Origin": settings.public_base_url},
        )
        responses.append(second_register)
        assert second_register.status_code == 201, second_register.text
        ids["second_user_id"] = str(second_register.json()["user"]["id"])

    # --- The actual claim: no marker anywhere it must not be ---------------------------------
    log_text = caplog.text
    never_logged = {
        "the marker email": marker_email,
        "the second marker email": second_marker_email,
        "the marker filename": marker_filename,
        "the marker label": marker_label,
        "the marker CV text": marker_cv_text,
        "the marker posting text": marker_posting_text,
    }
    for description, marker in never_logged.items():
        assert marker not in log_text, (
            f"{description} ({marker!r}) appeared in a captured log record — AC-49. "
            f"Full captured text:\n{log_text}"
        )

    # `original_filename` IS a pinned field of `SavedBaseCvResponse` (AC-20) — a saved CV's response
    # body is its one legitimate channel, exactly as the label is for `PATCH` (AC-26). AC-49 pins
    # what must never appear in a log record or a Redis key; it says nothing about a response body
    # echoing back a field the API contract itself promises to expose. Only the label is checked
    # here, and only outside its own legitimate echo.
    never_in_a_response_body = {
        "the marker label": marker_label,
    }
    for response in responses:
        for description, marker in never_in_a_response_body.items():
            if response.request.url.path == f"{ME_BASE_CVS_URL}/{ids.get('base_cv_id', '')}" and (
                "label" in description.lower()
            ):
                continue  # PATCH's own success body legitimately echoes the label back (AC-26)
            assert marker not in response.text, (
                f"{description} ({marker!r}) appeared in a response body it has no business in — "
                f"AC-49: {response.status_code} {response.request.url} -> {response.text}"
            )

    redis = create_redis(settings.redis_url)
    try:
        async for key in redis.scan_iter(match="*"):
            key_str = key.decode() if isinstance(key, bytes) else key
            for description, marker in never_logged.items():
                assert marker not in key_str, f"{description} appeared in a Redis key: {key_str!r}"
    finally:
        await redis.aclose()


async def _tailor(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    base_cv_id: str,
    job_posting_id: str,
) -> tuple[ExtractedText, JobPostingText]:
    """Queue a tailoring run on the working copy and execute it through the real
    `ExecuteTailoringRun`, bound to this test's own session — `test_tailoring.py`'s own
    `_a_draft`/`_install_worker_llm`/`_run_worker`, reused rather than re-derived (their exact
    field shapes — `TailoredDraft(documents=..., metrics=...)` — are that file's to own). Asserts
    the run was accepted, that the fake LLM was called **exactly once**, and returns its one
    `(cv, posting)` call argument pair — no soft "or None" escape: GREEN has landed for every route
    this helper drives, so a run that fails to queue or a worker that never calls the fake LLM is
    this test's own failure, not a reason to skip AC-50's assertions."""
    from tailorcraft.infrastructure.api.deps import get_tailoring_queue
    from tests.api.test_tailoring import _a_draft, _install_worker_llm, _run_worker
    from tests.integration.fakes import FakeLlm, FakeTailoringQueue

    app.dependency_overrides[get_tailoring_queue] = lambda: FakeTailoringQueue()

    created = await client.post(
        TAILORING_RUNS_URL, json={"base_cv_id": base_cv_id, "job_posting_id": job_posting_id}
    )
    assert created.status_code == 202, created.text

    fake_llm = FakeLlm(_a_draft())

    with pytest.MonkeyPatch.context() as mp:
        _install_worker_llm(mp, fake_llm)
        await _run_worker(session, settings, str(created.json()["id"]))

    assert len(fake_llm.calls) == 1, (
        f"expected the fake LlmPort to be called exactly once, was called {len(fake_llm.calls)} "
        "time(s)"
    )
    return fake_llm.calls[0]


# ---------------------------------------------------------------------------------------------
# AC-49's other named leg: "a purge and an orphan sweep" over 2.2's own shapes — added at /verify
# round 1. See the module docstring for why this cannot reuse the test above's HTTP-driven data.
# ---------------------------------------------------------------------------------------------

_PASSWORD_HASH = PasswordHash("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA")


def _sweep_marker(label: str) -> str:
    return f"QA49SWEEP-{label}-{uuid4().hex}"


def _age(path: Path, at: datetime) -> None:
    """`os.utime`, matching `test_registered_data_survives_purge_and_sweep.py`'s helper of the same
    name: the orphan sweep reads `st_mtime`, never a UUID's embedded timestamp."""
    timestamp = at.timestamp()
    os.utime(path, (timestamp, timestamp))


def _assert_test_database(settings: Settings) -> None:
    assert "_test" in settings.database_url, (
        f"refusing to run a deleting privacy test against {settings.database_url!r}"
    )


@pytest_asyncio.fixture
async def committed_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session bound directly to the session-scoped `engine`, never the rolled-back `connection`/
    `session` fixtures — `purge_command`'s own composition roots build a fresh engine and read this
    data back from a genuinely committed transaction (`test_purge_privacy_log_markers.py`'s fixture
    of the same name and the same reason)."""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        yield session


async def test_purge_and_orphan_sweep_over_a_saved_cv_and_its_working_copy_never_log_a_marker(
    settings: Settings,
    engine: AsyncEngine,
    committed_session: AsyncSession,
    tmp_path: Path,
    clear_redis: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-49's purge/orphan-sweep leg. A registered user's saved CV (marker filename, marker
    extracted text, marker file bytes) plus a working copy of it in an **expired** guest session
    (marker file bytes of its own, `copied_from_base_cv_id` set — 2.2's new shape), plus a genuine
    unrelated orphan on the same volume: a real `purge-guests` reclaims the expired session's
    working copy and leaves the saved CV completely untouched (AC-15/AC-17's own proof, reused here
    for the privacy question rather than the survival one); a real `purge-guests --orphans` then
    reclaims the planted orphan and still leaves the saved CV. Neither run may log any marker, and
    both runs' own completion lines must be present (the positive control: a silently-failed run
    that touched nothing would pass every absence assertion vacuously).

    Deferred repository imports for the reason `test_purge_privacy_log_markers.py`'s module comment
    gives: their mapping modules read `BaseCv._id`/etc. as plain class attributes at *their own*
    import time, which only works after `configure_mappings()` — a session-scoped, autouse fixture
    that has not necessarily run yet at collection time if these were hoisted to module scope.
    """
    from tailorcraft.infrastructure.persistence.repositories.identity.user import (
        SqlAlchemyUserRepository,
    )
    from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
        SqlAlchemyBaseCvRepository,
    )

    _assert_test_database(settings)
    local_settings = settings.model_copy(update={"upload_dir": tmp_path})
    files = LocalFileStore(tmp_path)

    now = datetime.now(UTC).replace(microsecond=0)

    # --- the saved CV, owned by a real, committed user ------------------------------------------
    users = SqlAlchemyUserRepository(committed_session)
    user_id = users.next_identity()
    email_marker = _sweep_marker("EMAIL").lower()
    await users.add(
        User.register_with_password(
            id=user_id,
            email=EmailAddress.parse(f"{email_marker}@example.com"),
            password_hash=_PASSWORD_HASH,
            at=now,
        )
    )

    cvs = SqlAlchemyBaseCvRepository(committed_session)
    saved_cv_id = cvs.next_identity()
    saved_ref = FileRef.for_base_cv(saved_cv_id, CvContentType.PDF)
    filename_marker = _sweep_marker("FILENAME")
    extracted_text_marker = _sweep_marker("EXTRACTEDTEXT")
    saved_bytes_marker = _sweep_marker("SAVEDBYTES")
    saved_cv = BaseCv.upload(
        id=saved_cv_id,
        owner=UserOwner(user_id),
        original_filename=OriginalFilename(f"{filename_marker}.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=64,
        file=saved_ref,
        uploaded_at=now,
    )
    saved_cv.mark_extracted(
        ExtractedText(f"{extracted_text_marker}\n" + "filler prose. " * 20), now
    )
    await cvs.add(saved_cv)
    await committed_session.commit()
    await files.put(saved_ref, f"{saved_bytes_marker}\nthe saved CV's own bytes".encode())

    # --- the working copy, owned by an EXPIRED guest session (2.2's new shape) -------------------
    from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
        guest_session_table,
    )

    expired_session_id = GuestSessionId(uuid4())
    token_hash_marker = (_sweep_marker("TOKENHASH") + "x" * 64)[:64]
    await committed_session.execute(
        guest_session_table.insert().values(
            id=expired_session_id,
            token_hash=token_hash_marker,
            created_at=now - timedelta(hours=25),
            expires_at=now - timedelta(hours=1),
        )
    )
    await committed_session.commit()

    working_copy_id = cvs.next_identity()
    working_copy_ref = FileRef.for_base_cv(working_copy_id, CvContentType.PDF)
    working_copy = BaseCv.copy_from(
        saved_cv,
        id=working_copy_id,
        into=GuestOwner(expired_session_id),
        file=working_copy_ref,
        at=now,
    )
    working_copy_bytes_marker = _sweep_marker("WORKINGCOPYBYTES")
    await cvs.add(working_copy)
    await committed_session.commit()
    await files.put(working_copy_ref, f"{working_copy_bytes_marker}\ncopy bytes".encode())

    # --- a genuine, unrelated orphan on the same volume, aged past the 48h floor -------------------
    orphan_ref = FileRef(key=f"{uuid4().hex[:2]}/{uuid4().hex[2:4]}/{uuid4()}.pdf")
    orphan_bytes_marker = _sweep_marker("ORPHANBYTES")
    await files.put(orphan_ref, f"{orphan_bytes_marker}\nnobody references this".encode())
    _age(tmp_path / orphan_ref.key, now - timedelta(hours=60))

    try:
        with caplog.at_level(logging.DEBUG):
            purge_exit = await purge_command._purge_guests(
                local_settings, dry_run=False, limit=None
            )
            orphan_exit = await purge_command._reclaim_orphans(
                local_settings, dry_run=False, limit=None, grace_hours=24
            )

        # --- vacuity guards: the runs must have actually happened -----------------------------------
        assert purge_exit == purge_command.EXIT_OK, caplog.text
        assert orphan_exit == purge_command.EXIT_OK, caplog.text
        assert "retention.purge_completed" in caplog.text
        assert "retention.orphan_scan_completed" in caplog.text

        # --- the purge took the expired session's working copy, and only that -------------------------
        assert not (tmp_path / working_copy_ref.key).exists(), (
            "the working copy's file must be gone after the purge"
        )
        assert (tmp_path / saved_ref.key).exists(), (
            "the saved CV's file must survive — it is user-owned, guest_session_id IS NULL"
        )
        assert (tmp_path / saved_ref.key).read_bytes() == (
            f"{saved_bytes_marker}\nthe saved CV's own bytes".encode()
        )

        # --- the orphan sweep took the planted orphan, and only that -----------------------------------
        assert not (tmp_path / orphan_ref.key).exists(), "the planted orphan must be reclaimed"
        assert (tmp_path / saved_ref.key).exists(), "the saved CV's file must survive the sweep too"

        # --- the actual privacy claim ------------------------------------------------------------------
        forbidden = {
            "the user's email": email_marker,
            "the saved CV's filename": filename_marker,
            "the saved CV's extracted text": extracted_text_marker,
            "the saved CV's file bytes": saved_bytes_marker,
            "the working copy's file bytes": working_copy_bytes_marker,
            "the orphan's file bytes": orphan_bytes_marker,
            "the guest session's token hash": token_hash_marker,
            "the saved CV's storage key": saved_ref.key,
            "the saved CV's joined path": str(tmp_path / saved_ref.key),
            "the working copy's storage key": working_copy_ref.key,
            "the working copy's joined path": str(tmp_path / working_copy_ref.key),
            "the orphan's storage key": orphan_ref.key,
            "the orphan's joined path": str(tmp_path / orphan_ref.key),
        }
        for description, marker in forbidden.items():
            assert marker not in caplog.text, (
                f"{description} ({marker!r}) appeared in a captured log record — AC-49. "
                f"Captured:\n{caplog.text}"
            )
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                guest_session_table.delete().where(guest_session_table.c.id == expired_session_id)
            )
        async with engine.begin() as conn:
            from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table

            await conn.execute(user_table.delete().where(user_table.c.id == user_id))
