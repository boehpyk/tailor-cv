"""FastAPI dependency wiring — the composition root.

This is the one place that knows which adapter satisfies which port. A route declares what it needs;
this module decides what it gets. Keeping that decision in a single file is what makes "is every
port bound?" a question with one place to look, rather than an archaeology exercise across the
codebase.

Long-lived resources — the engine, the session factory — are created once at startup and stashed on
`app.state`, not rebuilt per request. An engine constructed per request means a connection pool
constructed per request, which is a pool of one that never gets reused. Redis is the one exception,
built fresh from `SettingsDep` on every request (`get_redis`) — the same pattern
`infrastructure/health/probes.py::probe_redis` already uses — because a lazily-connecting client is
cheap to construct and this keeps a test's `Settings.redis_url` override (`_override_settings` in
`tests/api/test_intake.py`) effective without needing a second, `app.state`-shaped override path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tailorcraft.application.identity.start_guest_session import StartGuestSession
from tailorcraft.application.intake.get_base_cv import GetBaseCvForSession
from tailorcraft.application.intake.list_base_cvs import ListBaseCvsForSession
from tailorcraft.application.intake.upload_base_cv import UploadBaseCv
from tailorcraft.application.posting.capture_job_posting import CaptureJobPosting
from tailorcraft.application.posting.get_job_posting import GetJobPostingForSession
from tailorcraft.application.posting.list_job_postings import ListJobPostingsForSession
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ports import GuestSessionRepository
from tailorcraft.domain.intake.ports import BaseCvRepository, CvTextExtractorPort
from tailorcraft.domain.posting.ports import JobPostingFetcherPort, JobPostingRepository
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.shared.files import FileStorePort
from tailorcraft.infrastructure.api.errors import GUEST_SESSION_EXPIRED_DETAIL
from tailorcraft.infrastructure.api.guest_session import (
    hash_guest_token,
    mint_guest_token,
    read_guest_token,
    set_guest_cookie,
)
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.events.logging_publisher import LoggingEventPublisher
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.intake.extraction import PypdfDocxTextExtractor
from tailorcraft.infrastructure.posting.address_policy import TargetAddressPolicy
from tailorcraft.infrastructure.posting.fetching import HttpxTrafilaturaFetcher
from tailorcraft.infrastructure.rate_limit import RedisFixedWindowRateLimiter
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.settings import Settings


def get_app_settings(request: Request) -> Settings:
    """Read off `app.state`, the same way `get_engine` does immediately below — not the
    module-level, `lru_cache`d `get_settings()`.

    `get_settings()` is cached for the life of the *process*: the first call's result is what every
    later call gets back, forever, regardless of what `Settings` object a particular `FastAPI` app
    was actually built with. `create_app`'s lifespan (production) and every test's `app` fixture
    (`tests/conftest.py`, and the second, hand-wired app `test_cookie_is_secure_in_production`
    builds directly) both already stash the *real* settings for that app on `app.state.settings` —
    reading it from there is what makes a request see the settings its own app was configured with,
    including a settings object built later than the first ever call to `get_settings()` in this
    process (a `model_copy(update=...)`, for instance) that never touches that cache at all.
    """
    settings: Settings = request.app.state.settings
    return settings


def get_engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, committed on success and rolled back on any exception.

    The unit of work is a property of the *boundary*, not of the use case — which is why this lives
    here and why no `AsyncSession` is ever passed into the application layer.
    """
    factory = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


