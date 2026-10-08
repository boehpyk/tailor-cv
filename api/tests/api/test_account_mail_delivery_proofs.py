"""Delivery proofs for slice 2.5 (T33 PROOF — AC-38, AC-39, AC-40), over the real HTTP routes, the
worker's own composition root and **real PostgreSQL**; only the mailer is a recording double.

Written from the spec's three proof rows, not from the use cases:

- **AC-38 supersede** — a second `register` for an address makes the first task's id `MISSING`
  (nothing sent) and the first link `400 link_invalid`; a second *issued* reset deletes the first.
- **AC-39 redelivery sends once** — the same task twice is `SENT` then `SKIPPED` with one mail. A
  crash after the commit and before the send is **staged**, not simulated: the row is issued through
  the real committing adapter and the mailer is never called — exactly the state a worker death
  leaves. The redelivery is `SKIPPED` (no mail, row still issued) and a new request delivers.
- **AC-40 enumeration** — the structural half lives in `tests/integration/identity/
  test_request_registration.py` / `test_request_password_reset.py` (constructor type hints + the
  module's AST) and the byte-identity half over twenty pairs in `test_auth_email_verification.py`
  (register and reset-request). Neither is duplicated here.

**Outcomes are read, not inferred.** `deliver_registration` in `account_mail_support` discards the
use case's `DeliveryOutcome`; these tests need it (`MISSING` vs `SKIPPED` is the claim), so they
call the worker's builders directly.

Mutations, each run locally against production code and restored byte-exact (`git diff
--exit-code` clean). Green first; then, red:

| mutation | red |
|---|---|
| `DeliverRegistrationMail`: delete the `token_hash is not None -> SKIPPED` guard | the registration redelivery test and the registration crash test |
| `DeliverPasswordResetMail`: delete the `case IssuedReset() -> SKIPPED` arm | the reset redelivery test and the reset crash test |
| `put`: drop `"id"` from the upsert's `set_` (a supersede that keeps the old id) | the supersede-task test, the supersede-link test, the registration crash test |
| `put`: drop the token-column nulling from `set_` (the old link survives) | the supersede-link test, the registration crash test |
| reset `save_issued`: make the supersede `DELETE` match nothing | the second-issued-reset test |
| AC-40, `RequestRegistration` given a `UserRepository` + a `find_by_email` call | `tests/integration/identity/test_request_registration.py`: the constructor test, the module test and every behavioural test (ten red) |
| AC-40, `RequestRegistration` imports `UserRepository` inside the method only | the module test alone (the constructor test stays green, so the AST half is not redundant) |
| AC-40, `RequestPasswordReset` given a `UserRepository` | `tests/integration/identity/test_request_password_reset.py`: constructor and module tests red |
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.identity.delivery_outcome import DeliveryOutcome, DeliveryStatus
from tailorcraft.domain.identity.value_objects import (
    PasswordResetId,
    PendingRegistrationId,
    UserId,
)
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.identity.one_time_tokens import SecretsOneTimeTokenMinter
from tailorcraft.infrastructure.identity.token_access import (
    CommittingPasswordResetRepository,
    CommittingPendingRegistrationRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.password_reset import (
    SqlAlchemyPasswordResetRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.pending_registration import (
    SqlAlchemyPendingRegistrationRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container
from tests.api.account_mail_support import (
    confirmation_token,
    install_recording_queue,
    reset_token,
)
from tests.api.me_support import A_PASSWORD, count_rows, seed_user_and_sign_in
from tests.integration.fakes import RecordingAccountMailer, RecordingAccountMailQueue

REGISTER_URL = "/api/auth/register"
CONFIRM_URL = "/api/auth/registration/confirm"
RESET_URL = "/api/auth/password-reset"
RESET_CONFIRM_URL = "/api/auth/password-reset/confirm"
A_NEW_PASSWORD = "an entirely different passphrase 7"


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """The per-address limiters live in Redis, which the database rollback does not reach."""


@pytest.fixture
def queue(app: FastAPI) -> RecordingAccountMailQueue:
    return install_recording_queue(app)


def _origin(settings: Settings) -> dict[str, str]:
    return {"Origin": settings.public_base_url}


def _email(label: str) -> str:
    return f"t33-{label}-{uuid4().hex[:12]}@example.com"


async def _register(client: AsyncClient, settings: Settings, email: str) -> Response:
    response = await client.post(
        REGISTER_URL, json={"email": email, "password": A_PASSWORD}, headers=_origin(settings)
    )
    assert response.status_code == 202, response.text
    return response


async def _request_reset(client: AsyncClient, settings: Settings, email: str) -> Response:
    response = await client.post(RESET_URL, json={"email": email}, headers=_origin(settings))
    assert response.status_code == 202, response.text
    return response


async def _confirm(client: AsyncClient, settings: Settings, token: str) -> Response:
    return await client.post(CONFIRM_URL, json={"token": token}, headers=_origin(settings))


async def _reset_confirm(client: AsyncClient, settings: Settings, token: str) -> Response:
    return await client.post(
        RESET_CONFIRM_URL,
        json={"token": token, "password": A_NEW_PASSWORD},
        headers=_origin(settings),
    )


async def _run_registration(
    settings: Settings,
    session: AsyncSession,
    pending_id: PendingRegistrationId,
    mailer: RecordingAccountMailer | None = None,
) -> tuple[DeliveryOutcome, RecordingAccountMailer]:
    """One delivery task, the worker's own root; the outcome is what the use case returned."""
    mailer = mailer or RecordingAccountMailer()
    outcome = await container._build_registration_delivery(settings, session, mailer)(pending_id)
    await session.commit()
    return outcome, mailer


