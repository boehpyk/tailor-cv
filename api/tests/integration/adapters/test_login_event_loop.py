"""AC-20 — the event-loop liveness measurement for argon2 verification under `POST /api/auth/login`
(T34, `slow`), proven by mutation.

**Method, the same shape as AC-10/AC-42** (`loop_liveness.py`'s two lessons): a sample is the loop's
**turnaround** (`sleep(1 ms) + GET /health/live`, minus the sleep), collected until the sampler has
its own floor — the batch's duration is an output, never an assumption baked into an iteration count
(1.5's CI flake). What differs here is the workload and why it needs its own fixture rather than the
suite's shared one.

**Why this file cannot use `tests/conftest.py`'s `password_hasher` fixture.** That fixture is built
from `TEST_ARGON2_PARAMETERS` (`m=8, t=1, p=1`) deliberately — every other identity test hashes on
every register and login, and a production-cost hash there would make the whole suite slow for no
reason. But argon2's `PasswordHasher.verify()` reads its cost parameters **from the stored PHC
string**, never from the verifying instance's own configuration (measured against argon2-cffi, not
assumed): a cheap hasher verifying a cheap hash costs nothing on the loop regardless of whether the
hop through the executor exists at all, and a test built on one could pass by having nothing to
measure. So this file builds its own `Argon2PasswordHasher` with **`PRODUCTION_PARAMETERS`** (the
class's own default — `m=65536, t=3, p=4`, RFC 9106's low-memory profile) on its own dedicated
2-worker `ThreadPoolExecutor`, seeds the one user under test with a hash that hasher itself produced,
and wires it onto `app.state.password_hasher` in place of the suite's cheap one. One hash under these
parameters is 40-250 ms of real CPU (AC-19's own measured bound) — the thing AC-20 exists to keep off
the loop.

**Why this file cannot use `tests/conftest.py`'s `app`/`session` fixtures either.** Those bind every
request to one already-open, SAVEPOINT-per-test `AsyncSession`, which is not safe for concurrent use
— eight logins in flight at once need eight independent connections genuinely proceeding in parallel,
the same reason `test_identity_database_truths.py`'s `concurrent_app` fixture exists (T31). This file
borrows that shape: `app.state.session_factory = create_session_factory(engine)`, so every request
opens and commits its own session off the shared pool.

**The rate limiter would otherwise be the thing under test.** `login`'s two limiters (`20/h` per IP,
`10/h` per email, AC-27) would refuse most of this file's requests long before the sampler reaches its
floor, on a fixed test-suite budget — so this file raises both limits on its own `Settings` copy,
exactly as the task instructs. This does not weaken anything the production limiter still enforces:
nothing here reads a setting the real deployment would ever set this high.

**Every successful login is a `Login.start` and a real committed row** (`LogIn`'s own docstring, step
6) — this file therefore accumulates one `identity_login` row per request for as long as the batch
runs, and deletes them all at teardown by deleting the one seeded `identity_user` row
(`ON DELETE CASCADE`, `login.py`'s mapping module).

---

**Mutation record (2026-09-24).** `password_hasher.py`'s `verify` was rewritten to call
`self._verify_sync(...)` directly — dropping `await loop.run_in_executor(self._executor, ...)` around
it, leaving everything else byte-identical — then restored.

    before: 06078b0ab2071c2615ba12cd9b8843a5  password_hasher.py
    after:  06078b0ab2071c2615ba12cd9b8843a5  password_hasher.py   (identical — byte-exact restore)

| run | p50 turnaround | samples | max | wall-clock | note |
|---|---|---|---|---|---|
| healthy (x2) | 1.015 ms / 1.022 ms | 200 | 93.6 ms / 125.3 ms | 2.0 s / 2.1 s | asserted `< 5 ms` — passes with an 80-200x margin |
| mutated (x3, floor 200) | 1.55 / 1.64 / 1.63 ms | 200 | 196-203 ms | 5.2-5.5 s | **stayed green** — p50 never crossed the budget |
| mutated (floor 2000, one run) | 1.607 ms | 2000 | 253 ms | 44.1 s | same result at 10x the samples and a 20x longer window |

**This is not the "observe red" outcome the task anticipated, and it is not a hang either — recorded
honestly rather than forced.** The mutation is real (max turnaround roughly doubled, and the whole
batch took 2.5-20x longer to reach the same sample floor — "blocking suppresses sampling",
`loop_liveness.py`'s own lesson two, working exactly as documented), but **p50 specifically never
crosses 5 ms**, reproduced across five separate runs including a 10x-larger sample. The reason is the
same one CLAUDE.md already records for AC-10's fetcher test: this workload is *mostly async I/O*
(two Postgres round trips and a Redis rate-limiter check per login) with *one* synchronous CPU step —
under 8-way concurrency the blocked fraction of wall-clock time stays comfortably under half, so most
samples land in the free gaps between blocking calls and pull the median down regardless of how bad
the blocked calls themselves are. `max` and total wall-clock both show the regression plainly; p50
does not. CLAUDE.md already names the fix for this shape of test — "the loop's *unavailable fraction*
over the window (`sum(turnarounds) / wall-clock`) ... is the right eventual answer" — and just as
explicitly declines to make that call outside the criterion's own owner: "adopting it changes what
[the criterion] asserts and therefore a decision for [its] owner". AC-20 as written asserts p50, so
this file asserts p50, and this paragraph is the record that the assertion — passing here on real,
production-cost argon2 over a genuinely concurrent, genuinely committing 8-way login load — has *not*
yet been observed to fail against the regression it exists to catch. Owner: AC-20's owner; trigger:
adopt the unavailable-fraction statistic, or accept the gap in writing.
"""

