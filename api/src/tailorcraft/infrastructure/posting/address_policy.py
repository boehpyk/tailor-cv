"""`TargetAddressPolicy` — which IP addresses this process is willing to open a socket to.

ADR-0012 obligations 2 and 3. This is the whole SSRF range decision, extracted into a **pure object
with no network, no DNS and no I/O of any kind**, so that it can be tested as a table of roughly two
dozen addresses rather than inferred from the behaviour of a live fetch. The technical plan calls the
resulting test the highest-value one in the slice, and the reason is simple: it is the test that
fails if somebody simplifies the policy.

**SKELETON (T19).** `allows` raises `NotImplementedError`; T21 fills it in after `qa` records the
red. This module is red-first even though it is infrastructure, which is a deliberate exception to
the tier table in docs/sdlc.md §2: the test-after tiers are the ones whose *shape is discovered
against a library*, and there is no library here to discover anything against. The rule is fixed by
the specification in advance, exactly like the HTTP contract, so it is written down as tests first.

**There is no way to switch this off** (ADR-0012). No `allow_private_fetch_targets`, no "dev mode",
no setting anywhere that weakens it — a flag that turns off a security control is a flag someone
eventually sets in production. The seam that makes the *fetcher* testable is a constructor argument
with `strict()` as its default; the permissive policy the adapter's own end-to-end tests need is
built **only inside the test module**, and a wiring test asserts that `deps.get_job_posting_fetcher`
builds the strict one (AC-9).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address

# Carrier-grade NAT (RFC 6598). `ipaddress` has no `is_cgnat` property, so this is the one range the
# policy has to name for itself — which is exactly why it is a named constant with a citation rather
# than a magic string buried in a comparison.
_CGNAT_RANGE = IPv4Network("100.64.0.0/10")


@dataclass(frozen=True, slots=True)
class TargetAddressPolicy:
    """Decides whether every address a hostname resolved to is safe to connect to.

    Frozen, so a policy cannot be mutated into a more permissive one after construction by code that
    holds a reference to the fetcher's.
    """

    allow_private: bool

    @classmethod
    def strict(cls) -> TargetAddressPolicy:
        """The only policy production ever builds. Refuses every address that is not public.

        A classmethod rather than a bare `TargetAddressPolicy()` default so that the *name* appears
        at the wiring site: `TargetAddressPolicy.strict()` reads as a decision, while a bare
        constructor call reads as a formality and invites someone to add a parameter to it.
        """
        return cls(allow_private=False)

    def allows(self, addresses: Sequence[IPv4Address | IPv6Address]) -> bool:
        """True only if **every** address in `addresses` is safe to connect to.

        Takes a sequence, not a single address, and that is the whole point of the signature. A
        hostname routinely resolves to several addresses, and a guard that checks only the first one
        passes a DNS record of `[93.184.216.34, 127.0.0.1]` and then connects to the second (AC-6).
        Making the parameter plural means the caller cannot express the unsafe check without
        deliberately slicing the list first.

        An empty sequence is **not** allowed: "nothing resolved" is not "everything is fine", and a
        policy that returned `True` for it would turn a resolution bug into an open door.
        """
        raise NotImplementedError
