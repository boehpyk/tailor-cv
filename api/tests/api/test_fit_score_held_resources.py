"""What a fit-score request holds while the model thinks (slice 5.1, T32; plan §0.2 and §7).

Test-after: the shape of the held resources is discovered against SQLAlchemy and uvicorn. Every wait
is under `asyncio.wait_for`, so a regression fails the test instead of hanging the suite. A fake
scorer throughout; no test calls Gemini.

- **AC-19** `pool.checkedout() == 0` and no backend `idle in transaction` / `active` while `assess` is
  suspended. Mutation: the `commit()` removed from `CommittingSpendLedger.try_reserve`.
- **AC-20** a real uvicorn and a real socket close: the handler runs on, stores, settles. An ASGI
  transport cannot disconnect, which is why this is the only test here with a server.
- **AC-21** `task.cancel()` raises `CancelledError`; no row, the reservation stands, the connection is
  back.
- **AC-22** the loop's turnaround while a 250,000-character CV is hashed (`GET`, no model involved) and
  a 30,000-character posting is parsed (`GeminiFitScorer.assess` over its `generate` seam). Gated on the
  p95 turnaround (AC-43's statistic: the blocked work here blocks from its first instant).
- **The 404 pin**: `GetBaseCv` chains its not-found, so `__cause__` read on the outer `except` would log
  a settle that never ran. Mutation: the read moved there.

MUTATION RECORD (2026-10-31; each restored with `git checkout`, `git status` clean after):

| mutation | healthy | mutated |
|---|---|---|
| commit removed from `CommittingSpendLedger.try_reserve` (AC-19/20/21) | 3 green | `assert 1 == 0` (checkedout), `assert 0 > 0` x2 (no reservation committed); 3 red |
| `to_thread` dropped from `ResolveScoringPair` | thread test green | `loop thread not in [loop thread]`, red |
| `to_thread` dropped from `GeminiFitScorer` parse | thread test green | the same assertion, red |
| `__cause__` read moved to the outer `except` | 404 pin green | `scoring.settle_failed` logged for a not-owned 404 (`BaseCvNotOwnedByUser`), red |

**AC-22's turnaround tests are NOT mutation-sensitive, and are kept as smoke only.** p95 turnaround,
250k-character CV read: healthy 5.9 to 10.2 ms (max ~210 to 230 ms, one outlier), `to_thread` dropped
10.5 to 11.5 ms. 30k-character parse: healthy 1.2 to 1.5 ms, dropped 0.8 ms. Hashing costs ~4 ms and
parsing under 1 ms on this box, so neither can move a turnaround statistic past its noise; the
deterministic thread-identity test is the one that guards the regression. Without a worker yield the
parse-dropped mutation also never suspended and starved the sampler (a 3 minute hang), hence the
`sleep(0.001)` in `_measure` and its `wait_for`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import statistics
import threading
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
import uvicorn
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.intake.value_objects import ExtractedText
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingId, JobPostingText
from tailorcraft.domain.scoring.value_objects import CvForScoring, PostingForScoring
from tailorcraft.infrastructure.llm.fit_score_parsing import (
    KEY_GAPS,
    KEY_POSTING_QUOTE,
    KEY_REQUIREMENT,
    KEY_SCORE,
)
from tailorcraft.infrastructure.llm.fit_scorer import GeminiFitScorer
from tailorcraft.infrastructure.llm.gemini import raw_completion_from_response
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.claim_race_world import World
from tests.api.me_support import Account, error_code, new_client
from tests.integration.adapters.loop_liveness import (
    hammer_health_live_until_floor,
)
from tests.integration.fakes import FakeFitScorer
from tests.integration.owners import uploaded_cv
from tests.integration.scoring.support import assessed

pytestmark = pytest.mark.slow

URL = "/api/me/fit-scores"
_KEY = "not-a-real-secret-fake-scorer-only"
WAIT = 15.0


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """The IP limiter lives in Redis, which no rollback reaches."""


@pytest_asyncio.fixture
async def world(
    settings: Settings, engine: AsyncEngine, password_hasher: object, tmp_path: Path
) -> AsyncIterator[World]:
    w = World(settings, engine, password_hasher, tmp_path / "uploads")
    w.app.state.fit_scorer = FakeFitScorer(assessed())  # a test needing another replaces it
    try:
        yield w
    finally:
        await w.cleanup()
        async with engine.begin() as conn:
            assert "_test" in settings.database_url
            await conn.execute(text("DELETE FROM scoring_daily_spend"))


class GatedScorer:
    """A scorer that says when it started and waits for `release`. `cancelled` and `finished` record
    how the call ended, so "the handler ran on" is read off the scorer and not inferred."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.finished = False

    async def assess(self, cv: object, posting: object) -> object:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished = True
        return assessed(62)


