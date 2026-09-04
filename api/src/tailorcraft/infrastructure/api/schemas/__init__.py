"""Pydantic models — the wire format, and nothing else.

Pydantic lives here and only here (ADR-0002). It is excellent at validating and serialising HTTP
payloads, and it is not a domain model: a `BaseModel` in `domain/` drags JSON aliases,
`model_config` and an HTTP-shaped validation error type into business rules, and makes the business
model depend on a library whose major upgrades have historically been rewrites.
"""
