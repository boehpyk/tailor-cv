"""API tests for `intake-saved-base-cvs`'s registered-user HTTP surface: `/api/me/base-cvs*`
(slice 2.2, T19 — AC-20…AC-27 and the S-rows the account-upload/list/rename/delete routes can
produce).

**This is the RED half of a red-first cycle** (CLAUDE.md, sdlc.md §2). `routers/saved_base_cvs.py`'s
four handlers currently do nothing but `raise NotImplementedError` (T18's SKELETON) — every test here
is written against `docs/specs/intake-saved-base-cvs/feature-spec.md`'s failure contract and
`technical-plan.md`'s API contract, never against the handler bodies.

**Why this file's `client` fixture is not the shared one** — `test_intake.py`'s and `test_auth.py`'s
reason, unchanged: `tests/conftest.py`'s `client` uses `ASGITransport(app=app)`, whose default
`raise_app_exceptions=True` re-raises an unhandled handler exception as a bare Python exception,
turning every `NotImplementedError` into an ERROR rather than a `FAIL`. This module's own `client`
sets `raise_app_exceptions=False`, so `response.status_code` is a real `500` and an assertion like
`assert response.status_code == 201` fails on the assertion — `assert 500 == 201` — the way CLAUDE.md
wants a red to read.

**What already passes, and why.** `require_user` (the bearer dependency) is 2.1's own code, resolved
before any handler body runs — every bearer refusal (missing, forged, expired) already answers 401
`invalid_access_token`. `RenameSavedBaseCvRequest`'s own field validation (a missing `label` key, a
label over the 200-character wire cap) is FastAPI's, resolved before the handler runs. Both are noted
at the point where a test turns out to already pass.

**Fault injection is below the adapter floor, never on the adapter's own method** (CLAUDE.md's I-45
correction): storage failures point `upload_dir` at a non-directory (so `LocalFileStore`'s own
`OSError` path fires for real) or plant a real symlink on a real filesystem; commit failures
monkeypatch the request's own `session.commit`, never a repository method.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.api.deps import get_app_settings
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.settings import Settings

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "cvs"

ME_BASE_CVS_URL = "/api/me/base-cvs"
REGISTER_URL = "/api/auth/register"
BASE_CVS_URL = "/api/base-cvs"

A_PASSWORD = "correct horse battery staple 9"  # clear of the 12-char floor


# ---------------------------------------------------------------------------------------------
# Small HTTP / fixture helpers — mirrors test_intake.py's / test_auth.py's shapes
# ---------------------------------------------------------------------------------------------


def _read_fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def _file_part(filename: str, data: bytes, content_type: str) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (filename, data, content_type)}


def _error_code(response: Response) -> str:
    body = response.json()
    assert "error" in body, f"expected the {{'error': {{...}}}} envelope, got {body!r}"
    return str(body["error"]["code"])


def _error_message(response: Response) -> str:
    body = response.json()
    assert "error" in body, f"expected the {{'error': {{...}}}} envelope, got {body!r}"
    return str(body["error"]["message"])


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _override_settings(app: FastAPI, base: Settings, **updates: object) -> Settings:
    """`test_intake.py`'s helper, unchanged: both settings-reading paths this codebase uses
    (`SettingsDep` and `request.app.state.settings`) are overridden together."""
    modified = base.model_copy(update=updates)
    app.dependency_overrides[get_app_settings] = lambda: modified
    app.state.settings = modified
    return modified


# ---------------------------------------------------------------------------------------------
# Module-local fixtures
# ---------------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Shadows `conftest.py`'s `client` fixture for every test in this module — see the module
    docstring for why `raise_app_exceptions=False` matters here specifically."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def _new_client(app: FastAPI) -> AsyncClient:
    """A second, independent client against the same app — for tests that need a genuinely
    concurrent second request (S-19's race) or a distinct "requester" while sharing `ASGITransport`'s
    fixed peer IP."""
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver"
    )


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """Applies `clear_redis` (conftest.py) to every test in this module automatically — nearly every
    test here touches the upload rate limiter, and the database rollback never reaches Redis."""


# ---------------------------------------------------------------------------------------------
# Seeding: a real registered user with a real bearer token, through the real (already-implemented,
# 2.1) register endpoint — not a hand-built JWT, so these tests exercise the exact token `require_user`
# sees in production.
# ---------------------------------------------------------------------------------------------


async def _register(
    client: AsyncClient, settings: Settings, *, email: str | None = None
) -> tuple[str, str]:
    """Register a fresh user. Returns `(access_token, user_id)`."""
    email = email or f"t19-{uuid4().hex}@example.com"
    response = await client.post(
        REGISTER_URL,
        json={"email": email, "password": A_PASSWORD},
        headers={"Origin": settings.public_base_url},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return str(body["access_token"]), str(body["user"]["id"])


async def _upload_saved(
    client: AsyncClient,
    token: str,
    *,
    filename: str = "sample.txt",
    data: bytes | None = None,
    content_type: str = "text/plain",
) -> Response:
    payload = data if data is not None else _read_fixture(filename)
    return await client.post(
        ME_BASE_CVS_URL, files=_file_part(filename, payload, content_type), headers=_bearer(token)
    )


async def _upload_extracted_saved_cv(client: AsyncClient, token: str) -> str:
    """A saved CV whose extraction succeeded — the common precondition for rename/delete tests."""
    response = await _upload_saved(client, token)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extracted", body
    return str(body["id"])


async def _delete_user_row(session: AsyncSession, user_id: str) -> None:
    """Erase the `identity_user` row directly — the "valid bearer, account gone" precondition
    (S-2), without going through the not-yet-built `DeleteOwnAccount` route. A raw DELETE on the
    request's own session: subsequent reads on the same connection see it immediately (no commit
    needed for *this* test's own later calls, but a commit is issued anyway so the row is genuinely
    gone under any later query too, matching what "erased" means)."""
    await session.execute(text("DELETE FROM identity_user WHERE id = :id"), {"id": UUID(user_id)})
    await session.commit()


# ---------------------------------------------------------------------------------------------
# AC-20 — pinned key sets, exact statuses and codes
# ---------------------------------------------------------------------------------------------


_SAVED_BASE_CV_RESPONSE_KEYS = {
    "id",
    "label",
    "original_filename",
    "content_type",
    "size_bytes",
    "status",
    "character_count",
    "failure_reason",
    "failure_message",
    "uploaded_at",
}


async def test_upload_response_key_set_is_pinned_and_has_no_expires_at_or_text(
    client: AsyncClient, settings: Settings
) -> None:
    token, _ = await _register(client, settings)
    response = await _upload_saved(client, token)

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == _SAVED_BASE_CV_RESPONSE_KEYS, set(body)
    assert "expires_at" not in body, "a saved CV never expires"
    assert "extracted_text" not in body
    assert body["label"] is None


async def test_list_response_key_set_is_pinned(client: AsyncClient, settings: Settings) -> None:
    token, _ = await _register(client, settings)
    await _upload_extracted_saved_cv(client, token)

    response = await client.get(ME_BASE_CVS_URL, headers=_bearer(token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items"}
    assert len(body["items"]) == 1
    assert set(body["items"][0]) == _SAVED_BASE_CV_RESPONSE_KEYS


async def test_patch_response_key_set_is_pinned(client: AsyncClient, settings: Settings) -> None:
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)

    response = await client.patch(
        f"{ME_BASE_CVS_URL}/{cv_id}", json={"label": "Backend roles"}, headers=_bearer(token)
    )

    assert response.status_code == 200, response.text
    assert set(response.json()) == _SAVED_BASE_CV_RESPONSE_KEYS


async def test_delete_has_no_body(client: AsyncClient, settings: Settings) -> None:
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)

    response = await client.delete(f"{ME_BASE_CVS_URL}/{cv_id}", headers=_bearer(token))

    assert response.status_code == 204, response.text
    assert response.content == b""


async def test_cache_control_no_store_on_every_me_response(
    client: AsyncClient, settings: Settings
) -> None:
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)

    responses = [
        await client.get(ME_BASE_CVS_URL, headers=_bearer(token)),
        await client.patch(
            f"{ME_BASE_CVS_URL}/{cv_id}", json={"label": None}, headers=_bearer(token)
        ),
    ]
    for response in responses:
        assert response.headers.get("cache-control") == "no-store", (
            response.status_code,
            response.headers,
        )


# ---------------------------------------------------------------------------------------------
# AC-21 / S-1 / S-2 — `require_user` only; a valid bearer whose account is gone
# ---------------------------------------------------------------------------------------------


async def test_a_valid_guest_cookie_riding_along_changes_nothing_for_the_list(
    client: AsyncClient, settings: Settings
) -> None:
    """A `tc_guest` cookie is never read by this router (module docstring, AC-21): minting one via a
    guest upload first must not authorize `GET /api/me/base-cvs` on its own."""
    guest_upload = await client.post(
        BASE_CVS_URL, files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain")
    )
    assert guest_upload.status_code == 201, guest_upload.text  # mints tc_guest on this client

    response = await client.get(ME_BASE_CVS_URL)  # no bearer, but the guest cookie rides along

    assert response.status_code == 401, response.text
    assert _error_code(response) == "invalid_access_token"


async def test_missing_bearer_is_401_invalid_access_token_with_www_authenticate(
    client: AsyncClient,
) -> None:
    response = await client.get(ME_BASE_CVS_URL)

    assert response.status_code == 401, response.text
    assert _error_code(response) == "invalid_access_token"
    assert response.headers.get("www-authenticate") == 'Bearer error="invalid_token"'


async def test_forged_bearer_is_401_invalid_access_token(client: AsyncClient) -> None:
    response = await client.get(
        ME_BASE_CVS_URL, headers={"Authorization": "Bearer not-a-real-token"}
    )

    assert response.status_code == 401, response.text
    assert _error_code(response) == "invalid_access_token"


async def test_valid_bearer_whose_account_is_gone_is_401_not_signed_in_on_list(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    token, user_id = await _register(client, settings)
    await _delete_user_row(session, user_id)

    response = await client.get(ME_BASE_CVS_URL, headers=_bearer(token))

    assert response.status_code == 401, response.text
    assert _error_code(response) == "not_signed_in"


async def test_valid_bearer_whose_account_is_gone_stores_nothing_on_upload(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    """S-2. Checked before any file write: an erased account's bearer must not leave an orphan file
    behind either."""
    token, user_id = await _register(client, settings)
    await _delete_user_row(session, user_id)

    response = await _upload_saved(client, token)

    assert response.status_code == 401, response.text
    assert _error_code(response) == "not_signed_in"


# ---------------------------------------------------------------------------------------------
# AC-22 — user A cannot touch user B's, or a guest-owned, CV
# ---------------------------------------------------------------------------------------------


async def test_user_a_patching_user_bs_cv_is_404_base_cv_not_found(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    token_a, _ = await _register(client, settings)
    async with _new_client(app) as client_b:
        token_b, _ = await _register(client_b, settings)
        cv_b_id = await _upload_extracted_saved_cv(client_b, token_b)

    response = await client.patch(
        f"{ME_BASE_CVS_URL}/{cv_b_id}", json={"label": "stolen"}, headers=_bearer(token_a)
    )

    assert response.status_code == 404, response.text
    assert _error_code(response) == "base_cv_not_found"


async def test_user_a_deleting_user_bs_cv_is_404_and_bs_row_and_file_survive(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    token_a, _ = await _register(client, settings)
    async with _new_client(app) as client_b:
        token_b, _ = await _register(client_b, settings)
        cv_b_id = await _upload_extracted_saved_cv(client_b, token_b)

        response = await client.delete(f"{ME_BASE_CVS_URL}/{cv_b_id}", headers=_bearer(token_a))
        assert response.status_code == 404, response.text
        assert _error_code(response) == "base_cv_not_found"

        still_there = await client_b.get(ME_BASE_CVS_URL, headers=_bearer(token_b))
        assert still_there.status_code == 200, still_there.text
        assert [item["id"] for item in still_there.json()["items"]] == [cv_b_id]


async def test_a_guest_owned_id_on_a_user_route_is_404_byte_identical_to_nonexistent(
    client: AsyncClient, settings: Settings
) -> None:
    token, _ = await _register(client, settings)
    guest_upload = await client.post(
        BASE_CVS_URL, files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain")
    )
    assert guest_upload.status_code == 201, guest_upload.text
    guest_cv_id = guest_upload.json()["id"]

    on_guest_owned = await client.patch(
        f"{ME_BASE_CVS_URL}/{guest_cv_id}", json={"label": "mine now"}, headers=_bearer(token)
    )
    on_nonexistent = await client.patch(
        f"{ME_BASE_CVS_URL}/{uuid4()}", json={"label": "mine now"}, headers=_bearer(token)
    )

    assert on_guest_owned.status_code == 404, on_guest_owned.text
    assert on_nonexistent.status_code == 404, on_nonexistent.text
    assert on_guest_owned.json() == on_nonexistent.json()


async def test_list_never_contains_another_owner_or_a_guest_row(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    await client.post(
        BASE_CVS_URL, files=_file_part("sample.txt", _read_fixture("sample.txt"), "text/plain")
    )  # a guest-owned row, on the same underlying app

    token_a, _ = await _register(client, settings)
    cv_a_id = await _upload_extracted_saved_cv(client, token_a)

    async with _new_client(app) as client_b:
        token_b, _ = await _register(client_b, settings)
        await _upload_extracted_saved_cv(client_b, token_b)

    response = await client.get(ME_BASE_CVS_URL, headers=_bearer(token_a))

    assert response.status_code == 200, response.text
    ids = [item["id"] for item in response.json()["items"]]
    assert ids == [cv_a_id]


# ---------------------------------------------------------------------------------------------
# AC-25 / S-3 / S-4 / S-6 / S-7 / S-8 — upload: 1.1's boundary, the cap, rate limits, extraction
# ---------------------------------------------------------------------------------------------


async def test_missing_file_part_is_422_missing_file(
    client: AsyncClient, settings: Settings
) -> None:
    token, _ = await _register(client, settings)
    response = await client.post(ME_BASE_CVS_URL, headers=_bearer(token))

    assert response.status_code == 422, response.text
    assert _error_code(response) == "missing_file"


async def test_unsupported_format_is_415(client: AsyncClient, settings: Settings) -> None:
    token, _ = await _register(client, settings)
    response = await _upload_saved(
        client,
        token,
        filename="not-a-pdf.pdf",
        data=_read_fixture("not-a-pdf.pdf"),
        content_type="application/pdf",
    )

    assert response.status_code == 415, response.text
    assert _error_code(response) == "unsupported_format"


async def test_at_the_cap_the_next_upload_is_409_and_writes_no_file(
    client: AsyncClient, app: FastAPI, settings: Settings, tmp_path: Path
) -> None:
    modified = _override_settings(app, settings, max_saved_base_cvs_per_user=2)
    token, _ = await _register(client, settings)

    first = await _upload_saved(client, token, filename="a.txt")
    second = await _upload_saved(client, token, filename="b.txt")
    third = await _upload_saved(client, token, filename="c.txt")

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert third.status_code == 409, third.text
    assert _error_code(third) == "too_many_saved_base_cvs"
    assert "2" in _error_message(third), (
        f"S-4: the message must name the cap ({modified.max_saved_base_cvs_per_user}); "
        f"got {_error_message(third)!r}"
    )

    listing = await client.get(ME_BASE_CVS_URL, headers=_bearer(token))
    assert len(listing.json()["items"]) == 2, "the refused upload must not have written a row"


async def test_more_than_the_per_user_hourly_limit_returns_429(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    _override_settings(app, settings, upload_rate_limit_per_hour=2)
    token, _ = await _register(client, settings)

    first = await _upload_saved(client, token, filename="a.txt")
    second = await _upload_saved(client, token, filename="b.txt")
    third = await _upload_saved(client, token, filename="c.txt")

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert third.status_code == 429, third.text
    assert _error_code(third) == "rate_limited"
    assert "retry-after" in {name.lower() for name in third.headers}


async def test_redis_unavailable_fails_open_for_the_account_upload(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    """S-7, 1.1's rule (fail open, the cost is ours): pointing `redis_url` at a port nothing listens
    on must not block the account upload. Registration happens **before** the override — the
    login/register limiters fail *closed* (2.1's rule, the opposite of the upload limiter's), so a
    broken Redis at that point would refuse the registration itself rather than exercise S-7."""
    token, _ = await _register(client, settings)
    unreachable = _override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await _upload_saved(client, token)

    assert response.status_code == 201, response.text
    assert unreachable.redis_url != settings.redis_url


async def test_extraction_failure_is_a_201_with_the_reason_and_the_row_kept(
    client: AsyncClient, settings: Settings
) -> None:
    token, _ = await _register(client, settings)

    response = await _upload_saved(
        client, token, filename="tiny.txt", data=_read_fixture("tiny.txt")
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extraction_failed"
    assert body["failure_reason"] == "too_short"
    assert body["failure_message"]

    listed = await client.get(ME_BASE_CVS_URL, headers=_bearer(token))
    assert [item["id"] for item in listed.json()["items"]] == [body["id"]], (
        "the row is kept so the user can see it, and can delete it"
    )


async def test_account_erased_between_the_cap_check_and_the_insert_is_401_not_signed_in(
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S-12. The FK check (`fk_intake_base_cv_user_id_identity_user`), fired for real: the count
    check the use case makes between resolving the user and inserting the row is the injection
    point, patched below the repository's own translation logic (not around it) — the DELETE really
    runs, on the request's own connection, so the later real INSERT really violates the real FK and
    the real adapter really has to translate it."""
    from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
        SqlAlchemyBaseCvRepository,
    )

    token, user_id = await _register(client, settings)

    original_count_for_user = SqlAlchemyBaseCvRepository.count_for_user

    async def _count_then_erase(self: SqlAlchemyBaseCvRepository, uid: UserId) -> int:
        count = await original_count_for_user(self, uid)
        await session.execute(
            text("DELETE FROM identity_user WHERE id = :id"), {"id": UUID(user_id)}
        )
        return count

    monkeypatch.setattr(SqlAlchemyBaseCvRepository, "count_for_user", _count_then_erase)

    response = await _upload_saved(client, token)

    assert response.status_code == 401, response.text
    assert _error_code(response) == "not_signed_in"

    rows = await session.execute(
        text("SELECT count(*) FROM intake_base_cv WHERE user_id = :id"), {"id": UUID(user_id)}
    )
    assert rows.scalar_one() == 0, "the refused INSERT must not have landed a row"


