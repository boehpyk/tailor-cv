# ADR-0012: Outbound HTTP is a guarded egress, not a client

- **Status:** Accepted
- **Date:** 2026-09-09
- **Extends:** ADR-0004 (everything external crosses a port), ADR-0009 (CPU-bound work goes to a
  thread). Supersedes nothing.
- **Applies to:** every outbound HTTP request this codebase will ever make to a host it did not
  choose. Slice 1.2's job-posting fetcher is the first; a company-logo fetch, a webhook callback and
  an LLM proxy would all be the second.

## Context

Slice 1.2 (`posting-job-description-intake`) adds the first line of code that makes an HTTP request
to **an address a stranger typed into a form**. That is a different activity from calling the Gemini
API, and the difference is not one of degree:

| | Calling Gemini | Fetching a job posting |
|---|---|---|
| Who chose the host | We did, at deploy time | An anonymous visitor, at request time |
| What the response is | A payload from a party we trust to be non-hostile | A document written to be parsed by whoever asks |
| What a bug costs | A failed tailoring run | A read of `169.254.169.254`, or our IP on a blocklist |

The name for the failure this ADR exists to prevent is **SSRF** — Server-Side Request Forgery. A
process that will fetch any URL on request is a proxy with our network position and our IP address.
Inside a container on a VDS that means the Docker bridge network (`postgres`, `redis`, every sibling
container), the loopback interface, the host's private LAN, and — on any cloud provider — the
unauthenticated metadata endpoint at `169.254.169.254`, which on several providers hands out
credentials to anyone who asks from inside the box.

The reflex is to write a `_is_safe_url()` helper next to the one call site. That is exactly the
shape that fails, for a reason worth naming: **the second caller does not know the helper exists.**
So this is an ADR rather than a comment in the adapter, and it is written as a contract every future
outbound call must satisfy rather than as a description of the one that exists today.

Two more forces:

- **A validation function that runs before a connection is not a guarantee about the connection.**
  Between "we checked the hostname resolves somewhere public" and "the socket connected" there is a
  second DNS lookup, and it can return a different answer. That is DNS rebinding, and it is the
  residual risk in every SSRF guard that checks a name instead of pinning an address.
- **A security control with an off switch gets switched off.** Not maliciously — by someone debugging
  a local integration at 6pm who sets `ALLOW_PRIVATE_FETCH_TARGETS=1` in a `.env` that outranks the
  image's `ENV` (CLAUDE.md's footgun list already records that `env_file:` wins on a real box).

## Decision

**Outbound HTTP to a caller-chosen host is a guarded egress with ten obligations.** All ten apply to
every such call, and an adapter that makes one is not finished until it satisfies all ten.

**1. The scheme allow-list lives in a type, not in an `if`.** A `SourceUrl` value object parses and
validates the URL in `__post_init__`, admitting exactly `http` and `https`. `file:///etc/passwd`,
`gopher://`, `javascript:` and `data:` are then not *rejected downstream* — they are
**unconstructable**, so no code path exists that could hold one. The type also refuses userinfo
(`http://user:pass@host/`), which is both a credential in a URL and the classic parser-confusion
trick: two parsers disagreeing about where the host ends is how a guard and a client end up looking
at different hosts.

**2. Resolve before you connect, and check every address you get back.** DNS resolution goes through
the event loop's `getaddrinfo` (never `socket.getaddrinfo` directly — it is blocking C, and the
loop's method is what puts it in the default executor). The address policy then judges **all** the
resolved addresses, not the first. A record returning `[93.184.216.34, 127.0.0.1]` otherwise passes a
first-address check and connects to the second.

**3. The address policy is a pure object with a table-driven test and no network.** It unwraps
`ipv4_mapped` *first* — so `::ffff:127.0.0.1` is judged as `127.0.0.1` rather than as a global IPv6
address, which is the check people skip — and then rejects loopback, private, link-local, multicast,
reserved, unspecified, and the CGNAT range `100.64.0.0/10`. `169.254.169.254` is named explicitly in
the test, because the cloud metadata endpoint is the thing SSRF is *for*.

