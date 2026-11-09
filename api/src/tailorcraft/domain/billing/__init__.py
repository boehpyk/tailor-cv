"""The `billing` bounded context: a registered user's credits, as an append-only history (ADR-0042).

A ninth context, and neither `identity` nor `tailoring`: a credit is a ledger with invariants of its
own (never negative, one charge and one refund per run), and a run must not know what it cost the
user. The two meet in the application layer only.

**What this package may import is a short list:** the standard library, `domain.shared`, and
`domain.identity` for `UserId`. No sibling context: a run is named by billing's own `ChargedRunRef`.
Billing rows are user-only; there is no guest owner here.

**This package `__init__` re-exports nothing**, the rule argued in `domain/shared/files.py`.
"""