# ---------------------------------------------------------------------------------------------
# S-9 / S-10 — storage write fails; the commit fails after the file write
# ---------------------------------------------------------------------------------------------


async def test_storage_write_failure_returns_503_storage_unavailable(
    client: AsyncClient, app: FastAPI, settings: Settings, tmp_path: Path
) -> None:
    blocking_file = tmp_path / "not_a_directory"
    blocking_file.write_bytes(b"x")
    _override_settings(app, settings, upload_dir=blocking_file)
    token, _ = await _register(client, settings)

    response = await _upload_saved(client, token)

    assert response.status_code == 503, response.text
    assert _error_code(response) == "storage_unavailable"


async def test_commit_failure_after_writing_the_file_returns_503(
    client: AsyncClient, settings: Settings, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, _ = await _register(client, settings)

    async def _raise_operational_error() -> None:
        raise OperationalError("simulated commit failure (S-10)", {}, Exception("connection lost"))

    monkeypatch.setattr(session, "commit", _raise_operational_error)

    response = await _upload_saved(client, token)

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"


# ---------------------------------------------------------------------------------------------
# AC-26 / S-14 / S-15 / S-16 / S-17 — rename
# ---------------------------------------------------------------------------------------------


async def test_rename_sets_the_label(client: AsyncClient, settings: Settings) -> None:
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)

    response = await client.patch(
        f"{ME_BASE_CVS_URL}/{cv_id}", json={"label": "Backend roles"}, headers=_bearer(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["label"] == "Backend roles"


async def test_rename_with_null_clears_an_existing_label(
    client: AsyncClient, settings: Settings
) -> None:
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)
    await client.patch(f"{ME_BASE_CVS_URL}/{cv_id}", json={"label": "x"}, headers=_bearer(token))

    response = await client.patch(
        f"{ME_BASE_CVS_URL}/{cv_id}", json={"label": None}, headers=_bearer(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["label"] is None


async def test_rename_with_a_script_tag_is_stored_and_returned_as_plain_text(
    client: AsyncClient, settings: Settings
) -> None:
    """AC-26/AC-47: the API never sanitizes or escapes it — that is the React surface's job (it
    renders text nodes). This layer's only claim is that the bytes it was given round-trip exactly."""
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)
    marker = "<script>alert(1)</script>"

    response = await client.patch(
        f"{ME_BASE_CVS_URL}/{cv_id}", json={"label": marker}, headers=_bearer(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["label"] == marker


async def test_rename_with_an_empty_after_strip_label_is_422_invalid_label(
    client: AsyncClient, settings: Settings
) -> None:
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)

    response = await client.patch(
        f"{ME_BASE_CVS_URL}/{cv_id}", json={"label": "   "}, headers=_bearer(token)
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "invalid_label"


async def test_rename_with_a_missing_label_key_is_422_validation_error(
    client: AsyncClient, settings: Settings
) -> None:
    """`label` is required (technical-plan.md, `RenameSavedBaseCvRequest`): `{}` must not be read as
    a silent "clear"."""
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)

    response = await client.patch(f"{ME_BASE_CVS_URL}/{cv_id}", json={}, headers=_bearer(token))

    assert response.status_code == 422, response.text


async def test_rename_a_malformed_id_is_422_not_404(
    client: AsyncClient, settings: Settings
) -> None:
    token, _ = await _register(client, settings)
    response = await client.patch(
        f"{ME_BASE_CVS_URL}/not-a-uuid", json={"label": "x"}, headers=_bearer(token)
    )

    assert response.status_code == 422, response.text


async def test_rename_after_the_row_is_already_deleted_is_404(
    client: AsyncClient, settings: Settings
) -> None:
    """S-16: rename races a delete — the delete having already won, by the time this rename runs."""
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)
    deleted = await client.delete(f"{ME_BASE_CVS_URL}/{cv_id}", headers=_bearer(token))
    assert deleted.status_code == 204, deleted.text

    response = await client.patch(
        f"{ME_BASE_CVS_URL}/{cv_id}", json={"label": "too late"}, headers=_bearer(token)
    )

    assert response.status_code == 404, response.text
    assert _error_code(response) == "base_cv_not_found"


async def test_two_renames_the_later_one_stands(client: AsyncClient, settings: Settings) -> None:
    """S-17: no lock, stated on purpose — a label has no invariant across writes."""
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)

    first = await client.patch(
        f"{ME_BASE_CVS_URL}/{cv_id}", json={"label": "first"}, headers=_bearer(token)
    )
    second = await client.patch(
        f"{ME_BASE_CVS_URL}/{cv_id}", json={"label": "second"}, headers=_bearer(token)
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    listed = await client.get(ME_BASE_CVS_URL, headers=_bearer(token))
    assert listed.json()["items"][0]["label"] == "second"


# ---------------------------------------------------------------------------------------------
# AC-27 / S-13 / S-18 / S-19 / S-20 / S-22 / S-23 / S-24 — delete
# ---------------------------------------------------------------------------------------------


async def test_a_user_with_no_saved_cvs_gets_an_empty_list(
    client: AsyncClient, settings: Settings
) -> None:
    token, _ = await _register(client, settings)
    response = await client.get(ME_BASE_CVS_URL, headers=_bearer(token))

    assert response.status_code == 200, response.text
    assert response.json() == {"items": []}


async def test_delete_removes_the_row_and_the_file(client: AsyncClient, settings: Settings) -> None:
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)
    ref_path = settings.upload_dir / _base_cv_key(cv_id, "txt")
    assert ref_path.exists(), "setup sanity: the file must exist before the delete"

    response = await client.delete(f"{ME_BASE_CVS_URL}/{cv_id}", headers=_bearer(token))

    assert response.status_code == 204, response.text
    listing = await client.get(ME_BASE_CVS_URL, headers=_bearer(token))
    assert listing.json()["items"] == []
    assert not ref_path.exists(), "the file must be gone after the delete"


async def test_the_row_is_committed_before_the_file_is_unlinked(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-27, S-18. A `LocalFileStore.delete` wrapper checks, at the exact moment it is called,
    whether the row is already gone **on the request's own connection** — the fixture's SAVEPOINT
    design (`conftest.py`) means every commit issued by the handler is visible on that same
    connection immediately, so this is the same proof AC-27 describes ("a separate connection"),
    adapted to a fixture where a genuinely separate connection could never see an uncommitted outer
    transaction at all (`test_tailoring.py`'s `_run_worker` docstring records the identical
    constraint for the worker's own composition root)."""
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)

    row_present_at_unlink_time: list[bool] = []
    original_delete = LocalFileStore.delete

    async def _checking_delete(self: LocalFileStore, ref: FileRef) -> None:
        result = await session.execute(
            text("SELECT count(*) FROM intake_base_cv WHERE id = :id"), {"id": UUID(cv_id)}
        )
        row_present_at_unlink_time.append(result.scalar_one() > 0)
        await original_delete(self, ref)

    monkeypatch.setattr(LocalFileStore, "delete", _checking_delete)

    response = await client.delete(f"{ME_BASE_CVS_URL}/{cv_id}", headers=_bearer(token))

    assert response.status_code == 204, response.text
    assert row_present_at_unlink_time == [False], (
        "the row must already be committed-gone at the moment the file is unlinked"
    )


async def test_a_second_delete_is_404(client: AsyncClient, settings: Settings) -> None:
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)

    first = await client.delete(f"{ME_BASE_CVS_URL}/{cv_id}", headers=_bearer(token))
    second = await client.delete(f"{ME_BASE_CVS_URL}/{cv_id}", headers=_bearer(token))

    assert first.status_code == 204, first.text
    assert second.status_code == 404, second.text
    assert _error_code(second) == "base_cv_not_found"


async def test_two_concurrent_deletes_exactly_one_204_one_404_no_5xx(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)

    async with _new_client(app) as second_client:
        results = await asyncio.gather(
            client.delete(f"{ME_BASE_CVS_URL}/{cv_id}", headers=_bearer(token)),
            second_client.delete(f"{ME_BASE_CVS_URL}/{cv_id}", headers=_bearer(token)),
        )

    statuses = sorted(r.status_code for r in results)
    assert statuses == [204, 404], [r.text for r in results]

    listing = await client.get(ME_BASE_CVS_URL, headers=_bearer(token))
    assert listing.json()["items"] == []


async def test_delete_when_the_commit_fails_returns_503_and_keeps_the_row_and_file(
    client: AsyncClient, settings: Settings, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-20. The delete route's own commit (`CommittingBaseCvRemoval`) is on the same session this
    fixture wires up, so patching `session.commit` reaches it exactly as it reaches the upload's."""
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)

    async def _raise_operational_error() -> None:
        raise OperationalError("simulated commit failure (S-20)", {}, Exception("connection lost"))

    monkeypatch.setattr(session, "commit", _raise_operational_error)

    response = await client.delete(f"{ME_BASE_CVS_URL}/{cv_id}", headers=_bearer(token))

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"

    # Read directly off the same (now genuinely rolled-back) session — no second HTTP request,
    # which would need its *own* working commit and would just re-hit the same patched failure.
    row_count = await session.execute(
        text("SELECT count(*) FROM intake_base_cv WHERE id = :id"), {"id": UUID(cv_id)}
    )
    assert row_count.scalar_one() == 1, "the row must survive a failed commit"
    ref_path = settings.upload_dir / _base_cv_key(cv_id, "txt")
    assert ref_path.exists(), (
        "the file must survive a failed commit too — the unlink must never run"
    )


async def test_delete_when_the_file_is_already_missing_is_still_204(
    client: AsyncClient, settings: Settings
) -> None:
    """S-22: the crash's survivor from a different angle — the file is gone from under the row
    before the delete ever runs; `FileStorePort.delete` is `missing_ok`."""
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)
    ref_path = settings.upload_dir / _base_cv_key(cv_id, "txt")
    ref_path.unlink()

    response = await client.delete(f"{ME_BASE_CVS_URL}/{cv_id}", headers=_bearer(token))

    assert response.status_code == 204, response.text


