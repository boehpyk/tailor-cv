"""AC-49 / AC-50's planted-marker privacy test for `intake-saved-base-cvs` (slice 2.2, T20).

Follows `test_auth_privacy_markers.py`'s (2.1, AC-46) and `test_purge_privacy_log_markers.py`'s
(1.6, AC-38) structure and their measured reason for using `caplog` rather than
`structlog.testing.capture_logs()` (see either module's docstring — `configure_logging()` rebuilds
the processor chain from scratch per `create_app()` call, and a module-level logger cached from an
earlier test keeps its own stale reference `capture_logs()` cannot reach; `caplog` reads the
already-flattened string every `logging.Logger` call produces, regardless of which chain rendered
it).

**This is the RED half of a red-first cycle, and its shape is deliberately different from
`test_saved_base_cvs.py`'s (T19).** T19 asserts *status codes* against the skeleton and reds on
`500 != <expected>`. This file's job is narrower and does not need every step to succeed to make its
own claim: it drives the whole marker-laden flow the spec describes, does not hard-fail when an
individual step is still `NotImplementedError` (T18's SKELETON — most of this slice's own routes
are), and instead asks two questions of *whatever actually ran*: (1) did any marker leak anywhere it
must not, and (2) does at least one captured log line carry the ids the Privacy section promises are
logged. Right now, with `upload_saved_base_cv`, `rename_saved_base_cv`, `delete_saved_base_cv`,
`copy_saved_base_cv` and `delete_account` all still bodies of one `raise NotImplementedError`, no
2.2-specific operation ever completes, so **the negative claim (no leak) holds vacuously and the
positive control (a `base_cv_id`-bearing line exists) is false** — this file reds on
`assert "base_cv_id" in ids`, never on an import and never on an unguarded `KeyError`/
`AttributeError` from a step that did not run. (An earlier draft of this file's positive control
checked *any* known id against the captured lines, including `user_id` — which passed **vacuously**,
because `register`'s own already-implemented `UserRegistered` event line already carries it. Scoping
the control to `base_cv_id` specifically, which only ever enters `ids` once the account upload
itself succeeds, is what makes this file's red mean something.) As T21 (and T22, the
`erase-account` CLI, not yet built at all) land, the same test drives further and its positive
control starts finding real lines, without an edit.

**Why every step below is "soft"** (records the response, proceeds only if it succeeded, never
raises on a non-2xx). A hard `assert response.status_code == 201` at the account-upload step would
make this file redundant with T19's own reds for the same reason, and — the actual point — would
make it *impossible* to reach the later steps (list, rename, copy, tailor, delete, delete-account)
at all right now, collapsing this file's whole "whatever actually ran" design into "nothing ran,
vacuously". The soft-continuation shape is what keeps every later step's marker still worth planting
today, and worth exercising for real the moment each skeleton goes GREEN.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Final
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.intake.value_objects import ExtractedText
from tailorcraft.domain.posting.value_objects import JobPostingText
from tailorcraft.infrastructure.redis_client import create_redis
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


def _ok(response: Response) -> bool:
    return response.status_code < 400


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Shadows `conftest.py`'s `client` fixture — `test_intake.py`'s/`test_auth.py`'s reason,
    sharpened for this file's "soft continuation" design (module docstring): with the default
    `raise_app_exceptions=True`, a `NotImplementedError` from an unimplemented step would escape
    `await client.post(...)` as a bare Python exception and abort the whole flow right there,
    collapsing every later marker this file plants into "never even attempted" instead of letting
    `_ok()` record the failure and move on. `raise_app_exceptions=False` turns it into an ordinary
    `500` response, which is what `_ok()` is built to read."""
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
    the copy (fake LLM, AC-50) -> delete the saved CV -> delete the account -> `erase-account` on a
    second marker user, softly. Then: no marker anywhere in `caplog`, in any response body outside
    its one legitimate channel, in any domain event field, or in any Redis key — and (the positive
    control) at least one captured line names the ids this flow is supposed to log."""
    marker_email = _marker_email("email")
    marker_filename = _marker("original-filename") + ".txt"
    marker_label = _marker("label")
    marker_cv_text = _marker("cv-body-text") * 20  # comfortably over the 200-char extraction floor
    marker_posting_text = _marker("posting-text") * 20

    responses: list[Response] = []
    ids: dict[str, str] = {}

    with caplog.at_level(logging.DEBUG):
        # --- register ------------------------------------------------------------------------
        register = await client.post(
            REGISTER_URL,
            json={"email": marker_email, "password": "correct horse battery staple 9"},
            headers={"Origin": settings.public_base_url},
        )
        responses.append(register)
        if _ok(register):
            body = register.json()
            token = str(body["access_token"])
            ids["user_id"] = str(body["user"]["id"])

            # --- upload to the account (T18 SKELETON: currently NotImplementedError) ----------
            uploaded = await client.post(
                ME_BASE_CVS_URL,
                files={"file": (marker_filename, marker_cv_text.encode(), "text/plain")},
                headers=_bearer(token),
            )
            responses.append(uploaded)
            if _ok(uploaded):
                saved_cv_id = str(uploaded.json()["id"])
                ids["base_cv_id"] = saved_cv_id

                # --- list --------------------------------------------------------------------
                responses.append(await client.get(ME_BASE_CVS_URL, headers=_bearer(token)))

                # --- rename (plants the label marker) -----------------------------------------
                renamed = await client.patch(
                    f"{ME_BASE_CVS_URL}/{saved_cv_id}",
                    json={"label": marker_label},
                    headers=_bearer(token),
                )
                responses.append(renamed)

                # --- copy into the workspace ---------------------------------------------------
                copied = await client.post(
                    COPIES_URL,
                    json={"saved_base_cv_id": saved_cv_id},
                    headers=_bearer(token),
                )
                responses.append(copied)
                if _ok(copied):
                    working_copy_id = str(copied.json()["id"])
                    ids["working_copy_id"] = working_copy_id

                    # --- a tailoring run on the working copy, fake LLM (AC-50) ------------------
                    posting = await client.post(
                        JOB_POSTINGS_URL,
                        json={"source": "pasted", "text": marker_posting_text},
                    )
                    responses.append(posting)
                    if _ok(posting):
                        fake_llm_request = await _try_tailor(
                            client,
                            app,
                            session,
                            settings,
                            working_copy_id,
                            str(posting.json()["id"]),
                        )
                        if fake_llm_request is not None:
                            cv_sent, posting_sent = fake_llm_request
                            assert marker_email not in cv_sent.value
                            assert ids["user_id"] not in cv_sent.value
                            assert saved_cv_id not in cv_sent.value
                            assert marker_label not in cv_sent.value
                            assert marker_cv_text in cv_sent.value, (
                                "the working copy's own text IS the legitimate channel"
                            )
                            assert marker_email not in posting_sent.value
                            assert ids["user_id"] not in posting_sent.value

                # --- delete the saved CV ---------------------------------------------------------
                responses.append(
                    await client.delete(f"{ME_BASE_CVS_URL}/{saved_cv_id}", headers=_bearer(token))
                )

            # --- delete the account ---------------------------------------------------------------
            deleted_account = await client.post(
                DELETE_ACCOUNT_URL,
                json={"password": "correct horse battery staple 9"},
                headers={**_bearer(token), "Origin": settings.public_base_url},
            )
            responses.append(deleted_account)

        # --- erase-account on a SECOND marker user, via the operator CLI (T22 — not yet built at
        # all) ------------------------------------------------------------------------------------
        second_marker_email = _marker_email("second-user-email")
        second_register = await client.post(
            REGISTER_URL,
            json={"email": second_marker_email, "password": "correct horse battery staple 9"},
            headers={"Origin": settings.public_base_url},
        )
        responses.append(second_register)
        if _ok(second_register):
            ids["second_user_id"] = str(second_register.json()["user"]["id"])
            await _try_erase_account_cli(ids["second_user_id"])

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

    never_in_a_response_body = {
        "the marker filename": marker_filename,  # not an error-body concern; never in the body at all
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

    # --- The positive control (CLAUDE.md: an absence assertion proves nothing on its own; pair it
    # with a discriminating positive) — this is the row this file is expected to red on today.
    #
    # Deliberately scoped to `base_cv_id` specifically, not "any id this run happens to know about".
    # `register`'s own (already-implemented, 2.1) `UserRegistered`/`LoggedIn` event lines already
    # carry `user_id` — checking against every id in `ids` indiscriminately would let that
    # pre-existing, unrelated line satisfy the control every time, which is exactly the vacuous-pass
    # trap CLAUDE.md names (a test that cannot fail is not a test). `base_cv_id` only ever enters
    # `ids` if the account upload actually returned 2xx, which today's skeleton cannot do — so this
    # is the row that must fail until T21 lands, and the one line above (`assert "base_cv_id" in
    # ids`) is *why* it fails: not a KeyError, a named assertion with its own message. -------------
    assert "base_cv_id" in ids, (
        "the account upload never completed (still T18's NotImplementedError), so there is no "
        "saved-CV id to look for in the logs yet — this is the expected red before T21. Responses "
        f"collected this run: {[(r.request.url.path, r.status_code) for r in responses]!r}"
    )
    base_cv_id = ids["base_cv_id"]
    base_cv_id_lines = [record for record in caplog.records if base_cv_id in record.getMessage()]
    assert base_cv_id_lines, (
        f"the saved CV {base_cv_id!r} was created, but no captured log line names it — the "
        "Privacy section promises base_cv_id is logged on the operations that touch one "
        f"(uploaded, renamed, copied, deleted). Captured text:\n{caplog.text}"
    )


async def _try_tailor(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    base_cv_id: str,
    job_posting_id: str,
) -> tuple[ExtractedText, JobPostingText] | None:
    """Queue a tailoring run on the working copy and execute it through the real
    `ExecuteTailoringRun`, bound to this test's own session — `test_tailoring.py`'s own
    `_a_draft`/`_install_worker_llm`/`_run_worker`, reused rather than re-derived (their exact
    field shapes — `TailoredDraft(documents=..., metrics=...)` — are that file's to own). Returns
    the fake LLM's `(cv, posting)` call arguments, or `None` if the run could not even be queued
    (the copy's own route is still `NotImplementedError` today, so `base_cv_id` here is never
    reachable yet)."""
    from tailorcraft.infrastructure.api.deps import get_tailoring_queue
    from tests.api.test_tailoring import _a_draft, _install_worker_llm, _run_worker
    from tests.integration.fakes import FakeLlm, FakeTailoringQueue

    app.dependency_overrides[get_tailoring_queue] = lambda: FakeTailoringQueue()

    created = await client.post(
        TAILORING_RUNS_URL, json={"base_cv_id": base_cv_id, "job_posting_id": job_posting_id}
    )
    if not _ok(created):
        return None

    fake_llm = FakeLlm(_a_draft())

    with pytest.MonkeyPatch.context() as mp:
        _install_worker_llm(mp, fake_llm)
        await _run_worker(session, settings, str(created.json()["id"]))

    if not fake_llm.calls:
        return None
    return fake_llm.calls[0]


async def _try_erase_account_cli(user_id: str) -> None:
    """`python -m tailorcraft.cli erase-account --user-id ... --dry-run` — T22, not built at all
    yet (task-list.md). Tries the real entry point and swallows exactly the "not built" shape
    (`ImportError`/`AttributeError`/`SystemExit`) rather than failing this file over a task that is
    not this one's to implement; any other exception is a real bug and must still surface."""
    try:
        from tailorcraft.infrastructure.retention.erase_account_command import (  # type: ignore[import-not-found]
            run_erase_account_command,
        )
    except ImportError:
        return
    with contextlib.suppress(SystemExit):
        run_erase_account_command(["--user-id", user_id, "--dry-run"])
