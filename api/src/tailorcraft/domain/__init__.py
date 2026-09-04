"""The domain layer: pure Python business model.

This package imports the standard library and nothing else — no FastAPI, no SQLAlchemy, no
Pydantic, no Celery (Constitution §4, ADR-0002). Three things enforce that, at three different
moments:

* `.claude/hooks/domain-purity-guard.sh` refuses the edit as you type it,
* `tests/unit/test_domain_purity.py` fails the suite for *any* non-stdlib import,
* `lint-imports` fails the build in CI.

If you are here because one of them stopped you: the thing you reached for belongs behind a Protocol
port in this layer, with its implementation in `tailorcraft.infrastructure`.
"""
