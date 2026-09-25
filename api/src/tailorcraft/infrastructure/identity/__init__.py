"""Adapters for the `identity` bounded context: the password hasher (ADR-0021), the access-token
signer (ADR-0008), and the failed-login log line. The only modules in the codebase that import
`argon2` and `jwt` live here."""

from __future__ import annotations
