"""AC-54 / AC-55 — the planted-marker test for `identity-email-verification` (slice 2.5, T28).

Follows `test_auth_privacy_markers.py` (2.1) and `test_claim_privacy_markers.py` (2.4): markers in
every PII/credential field the slice touches, the whole flow driven through the real app, every
capture channel read afterwards.

**The flow** (spec AC-54): register a new address -> the worker half (in-process, through the real
composition root and a recording mailer) -> confirm with the token **taken off the mail** -> login ->
register the same address again (the account-exists notice) -> reset request for a known and an
unknown address -> deliver both -> reset-confirm -> login with the new password -> one address
registered and never confirmed, and one whose send blows up -> the sweep, 25 h on -> delete the
account.

**What is read for a marker.** Every captured log record (stdlib and structlog, all loggers: the
Alembic `disable_existing_loggers` footgun in CLAUDE.md is asserted *absent*, not assumed), every
Sentry envelope, every domain event the app published, every Redis key, every response body and
header outside its one legitimate channel, the **arguments the real queue adapter hands to Celery**,
the `repr` of every mail, and `traceback.format_exception` of an exception raised out of the
delivery use case while it held the token and the address in its frame. After the account is erased,
no row of any `identity_*` table carries any address the flow used.

**What a marker is.** The address's local part and the passwords are planted. The tokens are not
planted, they are *taken from the recording mailer* — the way a person gets them — and so are their
hashes, which are read from the database. A leak of a real token proves more than a leak of a
look-alike.

**Positive controls** (a marker search over a channel that captured nothing proves nothing): the
channel's capture of a deliberately logged marker; the enqueue line; the delivery line
(`identity.mail_delivered`); at least one domain event; a Sentry envelope; at least one Redis key.

**No soft gates.** Every step is a hard status assertion; against the skeleton the first failing step
is the red.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import time
import traceback
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sentry_sdk
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.identity.delivery_outcome import DeliveryOutcome
from tailorcraft.domain.identity.account_mail import AccountMail
from tailorcraft.domain.identity.value_objects import PasswordResetId, PendingRegistrationId
from tailorcraft.domain.intake.value_objects import ExtractedText
from tailorcraft.domain.posting.value_objects import JobPostingText
from tailorcraft.domain.tailoring.ports import LlmPort
from tailorcraft.infrastructure import observability
from tailorcraft.infrastructure.api.deps import get_event_publisher, get_llm
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.mail.queue import (
    KIND_PASSWORD_RESET,
    KIND_REGISTRATION,
    CeleryAccountMailQueue,
)
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container, identity_mail
from tests.api.account_mail_support import (
    confirmation_token,
    install_recording_queue,
    reset_token,
    sent_account_exists_notice,
)
from tests.api.test_history_privacy_markers import (
    _CapturingTransport,
    _draft,
    _event_text,
    _TeePublisher,
)
from tests.integration.fakes import FakeLlm, RecordingAccountMailer

REGISTER_URL = "/api/auth/register"
LOGIN_URL = "/api/auth/login"
CONFIRM_URL = "/api/auth/registration/confirm"
RESET_URL = "/api/auth/password-reset"
RESET_CONFIRM_URL = "/api/auth/password-reset/confirm"
DELETE_ACCOUNT_URL = "/api/auth/delete-account"

_PREFIX: Final = "QA54MARKER"


def _marker(label: str) -> str:
    return f"{_PREFIX}-{label}-{uuid4().hex}"


def _marker_email(label: str) -> str:
    """Well-formed and lower-case (the address is normalized, so a marker that is already
    normalized is what a leak would look like)."""
    return f"{_marker(label).lower()}@example.com"


def _headers(settings: Settings) -> dict[str, str]:
    return {"Origin": settings.public_base_url}


class _Boom(Exception):
    """An unexpected failure out of the mailer. Not `RuntimeError`: `NotImplementedError` is one, and
    a skeleton would satisfy `pytest.raises(RuntimeError)` vacuously (CLAUDE.md, 1.6)."""


class _ExplodingMailer:
    """`AccountMailPort` whose `send` raises a non-domain error, **after** the use case has the token
    and the address in its frame: the exception a Sentry report would carry."""

    async def send(self, mail: AccountMail) -> None:
        raise _Boom("the mail server fell over")


class _CapturingCelery:
    """Stands in for the `Celery` app: records exactly what `send_task` is asked to publish."""

    def __init__(self) -> None:
        self.published: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def send_task(self, name: str, *args: Any, **kwargs: Any) -> None:  # Any: celery's own typing
        self.published.append((name, args, kwargs))


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """`raise_app_exceptions=False`: a skeleton's `NotImplementedError` must be a 500 the step's
    assertion reads."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Every step here but the confirms touches a limiter; the rollback never reaches Redis."""


