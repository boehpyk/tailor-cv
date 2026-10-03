"""API tests for slice 2.5's HTTP surface (T27, **RED**): `POST /api/auth/register` (now 202),
`/registration/confirm`, `/password-reset` and `/password-reset/confirm`.

Written from `docs/specs/identity-email-verification/feature-spec.md` (AC-27 … AC-34 and the V-rows
reachable over HTTP) and technical plan §4's contract table — never from the router, whose three new
handlers raise `NotImplementedError` (T26's skeleton) and whose `register` still answers 201 (T29 is
its GREEN). A red here is an **assertion** on a status or a body: the skeleton surfaces as 500 (this
module's `client` uses `raise_app_exceptions=False`, `test_auth.py`'s reason), and `register`'s as
`201 != 202`.

**The token comes from the mail.** Nothing here reads a plaintext token out of the database — there
is none, only its hash (ADR-0027). The flow is the real one: `POST` the request → the recording queue
holds the row's id → `account_mail_support` runs the worker's own delivery use case (the same
composition root production calls) with a `RecordingAccountMailer` → the test reads the token off the
recorded mail and presents it.

**A skeleton satisfies every absence assertion** (`task-list.md`'s standing note), so each "no
`Set-Cookie`", "nothing enqueued", "no database read" below sits beside a discriminating positive: a
pending row exists, one task was enqueued, a well-formed token *does* read the table. **A refusal that
must not write is paired with the same route's success**, so the pair fails together when the route is
a stub.

**Byte-identity** is `(status, every header but `date`, body)` — the whole response, not the parts a
test author thought of.

**Redis is not rolled back**, so every test clears it (`clear_redis`), and the limiters are driven with
settings overrides, never by sleeping out an hour.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from tailorcraft.domain.identity.errors import AccountMailQueueUnavailable, PasswordHashingFailed
from tailorcraft.domain.identity.value_objects import Password, PasswordHash, PasswordVerdict
from tailorcraft.infrastructure.api.deps import (
    get_app_settings,
    get_password_reset_email_rate_limiter,
    get_password_reset_ip_rate_limiter,
    get_register_email_rate_limiter,
    get_register_rate_limiter,
)
from tailorcraft.infrastructure.api.main import create_app
from tailorcraft.infrastructure.api.refresh_cookie import COOKIE_NAME as REFRESH_COOKIE_NAME
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.database import create_session_factory
from tailorcraft.infrastructure.rate_limit import RedisFixedWindowRateLimiter
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks.app import app as celery_app
from tests.api.account_mail_support import (
    confirmation_token,
    deliver_password_reset,
    deliver_registration,
    install_recording_queue,
    reset_token,
)
from tests.api.me_support import A_PASSWORD, seed_user_and_sign_in
from tests.integration.fakes import RecordingAccountMailQueue

REGISTER_URL = "/api/auth/register"
LOGIN_URL = "/api/auth/login"
REFRESH_URL = "/api/auth/refresh"
CONFIRM_URL = "/api/auth/registration/confirm"
RESET_URL = "/api/auth/password-reset"
RESET_CONFIRM_URL = "/api/auth/password-reset/confirm"

A_NEW_PASSWORD = "an entirely different passphrase 7"
A_WELL_FORMED_UNKNOWN_TOKEN = "a" * 43
DEAD_REDIS_URL = "redis://127.0.0.1:1/0"


# ---------------------------------------------------------------------------------------------
# Fixtures and small helpers
# ---------------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """`test_auth.py`'s shadowing fixture, for its reason: a skeleton's `NotImplementedError` must be
    a 500 the assertion reads, not an exception re-raised into the test."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def _new_client(app: FastAPI) -> AsyncClient:
    """A second cookie jar against the same app (a second browser)."""
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver"
    )


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """Every route here but the two confirms touches a limiter; the rollback does not reach Redis."""


@pytest.fixture
def queue(app: FastAPI) -> RecordingAccountMailQueue:
    """The app's mail queue is a recording fake — never the dev broker's real `mail` queue."""
    return install_recording_queue(app)


class _RecordingHasher:
    """Counts calls on a real hasher (`test_auth.py`'s, extended with a failure switch)."""

    def __init__(self, real: Argon2PasswordHasher) -> None:
        self._real = real
        self.hash_calls = 0
        self.verify_calls = 0
        self.fail_hash = False

    async def hash(self, password: Password) -> PasswordHash:
        self.hash_calls += 1
        if self.fail_hash:
            raise PasswordHashingFailed
        return await self._real.hash(password)

    async def verify(self, password: Password, against: PasswordHash | None) -> PasswordVerdict:
        self.verify_calls += 1
        return await self._real.verify(password, against)


@pytest.fixture
def hasher(app: FastAPI) -> _RecordingHasher:
    recording = _RecordingHasher(app.state.password_hasher)
    app.state.password_hasher = recording
    return recording


def _headers(settings: Settings) -> dict[str, str]:
    return {"Origin": settings.public_base_url}


def _override_settings(app: FastAPI, base: Settings, **updates: object) -> Settings:
    modified = base.model_copy(update=updates)
    app.dependency_overrides[get_app_settings] = lambda: modified
    app.state.settings = modified
    return modified


def _unique_email(label: str) -> str:
    return f"t27-{label}-{uuid4().hex[:12]}@example.com"


def _fingerprint(response: Response) -> tuple[int, tuple[tuple[str, str], ...], bytes]:
    """The whole response minus the clock: status, every header but `date`, the raw body."""
    headers = tuple(sorted((k, v) for k, v in response.headers.multi_items() if k != "date"))
    return response.status_code, headers, response.content


def _error_code(response: Response) -> str:
    body = response.json()
    assert "error" in body, f"expected the error envelope, got {body!r}"
    return str(body["error"]["code"])


def _no_cookie(response: Response) -> bool:
    return response.headers.get_list("set-cookie") == []


async def _scalar(
    session: AsyncSession | AsyncConnection, sql: str, **params: object
) -> Any:  # a SQL scalar: its type is the column's, the caller knows it
    return (await session.execute(text(sql), params)).scalar_one()


async def _pending_count(session: AsyncSession | AsyncConnection, email: str) -> int:
    count = await _scalar(
        session,
        "SELECT count(*) FROM identity_pending_registration WHERE email = :e",
        e=email.lower(),
    )
    return int(count)


