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
from celery import Celery
from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tailorcraft.application.export.download_export_file import DownloadExportFile
from tailorcraft.application.export.get_export_job import GetExportJobForSession
from tailorcraft.application.export.list_exports_for_run import ListExportsForRun
from tailorcraft.application.export.render_document_inline import RenderDocumentInline
from tailorcraft.application.export.request_export import RequestExport
from tailorcraft.application.identity.start_guest_session import StartGuestSession
from tailorcraft.application.intake.get_base_cv import GetBaseCvForSession
from tailorcraft.application.intake.list_base_cvs import ListBaseCvsForSession
from tailorcraft.application.intake.upload_base_cv import UploadBaseCv
from tailorcraft.application.posting.capture_job_posting import CaptureJobPosting
from tailorcraft.application.posting.get_job_posting import GetJobPostingForSession
from tailorcraft.application.posting.list_job_postings import ListJobPostingsForSession
from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRunForSession
from tailorcraft.application.tailoring.list_tailoring_runs import ListTailoringRunsForSession
from tailorcraft.application.tailoring.request_tailoring_run import RequestTailoringRun
from tailorcraft.application.tailoring.revise_tailored_document import ReviseTailoredDocument
from tailorcraft.domain.export.ports import (
    DocumentRendererPort,
    ExportJobRepository,
    ExportQueuePort,
)
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ports import GuestSessionRepository
from tailorcraft.domain.intake.ports import BaseCvRepository, CvTextExtractorPort
from tailorcraft.domain.posting.ports import JobPostingFetcherPort, JobPostingRepository
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.shared.files import FileStorePort
from tailorcraft.domain.tailoring.ports import (
    LlmPort,
    TailoringQueuePort,
    TailoringRunRepository,
)
from tailorcraft.infrastructure.api.errors import GUEST_SESSION_EXPIRED_DETAIL
from tailorcraft.infrastructure.api.guest_session import (
    hash_guest_token,
    mint_guest_token,
    read_guest_token,
    set_guest_cookie,
)
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.events.logging_publisher import LoggingEventPublisher
from tailorcraft.infrastructure.export.queue import CeleryExportQueue
from tailorcraft.infrastructure.export.renderer import MarkdownDocumentRenderer
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.intake.extraction import PypdfDocxTextExtractor
from tailorcraft.infrastructure.llm.gemini import GeminiLlm
from tailorcraft.infrastructure.posting.address_policy import TargetAddressPolicy
from tailorcraft.infrastructure.posting.fetching import HttpxTrafilaturaFetcher
from tailorcraft.infrastructure.rate_limit import RedisFixedWindowRateLimiter
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tailoring.queue import CeleryTailoringQueue


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


def get_celery(request: Request) -> Celery:
    """Off `app.state`, the same way `get_engine` is — deliberately not the module-level
    `tasks.app.app` singleton this application's lifespan actually stores there.

    A `Celery` object owns a broker connection pool, so it is a long-lived resource and belongs to
    the *application*, not to a request. Reading it through `app.state` means the app under test and
    the app in production each publish through the object their own composition wired
    (`main.py`'s lifespan and `tests/conftest.py`'s `app` fixture both set `app.state.celery`), for
    the same reason `get_app_settings` refuses the `lru_cache`d `get_settings()`.
    """
    celery: Celery = request.app.state.celery
    return celery


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
CeleryDep = Annotated[Celery, Depends(get_celery)]


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


# ---------------------------------------------------------------------------------------------
# tailoring — T32. Same rule as the two sections above: a port with no binding is a bug. It is
# only *harder* to check here, because this slice is the first with a second composition root —
# `infrastructure/tasks/container.py`, which the worker uses and which binds a deliberately
# different subset (that module's docstring says which, and why). Neither file is the whole list;
# technical-plan.md's port list is, and it is checked against both.
# ---------------------------------------------------------------------------------------------


