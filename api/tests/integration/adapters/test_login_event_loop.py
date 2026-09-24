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

**First pass (2026-09-24, superseded below).** This file originally gated on p50, exactly as AC-20
was first written. Mutation-tested the same way (`verify`'s executor hop dropped, byte-exact restore
confirmed by `md5sum`, `06078b0ab2071c2615ba12cd9b8843a5` before and after both times), p50 stayed
green across five separate mutated runs (three at floor 200: 1.55/1.64/1.63 ms; one at floor
2000/n=2000/44 s: 1.607 ms) against healthy p50s of 1.015-1.022 ms — never crossing the 5 ms budget,
even though `max` turnaround roughly doubled (93-125 ms healthy to 196-253 ms mutated) and the
wall-clock to reach the same floor stretched 2.5-20x (2.0-2.1 s to 5.2-44.1 s). That result was
recorded rather than forced, and reported to AC-20's owner as a real, reproduced instance of the
weakness `loop_liveness.py`'s own docstring already names: "p50 is only sensitive when the blocked
fraction is comfortably over half," and this workload — mostly async I/O (two Postgres round trips
plus a Redis rate-limiter check per login) with one CPU-bound step — keeps that fraction under half
under 8-way concurrency.

**Amendment (2026-09-24): AC-20 now gates on `loop_liveness.unavailable_fraction`
(`sum(turnarounds) / wall_clock`), never on p50** — the owner's decision on the finding above, made
explicit in `feature-spec.md`'s own "Amended 2026-09-24" note on AC-20. p50 is still computed and
reported in the assertion message, for a human reading a failure to have it, but nothing here passes
or fails because of it.

**Mutation record for the amended assertion.** Same mutation as the first pass — `verify` rewritten
to call `self._verify_sync(...)` directly, dropping `await loop.run_in_executor(self._executor,
...)` around it, leaving everything else byte-identical — then restored.

    before: 06078b0ab2071c2615ba12cd9b8843a5  password_hasher.py
    after:  06078b0ab2071c2615ba12cd9b8843a5  password_hasher.py   (identical — byte-exact restore)

| run | unavailable_fraction | p50 | samples | max | wall-clock |
|---|---|---|---|---|---|
| healthy 1 | 0.5428 | 1.134 ms | 200 | 123.8 ms | 1.054 s |
| healthy 2 | 0.5523 | 1.110 ms | 200 | 88.3 ms | 1.080 s |
| healthy 3 | 0.5623 | 1.154 ms | 200 | 110.2 ms | 1.090 s |
| mutated 1 | 0.9018 | 1.866 ms | 200 | 198.6 ms | 4.756 s |
| mutated 2 | 0.8881 | 1.664 ms | 200 | 197.0 ms | 4.618 s |
| mutated 3 | 0.8672 | 1.673 ms | 200 | 246.1 ms | 4.667 s |

Healthy `unavailable_fraction` ranges **0.543-0.562**; mutated ranges **0.867-0.902** — a clean,
reproducible gap of roughly 0.30-0.36 with no overlap across three runs on each side. **The bound is
`_UNAVAILABLE_FRACTION_BUDGET = 0.7`**, chosen at (rather than merely inside) the numeric midpoint of
the worst healthy run (0.562) and the worst-case mutated run (0.867) — 0.7145 rounded down to one
readable decimal — giving healthy a **0.138** margin below the bound and mutated a **0.167** margin
above it: the mutated side, the one where a false negative is the dangerous failure mode, gets the
larger cushion on purpose. Every one of the three mutated runs above fails this bound; every one of
the three healthy runs passes it — the "observe red" outcome the task asked for, now genuinely
observed rather than reported absent.

