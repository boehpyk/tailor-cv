"""Slice 2.5, `/verify` round 1: password-reset **delivery** against account **erasure**, on two real
connections (technical plan section 0.8, AC-37; the lock-order table's claim that "everything that
takes both the user and a reset takes the user first").

**The defect this stages.** Delivery's issue (`save_issued`) used to lock the reset row `R` first (its
`UPDATE`), and only then take `FOR KEY SHARE` on the user row through the FK check. Erasure holds the
user `FOR UPDATE` and then deletes the account's addressed resets **by address** -- which waits on `R`.
Erasure waits on delivery for `R`, delivery waits on erasure for the user: `deadlock_detected`, and
one side aborts (a 503 on delete-account, the operator's `erase-account` failing, or a mail task
escaping a `DBAPIError`). The spec's order is **user first** for every actor that takes both.

**The staging, at the moment the spec names and independent of the fix.** Erasure takes the user lock
(the real `SqlAlchemyAccountData.files_of_account`). The real delivery use case then starts, and the
test waits until a backend is **waiting on a lock** -- whichever statement it is. Today that is the
reset `UPDATE` (R already locked, blocked on the user); after a fix that takes the user lock first it
is the user `FOR KEY SHARE` (R not yet locked). The seam is "delivery is blocked behind erasure's
lock", never "the UPDATE has locked R", so it survives the fix. Only then does erasure proceed to its
real `delete_account` and commit.

**Spec'd outcome (not what the code does today).** Erasure succeeds (the account and every reset row
for the address and the user are gone); delivery ends in a defined `DeliveryOutcome` that sent
nothing (`NO_ACCOUNT` -- no account has the address any more -- or `SKIPPED` -- the row it meant to
issue is gone); the `RecordingAccountMailer` holds no mail; and `pg_stat_database.deadlocks` does not
move. Neither side raises.

Real, committed rows on `tailorcraft_test`, cleaned up in a `finally`. Every transaction pins
`SET LOCAL lock_timeout` so a regression fails with a message naming a lock instead of hanging CI.
The operator path (`erase-account`) shares `delete_account` and `files_of_account` with the route's
`EraseAccount`, so this one staging covers both.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.application.identity.delivery_outcome import DeliveryOutcome, DeliveryStatus
from tailorcraft.domain.identity.value_objects import PasswordResetId, UserId
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.retention.account_data import SqlAlchemyAccountData
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container
from tests.api.account_mail_support import install_recording_queue
from tests.api.me_support import build_concurrent_app, new_client, seed_user_and_sign_in
from tests.integration.claim_race_support import (
    LOCK_TIMEOUT_MS,
    assert_test_database,
    wait_for_lock_waiter,
)
from tests.integration.fakes import RecordingAccountMailer

RESET_URL = "/api/auth/password-reset"
WAIT_SECONDS = 10.0
NOTHING_SENT = {DeliveryStatus.NO_ACCOUNT, DeliveryStatus.SKIPPED}


@dataclass
class World:
    app: FastAPI
    settings: Settings
    engine: AsyncEngine
    email: str
    user_id: UUID
    reset_id: PasswordResetId


@pytest_asyncio.fixture
async def world(
    settings: Settings,
    engine: AsyncEngine,
    password_hasher: Argon2PasswordHasher,
    clear_redis: None,
) -> AsyncIterator[World]:
    """A committed account and one committed, **addressed, undelivered** reset for it -- the state
    the API leaves for the worker (the route enqueues the id; the recording queue holds it)."""
    assert_test_database(settings)
    app = build_concurrent_app(settings, engine, password_hasher)
    queue = install_recording_queue(app)
    email = f"lockorder-{uuid4().hex[:12]}@example.com"
    async with new_client(app) as browser:
        _token, user_id = await seed_user_and_sign_in(browser, settings, email=email)
        response = await browser.post(
            RESET_URL, json={"email": email}, headers={"Origin": settings.public_base_url}
        )
        assert response.status_code == 202, response.text
    assert len(queue.resets) == 1, "the reset request enqueued no delivery"
    try:
        yield World(app, settings, engine, email, user_id, queue.resets[0])
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM identity_password_reset WHERE email = :e"), {"e": email}
            )
            await conn.execute(text("DELETE FROM identity_user WHERE email = :e"), {"e": email})


async def _scalar(world: World, sql: str, **params: object) -> Any:
    async with world.engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def _deadlock_count(world: World) -> int:
    return int(
        await _scalar(
            world, "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()"
        )
    )


async def _deliver(world: World) -> DeliveryOutcome:
    """The worker's own composition root, one transaction, `lock_timeout` pinned inside it."""
    mailer = RecordingAccountMailer()
    async with world.app.state.session_factory() as session:
        await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'"))
        deliver = container._build_password_reset_delivery(world.settings, session, mailer)
        outcome = await deliver(world.reset_id)
        await session.commit()
    assert mailer.sent == [], f"mail was sent for an account being erased: {mailer.sent!r}"
    return outcome


async def test_reset_delivery_and_account_erasure_never_deadlock_and_send_nothing(
    world: World,
) -> None:
    deadlocks_before = await _deadlock_count(world)
    erasure_error: BaseException | None = None
    delivery_error: BaseException | None = None
    outcome: DeliveryOutcome | None = None
    delivery: asyncio.Task[DeliveryOutcome] | None = None

    async with world.app.state.session_factory() as session:
        await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'"))
        data = SqlAlchemyAccountData(session)
        try:
            # Erasure takes the user row FOR UPDATE (its first real step) ...
            await data.files_of_account(UserId(world.user_id))
            # ... and delivery starts now: it must end up waiting on that lock.
            delivery = asyncio.create_task(_deliver(world))
            waiters = await wait_for_lock_waiter(world.engine, within=WAIT_SECONDS)
            assert waiters, "delivery never waited behind erasure's lock: the race was not staged"
            # Staged. Erasure proceeds to its delete-by-address and the account's delete.
            await asyncio.wait_for(data.delete_account(UserId(world.user_id)), WAIT_SECONDS)
            await session.commit()
        except BaseException as caught:
            erasure_error = caught
            with contextlib.suppress(Exception):
                await session.rollback()

    assert delivery is not None, f"erasure failed before delivery started: {erasure_error!r}"
    try:
        outcome = await asyncio.wait_for(delivery, WAIT_SECONDS)
    except BaseException as caught:
        delivery_error = caught

    assert await _deadlock_count(world) == deadlocks_before, (
        f"deadlock_detected was observed (erasure: {erasure_error!r}; delivery: {delivery_error!r})"
    )
    assert erasure_error is None, f"erasure failed: {erasure_error!r}"
    assert delivery_error is None, f"delivery failed: {delivery_error!r}"
    assert outcome is not None, "delivery returned no outcome"
    assert outcome.status in NOTHING_SENT, outcome
    assert (
        await _scalar(world, "SELECT count(*) FROM identity_user WHERE id = :u", u=world.user_id)
        == 0
    ), "the account survived its erasure"
    assert (
        await _scalar(
            world,
            "SELECT count(*) FROM identity_password_reset WHERE email = :e OR user_id = :u",
            e=world.email,
            u=world.user_id,
        )
        == 0
    ), "a reset row for the erased account's address or id was left behind"