async def seed_pair(
    engine: AsyncEngine, owner: Owner, *, cv_characters: int = 300, posting_words: int = 40
) -> dict[str, str]:
    at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
    cv = uploaded_cv(owner, at)
    cv.mark_extracted(ExtractedText(("word " * (cv_characters // 5 + 1))[:cv_characters]), at)
    cv.release_events()
    posting = JobPosting.from_pasted_text(
        id=JobPostingId(value=uuid4()),
        owner=owner,
        text=JobPostingText("post " * posting_words),
        created_at=at,
    )
    posting.release_events()
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        await SqlAlchemyBaseCvRepository(s).add(cv)
        await SqlAlchemyJobPostingRepository(s).add(posting)
        await s.commit()
    return {"base_cv_id": str(cv.id.value), "job_posting_id": str(posting.id.value)}


async def spent(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT coalesce(sum(spent_microusd), 0) FROM scoring_daily_spend")
                )
            ).scalar_one()
        )


async def stored_rows(engine: AsyncEngine, account: Account) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM scoring_fit_score WHERE user_id = :u"),
                    {"u": account.user_id.value},
                )
            ).scalar_one()
        )


async def backend_states(settings: Settings) -> dict[str, int]:
    """Other backends of this database by state, read on a connection outside the pool under test."""
    probe = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with probe.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT coalesce(state, 'none'), count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid() GROUP BY 1"
                )
            )
            return {state: int(n) for state, n in rows}
    finally:
        await probe.dispose()


async def until(condition: object, what: str) -> None:
    async def poll() -> None:
        while not await condition():  # type: ignore[operator]  # noqa: ASYNC110 - polling a socket flag
            await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(poll(), WAIT)
    except TimeoutError:
        pytest.fail(f"never became true: {what}")


# ------------------------------------------------------------------------------------------ AC-19


async def test_no_connection_or_transaction_is_held_while_the_scorer_is_suspended(
    world: World, engine: AsyncEngine
) -> None:
    """Mutation (commit removed from `CommittingSpendLedger.try_reserve`): `checkedout()` 0 -> 1 and
    the backend reads `idle in transaction`; both assertions red."""
    scorer = GatedScorer()
    world.app.state.fit_scorer = scorer
    account = await world.account()
    body = await seed_pair(engine, account.owner)
    async with new_client(world.app) as client:
        request = asyncio.ensure_future(client.post(URL, json=body, headers=account.headers))
        try:
            await asyncio.wait_for(scorer.started.wait(), WAIT)

            checked_out = engine.pool.checkedout()  # type: ignore[attr-defined]
            states = await backend_states(world.settings)

            assert checked_out == 0
            assert states.get("idle in transaction", 0) == 0, states
            assert states.get("active", 0) == 0, states
        finally:
            scorer.release.set()
            response = await asyncio.wait_for(request, WAIT)
    assert response.status_code == 201, response.text


# ------------------------------------------------------------------------------------------ AC-20


async def _serve(app: object) -> tuple[uvicorn.Server, asyncio.Task[None], int]:
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning")  # type: ignore[arg-type]
    )
    task = asyncio.ensure_future(server.serve())

    async def up() -> bool:
        return server.started

    await until(up, "uvicorn started")
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, int(port)


