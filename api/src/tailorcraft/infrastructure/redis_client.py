"""Redis client construction.

One tiny module for one reason: `redis.asyncio.from_url` is untyped in the package's own stubs, so
under `mypy --strict` every call site would need its own `# type: ignore`. Ignores scattered across a
codebase stop being read; one ignore, in the single place that constructs a client, stays visible and
disappears the day upstream annotates the function.
"""

from __future__ import annotations

import redis.asyncio as aioredis


def create_redis(url: str) -> aioredis.Redis:
    """Build an async Redis client.

    Redis is the Celery broker, the result backend, the cache and the rate-limiter store here — so
    "Redis is down" is "no exports and no purges", not "slightly slower" (ADR-0005).
    """
    # `from_url` is genuinely untyped upstream; the return type is correct and asserted by the
    # signature above.
    return aioredis.from_url(url)  # type: ignore[no-untyped-call,no-any-return]