**Why this number is not close to 0, and that is not a bug in the statistic.** Even the healthy
runs show `unavailable_fraction` above one half, because `sum(turnarounds)` is a sum across *every*
one of 200 samples' excess-over-pacing, including brief, ordinary scheduling jitter under 8-way
concurrent load — not a fraction of time that is literally "the loop could not respond at all" the
way the name might suggest read informally. `loop_liveness.unavailable_fraction`'s own docstring says
so explicitly: it is not bounded to `[0, 1]` by construction, and a caller compares it against a
bound measured for its *own* workload and sampler configuration, never a borrowed threshold. What
carries the proof here is not the absolute number but the **separation** between the two clusters,
verified above with margin on both sides.
"""

from __future__ import annotations

import asyncio
import statistics
import time
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

from .loop_liveness import hammer_health_live_until_floor, unavailable_fraction

pytestmark = pytest.mark.slow

LOGIN_URL = "/api/auth/login"
A_STRONG_PASSWORD = "correct horse battery staple 9"  # 31 chars, clear of the 12-char floor.

_CONCURRENT_LOGINS = 8
# Ten times the shared default (`loop_liveness.SAMPLE_FLOOR`), matching AC-10's own reasoning: the
# executor hop is awaited I/O from the loop's own perspective, so a short batch would sample mostly
# warm-up before any login has even reached the executor. Measured at 200 and again at 2000 (module
# docstring's mutation record) — the higher floor changes the batch's duration, not the statistic
# below, so 200 is kept as this file's floor.
_SAMPLE_FLOOR = 200
# AC-20's amended bound (2026-09-24), chosen with margin on both sides of the measured healthy and
# mutated `unavailable_fraction` values — the full set of numbers is in the module docstring's
# mutation record, which is also where "why this number and not p50" is argued in full.
_UNAVAILABLE_FRACTION_BUDGET = 0.7


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


async def test_loop_unavailable_fraction_stays_under_budget_during_8_concurrent_logins(
    live_app: FastAPI,
    engine: AsyncEngine,
    production_hasher: Argon2PasswordHasher,
    clear_redis: None,
) -> None:
    """AC-20 (amended 2026-09-24). Eight logins against one seeded, production-cost account, held in
    flight continuously (re-submitted until the sampler signals it has its floor) while
    `/health/live`'s turnaround is sampled on the same event loop.

    **Gates on the loop's unavailable fraction, `sum(turnarounds) / wall_clock`
    (`loop_liveness.unavailable_fraction`), not on p50.** The amendment's own reason, recorded in
    full in this module's docstring: p50 was measured, across five separate mutated runs including a
    10x-larger sample, to stay under its 5 ms budget even with the executor hop removed from
    `password_hasher.py`'s `verify` — this workload's blocked fraction stays under half under 8-way
    concurrency, which is precisely where CLAUDE.md already documents p50 losing its sensitivity.
    p50 is still computed and reported in the assertion message for context; it is never what this
    test's pass/fail depends on.
    """
    settings: Settings = live_app.state.settings
    email = f"t34-{uuid4().hex}@example.com"
    user_id = await _seed_user_with_a_production_cost_hash(
        engine, production_hasher, email=email, password=A_STRONG_PASSWORD
    )
    client = AsyncClient(transport=ASGITransport(app=live_app), base_url="http://testserver")
    try:
        turnarounds, wall_clock_seconds = await _hammer_logins_and_sample(client, settings, email)
    finally:
        await client.aclose()
        await _delete_user(engine, user_id)

    assert len(turnarounds) >= _SAMPLE_FLOOR, (
        f"only {len(turnarounds)} /health/live samples were taken during the eight concurrent "
        "logins — the sampler returned before reaching its own floor, which should be impossible; "
        "see loop_liveness.hammer_health_live_until_floor"
    )
    fraction = unavailable_fraction(turnarounds, wall_clock_seconds)
    p50 = statistics.median(turnarounds)
    assert fraction < _UNAVAILABLE_FRACTION_BUDGET, (
        f"the loop's unavailable fraction was {fraction:.4f} during eight concurrent logins "
        f"(budget {_UNAVAILABLE_FRACTION_BUDGET}; n={len(turnarounds)}, wall_clock="
        f"{wall_clock_seconds:.2f}s, p50={p50 * 1000:.2f}ms, max={max(turnarounds) * 1000:.2f}ms) "
        "— the event loop was blocked"
    )


async def _hammer_logins_and_sample(
    client: AsyncClient, settings: Settings, email: str
) -> tuple[list[float], float]:
    """Runs the sampler and the eight login workers together, and returns the sampler's turnarounds
    plus the wall-clock span of the whole window (module docstring: `unavailable_fraction` needs
    both, and the wall-clock half can only be measured by the caller — the sampler itself only ever
    times individual requests, never its own batch). Factored out so the mutation run below can call
    exactly this and nothing else — the same procedure, unmutated except for the one line under test.
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

    started = time.perf_counter()
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
    turnarounds = await asyncio.wait_for(hammer_task, timeout=30)
    wall_clock_seconds = time.perf_counter() - started
    return turnarounds, wall_clock_seconds