async def _run_reset(
    settings: Settings,
    session: AsyncSession,
    reset_id: PasswordResetId,
    mailer: RecordingAccountMailer | None = None,
) -> tuple[DeliveryOutcome, RecordingAccountMailer]:
    mailer = mailer or RecordingAccountMailer()
    outcome = await container._build_password_reset_delivery(settings, session, mailer)(reset_id)
    await session.commit()
    return outcome, mailer


async def _pending_rows(session: AsyncSession, email: str) -> int:
    return await count_rows(session, "identity_pending_registration", email=email.lower())


# --------------------------------------------------------------------------- AC-38: supersede


async def test_a_superseded_registrations_task_is_missing_and_sends_nothing(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _email("supersede-task")
    await _register(client, settings, email)
    await _register(client, settings, email)
    assert len(queue.registrations) == 2
    old_id, new_id = queue.registrations
    assert old_id != new_id
    assert await _pending_rows(session, email) == 1

    old_outcome, old_mailer = await _run_registration(settings, session, old_id)
    new_outcome, new_mailer = await _run_registration(settings, session, new_id)

    assert old_outcome.status is DeliveryStatus.MISSING
    assert old_mailer.sent == []
    assert new_outcome.status is DeliveryStatus.SENT
    assert len(confirmation_token(new_mailer)) == 43


async def test_a_superseded_registrations_mailed_link_is_400_and_the_newer_one_works(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _email("supersede-link")
    await _register(client, settings, email)
    first_outcome, first_mailer = await _run_registration(settings, session, queue.registrations[0])
    assert first_outcome.status is DeliveryStatus.SENT
    first_token = confirmation_token(first_mailer)

    await _register(client, settings, email)
    _, second_mailer = await _run_registration(settings, session, queue.registrations[1])
    second_token = confirmation_token(second_mailer)
    assert second_token != first_token

    stale = await _confirm(client, settings, first_token)
    assert stale.status_code == 400
    assert stale.json()["error"]["code"] == "link_invalid"
    assert await count_rows(session, "identity_user", email=email.lower()) == 0

    fresh = await _confirm(client, settings, second_token)
    assert fresh.status_code == 204, fresh.text
    assert await count_rows(session, "identity_user", email=email.lower()) == 1


async def test_a_second_issued_reset_deletes_the_first_and_its_link_is_400(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _email("supersede-reset")
    _, user_id = await seed_user_and_sign_in(client, settings, email=email)
    await _request_reset(client, settings, email)
    await _request_reset(client, settings, email)
    first_id, second_id = queue.resets
    assert first_id != second_id

    first_outcome, first_mailer = await _run_reset(settings, session, first_id)
    assert first_outcome.status is DeliveryStatus.SENT
    first_token = reset_token(first_mailer)
    assert await count_rows(session, "identity_password_reset", user_id=user_id) == 1

    second_outcome, second_mailer = await _run_reset(settings, session, second_id)
    assert second_outcome.status is DeliveryStatus.SENT
    second_token = reset_token(second_mailer)
    assert await count_rows(session, "identity_password_reset", user_id=user_id) == 1

    stale = await _reset_confirm(client, settings, first_token)
    assert stale.status_code == 400
    assert stale.json()["error"]["code"] == "link_invalid"
    fresh = await _reset_confirm(client, settings, second_token)
    assert fresh.status_code == 204, fresh.text


# ------------------------------------------------------------------ AC-39: redelivery sends once


async def test_redelivering_a_registration_task_sends_one_mail(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    await _register(client, settings, _email("redeliver"))
    pending_id = queue.registrations[0]
    mailer = RecordingAccountMailer()  # one mailer across both runs: its log is the count

    first, _ = await _run_registration(settings, session, pending_id, mailer)
    second, _ = await _run_registration(settings, session, pending_id, mailer)

    assert (first.status, second.status) == (DeliveryStatus.SENT, DeliveryStatus.SKIPPED)
    assert len(mailer.sent) == 1


async def test_redelivering_a_reset_task_sends_one_mail(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _email("redeliver-reset")
    await seed_user_and_sign_in(client, settings, email=email)
    await _request_reset(client, settings, email)
    reset_id = queue.resets[0]
    mailer = RecordingAccountMailer()

    first, _ = await _run_reset(settings, session, reset_id, mailer)
    second, _ = await _run_reset(settings, session, reset_id, mailer)

    assert (first.status, second.status) == (DeliveryStatus.SENT, DeliveryStatus.SKIPPED)
    assert len(mailer.sent) == 1


async def test_a_registration_whose_worker_died_after_the_commit_is_skipped_and_a_new_one_delivers(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    """V-30. The crash is staged: the row is issued **through the real committing adapter** and the
    mailer is never reached — the state a death between the commit and the send leaves."""
    email = _email("crash-registration")
    await _register(client, settings, email)
    crashed_id = queue.registrations[0]
    pending = CommittingPendingRegistrationRepository(
        SqlAlchemyPendingRegistrationRepository(session), session
    )
    row = await pending.get(crashed_id)
    assert row is not None
    assert row.token_hash is None
    row.issue(SecretsOneTimeTokenMinter().mint().token_hash, SystemClock().now())
    await pending.save_issued(row)

    outcome, mailer = await _run_registration(settings, session, crashed_id)

    assert outcome.status is DeliveryStatus.SKIPPED
    assert mailer.sent == []
    stored = await SqlAlchemyPendingRegistrationRepository(session).get(crashed_id)
    assert stored is not None
    assert stored.token_hash is not None  # issued, never mailed

    await _register(client, settings, email)  # the recovery: Send it again
    fresh_id = queue.registrations[1]
    assert fresh_id != crashed_id
    fresh_outcome, fresh_mailer = await _run_registration(settings, session, fresh_id)
    assert fresh_outcome.status is DeliveryStatus.SENT
    confirmed = await _confirm(client, settings, confirmation_token(fresh_mailer))
    assert confirmed.status_code == 204, confirmed.text


async def test_a_reset_whose_worker_died_after_the_commit_is_skipped_and_a_new_one_delivers(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    queue: RecordingAccountMailQueue,
) -> None:
    email = _email("crash-reset")
    _, user_uuid = await seed_user_and_sign_in(client, settings, email=email)
    await _request_reset(client, settings, email)
    crashed_id = queue.resets[0]
    resets = CommittingPasswordResetRepository(SqlAlchemyPasswordResetRepository(session), session)
    row = await resets.get(crashed_id)
    assert row is not None
    assert row.token_hash is None
    row.issue(
        UserId(UUID(str(user_uuid))),
        SecretsOneTimeTokenMinter().mint().token_hash,
        SystemClock().now(),
    )
    await resets.save_issued(row)

    outcome, mailer = await _run_reset(settings, session, crashed_id)

    assert outcome.status is DeliveryStatus.SKIPPED
    assert mailer.sent == []
    stored = await SqlAlchemyPasswordResetRepository(session).get(crashed_id)
    assert stored is not None
    assert stored.token_hash is not None

    await _request_reset(client, settings, email)
    fresh_id = queue.resets[1]
    fresh_outcome, fresh_mailer = await _run_reset(settings, session, fresh_id)
    assert fresh_outcome.status is DeliveryStatus.SENT
    reset = await _reset_confirm(client, settings, reset_token(fresh_mailer))
    assert reset.status_code == 204, reset.text