def get_clock() -> Clock:
    """The only source of "now" the application layer may use."""
    return SystemClock()


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
EngineDep = Annotated[AsyncEngine, Depends(get_engine)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ClockDep = Annotated[Clock, Depends(get_clock)]


# ---------------------------------------------------------------------------------------------
# intake / identity — T27: bind every port this slice's use cases need.
# ---------------------------------------------------------------------------------------------


def get_file_store(settings: SettingsDep) -> FileStorePort:
    """Binds `FileStorePort` -> `LocalFileStore` (ADR-0011)."""
    return LocalFileStore(settings.upload_dir)


FileStoreDep = Annotated[FileStorePort, Depends(get_file_store)]


def get_cv_text_extractor(settings: SettingsDep) -> CvTextExtractorPort:
    """Binds `CvTextExtractorPort` -> `PypdfDocxTextExtractor` (ADR-0009)."""
    return PypdfDocxTextExtractor(settings.extraction_timeout_seconds, settings.max_cv_pages)


CvTextExtractorDep = Annotated[CvTextExtractorPort, Depends(get_cv_text_extractor)]


def get_event_publisher() -> EventPublisherPort:
    """Binds `EventPublisherPort` -> `LoggingEventPublisher` (OQ-8)."""
    return LoggingEventPublisher()


EventPublisherDep = Annotated[EventPublisherPort, Depends(get_event_publisher)]


def get_base_cv_repository(session: SessionDep) -> BaseCvRepository:
    """Binds `BaseCvRepository` -> `SqlAlchemyBaseCvRepository` (ADR-0007).

    The import is deferred to call time, not hoisted to this module's top level, on purpose:
    `SqlAlchemyBaseCvRepository`'s own module reads `BaseCv._id` etc. as a plain attribute access at
    *import* time (to build its `InstrumentedAttribute` casts — see that module's docstring), which
    only exists once `configure_mappings()` has run. `deps.py` is the very first thing
    `tests/conftest.py` imports, well before its session-scoped `_mappings` fixture ever executes —
    an eager import here would fail at collection time, before a single test runs. By request time
    (when this provider is actually called), mappings are always configured, in production by
    `main.py`'s lifespan and in tests by that fixture.
    """
    from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
        SqlAlchemyBaseCvRepository,
    )

    return SqlAlchemyBaseCvRepository(session)


BaseCvRepositoryDep = Annotated[BaseCvRepository, Depends(get_base_cv_repository)]


def get_guest_session_repository(session: SessionDep) -> GuestSessionRepository:
    """Binds `GuestSessionRepository` -> `SqlAlchemyGuestSessionRepository` (ADR-0007).

    Deferred import — see `get_base_cv_repository`'s docstring for why.
    """
    from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
        SqlAlchemyGuestSessionRepository,
    )

    return SqlAlchemyGuestSessionRepository(session)


GuestSessionRepositoryDep = Annotated[GuestSessionRepository, Depends(get_guest_session_repository)]


def get_redis(settings: SettingsDep) -> aioredis.Redis:
    """A fresh, lazily-connecting client per request — see the module docstring for why this one
    resource is not stashed on `app.state` the way the engine is."""
    return create_redis(settings.redis_url)


RedisDep = Annotated[aioredis.Redis, Depends(get_redis)]


def get_rate_limiter(redis: RedisDep) -> RedisFixedWindowRateLimiter:
    """Not behind a domain port — rate limiting is an HTTP-boundary concern
    (`infrastructure/rate_limit.py`'s own docstring), so there is no `RateLimiterPort` to bind."""
    return RedisFixedWindowRateLimiter(redis, namespace="intake:upload")


RateLimiterDep = Annotated[RedisFixedWindowRateLimiter, Depends(get_rate_limiter)]


