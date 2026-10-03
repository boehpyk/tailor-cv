"""The one SHA-256 helper behind every opaque token this system stores by hash (ADR-0020 §7).

Two tokens are stored this way: the refresh cookie (`api/refresh_cookie.py`, slice 2.1) and the
one-time link token (`identity/one_time_tokens.py`, slice 2.5). Both are 256 bits from the OS CSPRNG,
both are looked up by their hash, and both must hash **identically** at mint and at lookup. One
function rather than two, so the two sides of a lookup can never drift apart by an encoding or a
digest choice made in one place and not the other.
"""

from __future__ import annotations

import hashlib

from tailorcraft.domain.identity.value_objects import TokenHash


def sha256_token_hash(token: str) -> TokenHash:
    """Hash a plaintext opaque token for storage or lookup.

    **Unsalted SHA-256, not argon2, and that is correct rather than an oversight** (ADR-0010 §3,
    restated by ADR-0020 §7). A KDF exists to make *guessing* expensive for a low-entropy secret a
    person chose. These tokens are 256 bits from a CSPRNG: there is no dictionary to run against a
    stolen hash, so a slow hash buys nothing, while it would cost 64 MiB and about 50 ms of argon2 on
    every refresh and every link click. A salt is the same argument one level down: it defeats
    precomputation across many low-entropy secrets, and there is no rainbow table for a random
    256-bit value. A KDF with a random salt also could not be *looked up* by at all.
    """
    return TokenHash(hashlib.sha256(token.encode("utf-8")).hexdigest())