from __future__ import annotations

import asyncio
import statistics
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, Password, UserId
from tailorcraft.infrastructure.api.main import create_app
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.database import create_session_factory
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks.app import app as celery_app

from .loop_liveness import hammer_health_live_until_floor

pytestmark = pytest.mark.slow

LOGIN_URL = "/api/auth/login"
A_STRONG_PASSWORD = "correct horse battery staple 9"  # 31 chars, clear of the 12-char floor.

_CONCURRENT_LOGINS = 8
# Ten times the shared default (`loop_liveness.SAMPLE_FLOOR`), matching AC-10's own reasoning: the
# executor hop is awaited I/O from the loop's own perspective, so a short batch would sample mostly
# warm-up before any login has even reached the executor. Measured at 200 and again at 2000 (module
# docstring's mutation record) — the higher floor changes the batch's duration, not its p50, so 200
# is kept as this file's floor.
_SAMPLE_FLOOR = 200
_BUDGET_SECONDS = 0.005


def _with_raised_login_limits(settings: Settings) -> Settings:
    """AC-27's limiters would otherwise refuse most of this file's requests before the sampler ever
    reaches its floor — raised here, on a private copy, never on anything a real deployment reads."""
    return settings.model_copy(
        update={
            "login_rate_limit_per_ip_per_hour": 1_000_000,
            "login_rate_limit_per_email_per_hour": 1_000_000,
        }
    )


@pytest_asyncio.fixture
async def production_hasher() -> AsyncIterator[Argon2PasswordHasher]:
    """A **production-parameter** hasher (`PRODUCTION_PARAMETERS` is the class's own default — never
    passed explicitly, so this is exactly what `main.py`'s lifespan builds) on its own dedicated
    2-worker executor, shut down at teardown as the lifespan's `finally` does."""
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="argon2-t34")
    try:
        yield Argon2PasswordHasher(executor)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


@pytest_asyncio.fixture
async def live_app(
    settings: Settings, engine: AsyncEngine, production_hasher: Argon2PasswordHasher
) -> FastAPI:
    """Wired for genuine per-request sessions and the production hasher (module docstring)."""
    live_settings = _with_raised_login_limits(settings)
    app = create_app(live_settings)
    app.state.settings = live_settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.celery = celery_app
    app.state.password_hasher = production_hasher
    return app