The test is the highest-value one in the slice: it is what fails if someone simplifies the policy.
It includes `172.32.0.1` as **allowed**, because `172.16.0.0/12` ends at `172.31.255.255` and the
off-by-one that blocks all of `172.x` is the classic hand-rolled version of this check.

**4. The socket connects to an address the guard verified. The pin holds — this was measured, not
assumed.** The request is made to `https://<verified-ip>/<path>` with an explicit `Host:` header and
`extensions={"sni_hostname": <hostname>}`, so TCP goes to the checked address while TLS SNI and
certificate verification run against the *name*.

Verified against the installed `httpx 0.28.1` / `httpcore 1.0.9` on 2026-09-09, because OQ-2 required
this to be checked rather than hoped for. `httpcore`'s `AsyncHTTPConnection._connect` computes
`server_hostname = sni_hostname or self._origin.host`, and passes it to `start_tls` on a context whose
`check_hostname` is `True` — so the extension really does move certificate verification onto the
hostname. Three observations pin it down:

- pinned to `104.20.23.154` with `sni_hostname="example.com"` → **200**;
- the same request with **no** `sni_hostname` (so the cert is checked against the IP literal) →
  `ConnectError: SSLV3_ALERT_HANDSHAKE_FAILURE`;
- the same request with `sni_hostname="wrong.invalid"` → the same handshake failure.

One trap met while measuring, recorded because it will be met again: **the connection pool is keyed
on the origin, and the origin here is the IP.** A "control" request issued from the *same client*
after a successful pinned request reuses the established TLS connection and succeeds, which looks
exactly like verification having been skipped. Each observation above used a fresh `AsyncClient`. A
second trap: a hostname that *looks* wrong may not be — `not-the-right-host.example.org` verified
successfully against the presented certificate, because that certificate genuinely covers
`*.example.org`. Choose a name no certificate can cover (`.invalid`) when testing a negative.

The residual risk this removes is real DNS rebinding: with the pin there is no second lookup between
the check and the connection, so there is no window. What it does **not** remove: a host that is
legitimately public and serves us something hostile — that is what obligations 5–8 are for.

**5. Redirects are followed by hand, and every hop re-runs the whole guard.** The client is built
`follow_redirects=False`. Each `Location` is resolved against the current URL, parsed into a **new
`SourceUrl`** (so a redirect to `file://` dies on obligation 1) and put through obligations 1–4 in
full. At most three hops.

This is the row people forget and the one that is actually exploited: an entirely public,
reputable host that answers `302 Location: http://169.254.169.254/latest/meta-data/`. A guard that
validates the URL the user typed and then hands it to a client with `follow_redirects=True` has
validated nothing.

**6. `trust_env=False`.** `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` from the environment must not
silently reroute egress. The deeper reason is not routing hygiene: **a proxy resolves the hostname on
our behalf**, which moves the DNS lookup outside our process and defeats obligations 2–4 entirely.

**7. Timeouts are layered, and the outer one is the real bound.** Per-phase timeouts (`connect`,
`read`, `write`, `pool`) plus the whole operation wrapped in `asyncio.wait_for`. Per-phase timeouts
are precisely what a hostile server evades by trickling one byte before each read deadline; the outer
bound is what actually stops it.

**8. The size cap is enforced while streaming, on the decoded bytes.** Accumulate
`response.aiter_bytes()` and abort the moment the running total crosses the cap.
**`Content-Length` is never the mechanism** — it is a claim by a server we do not trust, and a
chunked response carries none at all. `aiter_bytes()` yields *decoded* bytes, which is the right side
of a gzip bomb to measure: the decoded size is what hurts.

**9. Parsing the response is CPU-bound work on a document a stranger chose, so it goes in a thread.**
ADR-0009's rule applies with one aggravation: in slice 1.1 the size of the work was chosen by *our
own user* within a cap we set; here it is chosen by a **remote server**. `asyncio.to_thread` under
`asyncio.wait_for`, and the byte cap enforced *before* the bytes reach the parser.

