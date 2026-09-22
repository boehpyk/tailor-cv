"""Operational adapters for the guest purge: the heartbeat and the mutual-exclusion lock.

**Neither is behind a domain port, and that is ADR-0018 decision 8 rather than an omission.**
`infrastructure/rate_limit.py` set the precedent and its module docstring states the reasoning, which
transfers here word for word: rate limiting is an HTTP-boundary concern rather than a business rule
`application/` should know about, so there is no `RateLimiterPort` in `domain/`. A heartbeat and a
lock are facts about how this job is *operated* — once at a time, observably — not about what
retention *means*. `PurgeExpiredGuestSessions` runs identically with or without either of them; the
entry point owns both, exactly as a router owns its rate limiter.

The alternative (a `PurgeHeartbeatPort` in `domain/retention/ports.py`) is recorded as OQ-9. It buys
a fake in tests that a plain class already gives, and it would make an operational fact a domain one.

This package holds **no** filesystem work: the orphan scanner lives beside `LocalFileStore` in
`infrastructure/files/`, because it shares that adapter's root and its layout knowledge.
"""

from __future__ import annotations