async def test_delete_with_a_symlink_planted_at_the_keys_own_key_leaves_the_target_untouched(
    client: AsyncClient, settings: Settings, tmp_path: Path
) -> None:
    """S-23, on a real filesystem. `LocalFileStore._resolve_contained` refuses a symlink found at
    the *final* key outright (1.6's O_NOFOLLOW / parent-only-resolve hardening — verified directly
    against `test_local_file_store.py`): the delete still answers 204 (the use case's `_unlink`
    catches every exception into `file_unlinked=False`, S-24's own path), but nothing on disk is
    touched at all — neither the link nor its target. That is a **stronger** guarantee than the
    spec's "removes the link, never its target" reading of S-23 as literally "the link disappears";
    the shipped adapter's answer is "nothing at a key it does not trust is touched", which this test
    asserts directly rather than the softer reading."""
    token, _ = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)
    ref_path = settings.upload_dir / _base_cv_key(cv_id, "txt")

    victim_dir = tmp_path / "elsewhere"
    victim_dir.mkdir(exist_ok=True)
    victim_path = victim_dir / "a-strangers-live-file.bin"
    victim_path.write_bytes(b"a stranger's live, referenced file")

    ref_path.unlink()
    os.symlink(victim_path, ref_path)

    response = await client.delete(f"{ME_BASE_CVS_URL}/{cv_id}", headers=_bearer(token))

    assert response.status_code == 204, response.text
    assert victim_path.exists(), "the symlink's target must survive"
    assert victim_path.read_bytes() == b"a stranger's live, referenced file"
    assert ref_path.is_symlink(), "the planted link itself must be untouched, not unlinked"