@pytest.fixture
def sentry_envelopes(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Sentry through the real `configure_sentry` (production's PII settings), transport swapped for
    a capturing one; torn down to "no client"."""
    transport = _CapturingTransport()
    real_init = sentry_sdk.init

    def _init(*args: Any, **kwargs: Any) -> Any:  # Any: sentry_sdk.init's own signature
        return real_init(*args, transport=transport, **kwargs)

    monkeypatch.setattr(sentry_sdk, "init", _init)
    observability.configure_sentry(
        settings.model_copy(update={"sentry_dsn": "https://public@sentry.example.invalid/1"})
    )
    try:
        yield transport.envelopes
    finally:
        real_init()


async def _scalar(session: AsyncSession, sql: str, **params: object) -> Any:  # a SQL scalar
    return (await session.execute(text(sql), params)).scalar_one()


async def _deliver_registration(
    settings: Settings,
    session: AsyncSession,
    pending_id: PendingRegistrationId,
    mailer: RecordingAccountMailer,
) -> DeliveryOutcome:
    """The worker half through its own composition root, **keeping the outcome** so the task's own
    `_log_outcome` can be driven with a real one (the delivery line, the positive control)."""
    outcome = await container._build_registration_delivery(settings, session, mailer)(pending_id)
    await session.commit()
    return outcome


async def _deliver_reset(
    settings: Settings,
    session: AsyncSession,
    reset_id: PasswordResetId,
    mailer: RecordingAccountMailer,
) -> DeliveryOutcome:
    outcome = await container._build_password_reset_delivery(settings, session, mailer)(reset_id)
    await session.commit()
    return outcome


# --- AC-55 (b), pure: the port and the tasks ------------------------------------------------------


def test_ac55_the_llm_port_signature_is_pinned_and_no_mail_builder_takes_one() -> None:
    """Green on arrival — `LlmPort` is untouched by 2.5, and this pins that it stays so."""
    signature = inspect.signature(LlmPort.tailor)

    assert list(signature.parameters) == ["self", "cv", "posting"]
    assert signature.parameters["cv"].annotation in ("ExtractedText", ExtractedText)
    assert signature.parameters["posting"].annotation in ("JobPostingText", JobPostingText)
    for builder in (
        container._build_registration_delivery,
        container._build_password_reset_delivery,
        container._build_identity_token_sweep,
    ):
        assert not any("llm" in name for name in inspect.signature(builder).parameters), builder


def test_the_two_mail_tasks_take_one_id_string_and_nothing_else() -> None:
    """AC-23's task signature, from the code a worker would register: one `str`, no address, no token."""
    for task in (
        identity_mail.deliver_registration_mail,
        identity_mail.deliver_password_reset_mail,
    ):
        parameters = list(inspect.signature(task.run).parameters.values())
        assert len(parameters) == 1, parameters
        assert parameters[0].annotation in ("str", str)


# --- AC-55 (a): the fake LlmPort is never called --------------------------------------------------


async def test_ac55_no_route_of_the_slice_calls_the_llm(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession
) -> None:
    fake_llm = FakeLlm(_draft(_marker("cv"), _marker("letter")))
    app.dependency_overrides[get_llm] = lambda: fake_llm
    queue = install_recording_queue(app)
    email = _marker_email("llm-sees-nothing")

    registered = await client.post(
        REGISTER_URL,
        json={"email": email, "password": "correct horse battery staple 9"},
        headers=_headers(settings),
    )
    assert registered.status_code == 202, registered.text
    assert len(queue.registrations) == 1
    mailer = RecordingAccountMailer()
    await _deliver_registration(settings, session, queue.registrations[0], mailer)
    confirmed = await client.post(
        CONFIRM_URL, json={"token": confirmation_token(mailer)}, headers=_headers(settings)
    )
    assert confirmed.status_code == 204, confirmed.text
    reset = await client.post(RESET_URL, json={"email": email}, headers=_headers(settings))
    assert reset.status_code == 202, reset.text

    assert fake_llm.calls == []


# --- AC-54: the whole flow ------------------------------------------------------------------------


async def test_ac54_no_marker_token_hash_or_address_leaks_across_the_whole_account_mail_flow(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
    sentry_envelopes: list[str],
) -> None:
    email = _marker_email("address")
    unknown_email = _marker_email("unknown-address")
    unconfirmed_email = _marker_email("never-confirmed")
    exploding_email = _marker_email("mail-server-down")
    password = _marker("password") + " correct horse battery staple"
    new_password = _marker("new-password") + " an entirely different passphrase"
    wrong_password = _marker("wrong-password") + " nope nope nope"

    publisher = _TeePublisher()
    app.dependency_overrides[get_event_publisher] = lambda: publisher
    fake_llm = FakeLlm(_draft(_marker("cv"), _marker("letter")))
    app.dependency_overrides[get_llm] = lambda: fake_llm
    queue = install_recording_queue(app)
    headers = _headers(settings)

    responses: list[Response] = []
    mailers: list[RecordingAccountMailer] = []
    outcomes: list[tuple[str, str, DeliveryOutcome]] = []
    tokens: dict[str, str] = {}
    hashes: dict[str, str] = {}
    errors: list[str] = []

    # --- The capture is only worth reading if it is on. Alembic's `fileConfig` once disabled 24
    # loggers for the rest of the session (CLAUDE.md); a "no marker in the logs" over a disabled
    # logger is vacuous. ---------------------------------------------------------------------------
    disabled = [
        name
        for name, candidate in logging.root.manager.loggerDict.items()
        if isinstance(candidate, logging.Logger)
        and candidate.disabled
        and name.split(".")[0]
        in {"tailorcraft", "sqlalchemy", "celery", "kombu", "httpx", "asyncio"}
    ]
    assert disabled == [], f"loggers disabled before the flow ran: {disabled}"

    with caplog.at_level(logging.DEBUG):
        # --- positive control: this capture sees a marker ------------------------------------------
        control = _marker("control")
        structlog.get_logger("tailorcraft.test_control").info("control.line", planted=control)
        logging.getLogger("tailorcraft.test_control").warning("stdlib control %s", control)
        assert caplog.text.count(control) == 2, "the log capture would not have seen a leak"

        # --- register (new) ------------------------------------------------------------------------
        registered = await client.post(
            REGISTER_URL, json={"email": email, "password": password}, headers=headers
        )
        responses.append(registered)
        assert registered.status_code == 202, registered.text
        assert len(queue.registrations) == 1
        pending_id = queue.registrations[0]

        mailer = RecordingAccountMailer()
        mailers.append(mailer)
        outcomes.append(
            (
                KIND_REGISTRATION,
                str(pending_id.value),
                await _deliver_registration(settings, session, pending_id, mailer),
            )
        )
        tokens["confirmation token"] = confirmation_token(mailer)
        hashes["confirmation token hash"] = await _scalar(
            session,
            "SELECT token_hash FROM identity_pending_registration WHERE id = :i",
            i=pending_id.value,
        )
        hashes["pending password hash"] = await _scalar(
            session,
            "SELECT password_hash FROM identity_pending_registration WHERE id = :i",
            i=pending_id.value,
        )
        assert (
            hashes["confirmation token hash"]
            == hashlib.sha256(tokens["confirmation token"].encode()).hexdigest()
        ), "the stored hash is not the SHA-256 of the mailed token (ADR-0027)"

        # --- confirm -----------------------------------------------------------------------------------
        confirmed = await client.post(
            CONFIRM_URL, json={"token": tokens["confirmation token"]}, headers=headers
        )
        responses.append(confirmed)
        assert confirmed.status_code == 204, confirmed.text
        user_id = await _scalar(session, "SELECT id FROM identity_user WHERE email = :e", e=email)
        assert isinstance(user_id, UUID)
        hashes["user password hash"] = await _scalar(
            session, "SELECT password_hash FROM identity_user WHERE id = :i", i=user_id
        )

        # --- login (a wrong password first: its variant is a marker too) -------------------------------
        refused = await client.post(
            LOGIN_URL, json={"email": email, "password": wrong_password}, headers=headers
        )
        responses.append(refused)
        assert refused.status_code == 401, refused.text
        logged_in = await client.post(
            LOGIN_URL, json={"email": email, "password": password}, headers=headers
        )
        responses.append(logged_in)
        assert logged_in.status_code == 200, logged_in.text

        # --- register again: the address is an account now -> the notice -------------------------------
        again = await client.post(
            REGISTER_URL, json={"email": email, "password": password}, headers=headers
        )
        responses.append(again)
        assert again.status_code == 202, again.text
        assert len(queue.registrations) == 2
        notice_mailer = RecordingAccountMailer()
        mailers.append(notice_mailer)
        outcomes.append(
            (
                KIND_REGISTRATION,
                str(queue.registrations[1].value),
                await _deliver_registration(
                    settings, session, queue.registrations[1], notice_mailer
                ),
            )
        )
        assert sent_account_exists_notice(notice_mailer)

        # --- reset request: known and unknown ------------------------------------------------------------
        for address in (email, unknown_email):
            requested = await client.post(RESET_URL, json={"email": address}, headers=headers)
            responses.append(requested)
            assert requested.status_code == 202, requested.text
        assert len(queue.resets) == 2
        known_mailer, unknown_mailer = RecordingAccountMailer(), RecordingAccountMailer()
        mailers.extend([known_mailer, unknown_mailer])
        outcomes.append(
            (
                KIND_PASSWORD_RESET,
                str(queue.resets[0].value),
                await _deliver_reset(settings, session, queue.resets[0], known_mailer),
            )
        )
        tokens["reset token"] = reset_token(known_mailer)
        hashes["reset token hash"] = await _scalar(
            session,
            "SELECT token_hash FROM identity_password_reset WHERE id = :i",
            i=queue.resets[0].value,
        )
        outcomes.append(
            (
                KIND_PASSWORD_RESET,
                str(queue.resets[1].value),
                await _deliver_reset(settings, session, queue.resets[1], unknown_mailer),
            )
        )
        assert unknown_mailer.sent == [], "an unknown address is mailed nothing"

        # --- reset confirm, then login with the new password ---------------------------------------------
        reset = await client.post(
            RESET_CONFIRM_URL,
            json={"token": tokens["reset token"], "password": new_password},
            headers=headers,
        )
        responses.append(reset)
        assert reset.status_code == 204, reset.text
        relogin = await client.post(
            LOGIN_URL, json={"email": email, "password": new_password}, headers=headers
        )
        responses.append(relogin)
        assert relogin.status_code == 200, relogin.text
        bearer = relogin.json()["access_token"]
        hashes["user password hash after reset"] = await _scalar(
            session, "SELECT password_hash FROM identity_user WHERE id = :i", i=user_id
        )
        assert hashes["user password hash after reset"] != hashes["user password hash"]

        # --- one never confirmed, one whose send blows up (an exception with the secrets in its frame)
        for address in (unconfirmed_email, exploding_email):
            response = await client.post(
                REGISTER_URL, json={"email": address, "password": password}, headers=headers
            )
            responses.append(response)
            assert response.status_code == 202, response.text
        assert len(queue.registrations) == 4
        unconfirmed_mailer = RecordingAccountMailer()
        mailers.append(unconfirmed_mailer)
        outcomes.append(
            (
                KIND_REGISTRATION,
                str(queue.registrations[2].value),
                await _deliver_registration(
                    settings, session, queue.registrations[2], unconfirmed_mailer
                ),
            )
        )
        tokens["unconfirmed token"] = confirmation_token(unconfirmed_mailer)
        deliver_exploding = container._build_registration_delivery(
            settings, session, _ExplodingMailer()
        )
        with pytest.raises(_Boom) as raised:
            await deliver_exploding(queue.registrations[3])
        errors.append("".join(traceback.format_exception(raised.value)))
        sentry_sdk.capture_exception(raised.value)

        # --- the sweep, 25 h on ------------------------------------------------------------------------------
        sweep = container._build_identity_token_sweep(session)
        report = await sweep(SystemClock().now() + timedelta(hours=25))
        await session.commit()
        assert report.pending_registrations == 2, (
            report
        )  # the unconfirmed one and the exploding one

        # --- the delivery lines, through the task's own logging path (the positive control) -------------
        for kind, row_id, outcome in outcomes:
            identity_mail._log_outcome(kind, row_id, outcome, time.monotonic())

        # --- the published task arguments, through the real queue adapter --------------------------------
        celery = _CapturingCelery()
        real_queue = CeleryAccountMailQueue(celery, "mail")
        for registration_id in queue.registrations:
            await real_queue.enqueue_registration(registration_id)
        for reset_id in queue.resets:
            await real_queue.enqueue_password_reset(reset_id)

        # --- delete the account -------------------------------------------------------------------------------
        erased = await client.post(
            DELETE_ACCOUNT_URL,
            json={"password": new_password},
            headers={**headers, "Authorization": f"Bearer {bearer}"},
        )
        responses.append(erased)
        assert erased.status_code == 204, erased.text

    # --- The positive controls: each channel captured something -------------------------------------
    log_text = caplog.text
    assert '"event": "identity.mail_delivered"' in log_text, "the delivery line was not captured"
    assert '"event": "identity.mail_enqueued"' in log_text, "the enqueue line was not captured"
    assert len(publisher.events) >= 1, "no domain event was published: the event channel is blind"
    assert sentry_envelopes, "Sentry captured nothing: the envelope channel is blind"
    assert len(celery.published) == 6, celery.published
    assert fake_llm.calls == [], "AC-55: a route or task of this slice called the LLM"

    all_markers: dict[str, str] = {
        "the address": email,
        "the address's local part": email.split("@", 1)[0],
        "the unknown address": unknown_email,
        "the never-confirmed address": unconfirmed_email,
        "the address whose send blew up": exploding_email,
        "the password": password,
        "the new password": new_password,
        "the wrong-password variant": wrong_password,
        **tokens,
        **hashes,
    }

    def _assert_absent(channel: str, haystack: str, markers: dict[str, str]) -> None:
        for description, marker in markers.items():
            assert marker not in haystack, (
                f"{description} ({marker!r}) appeared in {channel} — AC-54"
            )

    # --- captured log records (stdlib and structlog, every logger) -------------------------------
    _assert_absent("a captured log record", log_text, all_markers)

    # --- Sentry-bound events, and the traceback of the exception raised mid-delivery ---------------
    _assert_absent("a Sentry envelope", "\n".join(sentry_envelopes), all_markers)
    _assert_absent("a traceback.format_exception", "\n".join(errors), all_markers)

    # --- domain-event fields -----------------------------------------------------------------------
    _assert_absent("a domain event's fields", _event_text(publisher.events), all_markers)

    # --- the arguments published to Celery: one UUID string, nothing else ---------------------------
    ids_published = {str(row.value) for row in queue.registrations}
    ids_published |= {str(row.value) for row in queue.resets}
    for name, args, kwargs in celery.published:
        assert args == (), (name, args, kwargs)
        assert set(kwargs) == {"args", "queue", "ignore_result"}, (name, args, kwargs)
        assert kwargs["ignore_result"] is True
        assert kwargs["queue"] == "mail"
        assert len(kwargs["args"]) == 1, kwargs
        (only,) = kwargs["args"]
        assert only in ids_published, kwargs
        assert str(UUID(only)) == only, kwargs
        _assert_absent("a published task argument", repr((args, kwargs)), all_markers)

    # --- every mail the worker half handled: the repr masks the address, the token's repr masks it
    mail_repr = "\n".join(repr(mail) + str(mail) for m in mailers for mail in m.sent)
    assert mail_repr, "no mail was recorded"
    _assert_absent("a mail's repr", mail_repr, all_markers)

    # --- response bodies and headers: no password, token or hash anywhere; an address only in the
    # login success body, whose `user.email` is the contract's one channel for it ---------------------
    never_in_a_response = {key: value for key, value in all_markers.items() if "address" not in key}
    for response in responses:
        wire = response.text + "\n" + "\n".join(f"{k}: {v}" for k, v in response.headers.items())
        _assert_absent(f"the response to {response.request.url}", wire, never_in_a_response)
        if not (response.request.url.path == LOGIN_URL and response.status_code == 200):
            _assert_absent(
                f"the response to {response.request.url}",
                wire,
                {k: v for k, v in all_markers.items() if "address" in k},
            )

    # --- Redis keys ---------------------------------------------------------------------------------
    redis = create_redis(settings.redis_url)
    try:
        keys = [
            key.decode() if isinstance(key, bytes) else key
            async for key in redis.scan_iter(match="*")
        ]
    finally:
        await redis.aclose()
    assert keys, "the flow touched five limiters and left no key: the Redis channel is blind"
    _assert_absent("a Redis key", "\n".join(keys), all_markers)

    # --- after erasure: no identity_* row carries any address the flow used --------------------------
    tables = [
        row[0]
        for row in (
            await session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name LIKE 'identity\\_%' "
                    "ORDER BY table_name"
                )
            )
        ).all()
    ]
    for required in (
        "identity_user",
        "identity_login",
        "identity_pending_registration",
        "identity_password_reset",
    ):
        assert required in tables, f"{required} not found among {tables}: the row scan is blind"
    addresses = {email, unknown_email, unconfirmed_email, exploding_email}
    for table in tables:
        rows = (await session.execute(text(f'SELECT t::text FROM "{table}" t'))).scalars().all()  # noqa: S608 -- names from the catalogue
        for row in rows:
            for address in addresses:
                assert address not in row.lower(), f"{table} still holds {address!r} after erasure"
    assert (
        await _scalar(session, "SELECT count(*) FROM identity_user WHERE id = :i", i=user_id) == 0
    )
