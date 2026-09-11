"""Adapters the `tailoring` context needs — the queue that carries a run to the worker.

The `LlmPort` adapter is deliberately NOT here: it lives in `infrastructure/llm/`, which is the
one package allowed to import the Gemini SDK (ADR-0004, and the import-linter contract that makes
it a gate rather than a convention).
"""