async def test_delete_when_the_unlink_fails_is_still_204_and_logs_a_warning(
    client: AsyncClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S-24. A failing unlink after the commit does not raise: the row is gone from everything the
    user can reach, and a 5xx would be a lie in the other direction. The warning line names the CV
    and the user, never a path or the exception's message."""
    token, user_id = await _register(client, settings)
    cv_id = await _upload_extracted_saved_cv(client, token)

    async def _raise_eio(self: LocalFileStore, ref: FileRef) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(LocalFileStore, "delete", _raise_eio)

    with caplog.at_level(logging.WARNING):
        response = await client.delete(f"{ME_BASE_CVS_URL}/{cv_id}", headers=_bearer(token))

    assert response.status_code == 204, response.text

    warning_lines = [
        r for r in caplog.records if "intake.saved_base_cv_file_unlink_failed" in r.getMessage()
    ]
    assert warning_lines, f"expected a warning line, captured:\n{caplog.text}"
    line = warning_lines[0]
    assert line.levelname == "WARNING"
    message = line.getMessage()
    assert cv_id in message
    assert user_id in message
    assert "OSError" in message


def _base_cv_key(cv_id: str, extension: str) -> str:
    """The storage key `FileRef.for_base_cv` derives from an id — computed via the real function
    rather than hand-derived, so this test cannot drift from ADR-0011's own sharding rule."""
    content_type = {"txt": CvContentType.TXT, "pdf": CvContentType.PDF, "docx": CvContentType.DOCX}[
        extension
    ]
    return FileRef.for_base_cv(BaseCvId(UUID(cv_id)), content_type).key
