"""`TargetAddressPolicy` — the SSRF address-policy table (ADR-0012 obligations 2 and 3).

Pure domain-adjacent tests: no network, no DNS, no event loop, no mocks, no sockets. Every address
below is built directly with `ipaddress.IPv4Address` / `IPv6Address` — if this file ever needs a
socket or a resolver it has been written wrong.

Every assertion comes from the feature spec (AC-5, AC-6, AC-9) and the technical plan's "The SSRF
guard has no off switch" section — **not** from running the code, which at the time of writing raises
`NotImplementedError` from `allows` on purpose (docs/sdlc.md §2). The technical plan calls this the
highest-value test in the slice: it is the one that fails if somebody simplifies the policy.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from ipaddress import IPv4Address, IPv6Address

import pytest

from tailorcraft.infrastructure.posting.address_policy import TargetAddressPolicy

# --- strict(): the accept/reject table -------------------------------------------------------------
#
# The table is the point (P-11). A guard with one example per branch is a guard nobody has actually
# read. Every row here is a single resolved address; the multi-address cases (P-12, AC-6) live in
# their own section below because that is the assertion the spec calls out as the one that matters
# most.

_BLOCKED: list[tuple[IPv4Address | IPv6Address, str]] = [
    (IPv4Address("127.0.0.1"), "loopback-v4"),
    (IPv4Address("127.0.0.53"), "loopback-v4-resolver-stub"),
    (IPv6Address("::1"), "loopback-v6"),
    (IPv4Address("10.0.0.1"), "rfc1918-10-slash-8"),
    (IPv4Address("172.16.0.1"), "rfc1918-172-16-slash-12-bottom"),
    (IPv4Address("172.31.255.255"), "rfc1918-172-16-slash-12-top"),
    (IPv4Address("192.168.1.1"), "rfc1918-192-168-slash-16"),
    (IPv4Address("169.254.0.1"), "link-local-v4"),
    (IPv6Address("fe80::1"), "link-local-v6"),
    (IPv6Address("fd00::1"), "unique-local-v6"),
    (IPv4Address("0.0.0.0"), "unspecified-v4"),  # noqa: S104 -- an address under test, not a bind
    (IPv6Address("::"), "unspecified-v6"),
    (IPv4Address("224.0.0.1"), "multicast-v4"),
    (IPv4Address("240.0.0.1"), "reserved-v4"),
    (IPv4Address("100.64.0.1"), "cgnat-bottom"),
    (IPv4Address("100.127.255.255"), "cgnat-top"),
]

_ALLOWED: list[tuple[IPv4Address | IPv6Address, str]] = [
    (IPv4Address("93.184.216.34"), "public-v4-example-com"),
    (IPv4Address("1.1.1.1"), "public-v4-cloudflare-dns"),
    (IPv4Address("8.8.8.8"), "public-v4-google-dns"),
    (
        IPv6Address("2606:2800:220:1:248:1893:25c8:1946"),
        "public-v6-example-com",
    ),
]


@pytest.mark.parametrize(
    "address",
    [pytest.param(addr, id=name) for addr, name in _BLOCKED],
)
def test_strict_policy_blocks_unsafe_single_address(
    address: IPv4Address | IPv6Address,
) -> None:
    policy = TargetAddressPolicy.strict()

    assert policy.allows([address]) is False


@pytest.mark.parametrize(
    "address",
    [pytest.param(addr, id=name) for addr, name in _ALLOWED],
)
def test_strict_policy_allows_public_single_address(
    address: IPv4Address | IPv6Address,
) -> None:
    policy = TargetAddressPolicy.strict()

    assert policy.allows([address]) is True


# --- The two addresses that get their own test, not just a table row -------------------------------


def test_strict_policy_blocks_the_cloud_metadata_endpoint() -> None:
    """`169.254.169.254` is the single most important address in this file.

    It is a link-local address, so it would be caught by the link-local rule alone even without a
    dedicated test — but the whole reason obligation 3 of ADR-0012 exists is this one address: on
    several cloud providers it hands out credentials to anyone who asks from inside the box, no
    authentication required. This is the address SSRF exists to reach. A change that narrows the
    link-local range and happens to leave this one address reachable is the single worst way this
    policy could regress, and it deserves an assertion that names it, not just a spot in a table an
    editor might trim.
    """
    policy = TargetAddressPolicy.strict()

    assert policy.allows([IPv4Address("169.254.169.254")]) is False


def test_strict_policy_blocks_ipv4_mapped_loopback() -> None:
    """`::ffff:127.0.0.1` is the check people skip.

    Written as plain text this is a 128-bit address with no obviously "special" prefix, and a naive
    range check over IPv6 blocks would wave it through as an ordinary global address. But it is the
    IPv4-mapped form of `127.0.0.1`: `ipaddress.ip_address` parses it as an `IPv6Address`, and only
    unwrapping `.ipv4_mapped` *first* (ADR-0012 obligation 3) reveals the loopback address underneath.
    Skip that unwrap and the socket ends up connecting to 127.0.0.1 while every table row above,
    checked against the wrong representation, reports the address as safe.
    """
    policy = TargetAddressPolicy.strict()

    assert policy.allows([IPv6Address("::ffff:127.0.0.1")]) is False


def test_strict_policy_allows_the_address_just_past_the_rfc1918_slash_12() -> None:
    """`172.32.0.1` is the classic off-by-one on `172.16.0.0/12`.

    `172.16.0.0/12` covers `172.16.0.0` through `172.31.255.255` — the top two bits of the second
    octet fixed at `01`, not the whole `172.x.x.x` space. A hand-rolled check that tests
    `octet_two in range(16, 32)` gets this right; one that tests `first_octet == 172` blocks a public
    /8 far larger than the private range it meant to cover, and this is the address that catches it.
    """
    policy = TargetAddressPolicy.strict()

    assert policy.allows([IPv4Address("172.32.0.1")]) is True


def test_strict_policy_allows_the_address_just_past_the_cgnat_slash_10() -> None:
    """`100.128.0.1` is the same off-by-one shape as `172.32.0.1`, over the CGNAT range.

    `100.64.0.0/10` (RFC 6598) ends at `100.127.255.255`. A check that rounds the boundary to a whole
    octet (`first_octet == 100`) blocks public addresses in `100.128.0.0/9` that were never part of
    the carrier-grade-NAT allocation.
    """
    policy = TargetAddressPolicy.strict()

    assert policy.allows([IPv4Address("100.128.0.1")]) is True


# --- Multi-address resolution (AC-6 / P-12) — the assertion that matters most -----------------------
#
# A hostname routinely resolves to several addresses. `allows` takes a sequence specifically so the
# caller cannot express "check only the first one" without deliberately slicing the list first.


def test_blocked_address_second_in_the_list_still_blocks_the_whole_fetch() -> None:
    """Checking only the first resolved address is the exact bug AC-6 exists to prevent.

    A DNS record can genuinely return a public address first and a blocked one second — this is not
    a contrived ordering, it is the shape a real record takes when an attacker controls one A record
    among several. A policy that stops at the first `is True` result opens exactly this door.
    """
    policy = TargetAddressPolicy.strict()

    addresses: list[IPv4Address | IPv6Address] = [
        IPv4Address("93.184.216.34"),
        IPv4Address("127.0.0.1"),
    ]

    assert policy.allows(addresses) is False


def test_blocked_address_first_in_the_list_still_blocks_the_whole_fetch() -> None:
    """The mirror of the case above: blocked first, public second — fails whichever end you check
    from, so neither a first-address nor a last-address shortcut passes this table.
    """
    policy = TargetAddressPolicy.strict()

    addresses: list[IPv4Address | IPv6Address] = [
        IPv4Address("127.0.0.1"),
        IPv4Address("93.184.216.34"),
    ]

    assert policy.allows(addresses) is False


def test_all_public_addresses_in_a_multi_address_record_are_allowed() -> None:
    policy = TargetAddressPolicy.strict()

    addresses: list[IPv4Address | IPv6Address] = [
        IPv4Address("93.184.216.34"),
        IPv4Address("1.1.1.1"),
    ]

    assert policy.allows(addresses) is True


def test_empty_address_list_is_blocked() -> None:
    """ "Nothing resolved" is not "everything is fine".

    A policy that returned `True` for an empty sequence would turn a resolution bug — a resolver
    returning no addresses for a reason nobody anticipated — into an open door: whatever code called
    `allows([])` expecting "no results" would instead be told the (nonexistent) target is safe.
    """
    policy = TargetAddressPolicy.strict()

    assert policy.allows([]) is False


# --- The permissive seam and the no-off-switch guarantee (AC-9) ------------------------------------


def test_permissive_policy_allows_loopback() -> None:
    """`TargetAddressPolicy(allow_private=True)` is the seam the adapter's own tests use to reach a
    stub server on loopback — constructed only inside test modules, never under `api/src/`.
    """
    policy = TargetAddressPolicy(allow_private=True)

    assert policy.allows([IPv4Address("127.0.0.1")]) is True


def test_strict_sets_allow_private_false() -> None:
    """`strict()` is the only policy production ever builds. If someone changes its default to
    `allow_private=True`, this is the test that turns that into a red rather than a silent
    regression (AC-9).
    """
    policy = TargetAddressPolicy.strict()

    assert policy.allow_private is False


def test_policy_is_frozen() -> None:
    """A policy cannot be mutated into a more permissive one after construction by code that holds a
    reference to the fetcher's — the whole reason `TargetAddressPolicy` is a frozen dataclass.
    """
    policy = TargetAddressPolicy.strict()

    with pytest.raises(FrozenInstanceError):
        policy.allow_private = True  # type: ignore[misc]
