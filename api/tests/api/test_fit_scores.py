"""The HTTP contract of fit scoring (slice 5.1, T29, **red first**): technical plan §4, four routes.

    POST /api/fit-scores            GET /api/fit-scores?base_cv_id=&job_posting_id=         (guest cookie)
    POST /api/me/fit-scores         GET /api/me/fit-scores?base_cv_id=&job_posting_id=      (bearer)

Written from the spec's acceptance criteria and failure contract (AC-1, 2, 5, 8-12, 15, 30-32, 48;
S-10..15, S-22..29), not from the handlers, whose bodies are `NotImplementedError` (T28).

- **The client re-raises nothing.** This module's `client` is built with `raise_app_exceptions=False`,
  so a skeleton's `NotImplementedError` is a real 500 and a red reads `assert 500 == 201`.
- **Both credentials run every shared test** (`who` is parametrized guest / account), which is how
  "guest and account agree" is a test and not a belief. Rows are seeded through the repositories on the
  test's own session; guests are minted through the real upload so the cookie is the real one.
- **A scorer is a fake on `app.state.fit_scorer`; no test calls Gemini.** The spend ledger and the
  repository are replaced through `dependency_overrides` only where a test needs a fault below them
  (a settle that raises, a store the database refuses).
- **Responses that come from a handler** (every 2xx, and every refusal a handler or the domain raises)
  are `no-store` and set no cookie. A credential 401 and a 422 from FastAPI's own validation are checked
  for no cookie only: they are not produced by a handler, and `no-store` on them is not in §4's text.
- **A stored hit is served before the limiter**: after one score is stored, each of Redis down, the
  session at its limit, the IP limit spent and the day's cap full still answers it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.domain.identity.errors import GuestSessionExpired, UserNotFound
from tailorcraft.domain.identity.ownership import Owner, UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.scoring.errors import FitScoringFailed
from tailorcraft.domain.scoring.value_objects import FitScoreFailureReason
from tailorcraft.infrastructure.api.deps import (
    get_fit_score_repository,
    get_fit_scorer,
    get_spend_ledger,
)
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.scoring.fit_score import (
    SqlAlchemyFitScoreRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.claim_race_world import World
from tests.api.me_support import (
    Account,
    bearer,
    error_body,
    error_code,
    mint_guest,
    new_client,
    override_settings,
    register,
    seed_entry,
    seed_user_and_sign_in,
)
from tests.integration.fakes import FakeFitScorer, RecordingSpendLedger
from tests.integration.owners import extracted_cv, extraction_failed_cv, pasted_posting
from tests.integration.scoring.support import THE_GAPS, assessed, extracted_cv_with, usage

GUEST_URL = "/api/fit-scores"
ACCOUNT_URL = "/api/me/fit-scores"
RESPONSE_KEYS = {"id", "score_percent", "gaps", "scored_at", "expires_at"}
FORBIDDEN_WORDS = ("fingerprint", "token", "cost", "micro", "model", "prompt", "usage")


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """The IP limiter and the guest-upload limiter live in Redis, which no rollback reaches."""


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


# ----------------------------------------------------------------------------------------- the rig


@dataclass
class Who:
    """One caller: a guest with a cookie on the client, or an account with a bearer."""

    kind: str
    url: str
    owner: Owner
    headers: dict[str, str]

    @property
    def is_guest(self) -> bool:
        return self.kind == "guest"


@dataclass(frozen=True)
class Inputs:
    cv_id: str
    posting_id: str

    @property
    def body(self) -> dict[str, str]:
        return {"base_cv_id": self.cv_id, "job_posting_id": self.posting_id}


@pytest_asyncio.fixture(params=["guest", "account"])
async def who(
    request: pytest.FixtureRequest, client: AsyncClient, session: AsyncSession, settings: Settings
) -> Who:
    if request.param == "guest":
        guest = await mint_guest(client, session)
        return Who("guest", GUEST_URL, guest, {})
    account = await register(client, settings)
    return Who("account", ACCOUNT_URL, account.owner, account.headers)


async def seed(
    session: AsyncSession, owner: Owner, *, marker: str = "p", extracted: bool = True
) -> Inputs:
    """A CV and a posting owned by `owner`. A different `marker` is a different posting, so a different
    pair over the same CV text."""
    at = SystemClock().now() - timedelta(minutes=5)
    cv = (extracted_cv if extracted else extraction_failed_cv)(owner, at)
    posting = pasted_posting(owner, at, marker=marker)
    ids = Inputs(str(cv.id.value), str(posting.id.value))
    await SqlAlchemyBaseCvRepository(session).add(cv)
    await SqlAlchemyJobPostingRepository(session).add(posting)
    await session.commit()
    return ids


async def another_posting(session: AsyncSession, owner: Owner, cv: str, marker: str) -> Inputs:
    """A second posting for the same CV: a second pair."""
    posting = pasted_posting(owner, SystemClock().now() - timedelta(minutes=5), marker=marker)
    posting_id = str(posting.id.value)
    await SqlAlchemyJobPostingRepository(session).add(posting)
    await session.commit()
    return Inputs(cv, posting_id)


def scorer(app: FastAPI, outcome: object = None) -> FakeFitScorer:
    fake = FakeFitScorer(outcome if outcome is not None else assessed())  # type: ignore[arg-type]
    app.state.fit_scorer = fake
    return fake


def failing(reason: FitScoreFailureReason) -> FitScoringFailed:
    return FitScoringFailed(reason, usage())


async def post(client: AsyncClient, who: Who, inputs: Inputs) -> Response:
    return await client.post(who.url, json=inputs.body, headers=who.headers)


async def get(client: AsyncClient, who: Who, inputs: Inputs) -> Response:
    return await client.get(
        who.url,
        params={"base_cv_id": inputs.cv_id, "job_posting_id": inputs.posting_id},
        headers=who.headers,
    )


def answered(response: Response, status_code: int, code: str | None = None) -> Response:
    """The common shape of an answer a handler produced: its status, `no-store`, no cookie, its code."""
    assert response.status_code == status_code, response.text
    assert response.headers.get("cache-control") == "no-store", dict(response.headers)
    assert "set-cookie" not in response.headers, dict(response.headers)
    if code is not None:
        assert error_code(response) == code
    return response


def ledger_of(app: FastAPI, **faults: Exception) -> RecordingSpendLedger:
    ledger = RecordingSpendLedger(**faults)  # type: ignore[arg-type]
    app.dependency_overrides[get_spend_ledger] = lambda: ledger
    return ledger


class _RepoRefusingStore:
    """The real repository, except that `store` raises: the database or a foreign key says no."""

    def __init__(self, real: SqlAlchemyFitScoreRepository, error: BaseException) -> None:
        self._real = real
        self._error = error

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)

    async def store(self, score: object) -> object:
        raise self._error


def store_raises(app: FastAPI, session: AsyncSession, error: BaseException) -> None:
    app.dependency_overrides[get_fit_score_repository] = lambda: _RepoRefusingStore(
        SqlAlchemyFitScoreRepository(session), error
    )


async def stranger_owns(client: AsyncClient, session: AsyncSession, settings: Settings) -> Inputs:
    """A CV and posting that belong to somebody else (a second account)."""
    _token, user_id = await seed_user_and_sign_in(client, settings)
    return await seed(session, UserOwner(UserId(user_id)))


# ------------------------------------------------------------------- AC-1, AC-2: score, then the same


async def test_a_new_pair_is_201_with_a_whole_score_and_at_most_three_gaps(
    client: AsyncClient, app: FastAPI, session: AsyncSession, who: Who
) -> None:
    fake = scorer(app, assessed(62))
    inputs = await seed(session, who.owner)

    response = answered(await post(client, who, inputs), 201)

    body = response.json()
    assert set(body) == RESPONSE_KEYS
    assert body["score_percent"] == 62
    assert body["gaps"] == list(THE_GAPS)
    assert len(fake.calls) == 1
    datetime.fromisoformat(body["scored_at"])
    if who.is_guest:
        assert body["expires_at"] is not None  # the session's expiry
    else:
        assert body["expires_at"] is None


async def test_a_get_after_the_post_returns_the_stored_score(
    client: AsyncClient, app: FastAPI, session: AsyncSession, who: Who
) -> None:
    scorer(app)
    inputs = await seed(session, who.owner)
    created = answered(await post(client, who, inputs), 201).json()

    read = answered(await get(client, who, inputs), 200)

    assert read.json() == {"fit_score": created}


async def test_the_same_pair_again_is_200_byte_identical_and_makes_no_second_call(
    client: AsyncClient, app: FastAPI, session: AsyncSession, who: Who
) -> None:
    fake = scorer(app)
    inputs = await seed(session, who.owner)
    first = answered(await post(client, who, inputs), 201)

    second = answered(await post(client, who, inputs), 200)

    assert second.content == first.content
    assert len(fake.calls) == 1


async def test_a_get_for_an_unscored_pair_is_200_with_null_and_never_calls_the_scorer(
    client: AsyncClient, app: FastAPI, session: AsyncSession, who: Who
) -> None:
    fake = scorer(app)
    inputs = await seed(session, who.owner)

    response = answered(await get(client, who, inputs), 200)

    assert response.json() == {"fit_score": None}
    assert fake.calls == []


async def test_no_body_carries_a_fingerprint_a_token_count_a_cost_or_a_model(
    client: AsyncClient, app: FastAPI, session: AsyncSession, who: Who
) -> None:
    scorer(app)
    inputs = await seed(session, who.owner)

    created = answered(await post(client, who, inputs), 201).text.lower()
    hit = answered(await post(client, who, inputs), 200).text.lower()
    read = answered(await get(client, who, inputs), 200).text.lower()

    for text_ in (created, hit, read):
        for word in FORBIDDEN_WORDS:
            assert word not in text_, word


async def test_guest_and_account_answer_the_same_shape_for_the_same_score(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    scorer(app)
    guest = Who("guest", GUEST_URL, await mint_guest(client, session), {})
    account = await register(client, settings)
    user = Who("account", ACCOUNT_URL, account.owner, account.headers)
    g = answered(await post(client, guest, await seed(session, guest.owner)), 201).json()
    a = answered(await post(client, user, await seed(session, user.owner)), 201).json()

    assert set(g) == set(a) == RESPONSE_KEYS
    assert (g["score_percent"], g["gaps"]) == (a["score_percent"], a["gaps"])
    assert g["expires_at"] is not None
    assert a["expires_at"] is None


# ------------------------------------------------------------------------------- AC-5: the two tabs


async def test_two_concurrent_posts_for_a_new_pair_store_one_row_and_both_carry_it(
    settings: Settings, engine: AsyncEngine, password_hasher: object, tmp_path: Path
) -> None:
    class _BothInside:
        """Holds each call until the other has arrived, so both really are in `assess` at once."""

        def __init__(self) -> None:
            self.barrier = asyncio.Barrier(2)
            self.calls = 0

        async def assess(self, cv: object, posting: object) -> object:
            self.calls += 1
            await asyncio.wait_for(self.barrier.wait(), timeout=20)
            return assessed(62)

    world = World(settings, engine, password_hasher, tmp_path / "uploads")
    try:
        both = _BothInside()
        world.app.state.fit_scorer = both
        account = await world.account()
        async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
            entry = await seed_entry(
                seeding,
                world.settings,
                account.owner,
                at=SystemClock().now() - timedelta(minutes=5),
            )
        body = {"base_cv_id": str(entry.cv_id.value), "job_posting_id": str(entry.posting_id.value)}
        async with new_client(world.app) as one, new_client(world.app) as two:
            first, second = await asyncio.gather(
                one.post(ACCOUNT_URL, json=body, headers=account.headers),
                two.post(ACCOUNT_URL, json=body, headers=account.headers),
            )
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT count(*) FROM scoring_fit_score WHERE user_id = :u"),
                    {"u": account.user_id.value},
                )
            ).scalar_one()

        assert sorted([first.status_code, second.status_code]) == [200, 201], (
            first.text,
            second.text,
        )
        assert first.json() == second.json()
        assert both.calls == 2
        assert rows == 1
    finally:
        await world.cleanup()
        async with engine.begin() as conn:
            assert "_test" in settings.database_url
            await conn.execute(text("DELETE FROM scoring_daily_spend"))


# ----------------------------------------------------------------- AC-8, AC-12: the stored-score limit


async def test_a_guest_past_the_session_limit_is_409_with_no_call_and_no_reservation(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    override_settings(app, settings, fit_score_max_per_guest_session=1)
    fake = scorer(app)
    ledger = ledger_of(app)
    guest = Who("guest", GUEST_URL, await mint_guest(client, session), {})
    first = await seed(session, guest.owner)
    second = await another_posting(session, guest.owner, first.cv_id, "q")
    answered(await post(client, guest, first), 201)
    reserved_before = len(ledger.reservations)

    refused = answered(await post(client, guest, second), 409, "too_many_fit_scores")

    body = error_body(refused)
    assert body["limit"] == 1
    assert body["retry_not_before"] is None
    assert len(fake.calls) == 1
    assert len(ledger.reservations) == reserved_before


async def test_a_user_past_the_daily_limit_is_409_and_retry_not_before_is_the_oldest_plus_24_hours(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    override_settings(app, settings, fit_score_max_per_user_per_day=1)
    fake = scorer(app)
    account = await register(client, settings)
    user = Who("account", ACCOUNT_URL, account.owner, account.headers)
    first = await seed(session, user.owner)
    second = await another_posting(session, user.owner, first.cv_id, "q")
    stored = answered(await post(client, user, first), 201).json()

    refused = answered(await post(client, user, second), 409, "too_many_fit_scores")

    body = error_body(refused)
    assert body["limit"] == 1
    # Parsed, never compared as text: the offset spelling is not the contract.
    assert datetime.fromisoformat(str(body["retry_not_before"])) == datetime.fromisoformat(
        stored["scored_at"]
    ) + timedelta(hours=24)
    assert len(fake.calls) == 1


async def test_a_failed_attempt_consumes_no_stored_score_limit(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, who: Who
) -> None:
    override_settings(
        app, settings, fit_score_max_per_guest_session=1, fit_score_max_per_user_per_day=1
    )
    inputs = await seed(session, who.owner)
    scorer(app, failing(FitScoreFailureReason.LLM_ERROR))
    answered(await post(client, who, inputs), 503, "scoring_failed")
    scorer(app, assessed())

    retried = answered(await post(client, who, inputs), 201)

    assert retried.json()["score_percent"] == 62


# ------------------------------------------------------------------- AC-9: a stored hit counts nothing


async def test_a_stored_hit_is_served_with_the_session_at_its_limit(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, who: Who
) -> None:
    override_settings(
        app, settings, fit_score_max_per_guest_session=1, fit_score_max_per_user_per_day=1
    )
    fake = scorer(app)
    inputs = await seed(session, who.owner)
    answered(await post(client, who, inputs), 201)

    hit = answered(await post(client, who, inputs), 200)

    assert hit.json()["score_percent"] == 62
    assert len(fake.calls) == 1


async def test_a_stored_hit_is_served_with_redis_down(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, who: Who
) -> None:
    fake = scorer(app)
    inputs = await seed(session, who.owner)
    answered(await post(client, who, inputs), 201)
    override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    hit = answered(await post(client, who, inputs), 200)

    assert hit.json()["score_percent"] == 62
    assert len(fake.calls) == 1


async def test_a_stored_hit_is_served_with_the_days_cap_full(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, who: Who
) -> None:
    fake = scorer(app)
    inputs = await seed(session, who.owner)
    answered(await post(client, who, inputs), 201)
    override_settings(app, settings, fit_score_daily_spend_cap_usd=Decimal(0))

    hit = answered(await post(client, who, inputs), 200)

    assert hit.json()["score_percent"] == 62
    assert len(fake.calls) == 1


async def test_a_stored_hit_is_served_with_the_ip_limit_spent(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, who: Who
) -> None:
    override_settings(app, settings, fit_score_rate_limit_per_ip_per_hour=2)
    scorer(app)
    first = await seed(session, who.owner)
    second = await another_posting(session, who.owner, first.cv_id, "q")
    third = await another_posting(session, who.owner, first.cv_id, "r")
    answered(await post(client, who, first), 201)
    answered(await post(client, who, second), 201)
    answered(await post(client, who, third), 429, "rate_limited")

    hit = answered(await post(client, who, first), 200)

    assert hit.json()["score_percent"] == 62


# ------------------------------------------------------------------ AC-10, AC-11: the IP limit


async def test_failed_attempts_spend_the_ip_limit_and_the_next_is_429_with_retry_after(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, who: Who
) -> None:
    override_settings(app, settings, fit_score_rate_limit_per_ip_per_hour=2)
    fake = scorer(app, failing(FitScoreFailureReason.LLM_ERROR))
    inputs = await seed(session, who.owner)
    answered(await post(client, who, inputs), 503, "scoring_failed")
    answered(await post(client, who, inputs), 503, "scoring_failed")

    refused = answered(await post(client, who, inputs), 429, "rate_limited")

    assert int(refused.headers["retry-after"]) > 0
    assert len(fake.calls) == 2


async def test_redis_down_on_a_new_pair_is_503_rate_limit_unavailable_with_no_call_or_reservation(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, who: Who
) -> None:
    fake = scorer(app)
    ledger = ledger_of(app)
    inputs = await seed(session, who.owner)
    override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    answered(await post(client, who, inputs), 503, "rate_limit_unavailable")

    assert fake.calls == []
    assert ledger.reservations == []


async def test_a_get_is_served_with_redis_down(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, who: Who
) -> None:
    scorer(app)
    inputs = await seed(session, who.owner)
    answered(await post(client, who, inputs), 201)
    override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    read = answered(await get(client, who, inputs), 200)

    assert read.json()["fit_score"]["score_percent"] == 62


# ---------------------------------------------------------------------------- AC-15, S-10: the cap


async def test_a_full_cap_is_503_scoring_busy_with_no_call(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, who: Who
) -> None:
    override_settings(app, settings, fit_score_daily_spend_cap_usd=Decimal(0))
    fake = scorer(app)
    inputs = await seed(session, who.owner)

    answered(await post(client, who, inputs), 503, "scoring_busy")

    assert fake.calls == []


async def test_a_too_long_cv_with_the_cap_full_is_422_inputs_too_large_not_busy(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, who: Who
) -> None:
    # The seeded CV is ~300 characters; a limit of 100 makes it too long.
    override_settings(
        app, settings, fit_score_daily_spend_cap_usd=Decimal(0), llm_max_cv_characters=100
    )
    fake = scorer(app)
    ledger = ledger_of(app)
    inputs = await seed(session, who.owner)

    answered(await post(client, who, inputs), 422, "inputs_too_large")

    assert fake.calls == []
    assert ledger.reservations == []


TOO_LARGE_LINE = "scoring.inputs_too_large"
_CV_MARKER = "Zanzibarquill"


async def test_a_too_long_cv_logs_one_inputs_too_large_line_with_counts_and_no_cv_text(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    who: Who,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The use case refuses before the adapter, so the router is the one place this line comes from.
    override_settings(app, settings, llm_max_cv_characters=100)
    fake = scorer(app)
    at = SystemClock().now() - timedelta(minutes=5)
    cv = extracted_cv_with(who.owner, at, f"{_CV_MARKER} " * 40)
    posting = pasted_posting(who.owner, at, marker="p")
    inputs = Inputs(str(cv.id.value), str(posting.id.value))
    await SqlAlchemyBaseCvRepository(session).add(cv)
    await SqlAlchemyJobPostingRepository(session).add(posting)
    await session.commit()

    with caplog.at_level(logging.DEBUG):
        answered(await post(client, who, inputs), 422, "inputs_too_large")

    assert fake.calls == []
    lines = [r for r in caplog.records if TOO_LARGE_LINE in r.getMessage()]
    assert len(lines) == 1, f"expected one {TOO_LARGE_LINE} line, got {len(lines)}"
    fields = json.loads(lines[0].getMessage())
    # Whitespace collapsed and stripped: 40 words, 39 spaces.
    assert fields["base_cv_character_count"] == len(f"{_CV_MARKER} " * 40) - 1
    assert fields["limit"] == 100
    assert fields["llm_call_skipped"] is True
    for record in caplog.records:
        assert _CV_MARKER not in f"{record.getMessage()} {record.__dict__}"


CAP_LINE = "scoring.spend_cap_reached"


def _cap_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if CAP_LINE in r.getMessage()]


async def test_a_refusal_at_the_cap_logs_one_line_with_the_utc_day_and_nothing_identifying(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    who: Who,
    caplog: pytest.LogCaptureFixture,
) -> None:
    override_settings(app, settings, fit_score_daily_spend_cap_usd=Decimal(0))
    scorer(app)
    inputs = await seed(session, who.owner)
    owner_id = str(
        who.owner.guest_session_id.value if who.is_guest else who.owner.user_id.value  # type: ignore[union-attr]
    )
    before = SystemClock().now().date().isoformat()

    with caplog.at_level(logging.DEBUG):
        answered(await post(client, who, inputs), 503, "scoring_busy")

    after = SystemClock().now().date().isoformat()
    lines = _cap_records(caplog)
    assert len(lines) == 1, f"expected one {CAP_LINE} line, got {len(lines)}"
    everything = f"{lines[0].getMessage()} {lines[0].__dict__}"
    assert before in everything or after in everything
    for forbidden in (owner_id, "127.0.0.1", inputs.cv_id, inputs.posting_id):
        assert forbidden not in everything


async def test_a_stored_hit_with_the_cap_full_logs_no_cap_line(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    who: Who,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scorer(app)
    inputs = await seed(session, who.owner)
    answered(await post(client, who, inputs), 201)
    override_settings(app, settings, fit_score_daily_spend_cap_usd=Decimal(0))

    with caplog.at_level(logging.DEBUG):
        answered(await post(client, who, inputs), 200)

    assert _cap_records(caplog) == []


# ------------------------------------------------- S-1..S-9: every failure the scorer can report


FAILURES = [
    (FitScoreFailureReason.LLM_UNAVAILABLE, 503, "scoring_failed"),
    (FitScoreFailureReason.LLM_TIMED_OUT, 503, "scoring_failed"),
    (FitScoreFailureReason.LLM_OUTPUT_INVALID, 503, "scoring_failed"),
    (FitScoreFailureReason.LLM_ERROR, 503, "scoring_failed"),
    (FitScoreFailureReason.LLM_RATE_LIMITED, 429, "rate_limited"),
    (FitScoreFailureReason.LLM_REFUSED, 422, "scoring_refused"),
    (FitScoreFailureReason.INPUTS_TOO_LARGE, 422, "inputs_too_large"),
]


@pytest.mark.parametrize(
    ("reason", "status_code", "code"), FAILURES, ids=[f[0].value for f in FAILURES]
)
async def test_a_failed_call_answers_its_code_settles_once_and_stores_nothing(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    who: Who,
    reason: FitScoreFailureReason,
    status_code: int,
    code: str,
) -> None:
    scorer(app, failing(reason))
    ledger = ledger_of(app)
    inputs = await seed(session, who.owner)

    answered(await post(client, who, inputs), status_code, code)

    assert len(ledger.reservations) == 1
    assert len(ledger.settlements) == 1
    assert (await get(client, who, inputs)).json() == {"fit_score": None}


async def test_a_providers_429_carries_retry_after_60(
    client: AsyncClient, app: FastAPI, session: AsyncSession, who: Who
) -> None:
    scorer(app, failing(FitScoreFailureReason.LLM_RATE_LIMITED))
    inputs = await seed(session, who.owner)

    refused = answered(await post(client, who, inputs), 429, "rate_limited")

    assert refused.headers["retry-after"] == "60"


# ------------------------------------------------------------- S-14, S-15, S-25..S-27: the store


async def test_a_reservation_whose_commit_fails_is_503_service_unavailable_with_no_call(
    client: AsyncClient, app: FastAPI, session: AsyncSession, who: Who
) -> None:
    fake = scorer(app)
    ledger_of(app, reserve_raises=SQLAlchemyError("boom"))
    inputs = await seed(session, who.owner)

    answered(await post(client, who, inputs), 503, "service_unavailable")

    assert fake.calls == []


async def test_a_database_failing_under_store_is_503_service_unavailable_and_nothing_settles(
    client: AsyncClient, app: FastAPI, session: AsyncSession, who: Who
) -> None:
    scorer(app)
    ledger = ledger_of(app)
    store_raises(app, session, SQLAlchemyError("boom"))
    inputs = await seed(session, who.owner)

    answered(await post(client, who, inputs), 503, "service_unavailable")

    assert ledger.settlements == []  # the reservation stands (S-15)


async def test_a_runtime_error_out_of_store_reaches_the_generic_503_not_a_500(
    client: AsyncClient, app: FastAPI, session: AsyncSession, who: Who
) -> None:
    scorer(app)
    ledger = ledger_of(app)
    store_raises(app, session, RuntimeError("the fit score that beat this one has gone"))
    inputs = await seed(session, who.owner)

    response = await post(client, who, inputs)

    assert response.status_code == 503, response.text
    assert error_code(response) in {"scoring_failed", "service_unavailable"}
    assert ledger.settlements == []


async def test_a_posting_deleted_during_the_call_is_404_and_the_call_is_settled(
    client: AsyncClient, app: FastAPI, session: AsyncSession, who: Who
) -> None:
    scorer(app)
    ledger = ledger_of(app)
    store_raises(app, session, BaseCvNotFound("gone"))
    inputs = await seed(session, who.owner)

    answered(await post(client, who, inputs), 404, "base_cv_not_found")

    assert len(ledger.settlements) == 1


async def test_a_guest_claimed_during_the_call_is_401_guest_session_expired_and_settles(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    scorer(app)
    ledger = ledger_of(app)
    guest = Who("guest", GUEST_URL, await mint_guest(client, session), {})
    inputs = await seed(session, guest.owner)
    store_raises(app, session, GuestSessionExpired("claimed"))

    answered(await post(client, guest, inputs), 401, "guest_session_expired")

    assert len(ledger.settlements) == 1


async def test_an_account_erased_during_the_call_is_401_and_settles(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    scorer(app)
    ledger = ledger_of(app)
    account = await register(client, settings)
    user = Who("account", ACCOUNT_URL, account.owner, account.headers)
    inputs = await seed(session, user.owner)
    store_raises(app, session, UserNotFound("erased"))

    response = await post(client, user, inputs)

    assert response.status_code == 401, response.text
    assert len(ledger.settlements) == 1


# --------------------------------------------- S-16: a settle that fails under another failure


class SettleBroke(Exception):
    """A distinctive type name to look for in the log, and a message that must never appear."""


_LEAK = "LEAK-MARKER-9f3c1a"


def _log_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


async def test_a_failed_settle_under_a_failed_call_logs_the_cause_type_and_still_answers_the_failure(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    who: Who,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scorer(app, failing(FitScoreFailureReason.LLM_REFUSED))
    ledger_of(app, settle_raises=SettleBroke(_LEAK))
    inputs = await seed(session, who.owner)

    with caplog.at_level(logging.DEBUG):
        answered(await post(client, who, inputs), 422, "scoring_refused")

    logged = _log_text(caplog)
    assert "SettleBroke" in logged
    assert _LEAK not in logged
    assert all(r.exc_info is None or _LEAK not in str(r.exc_info[1]) for r in caplog.records), (
        "the settle's message reached a log record through exc_info"
    )


async def test_a_failed_settle_under_a_refused_store_logs_the_cause_type_and_still_answers_the_refusal(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    who: Who,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scorer(app)
    ledger_of(app, settle_raises=SettleBroke(_LEAK))
    store_raises(app, session, BaseCvNotFound("gone"))
    inputs = await seed(session, who.owner)

    with caplog.at_level(logging.DEBUG):
        answered(await post(client, who, inputs), 404, "base_cv_not_found")

    logged = _log_text(caplog)
    assert "SettleBroke" in logged
    assert _LEAK not in logged


# ------------------------------------------------------- S-22, S-23, S-24, AC-31: what is not yours


@pytest.mark.parametrize("method", ["post", "get"])
async def test_a_missing_cv_is_404_base_cv_not_found(
    client: AsyncClient, app: FastAPI, session: AsyncSession, who: Who, method: str
) -> None:
    scorer(app)
    inputs = await seed(session, who.owner)
    absent = Inputs(str(uuid4()), inputs.posting_id)

    response = await (post if method == "post" else get)(client, who, absent)

    answered(response, 404, "base_cv_not_found")


@pytest.mark.parametrize("method", ["post", "get"])
async def test_a_missing_posting_is_404_job_posting_not_found(
    client: AsyncClient, app: FastAPI, session: AsyncSession, who: Who, method: str
) -> None:
    scorer(app)
    inputs = await seed(session, who.owner)
    absent = Inputs(inputs.cv_id, str(uuid4()))

    response = await (post if method == "post" else get)(client, who, absent)

    answered(response, 404, "job_posting_not_found")


@pytest.mark.parametrize("method", ["post", "get"])
async def test_someone_elses_cv_and_posting_answer_byte_identically_to_missing_ones(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    who: Who,
    method: str,
) -> None:
    fake = scorer(app)
    mine = await seed(session, who.owner)
    theirs = await stranger_owns(client, session, settings)
    call = post if method == "post" else get

    foreign_cv = answered(await call(client, who, Inputs(theirs.cv_id, mine.posting_id)), 404)
    missing_cv = answered(await call(client, who, Inputs(str(uuid4()), mine.posting_id)), 404)
    foreign_posting = answered(await call(client, who, Inputs(mine.cv_id, theirs.posting_id)), 404)
    missing_posting = answered(await call(client, who, Inputs(mine.cv_id, str(uuid4()))), 404)

    assert foreign_cv.content == missing_cv.content
    assert foreign_posting.content == missing_posting.content
    assert error_code(foreign_cv) == "base_cv_not_found"
    assert error_code(foreign_posting) == "job_posting_not_found"
    assert fake.calls == []


async def test_a_cv_with_no_extracted_text_is_409_on_post_and_200_null_on_get(
    client: AsyncClient, app: FastAPI, session: AsyncSession, who: Who
) -> None:
    fake = scorer(app)
    inputs = await seed(session, who.owner, extracted=False)

    answered(await post(client, who, inputs), 409, "base_cv_not_extracted")
    read = answered(await get(client, who, inputs), 200)

    assert read.json() == {"fit_score": None}
    assert fake.calls == []


# ------------------------------------------------------------------ S-29: an invalid body, no limiter


BAD_BODIES = {
    "not_a_uuid": {"base_cv_id": "nope", "job_posting_id": str(uuid4())},
    "missing_field": {"base_cv_id": str(uuid4())},
    "stray_field": {"base_cv_id": str(uuid4()), "job_posting_id": str(uuid4()), "model": "x"},
}


@pytest.mark.parametrize("name", BAD_BODIES)
async def test_an_invalid_body_is_422_validation_error_before_redis_or_the_scorer(
    client: AsyncClient, app: FastAPI, settings: Settings, who: Who, name: str
) -> None:
    fake = scorer(app)
    override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await client.post(who.url, json=BAD_BODIES[name], headers=who.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"
    assert "set-cookie" not in response.headers
    assert fake.calls == []


async def test_a_body_that_is_not_json_is_422(client: AsyncClient, app: FastAPI, who: Who) -> None:
    scorer(app)

    response = await client.post(
        who.url, content=b"{not json", headers={**who.headers, "content-type": "application/json"}
    )

    assert response.status_code == 422, response.text


async def test_an_oversize_body_is_413_before_redis_or_the_scorer(
    client: AsyncClient, app: FastAPI, settings: Settings, who: Who
) -> None:
    fake = scorer(app)
    override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")
    huge = {
        "base_cv_id": "a" * (settings.json_request_max_bytes + 1),
        "job_posting_id": str(uuid4()),
    }

    response = await client.post(who.url, json=huge, headers=who.headers)

    assert response.status_code == 413, response.text
    assert fake.calls == []


@pytest.mark.parametrize("missing", ["base_cv_id", "job_posting_id"])
async def test_a_get_without_both_query_ids_is_422(
    client: AsyncClient, app: FastAPI, session: AsyncSession, who: Who, missing: str
) -> None:
    scorer(app)
    inputs = await seed(session, who.owner)
    params = {"base_cv_id": inputs.cv_id, "job_posting_id": inputs.posting_id}
    del params[missing]

    response = await client.get(who.url, params=params, headers=who.headers)

    assert response.status_code == 422, response.text
    assert "set-cookie" not in response.headers


# ------------------------------------------------------------- AC-30, S-28: one credential per route


@pytest.mark.parametrize("method", ["post", "get"])
async def test_a_guest_route_without_a_cookie_is_401_guest_session_expired_and_mints_none(
    client: AsyncClient, app: FastAPI, method: str
) -> None:
    fake = scorer(app)
    inputs = Inputs(str(uuid4()), str(uuid4()))

    if method == "post":
        response = await client.post(GUEST_URL, json=inputs.body)
    else:
        response = await client.get(
            GUEST_URL, params={"base_cv_id": inputs.cv_id, "job_posting_id": inputs.posting_id}
        )

    assert response.status_code == 401, response.text
    assert error_code(response) == "guest_session_expired"
    assert "set-cookie" not in response.headers
    assert fake.calls == []


async def test_a_guest_route_ignores_a_bearer_with_no_cookie(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    scorer(app)
    account = await register(client, settings)
    client.cookies.clear()

    response = await client.post(
        GUEST_URL,
        json=Inputs(str(uuid4()), str(uuid4())).body,
        headers=account.headers,
    )

    assert response.status_code == 401, response.text
    assert error_code(response) == "guest_session_expired"


@pytest.mark.parametrize("method", ["post", "get"])
async def test_an_account_route_without_a_bearer_is_401_with_a_challenge(
    client: AsyncClient, app: FastAPI, method: str
) -> None:
    scorer(app)
    inputs = Inputs(str(uuid4()), str(uuid4()))

    if method == "post":
        response = await client.post(ACCOUNT_URL, json=inputs.body)
    else:
        response = await client.get(
            ACCOUNT_URL, params={"base_cv_id": inputs.cv_id, "job_posting_id": inputs.posting_id}
        )

    assert response.status_code == 401, response.text
    assert error_code(response) == "invalid_access_token"
    assert "bearer" in response.headers["www-authenticate"].lower()


async def test_an_account_route_ignores_a_live_guest_cookie(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    fake = scorer(app)
    guest = await mint_guest(client, session)
    inputs = await seed(session, guest)

    response = await client.post(ACCOUNT_URL, json=inputs.body)  # the cookie rides along

    assert response.status_code == 401, response.text
    assert error_code(response) == "invalid_access_token"
    assert fake.calls == []


@pytest.mark.parametrize("method", ["post", "get"])
async def test_a_disabled_account_is_403_account_disabled(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    method: str,
) -> None:
    fake = scorer(app)
    account: Account = await register(client, settings)
    inputs = await seed(session, account.owner)
    await session.execute(
        text("UPDATE identity_user SET disabled_at = now() WHERE id = :i"),
        {"i": account.user_id.value},
    )
    await session.commit()

    if method == "post":
        response = await client.post(ACCOUNT_URL, json=inputs.body, headers=bearer(account.token))
    else:
        response = await client.get(
            ACCOUNT_URL,
            params={"base_cv_id": inputs.cv_id, "job_posting_id": inputs.posting_id},
            headers=bearer(account.token),
        )

    assert response.status_code == 403, response.text
    assert error_code(response) == "account_disabled"
    assert fake.calls == []


# --------------------------------------------------------------------------- AC-32, AC-48: the graph


def _routes(app: FastAPI) -> dict[tuple[str, str], APIRoute]:
    found: dict[tuple[str, str], APIRoute] = {}

    def walk(routes: object) -> None:
        for route in routes:  # type: ignore[attr-defined]
            if isinstance(route, APIRoute):
                for method in route.methods or ():
                    found[(method, route.path)] = route
            elif hasattr(route, "original_router"):
                walk(route.original_router.routes)
            elif hasattr(route, "routes"):
                walk(route.routes)

    walk(app.routes)
    return found


def _reaches(dependant: Dependant) -> set[object]:
    calls: set[object] = set()
    stack = [dependant]
    while stack:
        for sub in stack.pop().dependencies:
            calls.add(sub.call)
            stack.append(sub)
    return calls


def test_both_post_routes_reach_the_scorer_and_neither_get_does(app: FastAPI) -> None:
    routes = _routes(app)
    posts = [("POST", GUEST_URL), ("POST", ACCOUNT_URL)]
    gets = [("GET", GUEST_URL), ("GET", ACCOUNT_URL)]

    assert all(key in routes for key in posts + gets), "the walker must find all four routes"
    for key in posts:
        assert get_fit_scorer in _reaches(routes[key].dependant), key
    for key in gets:
        assert get_fit_scorer not in _reaches(routes[key].dependant), key