async def _pending_id(session: AsyncSession, email: str) -> UUID:
    value = await _scalar(
        session, "SELECT id FROM identity_pending_registration WHERE email = :e", e=email.lower()
    )
    assert isinstance(value, UUID)
    return value


async def _addressed_reset_count(session: AsyncSession, email: str) -> int:
    count = await _scalar(
        session, "SELECT count(*) FROM identity_password_reset WHERE email = :e", e=email.lower()
    )
    return int(count)


async def _user_count(session: AsyncSession | AsyncConnection, email: str) -> int:
    count = await _scalar(
        session, "SELECT count(*) FROM identity_user WHERE email = :e", e=email.lower()
    )
    return int(count)


async def _rate_limit_keys(settings: Settings, pattern: str) -> list[str]:
    redis = create_redis(settings.redis_url)
    try:
        keys = [key async for key in redis.scan_iter(match=pattern)]
        return sorted(key.decode() if isinstance(key, bytes) else key for key in keys)
    finally:
        await redis.aclose()


@contextmanager
def _captured_sql(engine: AsyncEngine) -> Iterator[list[str]]:
    statements: list[str] = []

    def _capture(conn: object, cursor: object, statement: str, *_args: object) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)


def _touches_identity_tables(statements: list[str]) -> bool:
    return any("identity_" in s.lower() for s in statements)


async def _register(
    client: AsyncClient, settings: Settings, email: str, password: str = A_PASSWORD
) -> Response:
    return await client.post(
        REGISTER_URL, json={"email": email, "password": password}, headers=_headers(settings)
    )


async def _registered_and_mailed(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
    email: str,
    password: str = A_PASSWORD,
) -> str:
    """Register `email`, run the worker half, and return the token off the mail it sent."""
    response = await _register(client, settings, email, password)
    assert response.status_code == 202, response.text
    assert len(queue.registrations) >= 1, "register must enqueue a delivery"
    mailer = await deliver_registration(settings, session, queue.registrations[-1])
    return confirmation_token(mailer)


async def _reset_requested_and_mailed(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
    email: str,
) -> str:
    response = await client.post(RESET_URL, json={"email": email}, headers=_headers(settings))
    assert response.status_code == 202, response.text
    assert len(queue.resets) >= 1, "a reset request must enqueue a delivery"
    mailer = await deliver_password_reset(settings, session, queue.resets[-1])
    return reset_token(mailer)


async def _confirm(client: AsyncClient, settings: Settings, token: str) -> Response:
    return await client.post(CONFIRM_URL, json={"token": token}, headers=_headers(settings))


async def _reset_confirm(
    client: AsyncClient, settings: Settings, token: str, password: str = A_NEW_PASSWORD
) -> Response:
    return await client.post(
        RESET_CONFIRM_URL,
        json={"token": token, "password": password},
        headers=_headers(settings),
    )


async def _login(client: AsyncClient, settings: Settings, email: str, password: str) -> Response:
    return await client.post(
        LOGIN_URL, json={"email": email, "password": password}, headers=_headers(settings)
    )


# ---------------------------------------------------------------------------------------------
# AC-27 — register: 202, empty, no-store, no cookie, a pending row, one task
# ---------------------------------------------------------------------------------------------


