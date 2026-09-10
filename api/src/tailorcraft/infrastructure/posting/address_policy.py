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
from typing import Final

# Carrier-grade NAT (RFC 6598). `ipaddress` has no `is_cgnat` property, so this is the one range the
# policy has to name for itself — which is exactly why it is a named constant with a citation rather
# than a magic string buried in a comparison.
_CGNAT_RANGE = IPv4Network("100.64.0.0/10")

# The ONLY ranges `allow_private=True` widens: RFC 1918 plus loopback. Named explicitly rather than
# reached through `is_private`, and that is not a style preference — `IPv4Address.is_private`
# INCLUDES link-local (`169.254.0.0/16`), verified against the installed CPython:
#
#     IPv4Address("169.254.169.254").is_private  ->  True
#
# so a permissive branch written as `is_loopback or is_private` silently re-admits the cloud
# metadata endpoint, which is the one address this whole module exists to keep out of reach. That
# exact bug was written here first and caught by driving the fetcher against a stub server; the
# named list is what makes it unwritable.
_TEST_WIDENED_RANGES: Final = (
    IPv4Network("127.0.0.0/8"),
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)


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
        if not addresses:
            return False
        return all(self._is_allowed(address) for address in addresses)

    def _is_allowed(self, address: IPv4Address | IPv6Address) -> bool:
        """One address, under this policy's setting.

        **`allow_private` widens the loopback and RFC-1918 ranges and NOTHING else**, which is
        narrower than the name might suggest and narrower than the first version of this class. That
        version short-circuited to `True` for every address, and two things were wrong with it.

        The first is testability: the adapter's end-to-end tests need a stub server on loopback, and
        AC-7 needs a redirect from that server to `169.254.169.254` to be **refused at the hop**. If
        the permissive policy allows everything, that criterion cannot be exercised at all — the
        redirect would be followed and the test would prove the opposite of what it claims.

        The second matters more. `169.254.169.254` is the cloud metadata endpoint, and with the
        old shape the test seam could reach it. Narrowing this means **no policy this codebase is
        able to construct — strict or permissive, in `src/` or in a test — will connect to a
        link-local, multicast, reserved, CGNAT or unspecified address.** The seam widens exactly the
        two ranges a local stub server actually lives in, and the address SSRF exists to reach stays
        out of reach in every configuration.
        """
        if not self.allow_private:
            return self._is_public(address)

        unwrapped = self._unwrap(address)
        if isinstance(unwrapped, IPv4Address) and any(
            unwrapped in widened for widened in _TEST_WIDENED_RANGES
        ):
            return True
        if isinstance(unwrapped, IPv6Address) and unwrapped.is_loopback:
            return True
        return self._is_public(address)

    @staticmethod
    def _unwrap(address: IPv4Address | IPv6Address) -> IPv4Address | IPv6Address:
        """Judge an IPv4-mapped IPv6 address as the IPv4 address the kernel will connect to."""
        if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
            return address.ipv4_mapped
        return address

    @staticmethod
    def _is_public(address: IPv4Address | IPv6Address) -> bool:
        """Whether one address is safe to open a socket to.

        **Why the `ipv4_mapped` unwrap comes first — and what it does NOT do.** The received wisdom
        is that `::ffff:127.0.0.1` slips past an IPv6-only guard because "as an IPv6 address it is
        not `::1`". Measured against the installed CPython, that is **false**, and the measurement
        is recorded here because the false version is what a reader will otherwise assume:

            IPv6Address("::ffff:127.0.0.1").is_loopback   -> True
            IPv6Address("::ffff:10.0.0.1").is_private     -> True
            IPv6Address("::ffff:169.254.169.254").is_link_local -> True

        `ipaddress` already resolves mapped addresses for every property below. So the unwrap buys
        nothing for any of them — and if that were the whole story this line would be cargo cult.

        It is load-bearing for exactly one check, and it is **ours**: the CGNAT range at the bottom.
        `100.64.0.0/10` is the one block `ipaddress` has no property for (it is "shared address
        space", not private), so this method tests it by hand with `address in _CGNAT_RANGE` — and
        that test is `IPv4Network`-based, so it can only ever match an `IPv4Address`. Without the
        unwrap, `::ffff:100.64.0.1` reaches it as an `IPv6Address`, skips the `isinstance` guard,
        and is **allowed**:

            ::ffff:100.64.0.1  ->  no property fires, not an IPv4Address  ->  slips through

        That is the single address in the whole space that the unwrap saves, which makes it worth a
        paragraph rather than a word. Keeping the unwrap first also means any future hand-written
        range — and there will be one, the day IANA designates another block — is automatically
        judged against the address the kernel will actually connect to, rather than needing this
        discovery to be made again.

        The properties themselves are `ipaddress`'s own rather than a hand-written list of CIDR
        blocks, deliberately: the standard library tracks the IANA special-purpose registries, and a
        hand-rolled list is a snapshot that rots silently.
        """
        address = TargetAddressPolicy._unwrap(address)

        if (
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            return False

        # Carrier-grade NAT (RFC 6598). `is_private` does not cover it — `100.64.0.0/10` is
        # "shared address space", not private — but a target inside it is another subscriber on the
        # provider's network, not a public host, so it is refused for the same reason.
        return not (isinstance(address, IPv4Address) and address in _CGNAT_RANGE)
