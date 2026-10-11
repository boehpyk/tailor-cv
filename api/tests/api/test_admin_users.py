"""Slice 4.2, T14 RED: the admin users router's HTTP contract (AC-17 ... AC-23, AC-25 (a)(b), AC-26).

Written from the spec against the T13 skeleton (every handler raises `NotImplementedError`;
`require_user` does not read the disabled bit yet). The app is a **concurrent** one (a real, committed
session per request) because the claims are about state another connection sees: a disable deletes the
logins, an erase removes the rows and files. Those facts are read on a fresh `engine.connect()`.

Every account is real and deleted in teardown by id (the cascades take the rest); assertions are
scoped to ids or an email token the test created, since `tailorcraft_test` may hold other users.

`raise_app_exceptions=False` makes the skeleton's `NotImplementedError` a real 500 response, so a red
reads `assert 500 == 200`. Nothing here uses `pytest.raises(RuntimeError)`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.api.refresh_cookie import COOKIE_NAME as REFRESH_COOKIE_NAME
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    A_PASSWORD,
    assert_test_database,
    bearer,
    build_concurrent_app,
    error_code,
    new_client,
    override_settings,
    seed_user_and_sign_in,
)

USERS = "/api/admin/users"
UNMATCHED = "/api/admin/no-such-route"
ME = "/api/auth/me"
BOARD = "/api/me/board"
REFRESH = "/api/auth/refresh"
LOGIN = "/api/auth/login"
_PHC = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"
_STATUS_KEYS = {"user_id", "disabled_at", "changed", "logins_revoked"}
_ERASURE_KEYS = {
    "base_cvs",
    "files",
    "files_unlinked",
    "unlink_failures",
    "tailoring_runs",
    "job_postings",
    "export_jobs",
    "tracked_applications",
}
_COUNT_KEYS = {
    "base_cvs",
    "files",
    "logins",
    "tailoring_runs",
    "job_postings",
    "export_jobs",
    "tracked_applications",
}
_ITEM_KEYS = {"id", "email", "role", "created_at", "disabled_at"}


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """Login limiters live in Redis, which the database rollback does not reach."""


@pytest.fixture
def world_app(
    settings: Settings, engine: AsyncEngine, password_hasher: Argon2PasswordHasher
) -> FastAPI:
    assert_test_database(settings)
    return build_concurrent_app(settings, engine, password_hasher)


@pytest_asyncio.fixture
async def http(world_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(world_app) as client:
        yield client


@pytest_asyncio.fixture
async def seeded_ids(engine: AsyncEngine, settings: Settings) -> AsyncIterator[list[UUID]]:
    ids: list[UUID] = []
    yield ids
    assert_test_database(settings)
    async with engine.begin() as conn:
        for user_id in ids:
            await conn.execute(text("DELETE FROM identity_user WHERE id = :id"), {"id": user_id})


class _Who:
    """An account with a client of its own, so its `tc_refresh` cookie is not another's."""

    def __init__(self, client: AsyncClient, token: str, user_id: UUID) -> None:
        self.client = client
        self.token = token
        self.id = user_id

    @property
    def headers(self) -> dict[str, str]:
        return bearer(self.token)


@pytest_asyncio.fixture
async def make_account(
    world_app: FastAPI, settings: Settings, engine: AsyncEngine, seeded_ids: list[UUID]
) -> AsyncIterator[object]:
    clients: list[AsyncClient] = []

    async def make(*, admin: bool = False, email: str | None = None) -> _Who:
        client = new_client(world_app)
        clients.append(client)
        token, user_id = await seed_user_and_sign_in(client, settings, email=email)
        seeded_ids.append(user_id)
        if admin:
            async with engine.connect() as conn:
                await conn.execute(
                    text("UPDATE identity_user SET role = 'admin' WHERE id = :i"), {"i": user_id}
                )
                await conn.commit()
        return _Who(client, token, user_id)

    yield make
    for client in clients:
        await client.aclose()


async def _count(engine: AsyncEngine, sql: str, **params: object) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text(sql), params)).scalar_one())


async def _logins(engine: AsyncEngine, user_id: UUID) -> int:
    return await _count(engine, "SELECT count(*) FROM identity_login WHERE user_id = :u", u=user_id)