async def _seed_user_with_a_production_cost_hash(
    engine: AsyncEngine, hasher: Argon2PasswordHasher, *, email: str, password: str
) -> UserId:
    """A real, committed `User` whose stored hash was produced by `hasher` itself — so the PHC
    string this login verifies against genuinely carries `m=65536,t=3,p=4` (module docstring: verify
    reads cost from the stored hash, not from the verifying instance)."""
    from tailorcraft.infrastructure.persistence.repositories.identity.user import (
        SqlAlchemyUserRepository,
    )

    now = datetime.now(UTC).replace(microsecond=0)
    hashed = await hasher.hash(Password.from_input(password))
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        users = SqlAlchemyUserRepository(session)
        user_id = users.next_identity()
        user = User.register_with_password(user_id, EmailAddress.parse(email), hashed, now)
        await users.add(user)
        await session.commit()
    return user_id


async def _delete_user(engine: AsyncEngine, user_id: UserId) -> None:
    """Cascades to every `identity_login` row this run accumulated (`ON DELETE CASCADE`)."""
    async with engine.begin() as conn:
        await conn.execute(user_table.delete().where(user_table.c.id == user_id))


async def test_loop_turnaround_p50_stays_under_5ms_during_8_concurrent_logins(
    live_app: FastAPI,
    engine: AsyncEngine,
    production_hasher: Argon2PasswordHasher,
    clear_redis: None,
) -> None:
    """AC-20. Eight logins against one seeded, production-cost account, held in flight continuously
    (re-submitted until the sampler signals it has its floor) while `/health/live`'s turnaround is
    sampled on the same event loop. p50 must stay under 5 ms — argon2's ~50-250 ms per verify never
    reaches the loop if `loop.run_in_executor` is doing its job.
    """
    settings: Settings = live_app.state.settings
    email = f"t34-{uuid4().hex}@example.com"
    user_id = await _seed_user_with_a_production_cost_hash(
        engine, production_hasher, email=email, password=A_STRONG_PASSWORD
    )
    client = AsyncClient(transport=ASGITransport(app=live_app), base_url="http://testserver")
    try:
        turnarounds = await _hammer_logins_and_sample(client, settings, email)
    finally:
        await client.aclose()
        await _delete_user(engine, user_id)

    assert len(turnarounds) >= _SAMPLE_FLOOR, (
        f"only {len(turnarounds)} /health/live samples were taken during the eight concurrent "
        "logins — the sampler returned before reaching its own floor, which should be impossible; "
        "see loop_liveness.hammer_health_live_until_floor"
    )
    p50 = statistics.median(turnarounds)
    assert p50 < _BUDGET_SECONDS, (
        f"/health/live turnaround p50 was {p50 * 1000:.2f} ms during eight concurrent logins "
        f"(n={len(turnarounds)}, max={max(turnarounds) * 1000:.2f} ms) — the event loop was blocked"
    )


async def _hammer_logins_and_sample(
    client: AsyncClient, settings: Settings, email: str
) -> list[float]:
    """Runs the sampler and the eight login workers together, and returns the sampler's turnarounds.
    Factored out so the mutation run below can call exactly this and nothing else — the same
    procedure, unmutated except for the one line under test.
    """
    enough = asyncio.Event()
    hammer_task = asyncio.ensure_future(
        hammer_health_live_until_floor(client, enough=enough, floor=_SAMPLE_FLOOR)
    )
    await asyncio.sleep(0)  # let the sampler actually start before the logins begin

    headers = {"Origin": settings.public_base_url}
    body = {"email": email, "password": A_STRONG_PASSWORD}

    async def _login_until_enough() -> None:
        while not enough.is_set():
            response = await client.post(LOGIN_URL, json=body, headers=headers)
            assert response.status_code == 200, response.text

    try:
        # A generous but finite ceiling — `enough` is set by the sampler the instant it has its
        # floor. Wide enough to let a MUCH slower (mutated) run still finish and be measured, per
        # AC-10's identical reasoning. The mutation runs recorded in the module docstring wrapped
        # the whole `pytest` process in a 90-120 s shell `timeout` rather than relying on this bound
        # alone — the outer guard is what would have caught a genuine hang (1.5's uninterruptible
        # case); this `wait_for` is sized for "finish, however slowly", not for detecting one.
        await asyncio.wait_for(
            asyncio.gather(*(_login_until_enough() for _ in range(_CONCURRENT_LOGINS))),
            timeout=180,
        )
    finally:
        enough.set()
    return await asyncio.wait_for(hammer_task, timeout=30)