**10. Every failure leaving the port is a translated domain failure, guaranteed by an
`except Exception` floor.** The floor sits *beneath* the specific translations, which still run first
and carry the better reason. `Exception`, never `BaseException` — `asyncio.CancelledError` must still
cancel. It logs the exception's **fully-qualified type only** (never `str(exc)`, never `exc_info`: an
`httpx` or `lxml` message can quote the fetched document or the entire URL) and re-raises
**`from None`**, so the frame holding the response body is unreachable from a Sentry report —
`sentry_sdk` defaults `include_local_variables=True`, a setting neither `send_default_pii=False` nor
`max_request_body_size="never"` affects.

### And two rules about what must *not* exist

**There is no off switch.** No `allow_private_fetch_targets`, no "dev mode" bypass, no setting that
weakens the address policy. The seam that makes the adapter testable is a **constructor argument with
a strict default**; the permissive policy the adapter's own tests need is constructed *only inside
the test module*, and a test asserts that the production wiring builds the strict one. "Someone
loosened the default" is then a red test rather than a silent regression.

**We do not impersonate a browser.** The User-Agent is honest and identifying —
`TailorCraft/0.1 (+<public_base_url>)`. Constitution §5 closes the anti-bot road and FR-2 makes the
paste fallback the product's answer to a refusal. This is written down here because "just set a
Chrome UA" is the reflexive fix the first time a Cloudflare 403 appears, and the moment of temptation
is months after the moment of decision.

## Alternatives

- **A `_is_safe_url()` helper beside the call site.** Rejected: it is invisible to the second caller,
  and the second caller is the one who introduces the bug. A cross-cutting rule needs a document.
- **Check-then-connect, taking the rebinding residual and writing it down.** This was the fallback
  OQ-2 authorised if the pin turned out not to work. It does work (obligation 4), so the fallback is
  not taken — but it stays recorded as the honest option, because the day a transport cannot pin,
  shrinking the window and *documenting the residual* beats pretending the check was a guarantee.
- **An allow-list of permitted job-board domains.** Genuinely more secure, and rejected on product
  grounds: FR-2 is "paste a link", not "paste a link to one of nine sites we have heard of", and a
  user whose employer posts on its own careers page is exactly the user this product is for. The
  paste fallback already covers every host we cannot read.
- **Fetching through a third-party scraping/proxy service.** Rejected twice over: Constitution §5
  closes the anti-bot road, and it would also move the DNS resolution and the egress IP outside our
  control, which is obligation 6 in reverse.
- **A headless browser for pages that need JavaScript.** Rejected for this slice: it is a large
  attack surface, a large image, and a large CPU cost, all in service of a case the paste fallback
  already answers. If it ever returns, it returns as its own ADR — and it inherits all ten
  obligations, which a browser makes considerably harder to satisfy.
- **Trusting `Content-Length` for the size cap.** Rejected: see obligation 8. It is a claim by the
  party we are defending against.

## Consequences

- **`httpx` becomes a runtime dependency.** It was a dev-only dependency (the ASGI test client);
  importing it from runtime code while it is dev-only works locally and breaks the production image.
- **This ADR is a checklist for code review, not just a record.** A pull request adding an outbound
  call is reviewed against the ten obligations, and "it's only fetching a logo" is not an exemption —
  a logo URL is a caller-chosen URL.
- **The address policy will need revisiting if this product ever runs somewhere with a legitimate
  internal endpoint to fetch.** The answer then is a *separate* adapter with its own port and its own
  ADR, not a flag on this one. Widening a guard is how a guard stops being one.
- **The rate limiter in front of an outbound endpoint fails closed** (see slice 1.2's spec, and the
  rule it generalizes: *fail open when the cost is ours and bounded; fail closed when the cost is
  money or somebody else's infrastructure*). An unbounded outbound endpoint with no backstop is how a
  server ends up on a blocklist — and it is also the shape in which we are used as an anonymizing
  fetch proxy.
- **A host is loggable; a path, a query and a fragment are not.** The URL of a job posting identifies
  a specific job a specific person is applying to (Constitution §8). `SourceUrl.host` exists partly
  so the log line has something safe to carry, and a client IP never appears in the same log line as
  a host — that pairing is what links a person to their job hunt.
- **The three-hop redirect limit is a product decision as well as a security one.** Real job boards
  redirect once or twice (canonicalisation, a country splash). A chain longer than that is either
  broken or evasive, and the paste fallback is a better answer than following it.