def get_tailoring_run_repository(session: SessionDep) -> TailoringRunRepository:
    """Binds `TailoringRunRepository` -> `SqlAlchemyTailoringRunRepository` (ADR-0007).

    Deferred import, for the same mapper-configuration reason `get_base_cv_repository` documents:
    that module reads `TailoringRun._id` as a plain attribute at *import* time to build its
    `InstrumentedAttribute` casts, and those only exist once `configure_mappings()` has run — which
    in tests is a session-scoped fixture that runs long after `tests/conftest.py` has imported this
    module. By request time, when this function is actually called, mappings are always configured.

    The **bare** adapter, note — not `tasks/container.py`'s `CommittingTailoringRunRepository`. The
    unit of work here is the request (`get_session` commits once, at the end); in the worker it is
    the individual write, because `ExecuteTailoringRun` must make `running` visible to a polling
    client before it spends twelve seconds on a model call. Same port, two boundaries, and the
    difference between the two bindings *is* the boundary.
    """
    from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
        SqlAlchemyTailoringRunRepository,
    )

    return SqlAlchemyTailoringRunRepository(session)


TailoringRunRepositoryDep = Annotated[TailoringRunRepository, Depends(get_tailoring_run_repository)]


def get_llm(settings: SettingsDep) -> LlmPort:
    """Binds `LlmPort` -> `GeminiLlm` (ADR-0004). **The real client is the strict default.**

    `GeminiLlm.__init__` takes an optional `generate` seam whose default is the SDK call, and this
    provider does not pass it. That is the rule ADR-0004 and `gemini.py`'s own docstring both state:
    the stub lives in the test that injects it, never behind a setting and never as a fallback in
    the composition root. A wiring that could quietly degrade to a fake is a wiring where "no test
    calls the real Gemini API" and "production calls the real Gemini API" are the same line of code
    disagreeing with itself.

    **No route resolves this today, and that is expected rather than an oversight.** ADR-0014's
    whole point is that the API never calls the model — it commits a `queued` run and publishes to
    the queue, and the worker's composition root (`tasks/container.py::_build_use_case`) is where
    the port is bound for the process that actually calls `tailor`. The binding exists here so that
    the API's composition root answers "which adapter satisfies `LlmPort`?" without a reader having
    to know which of the two roots to look in, and so that `app.dependency_overrides[deps.get_llm]`
    is a key an API test can reach for — a belt-and-braces guarantee that no suite, present or
    future, can reach Google through this application.
    """
    return GeminiLlm(settings)


LlmDep = Annotated[LlmPort, Depends(get_llm)]


def get_tailoring_queue(celery: CeleryDep, settings: SettingsDep) -> TailoringQueuePort:
    """Binds `TailoringQueuePort` -> `CeleryTailoringQueue` (ADR-0005, ADR-0014 §5).

    Both arguments are passed explicitly because the adapter's constructor has no defaults, which is
    itself deliberate: `settings.tailoring_queue_name` is the single place the queue's name is
    written, and the worker's consumed-queue list (`tasks/app.py`'s `task_queues`) is derived from
    the same field. A producer publishing to `tailoring` while a worker consumes only `celery` is a
    run that queues forever behind a green health check — the one failure mode this whole naming
    arrangement exists to make impossible.
    """
    return CeleryTailoringQueue(celery, settings.tailoring_queue_name)


TailoringQueueDep = Annotated[TailoringQueuePort, Depends(get_tailoring_queue)]


def get_tailoring_rate_limiter(redis: RedisDep) -> RedisFixedWindowRateLimiter:
    """Bounds runs per session and per client IP. **Fails closed** — `fail_open=False`.

    This looks inconsistent beside `get_rate_limiter` and `get_posting_create_rate_limiter`, which
    both fail open, so here is the reason at the point of the inconsistency. The rule the other
    three limiters are all instances of: *fail open when the cost is ours and bounded; fail closed
    when the cost is money or somebody else's infrastructure* (`rate_limit.py::__init__`, recorded
    as 1.1's OQ-7 and generalized by 1.2's OQ-8). This is that spectrum's far end arriving. An
    unauthenticated endpoint that spends money on every call, with its only backstop switched off
    because Redis happens to be down, is a funded denial-of-wallet — and the first evidence of it
    would be an invoice, which is the worst possible monitoring. Refusing with 503 while Redis is
    unreachable costs a user a retry; failing open costs an amount nobody has bounded.

    A rate limit bounds how *often* a run is requested, never whether one is: the per-session cap
    (`max_tailoring_runs_per_session`), the single-active-run rule and the LLM's own total deadline
    each bound a different quantity, and none of them substitutes for another.
    """
    return RedisFixedWindowRateLimiter(redis, namespace="tailoring:create", fail_open=False)


