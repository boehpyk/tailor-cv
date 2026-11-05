"""The `scoring` bounded context: one owner's stored assessment of one CV and job posting pair.

An eighth context (ADR-0040): a score is neither a tailoring run nor a document, and it must later
judge a tailored CV through the same port, so its port takes text types of its own.

**What this package may import is a short list** (`tests/unit/scoring/test_import_allowlist.py`): the
standard library, `domain.shared`, and `domain.identity` for `Owner`. No sibling context.
`application/scoring/` is where `intake`'s and `posting`'s types meet this context's.

**This package `__init__` re-exports nothing**, the rule argued in `domain/shared/files.py`.
"""
