"""Fixed-window rate limiting over Redis, and the client-IP helper it needs.

This module is not behind a domain port. Rate limiting is an HTTP-boundary concern (a router decides
whether to *accept* a request at all) rather than a business rule `application/` should know about,
so it is called directly from `infrastructure/api/routers/intake.py` — there is no `RateLimiterPort`
in `domain/` for the same reason there is no `HttpRequestPort`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import redis.asyncio as aioredis
import structlog
from fastapi import Request
from redis.exceptions import RedisError

log = structlog.get_logger(__name__)

_SECONDS_PER_HOUR = 3600

RateLimitScope = Literal["session", "ip"]


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Whether a request may proceed, and how long until the current window rolls over.

    `retry_after_seconds` is meaningful even when `allowed` is `True` — it is always "seconds to the
    edge of the current fixed window" — but the router only needs it on the `429` path, for the
    `Retry-After` header.
    """

    allowed: bool
    retry_after_seconds: int


class RateLimiterUnavailable(Exception):
    """Redis was unreachable and this limiter is configured to fail closed.

    Not a `DomainError`: rate limiting is an HTTP-boundary concern and the domain has no idea it
    exists (see the module docstring). The router maps this to 503 `rate_limit_unavailable`.

    Carries the namespace and nothing else — never the identifier, which for the IP scope is the
    client's address (Constitution §8).
    """


class RedisFixedWindowRateLimiter:
    """`INCR` + `EXPIRE` on one key per (namespace, scope, identifier, hour) — a classic fixed
    window. Not a sliding window or a token bucket: a fixed window can let up to 2x the limit through
    across a window boundary, which is an accepted, documented imprecision for an endpoint whose
    entire budget is 10-30 requests/hour, not a case for the extra Lua script a sliding-window
    implementation needs.

    The key layout is `rl:<namespace>:<scope>:<identifier>:<epoch_hour>` — e.g.
    `rl:intake:upload:session:0192f0a1-...:486312`. The epoch hour is baked into the key rather than
    read back out of Redis, so the key self-expires: a window's key is simply never reused once its
    hour has passed, and `EXPIRE` is what reclaims it instead of a background sweep.
    """

    def __init__(self, redis: aioredis.Redis, namespace: str, *, fail_open: bool = True) -> None:
        """`fail_open` decides what an unreachable Redis means for this limiter.

        The default is `True` — the existing behaviour, unchanged, for every caller that had one
        before slice 1.2. A flag on this class rather than a second class, because the counting is
        identical and only the answer to one exception differs; two classes would be two copies of
        the `INCR`/`EXPIRE` logic kept in step by hand.

        **The asymmetry between the two settings is the interesting part, and it generalizes.**
        Slice 1.1 recorded the question as a two-point spectrum: the upload limiter fails open
        because the cost of an unlimited request is our own disk and CPU — real, but ours and
        bounded — while 1.3's tailoring limiter must fail closed because the cost is money paid to a
        third party. A job-posting *fetch* is a third point and it sits with 1.3, not with 1.1: the
        cost is **someone else's infrastructure, spent from our IP address**. An unbounded outbound
        endpoint with no backstop is how a server ends up on a job board's blocklist, and it is also
        the shape in which we are used as an anonymizing fetch proxy or an SSRF prober.

        The rule that generalizes, and the one to apply to the next limiter: *fail open when the
        cost is ours and bounded; fail closed when the cost is money or somebody else's.*
        """
        self._redis = redis
        self._namespace = namespace
        self._fail_open = fail_open

    @property
    def namespace(self) -> str:
        """The key namespace this limiter counts under (`tailoring:create`, …).

        Read-only and exposed so a router can name it in its own log line (G-11 logs `scope` and
        `namespace`) without writing the literal a second time — the composition root in `deps.py`
        stays the one place each namespace string exists.
        """
        return self._namespace

    async def check(self, scope: RateLimitScope, identifier: str, limit: int) -> RateLimitDecision:
        """Record one hit for `identifier` in the current hour's window and report whether it is
        within `limit`.

        On `redis.RedisError` this either fails **open** (serves the request, logs one warning) or
        fails **closed** (raises `RateLimiterUnavailable`), according to the `fail_open` flag given
        to the constructor — see `__init__` for why the two exist and which cost justifies which.
        """
        now = int(time.time())
        epoch_hour = now // _SECONDS_PER_HOUR
        seconds_to_edge = _SECONDS_PER_HOUR - (now % _SECONDS_PER_HOUR)
        redis_key = f"rl:{self._namespace}:{scope}:{identifier}:{epoch_hour}"

        try:
            count = await self._redis.incr(redis_key)
            if count == 1:
                # First hit of this window for this key: arm the TTL so the key (and the counter)
                # disappear on their own once the hour rolls over. A `count == 1` check rather than
                # an unconditional EXPIRE avoids resetting the TTL on every hit, which would let a
                # steady stream of requests keep the window alive indefinitely.
                await self._redis.expire(redis_key, seconds_to_edge)
        except RedisError as exc:
            # Deliberately no `identifier` in this log line: for scope="ip" the identifier *is* the
            # client IP, and this module has no `base_cv_id` to pair it with — but keeping that habit
            # here, rather than only where the pairing is possible, is what stops it from ever
            # appearing (F-16, the privacy table in feature-spec.md).
            log.warning(
                "rate_limit.unavailable",
                scope=scope,
                namespace=self._namespace,
                error=type(exc).__name__,
                fail_open=self._fail_open,
            )
            if not self._fail_open:
                # The caller cannot serve this request safely without a working counter, so it does
                # not serve it at all. `from None`, not `from exc`: this frame's locals include
                # `identifier`, which for the IP scope IS the client's address, and a chained
                # exception keeps that frame reachable from a Sentry report.
                raise RateLimiterUnavailable(self._namespace) from None
            return RateLimitDecision(allowed=True, retry_after_seconds=seconds_to_edge)

        return RateLimitDecision(allowed=count <= limit, retry_after_seconds=seconds_to_edge)


def client_ip(request: Request, trusted_proxy_hops: int) -> str:
    """Resolve the request's client IP from `X-Forwarded-For`, trusting exactly
    `trusted_proxy_hops` hops.

    This is the one place that reconstructs a client IP, and it must stay the *only* place: nginx
    forwards `X-Forwarded-For` and must **not** run `ngx_http_realip_module` (CLAUDE.md's
    infrastructure footguns) — two layers each reconstructing "the real client" independently is how
    a rate limiter ends up keyed on the proxy's own address, collapsing every visitor into one
    global bucket instead of one per visitor.

    `X-Forwarded-For` accumulates **left to right** as a request crosses proxies: each proxy appends
    the peer address it received the connection from, so the header reads
    `client, proxy_1, proxy_2, …, proxy_(n-1)` by the time it reaches us, and *our own* reverse proxy
    (nginx) appends one more hop on its way in. With exactly one trusted hop in front of this process
    (nginx itself, per `trusted_proxy_hops = 1`), the address nginx itself observed — the real client,
    assuming no other trusted proxy sits in front of nginx — is the **last** entry, so the client's
    address is taken `trusted_proxy_hops` entries from the right-hand end, never the first (client-
    supplied, and therefore spoofable) entry.
    """
    if trusted_proxy_hops >= 1:
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
            if len(hops) >= trusted_proxy_hops:
                return hops[-trusted_proxy_hops]

    # No header, a malformed one, or fewer hops than we trust (dev with no proxy in front, or a
    # misconfigured deploy) — fall back to the socket peer, which is the honest answer to "who
    # actually connected to this process" in that situation.
    if request.client is not None:
        return request.client.host
    return "unknown"