async def test_a_client_that_hangs_up_during_the_call_still_gets_its_score_stored_and_settled(
    world: World, engine: AsyncEngine
) -> None:
    """Real uvicorn, real socket close. The handler runs on: the scorer is never cancelled, the row is
    stored and the reservation is replaced by the (smaller) actual cost. Measured against the stack
    the plan names (uvicorn 0.52.4, pure-ASGI middleware): it holds."""
    scorer = GatedScorer()
    world.app.state.fit_scorer = scorer
    account = await world.account()
    pair = await seed_pair(engine, account.owner)
    server, serving, port = await _serve(world.app)
    payload = json.dumps(pair).encode()
    head = (
        f"POST {URL} HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {account.token}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), WAIT)
        writer.write(head + payload)
        await writer.drain()
        await asyncio.wait_for(scorer.started.wait(), WAIT)
        reserved = await spent(engine)
        assert reserved > 0

        writer.close()  # the tunnel: the client is gone
        with suppress(ConnectionError):
            await asyncio.wait_for(writer.wait_closed(), WAIT)
        await asyncio.sleep(0.2)  # let uvicorn see the close before the scorer answers
        scorer.release.set()

        async def stored() -> bool:
            return await stored_rows(engine, account) == 1

        await until(stored, "the score is stored after the client hung up")
        settled = await spent(engine)
        assert scorer.cancelled is False
        assert scorer.finished is True
        assert 0 < settled < reserved, (settled, reserved)
    finally:
        scorer.release.set()
        server.should_exit = True
        await asyncio.wait_for(serving, WAIT)


# ------------------------------------------------------------------------------------------ AC-21


async def test_cancelling_the_request_raises_cancelled_error_leaves_the_reservation_and_frees_the_connection(
    world: World, engine: AsyncEngine
) -> None:
    scorer = GatedScorer()
    world.app.state.fit_scorer = scorer
    account = await world.account()
    pair = await seed_pair(engine, account.owner)
    async with new_client(world.app) as client:
        request = asyncio.ensure_future(client.post(URL, json=pair, headers=account.headers))
        await asyncio.wait_for(scorer.started.wait(), WAIT)
        reserved = await spent(engine)

        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, WAIT)

    assert scorer.cancelled is True
    assert reserved > 0
    assert await spent(engine) == reserved  # the reservation stands as spend (AC-17)
    assert await stored_rows(engine, account) == 0
    assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    states = await backend_states(world.settings)
    assert states.get("idle in transaction", 0) == 0, states


# ------------------------------------------------------------------------------------------ AC-22

_FLOOR = 100
_P95_BUDGET_SECONDS = 0.050


def _valid_response(quote: str) -> object:
    from google.genai import types as genai_types

    answer = json.dumps(
        {KEY_SCORE: 60, KEY_GAPS: [{KEY_REQUIREMENT: "Kubernetes", KEY_POSTING_QUOTE: quote}]}
    )
    return genai_types.GenerateContentResponse(
        candidates=[
            genai_types.Candidate(
                content=genai_types.Content(role="model", parts=[genai_types.Part(text=answer)]),
                finish_reason=genai_types.FinishReason.STOP,
            )
        ],
        usage_metadata=genai_types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100, candidates_token_count=10
        ),
    )


async def _measure(client: AsyncClient, work: object, workers: int) -> tuple[float, float, float]:
    """(p95 turnaround, same-run baseline, max turnaround), the sampler ending
    the batch (`loop_liveness.py`, lessons one and three)."""
    idle = asyncio.Event()
    baseline = statistics.median(
        await hammer_health_live_until_floor(client, enough=idle, floor=50)
    )
    enough = asyncio.Event()

    async def worker() -> None:
        while not enough.is_set():
            await work()  # type: ignore[operator]
            await asyncio.sleep(
                0.001
            )  # a mutated, never-suspending `work` must not starve the sampler

    tasks = [asyncio.ensure_future(worker()) for _ in range(workers)]
    try:
        turnarounds = await asyncio.wait_for(
            hammer_health_live_until_floor(client, enough=enough, floor=_FLOOR), 60
        )
    finally:
        enough.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 60)
    ordered = sorted(turnarounds)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
    return p95, baseline, max(turnarounds)


async def test_the_loop_stays_free_while_a_250k_character_cv_is_hashed(
    world: World, engine: AsyncEngine
) -> None:
    """`ResolveScoringPair`'s `to_thread` (reads hit it on every `GET`). Mutation (the `to_thread`
    dropped): see the record in the module's report; healthy numbers are in the assertion message."""
    account = await world.account()
    pair = await seed_pair(engine, account.owner, cv_characters=250_000, posting_words=5_000)
    async with new_client(world.app) as probe, new_client(world.app) as worker_client:

        async def read() -> None:
            response = await worker_client.get(URL, params=pair, headers=account.headers)
            assert response.status_code == 200, response.text

        p95, baseline, worst = await _measure(probe, read, workers=4)

    assert p95 < _P95_BUDGET_SECONDS, (
        f"p95={p95 * 1000:.2f}ms baseline={baseline * 1000:.2f}ms max={worst * 1000:.1f}ms"
    )


