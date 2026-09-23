"""The `identity` bounded context: a guest session, and — from slice 2.1 — a `User` and its `Login`s.

The three share a context and nothing else: a guest session is not a weak login (ADR-0008,
ADR-0010), and no base class, `Protocol` or union alias spans `GuestSession` and `User` (AC-1).
Re-exports nothing, for the reason `domain/intake/__init__.py` and `domain/export/__init__.py` give.
"""
