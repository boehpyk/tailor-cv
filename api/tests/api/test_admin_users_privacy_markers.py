"""Slice 4.2, T16 (test-after): AC-27's planted-marker test, and the guest cookie on the new routes.

Markers are planted in the target's email, in a valid search term, in an invalid search term and in a
cursor, and the whole admin flow is driven through the real app: list, detail, disable, a refused
login and a refused bearer (the two lines the disabled bit adds), enable, erase. Every captured log
record is read afterwards. The ids are the positive control: they appear, so the capture would have
seen a leak (`test_account_mail_privacy_markers.py`'s method).

**Mutation, observed 2026-10-11 and restored byte-exact** (`git diff --stat -- api/src` empty):
`log.info("admin.mutation", q=q or "", cursor=cursor or "")` added to the list handler turned the
marker test red, `AssertionError: emk... reached a log record` (the first marker checked is the
target's email, which the valid search term reaches).

The guest cookie is ignored on the admin routes: `__Host-tc_guest` plus no bearer is 401, the cookie
beside an admin bearer changes nothing and is not touched, and beside a plain user's bearer it
cannot soften the 404.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import A_PASSWORD, error_code
from tests.api.test_admin_users import (  # noqa: F401
    USERS,
    _reset_redis_between_tests,
    http,
    make_account,
    seeded_ids,
    world_app,
)

UNMATCHED = "/api/admin/no-such-route"


@pytest.fixture(autouse=True)
def _configured_logging(settings: Settings) -> None:
    configure_logging(settings)


def _lines(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for record in caplog.records:
        message = record.getMessage()
        if event not in message:
            continue
        try:
            parsed = json.loads(message)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("event") == event:
            found.append(parsed)
    return found


async def test_ac27_markers_in_the_email_the_search_and_the_cursor_reach_no_log_record(
    http: AsyncClient,  # noqa: F811
    make_account: object,  # noqa: F811
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    email_marker = f"emk{uuid4().hex}"
    search_marker = f"sqm{uuid4().hex}"
    bad_search_marker = f"bsm{uuid4().hex}"
    cursor_marker = f"cum{uuid4().hex}"
    email = f"{email_marker}@example.com"
    admin = await make_account(admin=True)  # type: ignore[operator]
    target = await make_account(email=email)  # type: ignore[operator]
    origin = {"Origin": settings.public_base_url}
    stale_bearer = target.headers  # still a valid token after the disable (OQ-1 = B)

    with caplog.at_level(logging.DEBUG):
        by_email = await http.get(USERS, params={"q": email_marker}, headers=admin.headers)
        no_match = await http.get(USERS, params={"q": search_marker}, headers=admin.headers)
        bad_search = await http.get(
            USERS, params={"q": "\x01" + bad_search_marker}, headers=admin.headers
        )
        bad_cursor = await http.get(USERS, params={"cursor": cursor_marker}, headers=admin.headers)
        detail = await http.get(f"{USERS}/{target.id}", headers=admin.headers)
        disabled = await http.post(f"{USERS}/{target.id}/disable", headers=admin.headers)
        refused_login = await http.post(
            "/api/auth/login", json={"email": email, "password": A_PASSWORD}, headers=origin
        )
        refused_bearer = await http.get("/api/auth/me", headers=stale_bearer)
        enabled = await http.post(f"{USERS}/{target.id}/enable", headers=admin.headers)
        erased = await http.delete(f"{USERS}/{target.id}", headers=admin.headers)

    statuses = [
        r.status_code
        for r in (
            by_email,
            no_match,
            bad_search,
            bad_cursor,
            detail,
            disabled,
            refused_login,
            refused_bearer,
            enabled,
            erased,
        )
    ]
    assert statuses == [200, 200, 422, 422, 200, 200, 403, 403, 200, 200], statuses
    assert [i["email"] for i in by_email.json()["items"]] == [email], "premise: the email is real"

    log_text = caplog.text.lower()
    for marker in (email_marker, search_marker, bad_search_marker, cursor_marker):
        assert marker not in log_text, f"{marker[:3]}... reached a log record"
    # Positive controls: the capture saw the lines, and they carry ids and closed words.
    actor, tid = str(admin.id), str(target.id)
    actions = _lines(caplog, "admin.account_action")
    assert [(a["action"], a["outcome"]) for a in actions] == [
        ("disable", "changed"),
        ("enable", "changed"),
        ("erase", "changed"),
    ], actions
    assert all(a["actor_id"] == actor and a["target_user_id"] == tid for a in actions)
    (login_line,) = _lines(caplog, "identity.login_refused")
    assert (login_line["reason"], login_line["user_id"]) == ("account_disabled", tid)
    (bearer_line,) = _lines(caplog, "identity.access_refused")
    assert (bearer_line["reason"], bearer_line["user_id"]) == ("account_disabled", tid)
    assert any(tid in r.getMessage() for r in caplog.records if "account_erased" in r.getMessage())


async def test_ac27_a_missing_target_is_logged_as_not_found_with_ids_only(
    http: AsyncClient,  # noqa: F811
    make_account: object,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    missing = uuid4()

    with caplog.at_level(logging.INFO):
        for verb, url in (
            ("post", f"{USERS}/{missing}/disable"),
            ("post", f"{USERS}/{missing}/enable"),
            ("delete", f"{USERS}/{missing}"),
        ):
            response = await getattr(http, verb)(url, headers=admin.headers)
            assert response.status_code == 404, response.text

    actions = _lines(caplog, "admin.account_action")
    assert [(a["action"], a["outcome"]) for a in actions] == [
        ("disable", "not_found"),
        ("enable", "not_found"),
        ("erase", "not_found"),
    ]
    assert all(a["target_user_id"] == str(missing) for a in actions)


# --------------------------------------------------------------------------- the guest cookie


_ROUTES = [
    ("GET", USERS),
    ("GET", f"{USERS}/{uuid4()}"),
    ("POST", f"{USERS}/{uuid4()}/disable"),
    ("POST", f"{USERS}/{uuid4()}/enable"),
    ("DELETE", f"{USERS}/{uuid4()}"),
]


async def _guest_session_of(http: AsyncClient, engine: AsyncEngine) -> UUID:  # noqa: F811
    """A real guest cookie on `http` (a real 1.1 upload); returns the session id for cleanup."""
    response = await http.post(
        "/api/base-cvs", files={"file": ("g.txt", ("word " * 200).encode(), "text/plain")}
    )
    assert response.status_code == 201, response.text
    async with engine.connect() as conn:
        return UUID(
            str(
                (
                    await conn.execute(
                        text("SELECT guest_session_id FROM intake_base_cv WHERE id = :i"),
                        {"i": UUID(response.json()["id"])},
                    )
                ).scalar_one()
            )
        )


async def _drop_guest(engine: AsyncEngine, sid: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("SET lock_timeout = '5s'"))
        await conn.execute(text("DELETE FROM identity_guest_session WHERE id = :i"), {"i": sid})


@pytest.mark.parametrize(
    ("method", "url"), _ROUTES, ids=["list", "detail", "disable", "enable", "erase"]
)
async def test_the_guest_cookie_alone_is_not_a_credential_on_any_admin_route(
    http: AsyncClient,  # noqa: F811
    engine: AsyncEngine,
    method: str,
    url: str,
) -> None:
    sid = await _guest_session_of(http, engine)
    try:
        response = await http.request(method, url)
        assert response.status_code == 401, response.text
        assert error_code(response) == "invalid_access_token"
    finally:
        await _drop_guest(engine, sid)


async def test_the_guest_cookie_beside_an_admin_bearer_changes_nothing_and_is_not_touched(
    http: AsyncClient,  # noqa: F811
    make_account: object,  # noqa: F811
    engine: AsyncEngine,
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    sid = await _guest_session_of(http, engine)
    try:
        response = await http.get(USERS, headers=admin.headers)

        assert response.status_code == 200, response.text
        assert "set-cookie" not in response.headers, "an admin route never mints or clears a cookie"
        async with engine.connect() as conn:
            alive = (
                await conn.execute(
                    text("SELECT count(*) FROM identity_guest_session WHERE id = :i"), {"i": sid}
                )
            ).scalar_one()
        assert alive == 1
    finally:
        await _drop_guest(engine, sid)


async def test_the_guest_cookie_beside_a_plain_users_bearer_cannot_soften_the_404(
    http: AsyncClient,  # noqa: F811
    make_account: object,  # noqa: F811
    engine: AsyncEngine,
) -> None:
    user = await make_account()  # type: ignore[operator]
    sid = await _guest_session_of(http, engine)
    try:
        unmatched = await http.get(UNMATCHED, headers=user.headers)
        refused = await http.get(USERS, headers=user.headers)

        assert refused.status_code == 404, refused.text
        assert refused.content == unmatched.content
    finally:
        await _drop_guest(engine, sid)