async def test_the_loop_stays_free_while_a_30k_character_posting_is_parsed(
    world: World,
) -> None:
    """`GeminiFitScorer`'s `to_thread(parse_fit_score_response, ...)`, over the real `generate` seam."""
    quote = "five years of Kubernetes experience in production"
    posting_text = ("filler words " * 2_300)[:29_000] + quote
    posting = PostingForScoring(posting_text)
    response = _valid_response(quote)

    async def generate(prompt: str) -> object:
        return raw_completion_from_response(response)  # type: ignore[arg-type]

    scorer = GeminiFitScorer(
        world.settings.model_copy(
            update={
                "gemini_api_key": _KEY,
                "fit_score_retry_backoff_seconds": 0.0,
            }
        ),
        generate=generate,  # type: ignore[arg-type]
    )
    cv = CvForScoring("Platform engineer. " * 100)
    async with new_client(world.app) as probe:

        async def score() -> None:
            await scorer.assess(cv, posting)

        p95, baseline, worst = await _measure(probe, score, workers=4)

    assert p95 < _P95_BUDGET_SECONDS, (
        f"p95={p95 * 1000:.2f}ms baseline={baseline * 1000:.2f}ms max={worst * 1000:.1f}ms"
    )


def _records_thread(monkeypatch: pytest.MonkeyPatch, module: object, name: str) -> list[int]:
    """Wrap `module.name` so each call notes the thread it ran on."""
    real = getattr(module, name)
    seen: list[int] = []

    def wrapper(*args: object, **kwargs: object) -> object:
        seen.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)
    return seen


async def test_hashing_and_parsing_run_off_the_event_loop_thread(
    world: World, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deterministic half of AC-22. Healthy blocking is only ~4 ms (hashing 250k characters) and
    under 1 ms (parsing 30k), below any turnaround statistic's noise, so the turnaround tests above
    cannot see either mutation (recorded in the report). Which thread ran the work can: mutation (the
    `to_thread` dropped from either) puts the call on the loop's thread and this goes red."""
    from tailorcraft.application.scoring import resolve_scoring_pair as resolve_module
    from tailorcraft.infrastructure.llm import fit_scorer as scorer_module

    built = _records_thread(monkeypatch, resolve_module, "_build_pair")
    parsed = _records_thread(monkeypatch, scorer_module, "parse_fit_score_response")
    account = await world.account()
    pair = await seed_pair(engine, account.owner)
    quote = "five years of Kubernetes experience in production"
    response = _valid_response(quote)

    async def generate(prompt: str) -> object:
        return raw_completion_from_response(response)  # type: ignore[arg-type]

    scorer = GeminiFitScorer(
        world.settings.model_copy(update={"gemini_api_key": _KEY}),
        generate=generate,  # type: ignore[arg-type]
    )
    async with new_client(world.app) as client:
        got = await client.get(URL, params=pair, headers=account.headers)
    assert got.status_code == 200, got.text
    await scorer.assess(CvForScoring("Platform engineer. " * 100), PostingForScoring("x " + quote))

    loop_thread = threading.get_ident()
    assert built, "the pair was never built"
    assert parsed, "the answer was never parsed"
    assert loop_thread not in built
    assert loop_thread not in parsed


# --------------------------------------------------------------------------------- the 404 pin


async def test_an_ordinary_404_chained_from_a_cause_is_not_logged_as_a_failed_settle(
    world: World, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """`GetBaseCv` chains `BaseCvNotFound` on purpose. Mutation (the `__cause__` read moved to the outer
    `except DomainError`): the line is logged for a 404 and this goes red."""
    account = await world.account()
    stranger = await world.account()
    pair = await seed_pair(
        engine, stranger.owner
    )  # someone else's CV: 404, chained from the ownership miss
    async with new_client(world.app) as client:
        with caplog.at_level(logging.DEBUG):
            response = await client.post(URL, json=pair, headers=account.headers)

    assert response.status_code == 404, response.text
    assert error_code(response) == "base_cv_not_found"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "scoring.settle_failed" not in logged