def get_upload_base_cv(
    cvs: BaseCvRepositoryDep,
    sessions: GuestSessionRepositoryDep,
    files: FileStoreDep,
    extractor: CvTextExtractorDep,
    events: EventPublisherDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> UploadBaseCv:
    return UploadBaseCv(
        cvs,
        sessions,
        files,
        extractor,
        events,
        clock,
        max_per_session=settings.max_base_cvs_per_session,
    )


UploadBaseCvDep = Annotated[UploadBaseCv, Depends(get_upload_base_cv)]


def get_start_guest_session(
    sessions: GuestSessionRepositoryDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> StartGuestSession:
    return StartGuestSession(sessions, clock, retention_hours=settings.guest_retention_hours)


StartGuestSessionDep = Annotated[StartGuestSession, Depends(get_start_guest_session)]


def get_get_base_cv(
    cvs: BaseCvRepositoryDep,
    sessions: GuestSessionRepositoryDep,
    clock: ClockDep,
) -> GetBaseCvForSession:
    return GetBaseCvForSession(cvs, sessions, clock)


GetBaseCvDep = Annotated[GetBaseCvForSession, Depends(get_get_base_cv)]


def get_list_base_cvs(
    cvs: BaseCvRepositoryDep,
    sessions: GuestSessionRepositoryDep,
    clock: ClockDep,
) -> ListBaseCvsForSession:
    return ListBaseCvsForSession(cvs, sessions, clock)


ListBaseCvsDep = Annotated[ListBaseCvsForSession, Depends(get_list_base_cvs)]


# ---------------------------------------------------------------------------------------------
# The guest session cookie dependencies (F-17/F-18/F-19).
# ---------------------------------------------------------------------------------------------


async def require_guest_session(
    request: Request,
    sessions: GuestSessionRepositoryDep,
    clock: ClockDep,
) -> GuestSession:
    """The GET-side cookie rule (F-19): a missing, unknown or expired `tc_guest` cookie all refuse
    with 401 `guest_session_expired` — never a silently-minted session, unlike `POST`.

    Safe to use as an ordinary `Depends()` parameter, unlike `resolve_or_start_guest_session` below:
    it never mutates anything on any path (no session minted, no row written, no cookie set), so it
    does not matter that FastAPI resolves a route's dependencies even when a *different* required
    parameter of the same route turns out to be invalid (verified empirically for the POST case —
    see `resolve_or_start_guest_session`'s docstring).
    """
    token = read_guest_token(request)
    if token is not None:
        session = await sessions.find_by_token_hash(hash_guest_token(token))
        if session is not None and not session.is_expired(clock.now()):
            return session
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=GUEST_SESSION_EXPIRED_DETAIL)


RequireGuestSessionDep = Annotated[GuestSession, Depends(require_guest_session)]


async def resolve_or_start_guest_session(
    request: Request,
    response: Response,
    sessions: GuestSessionRepositoryDep,
    clock: ClockDep,
    settings: SettingsDep,
    start_guest_session: StartGuestSessionDep,
) -> GuestSession:
    """The POST-side cookie rule (F-17/F-18): a missing, unknown or expired `tc_guest` cookie all
    mint a fresh `GuestSession`, set it as the new cookie, and let the request proceed — the
    opposite forgiveness direction from `require_guest_session`.

    **Deliberately not wired as `upload_base_cv`'s own `Depends()` parameter.** Verified empirically
    while building T26: FastAPI resolves every sibling dependency of a path operation function even
    when a *different*, required parameter of that same route is missing from the request (a
    required `File(...)` part absent from the multipart body, here) — dependency resolution and body
    validation are separate steps, and the former does not short-circuit on the latter's eventual
    failure. A `Depends(resolve_or_start_guest_session)` parameter sitting next to
    `file: UploadFile = File(...)` would therefore mint and flush a brand-new `GuestSession` row
    even on F-1's case ("No `file` part... nothing stored, no row, no session mutated") — the
    `RequestValidationError` that ultimately rejects the request fires only *after* this function
    would already have run.

    `routers/intake.py::upload_base_cv` instead calls this function directly, from inside its own
    body, which FastAPI only ever enters once `file` has already been confirmed present — so the
    mutation this function performs is correctly gated on the request actually carrying a file.
    `ResolveOrStartGuestSessionDep` below is kept for a future endpoint with no such conflicting
    required parameter, where the ordinary `Depends()` form would be safe.
    """
    token = read_guest_token(request)
    if token is not None:
        existing = await sessions.find_by_token_hash(hash_guest_token(token))
        if existing is not None and not existing.is_expired(clock.now()):
            return existing

    minted = mint_guest_token()
    session = await start_guest_session(minted.token_hash)
    set_guest_cookie(response, minted.token, settings)
    return session


ResolveOrStartGuestSessionDep = Annotated[GuestSession, Depends(resolve_or_start_guest_session)]


# ---------------------------------------------------------------------------------------------
# posting — T28. Every port this slice's use cases need gets a binding here. A port with no
# binding is a bug, and this is the one file where that question has a single place to look.
# ---------------------------------------------------------------------------------------------


def get_job_posting_repository(session: SessionDep) -> JobPostingRepository:
    """Binds `JobPostingRepository` -> `SqlAlchemyJobPostingRepository` (ADR-0007).

    Deferred import, for the same mapper-configuration reason `get_base_cv_repository` documents:
    that module reads `JobPosting._id` as a plain attribute at *import* time to build its
    `InstrumentedAttribute` casts, and those only exist once `configure_mappings()` has run.
    """
    from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
        SqlAlchemyJobPostingRepository,
    )

    return SqlAlchemyJobPostingRepository(session)