TailoringRateLimiterDep = Annotated[
    RedisFixedWindowRateLimiter, Depends(get_tailoring_rate_limiter)
]


def get_request_tailoring_run(
    runs: TailoringRunRepositoryDep,
    get_base_cv: GetBaseCvDep,
    get_job_posting: GetJobPostingDep,
    events: EventPublisherDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> RequestTailoringRun:
    """Note what the second and third arguments are: the two *use cases*, not their repositories.

    `RequestTailoringRun` reads the base CV and the job posting through `GetBaseCvForSession` and
    `GetJobPostingForSession` so that "what authorizes access is the link to the session" is
    inherited from the slices that already own that rule, rather than written a third time here
    (ADR-0008). Rebuilding those two from `BaseCvRepositoryDep`/`JobPostingRepositoryDep` would
    compile, produce an identical object graph today, and quietly become a second copy of an
    authorization rule the moment either use case grows a check — so the existing providers are
    reused instead.
    """
    return RequestTailoringRun(
        runs,
        get_base_cv,
        get_job_posting,
        events,
        clock,
        max_per_session=settings.max_tailoring_runs_per_session,
    )


RequestTailoringRunDep = Annotated[RequestTailoringRun, Depends(get_request_tailoring_run)]


def get_get_tailoring_run(
    runs: TailoringRunRepositoryDep,
    sessions: GuestSessionRepositoryDep,
    clock: ClockDep,
) -> GetTailoringRunForSession:
    return GetTailoringRunForSession(runs, sessions, clock)


GetTailoringRunDep = Annotated[GetTailoringRunForSession, Depends(get_get_tailoring_run)]


def get_list_tailoring_runs(
    runs: TailoringRunRepositoryDep,
    sessions: GuestSessionRepositoryDep,
    clock: ClockDep,
) -> ListTailoringRunsForSession:
    return ListTailoringRunsForSession(runs, sessions, clock)


ListTailoringRunsDep = Annotated[ListTailoringRunsForSession, Depends(get_list_tailoring_runs)]


def get_tailoring_revise_rate_limiter(redis: RedisDep) -> RedisFixedWindowRateLimiter:
    """Bounds saves of an edited document per session (slice 1.4). **Fails open** — `fail_open=True`.

    The rule from 1.1 (OQ-7) and 1.2 (OQ-8), applied for the third time: *fail open when the cost is
    ours and bounded; fail closed when the cost is money or somebody else's infrastructure.* A save
    is one `UPDATE` of one document, at most 20,000 characters, bounded by the value object the
    router constructs before the use case ever runs, so an unlimited save costs our own database and
    nothing else — and Redis being down must not stop a person saving their CV (E-31). The same rule
    sends `get_tailoring_rate_limiter` the other way: a run spends money on every call.

    Per session only, no per-IP scope, on purpose: a save is always bound to a session that already
    owns the run, so a fresh session buys a hammering script nothing.
    """
    return RedisFixedWindowRateLimiter(redis, namespace="tailoring:revise", fail_open=True)


TailoringReviseRateLimiterDep = Annotated[
    RedisFixedWindowRateLimiter, Depends(get_tailoring_revise_rate_limiter)
]


def get_revise_tailored_document(
    runs: TailoringRunRepositoryDep,
    get_run: GetTailoringRunDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> ReviseTailoredDocument:
    """The second argument is the `GetTailoringRunForSession` *use case*, not the repository, for
    the reason `get_request_tailoring_run` gives: "what authorizes the write is the link to the
    session" is inherited from the read that already owns that rule (AC-14), not written again."""
    return ReviseTailoredDocument(runs, get_run, events, clock)


ReviseTailoredDocumentDep = Annotated[ReviseTailoredDocument, Depends(get_revise_tailored_document)]


# ---------------------------------------------------------------------------------------------
# export — I16. Same rule as every section above: **a port with no binding is a bug.** This slice
# adds three ports — `ExportJobRepository`, `DocumentRendererPort`, `ExportQueuePort` — and they
# are checked against *both* composition roots, because the boundaries split them differently:
#
#   * `ExportJobRepository` is bound in both. Here, the bare adapter; in
#     `tasks/container.py`, `CommittingExportJobRepository`. Same port, two units of work.
#   * `DocumentRendererPort` is bound in both, and for two different deliveries. Here it renders
#     `md`/`txt` **inside the request**; in the worker it renders `pdf`/`docx`. One adapter, one
#     set of defaults, two callers — and that is why `MarkdownDocumentRenderer(settings)` is
#     written identically in both places, with neither passing the `url_fetcher`/`sanitize` seams.
#   * `ExportQueuePort` is bound **here only**. The API publishes; the worker consumes. A worker
#     that could enqueue its own renders is a loop nobody asked for.
#
# Neither file is the whole list; technical-plan.md's port list is, and it is checked against both.
# ---------------------------------------------------------------------------------------------


def get_export_job_repository(session: SessionDep) -> ExportJobRepository:
    """Binds `ExportJobRepository` -> `SqlAlchemyExportJobRepository` (ADR-0007).

    Deferred import, for the same mapper-configuration reason `get_base_cv_repository` and
    `get_tailoring_run_repository` both document: that module reads `ExportJob._id` as a plain
    attribute at *import* time to build its `InstrumentedAttribute` casts, and those only exist once
    `configure_mappings()` has run — which in tests is a session-scoped fixture that runs long after
    `tests/conftest.py` has imported this module. By request time, when this function is actually
    called, mappings are always configured.

    The **bare** adapter — not `tasks/container.py`'s `CommittingExportJobRepository`. The unit of
    work here is the request (`get_session` commits once, at the end); in the worker it is the
    individual write, because `RenderExportJob` must make `rendering` visible to a polling client
    before it spends seconds inside WeasyPrint. Same port, two boundaries, and the difference
    between the two bindings *is* the boundary.
    """
    from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
        SqlAlchemyExportJobRepository,
    )

    return SqlAlchemyExportJobRepository(session)


ExportJobRepositoryDep = Annotated[ExportJobRepository, Depends(get_export_job_repository)]


def get_document_renderer(settings: SettingsDep) -> DocumentRendererPort:
    """Binds `DocumentRendererPort` -> `MarkdownDocumentRenderer` (ADR-0017).

    **The strict defaults are the binding**, exactly as `get_llm` refuses to pass its `generate`
    seam. `MarkdownDocumentRenderer.__init__` takes an optional `url_fetcher` (default
    `refuse_every_url`) and an optional `sanitize` (default `sanitize_html`); this line passes
    neither, and neither does the worker's `_build_export_use_case`. Those two lines are the only
    production constructions of this adapter in the codebase, and a wiring that could reach the
    network — or skip the sanitizer — from a keyword argument would be a wiring where "WeasyPrint
    fetches nothing" is a claim rather than a fact (AC-30).

    **The API binds this for the inline half only** (`md`, `txt`): a render inside a request, in a
    thread, under a 5-second deadline. `pdf` and `docx` are structurally unreachable from here —
    the query parameter's `Literal["md", "txt"]` and `ExportFormatNotInline` are the two locks — and
    that is ADR-0005's rule made physical: the cost of the work decides where it runs.
    """
    return MarkdownDocumentRenderer(settings)


DocumentRendererDep = Annotated[DocumentRendererPort, Depends(get_document_renderer)]


def get_export_queue(celery: CeleryDep, settings: SettingsDep) -> ExportQueuePort:
    """Binds `ExportQueuePort` -> `CeleryExportQueue` (ADR-0005, ADR-0016 (d)).

    Both arguments are passed explicitly because the adapter's constructor has no defaults, which is
    itself deliberate — `get_tailoring_queue`'s reasoning, one context over:
    `settings.export_queue_name` is the single place the queue's name is written, and the worker's
    consumed-queue list (`tasks/app.py`'s `task_queues`) is derived from the same field. A producer
    publishing to `export` while a worker consumes only `celery` and `tailoring` is a job that
    queues for ever behind a green health check.
    """
    return CeleryExportQueue(celery, settings.export_queue_name)


ExportQueueDep = Annotated[ExportQueuePort, Depends(get_export_queue)]


def get_export_rate_limiter(redis: RedisDep) -> RedisFixedWindowRateLimiter:
    """Bounds export requests per session and per client IP. **Fails open** — `fail_open=True`.

    The rule from 1.1 (OQ-7), generalized by 1.2 (OQ-8) and applied here for the fourth time: *fail
    open when the cost is ours and bounded; fail closed when the cost is money or somebody else's
    infrastructure.* A render costs worker seconds and disk of ours — bounded on three sides by
    `max_export_jobs_per_session` (40), the 20 MiB output cap and the 24-hour purge — and there is
    no invoice at the end of it. Redis being down must not stop a person downloading their own CV.

    This is the **middle** of that spectrum, not either end: `get_tailoring_rate_limiter` fails
    closed because a run spends money on every call; `get_tailoring_revise_rate_limiter` fails open
    because a save is one `UPDATE`. An export is heavier than a save and free, so it lands here, and
    X-20 says what a failed-open limiter does: nothing the user can see, one
    `rate_limiter.unavailable` log line with the namespace and the error type and **never the
    identifier**.

    Both scopes, unlike the revise limiter's session-only: an export is expensive enough that a
    script minting fresh sessions is worth bounding by address as well (30/h/session, 60/h/IP).
    """
    return RedisFixedWindowRateLimiter(redis, namespace="export:create", fail_open=True)


ExportRateLimiterDep = Annotated[RedisFixedWindowRateLimiter, Depends(get_export_rate_limiter)]


def get_request_export(
    jobs: ExportJobRepositoryDep,
    get_tailoring_run: GetTailoringRunDep,
    events: EventPublisherDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> RequestExport:
    """Note the second argument: the `GetTailoringRunForSession` *use case*, not a run repository.

    `RequestExport` reads the run through the use case that already owns "what authorizes access is
    the link to the session", so the rule is inherited rather than written a sixth time (ADR-0008,
    X-13) — `get_request_tailoring_run` and `get_revise_tailored_document` give the argument in
    full. It is also why this provider never mentions `TailoringRunRepositoryDep`: the use case
    cannot reach a run any other way, so it cannot forget the check.
    """
    return RequestExport(
        jobs,
        get_tailoring_run,
        events,
        clock,
        max_per_session=settings.max_export_jobs_per_session,
    )


RequestExportDep = Annotated[RequestExport, Depends(get_request_export)]


def get_render_document_inline(
    get_tailoring_run: GetTailoringRunDep,
    renderer: DocumentRendererDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> RenderDocumentInline:
    """The one use case in this codebase that **constructs** a domain event rather than releasing
    one an aggregate recorded — an inline export writes no row, so there is no aggregate to record
    it — which is why it is handed a `Clock` when the technical plan's constructor list omitted one.
    `DomainEvent.occurred_at` has no default, deliberately, so that no event can be stamped from a
    hidden `datetime.now()`."""
    return RenderDocumentInline(get_tailoring_run, renderer, events, clock)


RenderDocumentInlineDep = Annotated[RenderDocumentInline, Depends(get_render_document_inline)]


def get_get_export_job(
    jobs: ExportJobRepositoryDep,
    runs: TailoringRunRepositoryDep,
    sessions: GuestSessionRepositoryDep,
    clock: ClockDep,
) -> GetExportJobForSession:
    """The run repository is here, and it is the exception that proves `get_request_export`'s rule.

    This use case reads a **job**, and it reads the run only to learn one integer — the version the
    run is at right now, which is what makes `current` computable at the boundary (AC-24). It uses
    `runs.find`, not `runs.get`: a run that has gone is an ordinary answer on a read of a job that
    still exists. There is no authorization to inherit from a run here, because the job carries its
    own `guest_session_id` and *that* is what this use case checks (X-43).
    """
    return GetExportJobForSession(jobs, runs, sessions, clock)


GetExportJobDep = Annotated[GetExportJobForSession, Depends(get_get_export_job)]


def get_list_exports_for_run(
    jobs: ExportJobRepositoryDep,
    get_tailoring_run: GetTailoringRunDep,
) -> ListExportsForRun:
    return ListExportsForRun(jobs, get_tailoring_run)


ListExportsForRunDep = Annotated[ListExportsForRun, Depends(get_list_exports_for_run)]


def get_download_export_file(
    get_export_job: GetExportJobDep,
    files: FileStoreDep,
) -> DownloadExportFile:
    """The first argument is the `GetExportJobForSession` *use case*: the download inherits the
    poll's authorization and its collapse of "not mine" into "not found" whole (X-43), rather than
    repeating an ownership check beside a file read — which is the one place in this slice where
    forgetting it would hand a stranger a stranger's CV."""
    return DownloadExportFile(get_export_job, files)


DownloadExportFileDep = Annotated[DownloadExportFile, Depends(get_download_export_file)]
