"""Use cases for the `scoring` bounded context (slice 5.1): resolve a pair, find its stored score,
and request a new one. A `POST` runs all three in that order, with the IP limiter between the find
and the request (plan §0.5). A `GET` runs only the find.

**This is the seam between `scoring` and its siblings.** `domain/scoring` imports neither `intake`
nor `posting` (ADR-0040 decision 1), so `ResolveScoringPair` authorizes through their own use cases
and converts at exactly one place, into `ScoringPair`. Past it, nothing here names a sibling.

**No logger and no commit here** (plan §2): `SpendLedgerPort`'s methods are durable on return, so
the call order in `RequestFitScore` is the transaction boundary.
"""