JobPostingRepositoryDep = Annotated[JobPostingRepository, Depends(get_job_posting_repository)]


def get_job_posting_fetcher(settings: SettingsDep) -> JobPostingFetcherPort:
    """Binds `JobPostingFetcherPort` -> `HttpxTrafilaturaFetcher` (ADR-0012).

    **`TargetAddressPolicy.strict()` is named explicitly here rather than left to the adapter's
    default, and the redundancy is the point.** AC-9 asserts that production builds the strict
    policy, and an assertion about a default is an assertion about a value nobody wrote down. Naming
    it means the wiring test reads the same decision a human reviewer does, and that loosening it
    would require editing this line — which is exactly the line a reviewer looks at.

    The User-Agent is honest and identifying. We do not impersonate a browser: Constitution §5 closes
    the anti-bot road and FR-2 makes the paste fallback the product's answer to a refusal. "Just set
    a Chrome UA" is the reflexive fix the first time a 403 appears, months after this decision was
    made, which is why it is written down at the point of temptation.
    """
    return HttpxTrafilaturaFetcher(
        user_agent=f"TailorCraft/0.1 (+{settings.public_base_url})",
        timeout_seconds=settings.posting_fetch_timeout_seconds,
        connect_timeout_seconds=settings.posting_fetch_connect_timeout_seconds,
        read_timeout_seconds=settings.posting_fetch_read_timeout_seconds,
        max_bytes=settings.posting_fetch_max_bytes,
        max_redirects=settings.posting_fetch_max_redirects,
        extraction_timeout_seconds=settings.posting_extraction_timeout_seconds,
        policy=TargetAddressPolicy.strict(),
    )


JobPostingFetcherDep = Annotated[JobPostingFetcherPort, Depends(get_job_posting_fetcher)]


def get_posting_create_rate_limiter(redis: RedisDep) -> RedisFixedWindowRateLimiter:
    """Bounds rows and Postgres writes. **Fails open** — the cost of an unlimited request here is
    our own database, which is real but ours and bounded."""
    return RedisFixedWindowRateLimiter(redis, namespace="posting:create", fail_open=True)


PostingCreateRateLimiterDep = Annotated[
    RedisFixedWindowRateLimiter, Depends(get_posting_create_rate_limiter)
]


def get_posting_fetch_rate_limiter(redis: RedisDep) -> RedisFixedWindowRateLimiter:
    """Bounds outbound requests made from our server's IP. **Fails closed** — the cost is someone
    else's infrastructure, and an unbounded outbound endpoint with no backstop is how a server ends
    up on a job board's blocklist (ADR-0012's consequences)."""
    return RedisFixedWindowRateLimiter(redis, namespace="posting:fetch", fail_open=False)


PostingFetchRateLimiterDep = Annotated[
    RedisFixedWindowRateLimiter, Depends(get_posting_fetch_rate_limiter)
]


def get_capture_job_posting(
    postings: JobPostingRepositoryDep,
    sessions: GuestSessionRepositoryDep,
    fetcher: JobPostingFetcherDep,
    events: EventPublisherDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> CaptureJobPosting:
    return CaptureJobPosting(
        postings,
        sessions,
        fetcher,
        events,
        clock,
        max_per_session=settings.max_job_postings_per_session,
    )


CaptureJobPostingDep = Annotated[CaptureJobPosting, Depends(get_capture_job_posting)]


def get_get_job_posting(
    postings: JobPostingRepositoryDep,
    sessions: GuestSessionRepositoryDep,
    clock: ClockDep,
) -> GetJobPostingForSession:
    return GetJobPostingForSession(postings, sessions, clock)


GetJobPostingDep = Annotated[GetJobPostingForSession, Depends(get_get_job_posting)]


def get_list_job_postings(
    postings: JobPostingRepositoryDep,
    sessions: GuestSessionRepositoryDep,
    clock: ClockDep,
) -> ListJobPostingsForSession:
    return ListJobPostingsForSession(postings, sessions, clock)


ListJobPostingsDep = Annotated[ListJobPostingsForSession, Depends(get_list_job_postings)]
