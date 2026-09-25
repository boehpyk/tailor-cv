"""Unit tests for `infrastructure/rate_limit.py::client_ip` — /verify round 1 finding 5.

Pure: `client_ip` reads only `request.headers` and `request.client`, so a hand-built `fastapi.Request`
over a raw ASGI scope is the whole fixture — no app, no database, no event loop.

**Production is client → Traefik → nginx → api** (CLAUDE.md, infrastructure footguns: nginx must not
run `ngx_http_realip_module`, so `X-Forwarded-For` reaches this process exactly as the two proxies
built it). Each proxy *appends* the peer address it saw, so with two trusted hops in front of this
process the header reads `client, traefik` by the time api sees it, and the client's own address is
two entries from the right — `trusted_proxy_hops=2`, not the `1` the settings default still carries
today (that default is `devops`'s to raise when the two-proxy chain is deployed; this file pins what
the function does at each hop count, not which count production is configured with).

**Proven by mutation** (CLAUDE.md: "a performance or liveness assertion is a claim about a mechanism,
and the only proof is a mutation" — the same standard applied here to a security-relevant parsing
rule). `client_ip`'s `return hops[-trusted_proxy_hops]` was changed locally to `return hops[0]` — the
"trust the leftmost, client-supplied entry" bug this function exists to avoid — and back. Observed,
not assumed:

- `test_two_trusted_hops_returns_the_third_from_last_entry_the_client` stayed GREEN: its 2-entry
  header (`client, traefik`) happens to have `hops[0] == hops[-2]`, so this assertion alone cannot
  tell the mutation from the original — recorded here rather than silently relying on it to catch
  anything.
- `test_one_trusted_hop_on_a_two_proxy_chain_returns_the_wrong_address` went RED:
  `assert '203.0.113.9' == '10.0.0.1'` — the mutation returns the client's own address regardless of
  `trusted_proxy_hops`, so the test documenting *today's* misconfigured-hop-count answer (Traefik's
  address) no longer matched.
- `test_a_forged_leading_entry_is_still_ignored_under_two_trusted_hops` went RED:
  `assert '9.9.9.9' == '203.0.113.9'` — for `forged, client, traefik` the mutation returns the
  client-forged `"9.9.9.9"` instead of the real client, exactly the spoofing this test exists to
  catch.
- The other two (`test_no_header_falls_back_to_the_socket_peer`,
  `test_fewer_hops_than_trusted_falls_back_to_the_socket_peer`) stayed GREEN, correctly — neither
  exercises the mutated line (no header, or too few hops to reach the `return` at all).

So the file as a whole is sensitive to this mutation (2 of 5 cases catch it) even though no single
assertion is a complete proof by itself — worth stating plainly rather than overclaiming a single
test's power. Restored byte-exact afterwards
(`git diff --quiet api/src/tailorcraft/infrastructure/rate_limit.py`).
"""

from __future__ import annotations

from fastapi import Request

from tailorcraft.infrastructure.rate_limit import client_ip


def _request(
    *, forwarded_for: str | None = None, client_host: str | None = "203.0.113.9"
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("utf-8")))
    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": (client_host, 12345) if client_host is not None else None,
    }
    return Request(scope)


def test_two_trusted_hops_returns_the_third_from_last_entry_the_client() -> None:
    """Production shape: `client, traefik` after Traefik and nginx each append one hop.
    `trusted_proxy_hops=2` must return the client, not either proxy."""
    request = _request(forwarded_for="203.0.113.9, 10.0.0.1")

    assert client_ip(request, trusted_proxy_hops=2) == "203.0.113.9"


def test_one_trusted_hop_on_a_two_proxy_chain_returns_the_wrong_address() -> None:
    """**This documents the bug, not the desired behaviour.** `trusted_proxy_hops` defaults to `1`
    (nginx alone); deployed behind Traefik *and* nginx with that default unchanged, `client_ip`
    returns Traefik's own address, not the visitor's — every visitor through the same Traefik
    collapses into one rate-limit bucket. Raising `trusted_proxy_hops` to `2` for that topology is
    `devops`'s job (CLAUDE.md); this test exists so nobody discovers the collapse by reading a rate
    limiter graph."""
    request = _request(forwarded_for="203.0.113.9, 10.0.0.1")

    assert client_ip(request, trusted_proxy_hops=1) == "10.0.0.1"


def test_a_forged_leading_entry_is_still_ignored_under_two_trusted_hops() -> None:
    """A client can put anything in front of its own address — `X-Forwarded-For` accumulates
    left-to-right, and a proxy only ever *appends*, so nothing stops a client from sending
    `X-Forwarded-For: <anything>` itself. With `trusted_proxy_hops=2`, the answer must still come
    from the right-hand end (Traefik's own view of who connected to it), never from the client-
    controlled front of the list."""
    request = _request(forwarded_for="9.9.9.9, 203.0.113.9, 10.0.0.1")

    assert client_ip(request, trusted_proxy_hops=2) == "203.0.113.9"


def test_no_header_falls_back_to_the_socket_peer() -> None:
    """Dev, with no proxy in front at all — the honest answer is whoever actually connected."""
    request = _request(forwarded_for=None, client_host="127.0.0.1")

    assert client_ip(request, trusted_proxy_hops=1) == "127.0.0.1"


def test_fewer_hops_than_trusted_falls_back_to_the_socket_peer() -> None:
    """A malformed or missing chain (one hop, `trusted_proxy_hops=2`) must not index into a list
    that is too short — the honest fallback is the socket peer, not a wrapped/negative index."""
    request = _request(forwarded_for="203.0.113.9", client_host="10.0.0.1")

    assert client_ip(request, trusted_proxy_hops=2) == "10.0.0.1"