async def test_register_a_new_address_is_202_empty_no_store_with_no_cookie_a_pending_row_and_one_task(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _unique_email("new")

    response = await _register(client, settings, email)

    assert response.status_code == 202, response.text
    assert response.content == b""
    assert response.headers.get("cache-control") == "no-store"
    assert _no_cookie(response)
    # The discriminating positives: a pending row exists, one task was enqueued, and it is that row.
    assert await _pending_count(session, email) == 1
    assert [p.value for p in queue.registrations] == [await _pending_id(session, email)]
    assert await _user_count(session, email) == 0, "registering creates no account (ADR-0027)"


async def test_register_responses_are_byte_identical_for_a_new_a_registered_and_a_pending_address(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    registered_email = _unique_email("registered")
    pending_email = _unique_email("pending")
    new_email = _unique_email("new")
    await seed_user_and_sign_in(client, settings, email=registered_email)
    first = await _register(client, settings, pending_email)  # leaves a pending row behind
    assert first.status_code == 202, first.text
    queued_before = len(queue.registrations)

    responses = {
        "new": await _register(client, settings, new_email),
        "registered": await _register(client, settings, registered_email),
        "pending": await _register(client, settings, pending_email),
    }

    for label, response in responses.items():
        assert response.status_code == 202, (label, response.text)
        assert _no_cookie(response), label
    assert len({_fingerprint(r) for r in responses.values()}) == 1, {
        label: _fingerprint(r) for label, r in responses.items()
    }
    # All three cases leave a pending row, and each request enqueued exactly one task.
    for email in (new_email, registered_email, pending_email):
        assert await _pending_count(session, email) == 1, email
    assert len(queue.registrations) == queued_before + 3


async def test_register_responses_are_byte_identical_over_twenty_new_and_existing_pairs(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    queue: RecordingAccountMailQueue,
) -> None:
    """AC-40's byte-identity half: not three samples but twenty pairs, so a header or a body that
    varies with something rare (a counter, an id) cannot hide in one lucky pair."""
    _override_settings(
        app,
        settings,
        register_rate_limit_per_ip_per_hour=1000,
        register_rate_limit_per_email_per_hour=1000,
    )
    existing = _unique_email("existing")
    await seed_user_and_sign_in(client, settings, email=existing)

    fingerprints: set[tuple[int, tuple[tuple[str, str], ...], bytes]] = set()
    for _ in range(20):
        fingerprints.add(_fingerprint(await _register(client, settings, _unique_email("pair"))))
        fingerprints.add(_fingerprint(await _register(client, settings, existing)))

    assert len(fingerprints) == 1, fingerprints
    assert next(iter(fingerprints))[0] == 202
    assert len(queue.registrations) == 40


# ---------------------------------------------------------------------------------------------
# AC-28 — register's refusals: order and shape
# ---------------------------------------------------------------------------------------------


async def test_register_a_foreign_origin_is_403_before_anything_is_touched(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
    hasher: _RecordingHasher,
) -> None:
    email = _unique_email("origin")

    refused = await client.post(
        REGISTER_URL,
        json={"email": email, "password": A_PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    accepted = await _register(client, settings, _unique_email("origin-ok"))

    assert refused.status_code == 403, refused.text
    assert _error_code(refused) == "origin_not_allowed"
    assert hasher.hash_calls == 1, "only the accepted request may reach the hasher"
    assert await _pending_count(session, email) == 0
    assert accepted.status_code == 202, accepted.text
    assert len(queue.registrations) == 1


async def test_register_the_per_ip_limit_answers_429_with_retry_after_before_the_address_is_parsed(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    queue: RecordingAccountMailQueue,
) -> None:
    _override_settings(app, settings, register_rate_limit_per_ip_per_hour=1)

    first = await _register(client, settings, _unique_email("ip-1"))
    second = await _register(client, settings, "not-an-email")

    assert first.status_code == 202, first.text
    assert second.status_code == 429, second.text  # not 422: the IP limiter precedes the parse
    assert _error_code(second) == "rate_limited"
    assert int(second.headers["retry-after"]) > 0
    assert _no_cookie(second)


async def test_register_an_invalid_address_is_422_before_the_per_address_limiter_is_consulted(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    queue: RecordingAccountMailQueue,
) -> None:
    _override_settings(
        app,
        settings,
        register_rate_limit_per_ip_per_hour=100,
        register_rate_limit_per_email_per_hour=1,
    )

    invalid = [await _register(client, settings, "a@@b.com") for _ in range(3)]
    keys_after_invalid = await _rate_limit_keys(settings, "rl:auth:register:email:*")
    valid = await _register(client, settings, _unique_email("valid"))
    keys_after_valid = await _rate_limit_keys(settings, "rl:auth:register:email:*")

    assert [r.status_code for r in invalid] == [422, 422, 422], [r.text for r in invalid]
    assert _error_code(invalid[0]) == "invalid_email"
    assert keys_after_invalid == [], "an unparseable address must never key the per-address limiter"
    assert valid.status_code == 202, valid.text
    assert len(keys_after_valid) == 1, keys_after_valid


async def test_register_the_per_address_limit_is_3_an_hour_keyed_by_hmac_with_retry_after(
    client: AsyncClient,
    settings: Settings,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _unique_email("per-address")

    responses = [await _register(client, settings, email) for _ in range(4)]

    assert [r.status_code for r in responses] == [202, 202, 202, 429], [r.text for r in responses]
    assert _error_code(responses[3]) == "rate_limited"
    assert int(responses[3].headers["retry-after"]) > 0
    keys = await _rate_limit_keys(settings, "rl:auth:register:email:*")
    assert len(keys) == 1, keys
    assert email not in keys[0], "the address must never appear in a Redis key (HMAC-keyed)"
    assert email.lower() not in keys[0]
    assert len(queue.registrations) == 3, "the refused fourth request enqueued nothing"


async def test_register_the_per_address_limiter_precedes_the_password_policy(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    queue: RecordingAccountMailQueue,
) -> None:
    _override_settings(
        app,
        settings,
        register_rate_limit_per_ip_per_hour=100,
        register_rate_limit_per_email_per_hour=1,
    )
    email = _unique_email("order")

    first = await _register(client, settings, email)
    over_limit_weak = await _register(client, settings, email, "short")
    fresh_weak = await _register(client, settings, _unique_email("order-weak"), "short")

    assert first.status_code == 202, first.text
    assert over_limit_weak.status_code == 429, over_limit_weak.text  # not 422 password_too_short
    assert _error_code(over_limit_weak) == "rate_limited"
    assert fresh_weak.status_code == 422, fresh_weak.text
    assert _error_code(fresh_weak) == "password_too_short"
    assert _no_cookie(fresh_weak)


@pytest.mark.parametrize(
    ("password", "code"),
    [("short1", "password_too_short"), ("x" * 129, "password_too_long")],
)
async def test_register_a_policy_refusal_is_422_after_both_limiters_and_enqueues_nothing(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
    hasher: _RecordingHasher,
    password: str,
    code: str,
) -> None:
    email = _unique_email("policy")

    refused = await _register(client, settings, email, password)
    accepted = await _register(client, settings, email)

    assert refused.status_code == 422, refused.text
    assert _error_code(refused) == code
    assert hasher.hash_calls == 1, "the refused request must not hash; only the accepted one did"
    assert accepted.status_code == 202, accepted.text
    assert await _pending_count(session, email) == 1
    assert len(queue.registrations) == 1


async def test_register_never_answers_409_email_already_registered(
    client: AsyncClient, settings: Settings, queue: RecordingAccountMailQueue
) -> None:
    email = _unique_email("never-409")
    await seed_user_and_sign_in(client, settings, email=email)

    response = await _register(client, settings, email)

    assert response.status_code == 202, response.text


# ---------------------------------------------------------------------------------------------
# V-7 — both new limiters fail closed (Redis down -> 503 before any hash or write)
# ---------------------------------------------------------------------------------------------


def _dead_limiter(namespace: str) -> RedisFixedWindowRateLimiter:
    return RedisFixedWindowRateLimiter(
        create_redis(DEAD_REDIS_URL), namespace=namespace, fail_open=False
    )


@pytest.mark.parametrize(
    "limiter_dependency",
    [get_register_rate_limiter, get_register_email_rate_limiter],
    ids=["per-ip", "per-address"],
)
async def test_register_with_either_limiter_unable_to_reach_redis_is_503_and_writes_nothing(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
    hasher: _RecordingHasher,
    limiter_dependency: Callable[..., Any],
) -> None:
    email = _unique_email("redis-down")
    dead = _dead_limiter("auth:register")
    app.dependency_overrides[limiter_dependency] = lambda: dead

    refused = await _register(client, settings, email)
    del app.dependency_overrides[limiter_dependency]
    accepted = await _register(client, settings, email)

    assert refused.status_code == 503, refused.text
    assert _error_code(refused) == "rate_limit_unavailable"
    assert _no_cookie(refused)
    assert hasher.hash_calls == 1, "the 503 must come before any hash"
    # The positive: with the limiter back, the same request writes one row and enqueues one task.
    assert accepted.status_code == 202, accepted.text
    assert await _pending_count(session, email) == 1
    assert len(queue.registrations) == 1


# ---------------------------------------------------------------------------------------------
# AC-34 / V-17 / V-8 — broker down, database down
# ---------------------------------------------------------------------------------------------


async def test_register_with_the_broker_down_is_503_service_unavailable_and_the_row_remains(
    app: FastAPI, client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    broker = install_recording_queue(app, error=AccountMailQueueUnavailable())
    new_email = _unique_email("broker-new")
    existing_email = _unique_email("broker-existing")
    await seed_user_and_sign_in(client, settings, email=existing_email)

    for_new = await _register(client, settings, new_email)
    for_existing = await _register(client, settings, existing_email)

    assert for_new.status_code == 503, for_new.text
    assert _error_code(for_new) == "service_unavailable"
    assert _fingerprint(for_new) == _fingerprint(for_existing), "identical for every address"
    assert _no_cookie(for_new)
    assert len(broker.registrations) == 2, "the enqueue was attempted — after the commit"
    assert await _pending_count(session, new_email) == 1, "the committed row remains (V-17)"


async def test_register_with_the_database_down_is_503_before_any_enqueue(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail() -> None:
        raise SQLAlchemyError("simulated commit failure (V-8)")

    monkeypatch.setattr(session, "commit", _fail)

    email = _unique_email("db-down")

    response = await _register(client, settings, email)
    nothing_enqueued = list(queue.registrations)
    monkeypatch.undo()  # the database is back
    recovered = await _register(client, settings, email)

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"
    assert _no_cookie(response)
    assert nothing_enqueued == [], "nothing may be enqueued for a row that was not committed"
    # The positive: the same request, once the database answers, enqueues exactly one task.
    assert recovered.status_code == 202, recovered.text
    assert len(queue.registrations) == 1


# ---------------------------------------------------------------------------------------------
# AC-29 — registration/confirm
# ---------------------------------------------------------------------------------------------


async def test_confirming_a_registration_is_204_creates_the_user_with_the_pending_hash_and_no_login(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _unique_email("confirm")
    token = await _registered_and_mailed(client, settings, session, queue, email)
    pending_hash = await _scalar(
        session,
        "SELECT password_hash FROM identity_pending_registration WHERE email = :e",
        e=email,
    )
    assert await _user_count(session, email) == 0

    response = await _confirm(client, settings, token)

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert response.headers.get("cache-control") == "no-store"
    assert _no_cookie(response), "confirming does not sign in (OQ-3)"
    assert await _user_count(session, email) == 1
    assert (
        await _scalar(session, "SELECT password_hash FROM identity_user WHERE email = :e", e=email)
        == pending_hash
    )
    assert await _pending_count(session, email) == 0
    logins = await _scalar(
        session,
        "SELECT count(*) FROM identity_login l JOIN identity_user u ON u.id = l.user_id "
        "WHERE u.email = :e",
        e=email,
    )
    assert logins == 0, "no Login exists after confirming"


async def test_the_confirmed_user_can_log_in_with_the_password_they_registered_with(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _unique_email("then-login")
    token = await _registered_and_mailed(client, settings, session, queue, email)

    confirmed = await _confirm(client, settings, token)
    logged_in = await _login(client, settings, email, A_PASSWORD)

    assert confirmed.status_code == 204, confirmed.text
    assert logged_in.status_code == 200, logged_in.text
    assert REFRESH_COOKIE_NAME in {
        h.split("=", 1)[0] for h in logged_in.headers.get_list("set-cookie")
    }


async def test_login_before_confirming_is_the_unknown_email_answer_and_runs_the_decoy_once(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
    hasher: _RecordingHasher,
) -> None:
    """AC-33. Only a pending registration exists: the login is 401 `invalid_credentials`, the same
    bytes as for an address nobody ever used, and each ran exactly one verify (the decoy)."""
    pending_email = _unique_email("unconfirmed")
    unknown_email = _unique_email("unknown")
    registered = await _register(client, settings, pending_email)
    assert registered.status_code == 202, registered.text
    assert await _pending_count(session, pending_email) == 1
    hasher.verify_calls = 0

    before_confirming = await _login(client, settings, pending_email, A_PASSWORD)
    verifies_for_pending = hasher.verify_calls
    unknown = await _login(client, settings, unknown_email, A_PASSWORD)
    verifies_for_unknown = hasher.verify_calls - verifies_for_pending

    assert before_confirming.status_code == 401, before_confirming.text
    assert _error_code(before_confirming) == "invalid_credentials"
    assert (before_confirming.status_code, before_confirming.content) == (
        unknown.status_code,
        unknown.content,
    )
    assert verifies_for_pending == 1
    assert verifies_for_unknown == 1


_MALFORMED_TOKENS = [
    pytest.param("", id="empty"),
    pytest.param("short", id="short"),
    pytest.param("a" * 42, id="one-short"),
    pytest.param("a" * 44, id="one-long"),
    pytest.param("a" * 42 + "+", id="standard-base64-plus"),
    pytest.param("a" * 42 + "/", id="standard-base64-slash"),
    pytest.param("a" * 42 + "=", id="padding"),
    pytest.param("a" * 42 + "\n", id="trailing-newline"),
    pytest.param(" " + "a" * 42, id="leading-space"),
    pytest.param("é" * 43, id="non-ascii"),
]


@pytest.mark.parametrize("token", _MALFORMED_TOKENS)
async def test_confirm_with_a_malformed_token_is_400_link_invalid_with_no_database_read(
    client: AsyncClient,
    settings: Settings,
    engine: AsyncEngine,
    token: str,
) -> None:
    with _captured_sql(engine) as statements:
        response = await _confirm(client, settings, token)
        malformed_statements = list(statements)
    with _captured_sql(engine) as statements:
        control = await _confirm(client, settings, A_WELL_FORMED_UNKNOWN_TOKEN)
        control_statements = list(statements)

    assert response.status_code == 400, response.text
    assert _error_code(response) == "link_invalid"
    assert not _touches_identity_tables(malformed_statements), malformed_statements
    # The positive control: a *well-formed* unknown token is also 400, but it does read the table —
    # so the empty capture above is the route refusing early, not a capture that cannot see.
    assert control.status_code == 400, control.text
    assert _error_code(control) == "link_invalid"
    assert any("identity_pending_registration" in s.lower() for s in control_statements)


async def test_confirm_with_an_unknown_token_is_400_link_invalid_and_sets_no_cookie(
    client: AsyncClient, settings: Settings
) -> None:
    response = await _confirm(client, settings, A_WELL_FORMED_UNKNOWN_TOKEN)

    assert response.status_code == 400, response.text
    assert _error_code(response) == "link_invalid"
    assert _no_cookie(response)


async def test_confirming_the_same_link_twice_is_204_then_400_and_one_user(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _unique_email("twice")
    token = await _registered_and_mailed(client, settings, session, queue, email)

    first = await _confirm(client, settings, token)
    second = await _confirm(client, settings, token)

    assert first.status_code == 204, first.text
    assert second.status_code == 400, second.text
    assert _error_code(second) == "link_invalid"
    assert await _user_count(session, email) == 1


# --- Tests that must prove a *commit*: a real session per request, read back on another connection --
#
# The shared-session `app` fixture runs every request in one transaction, where an uncommitted
# `DELETE` is already invisible to the test's own reads — so "deleted and committed" cannot be told
# from "deleted" there (2.2's AC-27 lesson). These tests run on a second app whose every request opens
# and commits its own session, and read the outcome through `engine.connect()`. Their rows are real:
# each test deletes what it made in a `finally`.


@pytest.fixture
def concurrent_app(
    settings: Settings, engine: AsyncEngine, password_hasher: Argon2PasswordHasher
) -> FastAPI:
    app = create_app(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.celery = celery_app
    app.state.password_hasher = password_hasher
    return app


async def _forget(engine: AsyncEngine, *emails: str) -> None:
    """Delete everything the real-commit tests made. Refuses a database not named `*_test`."""
    assert "_test" in str(engine.url), f"refusing a database not named *_test: {engine.url!r}"
    async with engine.begin() as conn:
        for email in emails:
            await conn.execute(
                text("DELETE FROM identity_pending_registration WHERE email = :e"), {"e": email}
            )
            await conn.execute(
                text("DELETE FROM identity_password_reset WHERE email = :e"), {"e": email}
            )
            await conn.execute(text("DELETE FROM identity_user WHERE email = :e"), {"e": email})


async def _real_registered_and_mailed(
    app: FastAPI, queue: RecordingAccountMailQueue, settings: Settings, email: str
) -> str:
    async with _new_client(app) as client:
        response = await _register(client, settings, email)
    assert response.status_code == 202, response.text
    async with app.state.session_factory() as worker_session:
        mailer = await deliver_registration(settings, worker_session, queue.registrations[-1])
    return confirmation_token(mailer)


async def test_confirming_an_expired_link_is_400_and_the_deletion_is_committed(
    concurrent_app: FastAPI, settings: Settings, engine: AsyncEngine
) -> None:
    """Mutation-proven (T33 carried item): `CommittingPendingRegistrationRepository.remove` without its
    `commit()` -> red on `the deletion must be committed before the request answers` (`assert 1 == 0`).
    Production restored byte-exact."""
    queue = install_recording_queue(concurrent_app)
    email = _unique_email("expired")
    try:
        token = await _real_registered_and_mailed(concurrent_app, queue, settings, email)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE identity_pending_registration SET requested_at = now() - interval '3 days', "
                    "expires_at = now() - interval '2 days' WHERE email = :e"
                ),
                {"e": email},
            )
        async with engine.connect() as conn:
            assert await _pending_count(conn, email) == 1

        async with _new_client(concurrent_app) as client:
            response = await _confirm(client, settings, token)

        assert response.status_code == 400, response.text
        assert _error_code(response) == "link_invalid"
        async with engine.connect() as other_connection:
            assert await _pending_count(other_connection, email) == 0, (
                "the deletion must be committed before the request answers"
            )
            assert await _user_count(other_connection, email) == 0
    finally:
        await _forget(engine, email)


async def test_confirming_an_address_that_became_an_account_meanwhile_is_409_and_commits_the_deletion(
    concurrent_app: FastAPI, settings: Settings, engine: AsyncEngine
) -> None:
    """Mutation-proven (T33 carried item): the same missing `commit()` on
    `CommittingPendingRegistrationRepository.remove` -> red. Production restored byte-exact."""
    queue = install_recording_queue(concurrent_app)
    email = _unique_email("raced")
    try:
        token = await _real_registered_and_mailed(concurrent_app, queue, settings, email)
        async with _new_client(concurrent_app) as other_browser:
            await seed_user_and_sign_in(other_browser, settings, email=email)
        async with engine.connect() as conn:
            assert await _pending_count(conn, email) == 1
            assert await _user_count(conn, email) == 1

        async with _new_client(concurrent_app) as client:
            response = await _confirm(client, settings, token)

        assert response.status_code == 409, response.text
        assert _error_code(response) == "email_already_registered"
        assert _no_cookie(response)
        async with engine.connect() as other_connection:
            assert await _pending_count(other_connection, email) == 0, (
                "the pending row's deletion must be committed before the request answers"
            )
            assert await _user_count(other_connection, email) == 1, "the account is untouched"
    finally:
        await _forget(engine, email)


async def test_confirm_has_no_limiter_and_works_with_redis_unreachable(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _unique_email("confirm-no-redis")
    token = await _registered_and_mailed(client, settings, session, queue, email)
    _override_settings(app, settings, redis_url=DEAD_REDIS_URL)

    response = await _confirm(client, settings, token)

    assert response.status_code == 204, response.text
    assert await _user_count(session, email) == 1


async def test_confirm_with_a_foreign_origin_is_403_and_the_link_stays_usable(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _unique_email("confirm-origin")
    token = await _registered_and_mailed(client, settings, session, queue, email)

    refused = await client.post(
        CONFIRM_URL, json={"token": token}, headers={"Origin": "https://evil.example"}
    )
    accepted = await _confirm(client, settings, token)

    assert refused.status_code == 403, refused.text
    assert _error_code(refused) == "origin_not_allowed"
    assert accepted.status_code == 204, accepted.text


# ---------------------------------------------------------------------------------------------
# AC-30 — POST /api/auth/password-reset
# ---------------------------------------------------------------------------------------------


async def test_a_reset_request_is_202_empty_no_store_with_no_cookie_a_row_and_one_task(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _unique_email("reset")
    await seed_user_and_sign_in(client, settings, email=email)

    response = await client.post(RESET_URL, json={"email": email}, headers=_headers(settings))

    assert response.status_code == 202, response.text
    assert response.content == b""
    assert response.headers.get("cache-control") == "no-store"
    assert _no_cookie(response)
    assert await _addressed_reset_count(session, email) == 1
    assert len(queue.resets) == 1


async def test_reset_request_responses_are_byte_identical_for_a_registered_and_an_unknown_address(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    known = _unique_email("reset-known")
    unknown = _unique_email("reset-unknown")
    await seed_user_and_sign_in(client, settings, email=known)

    for_known = await client.post(RESET_URL, json={"email": known}, headers=_headers(settings))
    for_unknown = await client.post(RESET_URL, json={"email": unknown}, headers=_headers(settings))

    assert for_known.status_code == 202, for_known.text
    assert _fingerprint(for_known) == _fingerprint(for_unknown)
    # Both left a row and enqueued: the request cannot tell them apart (the worker decides).
    assert await _addressed_reset_count(session, known) == 1
    assert await _addressed_reset_count(session, unknown) == 1
    assert len(queue.resets) == 2


async def test_reset_request_responses_are_byte_identical_over_twenty_known_and_unknown_pairs(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    queue: RecordingAccountMailQueue,
) -> None:
    _override_settings(
        app,
        settings,
        password_reset_rate_limit_per_ip_per_hour=1000,
        password_reset_rate_limit_per_email_per_hour=1000,
    )
    known = _unique_email("reset-pair")
    await seed_user_and_sign_in(client, settings, email=known)

    fingerprints = set()
    for _ in range(20):
        for email in (known, _unique_email("reset-pair-unknown")):
            response = await client.post(
                RESET_URL, json={"email": email}, headers=_headers(settings)
            )
            fingerprints.add(_fingerprint(response))

    assert len(fingerprints) == 1, fingerprints
    assert next(iter(fingerprints))[0] == 202
    assert len(queue.resets) == 40


async def test_the_worker_mails_a_reset_link_to_a_known_address_and_nothing_to_an_unknown_one(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    """The half of enumeration safety the request cannot show: the worker, which nobody can time,
    is where the two cases part (V-41 … V-44)."""
    known = _unique_email("worker-known")
    unknown = _unique_email("worker-unknown")
    await seed_user_and_sign_in(client, settings, email=known)
    await client.post(RESET_URL, json={"email": known}, headers=_headers(settings))
    await client.post(RESET_URL, json={"email": unknown}, headers=_headers(settings))
    assert len(queue.resets) == 2

    known_mailer = await deliver_password_reset(settings, session, queue.resets[0])
    unknown_mailer = await deliver_password_reset(settings, session, queue.resets[1])

    assert len(reset_token(known_mailer)) == 43
    assert unknown_mailer.sent == []


async def test_reset_request_a_foreign_origin_is_403_and_nothing_is_written(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _unique_email("reset-origin")

    refused = await client.post(
        RESET_URL, json={"email": email}, headers={"Origin": "https://evil.example"}
    )
    accepted = await client.post(RESET_URL, json={"email": email}, headers=_headers(settings))

    assert refused.status_code == 403, refused.text
    assert _error_code(refused) == "origin_not_allowed"
    assert accepted.status_code == 202, accepted.text
    assert await _addressed_reset_count(session, email) == 1, "only the accepted request wrote"
    assert len(queue.resets) == 1


async def test_reset_request_the_per_ip_limit_is_429_with_retry_after_before_the_address_is_parsed(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    queue: RecordingAccountMailQueue,
) -> None:
    _override_settings(app, settings, password_reset_rate_limit_per_ip_per_hour=1)

    first = await client.post(
        RESET_URL, json={"email": _unique_email("r-ip")}, headers=_headers(settings)
    )
    second = await client.post(
        RESET_URL, json={"email": "not-an-email"}, headers=_headers(settings)
    )

    assert first.status_code == 202, first.text
    assert second.status_code == 429, second.text  # not 422: the IP limiter precedes the parse
    assert _error_code(second) == "rate_limited"
    assert int(second.headers["retry-after"]) > 0
    assert _no_cookie(second)


async def test_reset_request_an_invalid_address_is_422_before_the_per_address_limiter(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    queue: RecordingAccountMailQueue,
) -> None:
    _override_settings(
        app,
        settings,
        password_reset_rate_limit_per_ip_per_hour=100,
        password_reset_rate_limit_per_email_per_hour=1,
    )

    invalid = [
        await client.post(RESET_URL, json={"email": "a@@b.com"}, headers=_headers(settings))
        for _ in range(3)
    ]
    keys_after_invalid = await _rate_limit_keys(settings, "rl:auth:password-reset:email:*")
    valid = await client.post(
        RESET_URL, json={"email": _unique_email("r-valid")}, headers=_headers(settings)
    )
    keys_after_valid = await _rate_limit_keys(settings, "rl:auth:password-reset:email:*")

    assert [r.status_code for r in invalid] == [422, 422, 422], [r.text for r in invalid]
    assert _error_code(invalid[0]) == "invalid_email"
    assert keys_after_invalid == []
    assert valid.status_code == 202, valid.text
    assert len(keys_after_valid) == 1, keys_after_valid


async def test_reset_request_the_per_address_limit_is_3_an_hour_with_retry_after(
    client: AsyncClient,
    settings: Settings,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _unique_email("r-per-address")

    responses = [
        await client.post(RESET_URL, json={"email": email}, headers=_headers(settings))
        for _ in range(4)
    ]

    assert [r.status_code for r in responses] == [202, 202, 202, 429], [r.text for r in responses]
    assert _error_code(responses[3]) == "rate_limited"
    assert int(responses[3].headers["retry-after"]) > 0
    keys = await _rate_limit_keys(settings, "rl:auth:password-reset:email:*")
    assert len(keys) == 1, keys
    assert email not in keys[0], keys
    assert len(queue.resets) == 3


async def test_reset_request_the_default_per_ip_limit_is_ten_an_hour(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    queue: RecordingAccountMailQueue,
) -> None:
    statuses = [
        (
            await client.post(
                RESET_URL, json={"email": _unique_email(f"r-ip-{i}")}, headers=_headers(settings)
            )
        ).status_code
        for i in range(11)
    ]

    assert statuses == [202] * 10 + [429], statuses


@pytest.mark.parametrize(
    "limiter_dependency",
    [get_password_reset_ip_rate_limiter, get_password_reset_email_rate_limiter],
    ids=["per-ip", "per-address"],
)
async def test_reset_request_with_either_limiter_unable_to_reach_redis_is_503_and_writes_nothing(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
    limiter_dependency: Callable[..., Any],
) -> None:
    email = _unique_email("r-redis-down")
    dead = _dead_limiter("auth:password-reset")
    app.dependency_overrides[limiter_dependency] = lambda: dead

    refused = await client.post(RESET_URL, json={"email": email}, headers=_headers(settings))
    del app.dependency_overrides[limiter_dependency]
    accepted = await client.post(RESET_URL, json={"email": email}, headers=_headers(settings))

    assert refused.status_code == 503, refused.text
    assert _error_code(refused) == "rate_limit_unavailable"
    assert _no_cookie(refused)
    assert accepted.status_code == 202, accepted.text
    assert await _addressed_reset_count(session, email) == 1, (
        "the 503 wrote nothing; the retry one row"
    )
    assert len(queue.resets) == 1


async def test_reset_request_with_the_broker_down_is_503_service_unavailable_and_the_row_remains(
    app: FastAPI, client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    broker = install_recording_queue(app, error=AccountMailQueueUnavailable())
    known = _unique_email("r-broker-known")
    unknown = _unique_email("r-broker-unknown")
    await seed_user_and_sign_in(client, settings, email=known)

    for_known = await client.post(RESET_URL, json={"email": known}, headers=_headers(settings))
    for_unknown = await client.post(RESET_URL, json={"email": unknown}, headers=_headers(settings))

    assert for_known.status_code == 503, for_known.text
    assert _error_code(for_known) == "service_unavailable"
    assert _fingerprint(for_known) == _fingerprint(for_unknown), "identical for every address"
    assert _no_cookie(for_known)
    assert len(broker.resets) == 2
    assert await _addressed_reset_count(session, known) == 1


async def test_reset_request_with_the_database_down_is_503_before_any_enqueue(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail() -> None:
        raise SQLAlchemyError("simulated commit failure (V-8)")

    monkeypatch.setattr(session, "commit", _fail)

    response = await client.post(
        RESET_URL, json={"email": _unique_email("r-db-down")}, headers=_headers(settings)
    )

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"
    assert queue.resets == []


# ---------------------------------------------------------------------------------------------
# AC-31 — POST /api/auth/password-reset/confirm
# ---------------------------------------------------------------------------------------------


async def _account_with_two_logins(
    app: FastAPI, settings: Settings, email: str
) -> tuple[AsyncClient, AsyncClient]:
    """Two browsers signed in to one account (two `Login`s, two refresh cookies); both refresh once
    first, so "old refresh cookie" means a cookie that **worked** a moment before the reset."""
    first = _new_client(app)
    await seed_user_and_sign_in(first, settings, email=email)
    second = _new_client(app)
    logged_in = await _login(second, settings, email, A_PASSWORD)
    assert logged_in.status_code == 200, logged_in.text
    for browser in (first, second):
        refreshed = await browser.post(REFRESH_URL, headers=_headers(settings))
        assert refreshed.status_code == 200, refreshed.text
    return first, second


async def test_resetting_a_password_is_204_changes_the_hash_and_revokes_every_login_and_reset(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    """Mutation-proven (T33 carried item): `revoked = await self._logins.remove_all_for_user(user.id)`
    replaced by `revoked = 0` in `ResetPassword` -> red on `assert 2 == 0` (both logins survive).
    Production restored byte-exact."""
    email = _unique_email("reset-ok")
    first, second = await _account_with_two_logins(app, settings, email)
    user_id = await _scalar(session, "SELECT id FROM identity_user WHERE email = :e", e=email)
    old_hash = await _scalar(
        session, "SELECT password_hash FROM identity_user WHERE email = :e", e=email
    )
    token = await _reset_requested_and_mailed(client, settings, session, queue, email)
    assert (
        await _scalar(session, "SELECT count(*) FROM identity_login WHERE user_id = :u", u=user_id)
        == 2
    )
    assert (
        await _scalar(
            session, "SELECT count(*) FROM identity_password_reset WHERE user_id = :u", u=user_id
        )
        == 1
    )

    try:
        response = await _reset_confirm(client, settings, token)

        assert response.status_code == 204, response.text
        assert response.content == b""
        assert response.headers.get("cache-control") == "no-store"
        assert _no_cookie(response)
        assert (
            await _scalar(
                session, "SELECT password_hash FROM identity_user WHERE email = :e", e=email
            )
            != old_hash
        )
        # Every login of the account is deleted, and every reset of the user with them.
        assert (
            await _scalar(
                session, "SELECT count(*) FROM identity_login WHERE user_id = :u", u=user_id
            )
            == 0
        )
        assert (
            await _scalar(
                session,
                "SELECT count(*) FROM identity_password_reset WHERE user_id = :u",
                u=user_id,
            )
            == 0
        )
        # The old refresh cookies — which worked a moment ago — are now 401 `not_signed_in`.
        for browser in (first, second):
            refreshed = await browser.post(REFRESH_URL, headers=_headers(settings))
            assert refreshed.status_code == 401, refreshed.text
            assert _error_code(refreshed) == "not_signed_in"
        # The new password signs in; the old one is the wrong-password answer.
        old_password = await _login(client, settings, email, A_PASSWORD)
        new_password = await _login(client, settings, email, A_NEW_PASSWORD)
        assert old_password.status_code == 401, old_password.text
        assert _error_code(old_password) == "invalid_credentials"
        assert new_password.status_code == 200, new_password.text
    finally:
        await first.aclose()
        await second.aclose()


async def test_a_reset_link_works_once(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _unique_email("reset-once")
    await seed_user_and_sign_in(client, settings, email=email)
    token = await _reset_requested_and_mailed(client, settings, session, queue, email)

    first = await _reset_confirm(client, settings, token)
    second = await _reset_confirm(client, settings, token, "yet another passphrase 11")

    assert first.status_code == 204, first.text
    assert second.status_code == 400, second.text
    assert _error_code(second) == "link_invalid"
    assert (await _login(client, settings, email, A_NEW_PASSWORD)).status_code == 200


@pytest.mark.parametrize(
    ("password", "code", "bound"),
    [
        ("short1", "password_too_short", ("min_length", 12)),
        ("x" * 129, "password_too_long", ("max_length", 128)),
        (None, "password_matches_email", None),
    ],
    ids=["too-short", "too-long", "equals-the-address"],
)
async def test_a_policy_refusal_is_422_hashes_nothing_and_leaves_the_link_usable(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
    hasher: _RecordingHasher,
    password: str | None,
    code: str,
    bound: tuple[str, int] | None,
) -> None:
    """Mutation-proven (T33 carried item): `await self._resets.remove(reset.id)` inserted in
    `ResetPassword` before `policy.check` -> red for all three parametrizations on `the refusal
    hashed nothing; only the retry did` (`assert 1 == (1 + 1)`: the consumed link never reaches the
    retry's hash). Production restored byte-exact."""
    email = _unique_email("reset-policy")
    await seed_user_and_sign_in(client, settings, email=email)
    token = await _reset_requested_and_mailed(client, settings, session, queue, email)
    hashes_before = hasher.hash_calls

    refused = await _reset_confirm(
        client, settings, token, password if password is not None else email
    )
    retried = await _reset_confirm(client, settings, token)

    assert refused.status_code == 422, refused.text
    assert _error_code(refused) == code
    if bound is not None:
        assert refused.json()["error"][bound[0]] == bound[1]
    assert hasher.hash_calls == hashes_before + 1, "the refusal hashed nothing; only the retry did"
    assert retried.status_code == 204, retried.text  # the token survived the refusal
    assert _no_cookie(refused)


async def test_a_hasher_failure_on_reset_is_503_with_nothing_changed_and_the_link_still_usable(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
    hasher: _RecordingHasher,
) -> None:
    email = _unique_email("reset-hasher")
    browser = _new_client(app)
    try:
        await seed_user_and_sign_in(browser, settings, email=email)
        user_id = await _scalar(session, "SELECT id FROM identity_user WHERE email = :e", e=email)
        old_hash = await _scalar(
            session, "SELECT password_hash FROM identity_user WHERE email = :e", e=email
        )
        token = await _reset_requested_and_mailed(client, settings, session, queue, email)
        hasher.fail_hash = True

        refused = await _reset_confirm(client, settings, token)

        assert refused.status_code == 503, refused.text
        assert _error_code(refused) == "service_unavailable"
        assert (
            await _scalar(
                session, "SELECT password_hash FROM identity_user WHERE email = :e", e=email
            )
            == old_hash
        )
        assert (
            await _scalar(
                session, "SELECT count(*) FROM identity_login WHERE user_id = :u", u=user_id
            )
            == 1
        ), "no login was revoked"
        hasher.fail_hash = False
        retried = await _reset_confirm(client, settings, token)
        assert retried.status_code == 204, retried.text
    finally:
        await browser.aclose()


async def test_reset_confirm_with_an_unknown_token_is_400_link_invalid(
    client: AsyncClient, settings: Settings
) -> None:
    response = await _reset_confirm(client, settings, A_WELL_FORMED_UNKNOWN_TOKEN)

    assert response.status_code == 400, response.text
    assert _error_code(response) == "link_invalid"
    assert _no_cookie(response)


@pytest.mark.parametrize("token", _MALFORMED_TOKENS)
async def test_reset_confirm_with_a_malformed_token_is_400_with_no_database_read_and_no_hash(
    client: AsyncClient,
    settings: Settings,
    engine: AsyncEngine,
    hasher: _RecordingHasher,
    token: str,
) -> None:
    with _captured_sql(engine) as statements:
        response = await _reset_confirm(client, settings, token)
        malformed_statements = list(statements)
    hashes_after_malformed = hasher.hash_calls
    with _captured_sql(engine) as statements:
        control = await _reset_confirm(client, settings, A_WELL_FORMED_UNKNOWN_TOKEN)
        control_statements = list(statements)

    assert response.status_code == 400, response.text
    assert _error_code(response) == "link_invalid"
    assert not _touches_identity_tables(malformed_statements), malformed_statements
    assert hashes_after_malformed == 0, "a malformed link must never reach argon2"
    assert control.status_code == 400, control.text
    assert any("identity_password_reset" in s.lower() for s in control_statements)


async def test_an_expired_reset_link_is_400_the_deletion_is_committed_and_nothing_changed(
    concurrent_app: FastAPI, settings: Settings, engine: AsyncEngine
) -> None:
    """Mutation-proven (T33 carried item): `CommittingPasswordResetRepository.remove` without its
    `commit()` -> red on `the expired row's deletion must be committed before the request answers`
    (`assert 1 == 0`). Production restored byte-exact."""
    queue = install_recording_queue(concurrent_app)
    email = _unique_email("reset-expired")
    try:
        async with _new_client(concurrent_app) as browser:
            await seed_user_and_sign_in(browser, settings, email=email)
            async with engine.connect() as conn:
                user_id = await _scalar(
                    conn, "SELECT id FROM identity_user WHERE email = :e", e=email
                )
                old_hash = await _scalar(
                    conn, "SELECT password_hash FROM identity_user WHERE email = :e", e=email
                )
            requested = await browser.post(
                RESET_URL, json={"email": email}, headers=_headers(settings)
            )
            assert requested.status_code == 202, requested.text
            async with concurrent_app.state.session_factory() as worker_session:
                mailer = await deliver_password_reset(settings, worker_session, queue.resets[-1])
            token = reset_token(mailer)
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE identity_password_reset SET requested_at = now() - interval '3 days', "
                        "expires_at = now() - interval '2 days' WHERE user_id = :u"
                    ),
                    {"u": user_id},
                )

            response = await _reset_confirm(browser, settings, token)

        assert response.status_code == 400, response.text
        assert _error_code(response) == "link_invalid"
        async with engine.connect() as other_connection:
            assert (
                await _scalar(
                    other_connection,
                    "SELECT count(*) FROM identity_password_reset WHERE user_id = :u",
                    u=user_id,
                )
                == 0
            ), "the expired row's deletion must be committed before the request answers"
            assert (
                await _scalar(
                    other_connection,
                    "SELECT password_hash FROM identity_user WHERE email = :e",
                    e=email,
                )
                == old_hash
            )
            assert (
                await _scalar(
                    other_connection,
                    "SELECT count(*) FROM identity_login WHERE user_id = :u",
                    u=user_id,
                )
                == 1
            ), "an expired link revokes nothing"
    finally:
        await _forget(engine, email)


async def test_reset_confirm_has_no_limiter_and_works_with_redis_unreachable(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _unique_email("reset-no-redis")
    await seed_user_and_sign_in(client, settings, email=email)
    token = await _reset_requested_and_mailed(client, settings, session, queue, email)
    _override_settings(app, settings, redis_url=DEAD_REDIS_URL)

    response = await _reset_confirm(client, settings, token)

    assert response.status_code == 204, response.text


async def test_reset_confirm_with_a_foreign_origin_is_403_and_the_link_stays_usable(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _unique_email("reset-origin-confirm")
    await seed_user_and_sign_in(client, settings, email=email)
    token = await _reset_requested_and_mailed(client, settings, session, queue, email)

    refused = await client.post(
        RESET_CONFIRM_URL,
        json={"token": token, "password": A_NEW_PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    accepted = await _reset_confirm(client, settings, token)

    assert refused.status_code == 403, refused.text
    assert _error_code(refused) == "origin_not_allowed"
    assert accepted.status_code == 204, accepted.text
