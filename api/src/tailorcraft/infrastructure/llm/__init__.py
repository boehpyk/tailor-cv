"""Adapters for the LLM boundary (ADR-0004): the prompt, the Gemini client, and the re-validation
that stands between the provider's answer and the domain.

Nothing outside this package imports the Gemini SDK. `parsing.py` is the exception in the other
direction: it imports nothing but the standard library and the domain, on purpose.
"""

from __future__ import annotations