async def _user_rows(engine: AsyncEngine, user_id: UUID) -> int:
    return await _count(engine, "SELECT count(*) FROM identity_user WHERE id = :u", u=user_id)


async def _seed_directory_users(
    engine: AsyncEngine, seeded_ids: list[UUID], token: str, count: int
) -> list[UUID]:
    """Raw committed users with a shared email token and distinct creation seconds (oldest first)."""
    base = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)
    ids: list[UUID] = []
    async with engine.begin() as conn:
        for i in range(count):
            user_id = uuid4()
            await conn.execute(
                text(
                    "INSERT INTO identity_user (id, email, password_hash, created_at, "
                    "password_updated_at) VALUES (:i, :e, :p, :t, :t)"
                ),
                {
                    "i": user_id,
                    "e": f"{token}-{i:02d}@example.com",
                    "p": _PHC,
                    "t": base + timedelta(seconds=i),
                },
            )
            ids.append(user_id)
    seeded_ids.extend(ids)
    return ids


def _no_store(response: Response) -> None:
    assert response.headers.get("cache-control") == "no-store", response.headers


def _comparable(response: Response) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k != "date"}


# --------------------------------------------------------------------------- AC-17 the list


async def test_ac17_an_admin_gets_a_page_with_exactly_the_documented_keys_and_no_store(
    http: AsyncClient, make_account: object, engine: AsyncEngine, seeded_ids: list[UUID]
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    token = uuid4().hex
    await _seed_directory_users(engine, seeded_ids, token, 3)

    response = await http.get(USERS, params={"q": token}, headers=admin.headers)

    assert response.status_code == 200, response.text
    _no_store(response)
    body = response.json()
    assert set(body) == {"items", "next_cursor"}
    assert len(body["items"]) == 3
    assert all(set(item) == _ITEM_KEYS for item in body["items"])
    assert body["next_cursor"] is None
    assert all(item["disabled_at"] is None and item["role"] == "user" for item in body["items"])


async def test_ac17_paging_25_users_at_limit_10_returns_25_distinct_ids_in_order_then_null(
    http: AsyncClient, make_account: object, engine: AsyncEngine, seeded_ids: list[UUID]
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    token = uuid4().hex
    ids = await _seed_directory_users(engine, seeded_ids, token, 25)

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params = {"q": token, "limit": "10", **({"cursor": cursor} if cursor else {})}
        response = await http.get(USERS, params=params, headers=admin.headers)
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(item["id"] for item in body["items"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None or pages > 5:
            break

    assert pages == 3
    assert seen == [str(i) for i in reversed(ids)], "newest first, none skipped or repeated"


async def test_ac17_the_default_limit_is_20(
    http: AsyncClient, make_account: object, engine: AsyncEngine, seeded_ids: list[UUID]
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    token = uuid4().hex
    await _seed_directory_users(engine, seeded_ids, token, 25)

    response = await http.get(USERS, params={"q": token}, headers=admin.headers)

    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 20
    assert response.json()["next_cursor"] is not None


@pytest.mark.parametrize("limit", ["1", "50"])
async def test_ac17_the_limit_bounds_are_inclusive(
    http: AsyncClient, make_account: object, limit: str
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]

    response = await http.get(USERS, params={"limit": limit}, headers=admin.headers)

    assert response.status_code == 200, response.text
    assert 1 <= len(response.json()["items"]) <= int(limit)


# --------------------------------------------------------------------------- AC-18 search and refusals


async def test_ac18_the_search_is_a_case_insensitive_substring(
    http: AsyncClient, make_account: object, engine: AsyncEngine, seeded_ids: list[UUID]
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    token = uuid4().hex
    ids = await _seed_directory_users(engine, seeded_ids, token, 2)

    response = await http.get(USERS, params={"q": token[3:20].upper()}, headers=admin.headers)

    assert response.status_code == 200, response.text
    assert {item["id"] for item in response.json()["items"]} == {str(i) for i in ids}


@pytest.mark.parametrize(
    "q", ["a" * 255, "al\x01ex", "al\nex"], ids=["too-long", "control-char", "newline"]
)
async def test_ac18_an_invalid_search_is_422_invalid_search(
    http: AsyncClient, make_account: object, q: str
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]

    response = await http.get(USERS, params={"q": q}, headers=admin.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "invalid_search"


@pytest.mark.parametrize(
    "cursor", ["not-base64!!", "AAAA", "e30"], ids=["garbage", "short", "empty-json"]
)
async def test_ac18_a_malformed_or_tampered_cursor_is_422_invalid_cursor(
    http: AsyncClient, make_account: object, cursor: str
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]

    response = await http.get(USERS, params={"cursor": cursor}, headers=admin.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "invalid_cursor"


@pytest.mark.parametrize("limit", ["0", "51", "-1", "abc"])
async def test_ac18_a_limit_outside_1_to_50_is_422_validation_error(
    http: AsyncClient, make_account: object, limit: str
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]

    response = await http.get(USERS, params={"limit": limit}, headers=admin.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"


@pytest.mark.parametrize(
    ("param", "value"), [("q", "a" * 513), ("cursor", "a" * 257)], ids=["q-raw", "cursor-raw"]
)
async def test_ac18_a_value_over_the_raw_bound_is_422_validation_error(
    http: AsyncClient, make_account: object, param: str, value: str
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]

    response = await http.get(USERS, params={param: value}, headers=admin.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"


async def test_ac18_neither_the_search_nor_the_cursor_appears_in_a_response_or_a_log_line(
    http: AsyncClient, make_account: object, caplog: pytest.LogCaptureFixture
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    q_marker = f"QMARKER{uuid4().hex}"
    cursor_marker = f"CMARKER{uuid4().hex}"
    bad_search = "\x01" + q_marker

    with caplog.at_level(logging.DEBUG):
        valid = await http.get(USERS, params={"q": q_marker.lower()}, headers=admin.headers)
        invalid_search = await http.get(USERS, params={"q": bad_search}, headers=admin.headers)
        invalid_cursor = await http.get(
            USERS, params={"cursor": cursor_marker}, headers=admin.headers
        )

    assert valid.status_code == 200, valid.text
    assert invalid_search.status_code == 422, invalid_search.text
    assert invalid_cursor.status_code == 422, invalid_cursor.text
    for response in (valid, invalid_search, invalid_cursor):
        assert q_marker.lower() not in response.text.lower()
        assert cursor_marker not in response.text
    assert q_marker.lower() not in caplog.text.lower()
    assert cursor_marker not in caplog.text


# --------------------------------------------------------------------------- AC-19 the firewall


_FIREWALLED = [
    ("GET", USERS, "list"),
    ("GET", f"{USERS}?q=%01&limit=999&cursor=!!", "list-invalid-everything"),
    ("GET", f"{USERS}/{uuid4()}", "detail"),
    ("GET", f"{USERS}/not-a-uuid", "detail-malformed-id"),
    ("POST", f"{USERS}/{uuid4()}/disable", "disable"),
    ("POST", f"{USERS}/not-a-uuid/disable", "disable-malformed-id"),
    ("POST", f"{USERS}/{uuid4()}/enable", "enable"),
    ("POST", f"{USERS}/not-a-uuid/enable", "enable-malformed-id"),
    ("DELETE", f"{USERS}/{uuid4()}", "erase"),
    ("DELETE", f"{USERS}/not-a-uuid", "erase-malformed-id"),
]


@pytest.mark.parametrize(
    ("method", "url"), [(m, u) for m, u, _ in _FIREWALLED], ids=[i for _, _, i in _FIREWALLED]
)
async def test_ac19_a_plain_user_gets_the_404_of_an_unmatched_path_on_every_route(
    http: AsyncClient, make_account: object, method: str, url: str
) -> None:
    user = await make_account()  # type: ignore[operator]

    unmatched = await http.get(UNMATCHED, headers=user.headers)
    refused = await http.request(method, url, headers=user.headers)

    assert unmatched.status_code == 404, unmatched.text
    assert refused.status_code == 404, refused.text
    assert refused.content == unmatched.content
    assert refused.json() == {"detail": "Not Found"}
    assert _comparable(refused) == _comparable(unmatched)


@pytest.mark.parametrize(
    ("method", "url"), [(m, u) for m, u, _ in _FIREWALLED], ids=[i for _, _, i in _FIREWALLED]
)
async def test_ac19_anonymous_is_401_invalid_access_token_on_every_route(
    http: AsyncClient, method: str, url: str
) -> None:
    response = await http.request(method, url)

    assert response.status_code == 401, response.text
    assert error_code(response) == "invalid_access_token"


# --------------------------------------------------------------------------- AC-20 detail


async def test_ac20_detail_returns_the_account_and_its_counts_with_no_store(
    http: AsyncClient, make_account: object, engine: AsyncEngine
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    target = await make_account()  # type: ignore[operator]
    upload = await http.post(
        "/api/me/base-cvs",
        files={"file": ("a.txt", ("word " * 200).encode(), "text/plain")},
        headers=target.headers,
    )
    assert upload.status_code == 201, upload.text

    response = await http.get(f"{USERS}/{target.id}", headers=admin.headers)

    assert response.status_code == 200, response.text
    _no_store(response)
    body = response.json()
    assert set(body) == {*_ITEM_KEYS, "counts"}
    assert body["id"] == str(target.id)
    assert body["disabled_at"] is None
    assert set(body["counts"]) == _COUNT_KEYS
    assert body["counts"]["base_cvs"] == 1
    assert body["counts"]["files"] == 1
    assert body["counts"]["logins"] == 1


async def test_ac20_an_unknown_id_is_404_user_not_found_in_the_envelope_with_no_store(
    http: AsyncClient, make_account: object
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]

    response = await http.get(f"{USERS}/{uuid4()}", headers=admin.headers)

    assert response.status_code == 404, response.text
    assert error_code(response) == "user_not_found"
    _no_store(response)


async def test_ac20_a_malformed_id_is_422_validation_error_for_an_admin(
    http: AsyncClient, make_account: object
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]

    response = await http.get(f"{USERS}/not-a-uuid", headers=admin.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"


# --------------------------------------------------------------------------- AC-21 disable


async def test_ac21_disable_deletes_the_logins_and_kills_the_refresh_cookie_and_the_bearer(
    http: AsyncClient, make_account: object, engine: AsyncEngine, settings: Settings
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    target = await make_account()  # type: ignore[operator]
    assert await _logins(engine, target.id) == 1, "premise: the target is signed in"

    response = await http.post(f"{USERS}/{target.id}/disable", headers=admin.headers)

    assert response.status_code == 200, response.text
    _no_store(response)
    body = response.json()
    assert set(body) == _STATUS_KEYS
    assert body["user_id"] == str(target.id)
    assert body["changed"] is True
    assert body["logins_revoked"] == 1
    assert body["disabled_at"] is not None
    assert await _logins(engine, target.id) == 0

    refresh = await target.client.post(REFRESH, headers={"Origin": settings.public_base_url})
    assert refresh.status_code == 401, refresh.text
    assert error_code(refresh) == "not_signed_in"

    for url in (ME, BOARD):
        refused = await http.get(url, headers=target.headers)
        assert refused.status_code == 403, f"{url}: {refused.text}"
        assert error_code(refused) == "account_disabled"


async def test_ac21_a_second_disable_is_200_unchanged_with_zero_revoked(
    http: AsyncClient, make_account: object
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    target = await make_account()  # type: ignore[operator]
    first = await http.post(f"{USERS}/{target.id}/disable", headers=admin.headers)

    second = await http.post(f"{USERS}/{target.id}/disable", headers=admin.headers)

    assert second.status_code == 200, second.text
    _no_store(second)
    assert second.json()["changed"] is False
    assert second.json()["logins_revoked"] == 0
    assert second.json()["disabled_at"] == first.json()["disabled_at"]


async def test_ac21_disabling_an_unknown_id_is_404_user_not_found(
    http: AsyncClient, make_account: object
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]

    response = await http.post(f"{USERS}/{uuid4()}/disable", headers=admin.headers)

    assert response.status_code == 404, response.text
    assert error_code(response) == "user_not_found"
    _no_store(response)


# --------------------------------------------------------------------------- AC-22 enable


async def test_ac22_enable_lets_the_target_log_in_again_and_resurrects_no_session(
    http: AsyncClient, make_account: object, settings: Settings, engine: AsyncEngine
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    email = f"enable-{uuid4().hex}@example.com"
    target = await make_account(email=email)  # type: ignore[operator]
    old_refresh = target.client.cookies.get(REFRESH_COOKIE_NAME)
    assert old_refresh, "premise: the target holds a refresh cookie"
    await http.post(f"{USERS}/{target.id}/disable", headers=admin.headers)

    response = await http.post(f"{USERS}/{target.id}/enable", headers=admin.headers)

    assert response.status_code == 200, response.text
    _no_store(response)
    body = response.json()
    assert set(body) == _STATUS_KEYS
    assert (body["changed"], body["logins_revoked"], body["disabled_at"]) == (True, 0, None)
    assert await _logins(engine, target.id) == 0, "an enable creates no login"

    async with new_client(http._transport.app) as stale:  # type: ignore[attr-defined]
        stale.cookies.set(REFRESH_COOKIE_NAME, old_refresh)
        refresh = await stale.post(REFRESH, headers={"Origin": settings.public_base_url})
    assert refresh.status_code == 401, refresh.text

    login = await http.post(
        LOGIN,
        json={"email": email, "password": A_PASSWORD},
        headers={"Origin": settings.public_base_url},
    )
    assert login.status_code == 200, login.text


async def test_ac22_enabling_an_enabled_account_is_200_unchanged(
    http: AsyncClient, make_account: object
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    target = await make_account()  # type: ignore[operator]

    response = await http.post(f"{USERS}/{target.id}/enable", headers=admin.headers)

    assert response.status_code == 200, response.text
    assert response.json()["changed"] is False
    assert response.json()["logins_revoked"] == 0


async def test_ac22_enabling_an_unknown_id_is_404_user_not_found(
    http: AsyncClient, make_account: object
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]

    response = await http.post(f"{USERS}/{uuid4()}/enable", headers=admin.headers)

    assert response.status_code == 404, response.text
    assert error_code(response) == "user_not_found"
    _no_store(response)


# --------------------------------------------------------------------------- AC-23 erase


def _saved_cv_path(settings: Settings, cv_id: str) -> Path:
    return settings.upload_dir / FileRef.for_base_cv(BaseCvId(UUID(cv_id)), CvContentType.TXT).key


async def _upload(http: AsyncClient, who: _Who, name: str) -> str:
    response = await http.post(
        "/api/me/base-cvs",
        files={"file": (name, ("word " * 200).encode(), "text/plain")},
        headers=who.headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def test_ac23_erase_removes_every_row_and_file_and_reports_integer_counts(
    http: AsyncClient, make_account: object, settings: Settings, engine: AsyncEngine
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    target = await make_account()  # type: ignore[operator]
    cv_ids = [await _upload(http, target, "a.txt"), await _upload(http, target, "b.txt")]
    paths = [_saved_cv_path(settings, c) for c in cv_ids]
    assert all(p.exists() for p in paths), "premise: the files exist"

    response = await http.delete(f"{USERS}/{target.id}", headers=admin.headers)

    assert response.status_code == 200, response.text
    _no_store(response)
    body = response.json()
    assert set(body) == _ERASURE_KEYS
    assert all(type(v) is int for v in body.values()), body
    assert (body["base_cvs"], body["files_unlinked"], body["unlink_failures"]) == (2, 2, 0)
    assert await _user_rows(engine, target.id) == 0
    assert (
        await _count(engine, "SELECT count(*) FROM intake_base_cv WHERE user_id = :u", u=target.id)
        == 0
    )
    assert await _logins(engine, target.id) == 0
    assert not any(p.exists() for p in paths), "every file is unlinked"


async def test_ac23_a_second_erase_is_404_user_not_found(
    http: AsyncClient, make_account: object
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    target = await make_account()  # type: ignore[operator]
    first = await http.delete(f"{USERS}/{target.id}", headers=admin.headers)
    assert first.status_code == 200, first.text

    second = await http.delete(f"{USERS}/{target.id}", headers=admin.headers)

    assert second.status_code == 404, second.text
    assert error_code(second) == "user_not_found"
    _no_store(second)


async def test_ac23_a_failing_unlink_is_still_200_with_the_rows_gone(
    http: AsyncClient,
    make_account: object,
    settings: Settings,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected below `LocalFileStore`'s own floor (`Path.unlink`, matched by exact path)."""
    admin = await make_account(admin=True)  # type: ignore[operator]
    target = await make_account()  # type: ignore[operator]
    ok_id = await _upload(http, target, "a.txt")
    failing_id = await _upload(http, target, "b.txt")
    failing_path = _saved_cv_path(settings, failing_id)
    original_unlink = Path.unlink

    def _selective_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == failing_path:
            raise OSError(5, "Input/output error")
        return original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _selective_unlink)

    response = await http.delete(f"{USERS}/{target.id}", headers=admin.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unlink_failures"] > 0
    assert type(body["unlink_failures"]) is int
    assert await _user_rows(engine, target.id) == 0
    assert not _saved_cv_path(settings, ok_id).exists()
    assert failing_path.exists(), "the failing unlink left its bytes for the orphan sweep"


# --------------------------------------------------------------------------- AC-25 (a) (b) at the edge


async def test_ac25a_an_admin_disabling_itself_is_200_and_its_next_request_is_403(
    http: AsyncClient, make_account: object
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]

    response = await http.post(f"{USERS}/{admin.id}/disable", headers=admin.headers)
    assert response.status_code == 200, response.text
    assert response.json()["changed"] is True

    next_request = await http.get(ME, headers=admin.headers)
    assert next_request.status_code == 403, next_request.text
    assert error_code(next_request) == "account_disabled"


async def test_ac25b_an_admin_erasing_itself_is_200_and_its_next_request_is_401_not_signed_in(
    http: AsyncClient, make_account: object, engine: AsyncEngine
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]

    response = await http.delete(f"{USERS}/{admin.id}", headers=admin.headers)
    assert response.status_code == 200, response.text
    assert await _user_rows(engine, admin.id) == 0

    next_request = await http.get(USERS, headers=admin.headers)
    assert next_request.status_code == 401, next_request.text
    assert error_code(next_request) == "not_signed_in"


# --------------------------------------------------------------------------- AC-26 infrastructure


_ROUTES = [
    ("GET", USERS),
    ("GET", f"{USERS}/{uuid4()}"),
    ("POST", f"{USERS}/{uuid4()}/disable"),
    ("POST", f"{USERS}/{uuid4()}/enable"),
    ("DELETE", f"{USERS}/{uuid4()}"),
]


@pytest.mark.parametrize(
    ("method", "url"), _ROUTES, ids=["list", "detail", "disable", "enable", "erase"]
)
async def test_ac26_a_failing_database_is_503_on_every_admin_route_never_404_403_or_200(
    http: AsyncClient,
    make_account: object,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    url: str,
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]

    async def _down(self: AsyncSession, *args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("simulated: postgres unavailable (AC-26)")

    monkeypatch.setattr(AsyncSession, "execute", _down)

    response = await http.request(method, url, headers=admin.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"


async def test_ac26_a_failing_database_during_the_bearer_check_is_503(
    http: AsyncClient, make_account: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await make_account()  # type: ignore[operator]

    async def _down(self: AsyncSession, *args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("simulated: postgres unavailable (AC-26)")

    monkeypatch.setattr(AsyncSession, "execute", _down)

    response = await http.get(BOARD, headers=user.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"


async def test_ac26_with_redis_unreachable_every_admin_route_answers_as_normal(
    http: AsyncClient, make_account: object, world_app: FastAPI, settings: Settings
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    target = await make_account()  # type: ignore[operator]
    override_settings(world_app, settings, redis_url="redis://127.0.0.1:1/0")

    listed = await http.get(USERS, headers=admin.headers)
    detail = await http.get(f"{USERS}/{target.id}", headers=admin.headers)
    disabled = await http.post(f"{USERS}/{target.id}/disable", headers=admin.headers)
    enabled = await http.post(f"{USERS}/{target.id}/enable", headers=admin.headers)
    erased = await http.delete(f"{USERS}/{target.id}", headers=admin.headers)

    assert [r.status_code for r in (listed, detail, disabled, enabled, erased)] == [200] * 5, [
        r.text for r in (listed, detail, disabled, enabled, erased)
    ]
