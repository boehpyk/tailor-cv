"""`HttpxTrafilaturaFetcher` — the `JobPostingFetcherPort` adapter, and this codebase's first
outbound request to a host a stranger chose.

Everything ADR-0012 specifies lives here: the scheme re-assert, DNS-before-connect, the address
policy on every resolved address, the IP-pinned connect, the per-hop redirect re-check,
`trust_env=False`, layered timeouts under a hard outer bound, the streamed decoded-byte cap,
extraction off the event loop, and the `except Exception` floor. The domain says nothing about any
of it — `JobPostingFetcherPort.fetch` mentions no httpx, no timeout and no status code, which is
what makes it a port rather than a rename.

**Never log the fetched page, the posting text, or the URL's path, query or fragment**
(Constitution §8). A job-posting URL names the job a specific, probably anxious person is applying
for. Every log line here carries `source_host` and nothing else from the URL, and the catch-all logs
an exception's fully-qualified *type* — never `str(exc)`, which for httpx and lxml routinely quotes
the document or the whole URL.

**A host and a client IP never appear in the same log line.** That pairing links a person to their
job hunt; it is the direct analogue of 1.1's `base_cv_id`-plus-IP rule. Nothing here logs an IP at
all, including the resolved address of a *blocked* target — that is the single fact an SSRF prober
most wants confirmed.
"""

from __future__ import annotations

import asyncio
import socket
import time
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import TYPE_CHECKING, Final
from urllib.parse import urljoin

import httpx
import structlog
import trafilatura

from tailorcraft.domain.posting.errors import (
    EmptyJobPostingText,
    JobPostingFetchFailed,
    JobPostingTextTooLong,
    JobPostingTextTooShort,
    SourceHasNoReadableText,
    SourceNotHtml,
    SourceRejectedRequest,
    SourceResponseTooLarge,
    SourceTextTooLong,
    SourceTimedOut,
    SourceTooManyRedirects,
    SourceUnreachable,
    SourceUrlNotAllowed,
)
from tailorcraft.domain.posting.value_objects import (
    FetchedPosting,
    FetchFailureReason,
    JobPostingText,
    PostingTitle,
    SourceUrl,
)
from tailorcraft.infrastructure.posting.address_policy import TargetAddressPolicy

if TYPE_CHECKING:
    from tailorcraft.domain.posting.ports import JobPostingFetcherPort

log = structlog.get_logger(__name__)

# The media types worth trying to extract text from. A server-supplied claim used ONLY to decline
# early — see `_check_content_type` for why trusting it here is not the mistake slice 1.1 refused to
# make with an upload's Content-Type.
_ACCEPTABLE_MEDIA_TYPES: Final = frozenset({"text/html", "application/xhtml+xml", "text/plain"})

# Read the body in bounded pieces so the cap is checked against a running total rather than after
# the fact. `aiter_bytes` yields decoded bytes, which is the side of a gzip bomb that hurts.
_STREAM_CHUNK_BYTES: Final = 64 * 1024


class HttpxTrafilaturaFetcher:
    """Fetch one job posting from one URL, under every obligation in ADR-0012.

    `policy` is a constructor argument with a **strict default** and there is no setting anywhere
    that weakens it. That is the whole testability seam: the adapter's own end-to-end tests build a
    permissive policy *inside the test module*, nothing under `api/src/` ever does, and a wiring test
    asserts `deps.get_job_posting_fetcher` builds the strict one (AC-9). A flag that turns off a
    security control is a flag someone eventually sets in production.

    There is deliberately **no injectable resolver**. An earlier draft of this docstring claimed one
    existed; it did not, and the claim is removed rather than the parameter added, because the
    adapter's tests do not need it: `TargetAddressPolicy(allow_private=True)` widens loopback only,
    so a stub server on `127.0.0.1` is reachable with real DNS doing real work. Adding a resolver
    seam would mean the thing under test in an SSRF suite is a stub of the exact step the guard
    depends on.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: int,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        max_bytes: int,
        max_redirects: int,
        extraction_timeout_seconds: int,
        policy: TargetAddressPolicy | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._timeout_seconds = timeout_seconds
        self._connect_timeout_seconds = connect_timeout_seconds
        self._read_timeout_seconds = read_timeout_seconds
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._extraction_timeout_seconds = extraction_timeout_seconds
        # `None` -> strict, rather than `policy: TargetAddressPolicy = TargetAddressPolicy.strict()`
        # as a default argument: a mutable-looking default evaluated once at import time is a
        # different object shared by every instance, and while this one is frozen and harmless, the
        # habit is not. The `or` makes "no policy given" mean "the strict one" at call time.
        self._policy = policy or TargetAddressPolicy.strict()

    async def fetch(self, url: SourceUrl) -> FetchedPosting:
        """Fetch and extract one job posting, or raise a `JobPostingFetchFailed` subclass.

        The whole operation runs under one `asyncio.wait_for`. Per-phase timeouts (connect, read,
        write, pool) are what a hostile server evades by trickling one byte before each deadline;
        this outer bound is what actually stops it, and it is why the unit of work can be held open
        across a fetch with a known worst case (technical-plan.md's transaction note).
        """
        started_at = time.monotonic()
        try:
            fetched = await asyncio.wait_for(
                self._fetch_guarded(url), timeout=self._timeout_seconds
            )
        except TimeoutError as exc:
            self._log_outcome(url, started_at, outcome="timed_out", character_count=None)
            raise SourceTimedOut() from exc
        except JobPostingFetchFailed as exc:
            self._log_outcome(url, started_at, outcome=exc.reason.value, character_count=None)
            raise
        except httpx.TimeoutException as exc:
            # A PER-PHASE timeout: connect, read, write or pool. Distinct from the outer
            # `asyncio.wait_for` above, which bounds the whole operation — these fire first and
            # more often, and `httpx.TimeoutException` is NOT a subclass of the builtin
            # `TimeoutError`, so the handler above does not see them.
            #
            # This translation was MISSING in the first implementation, and the gap was invisible
            # for a specific reason worth recording: the only timeout test in the suite used a
            # slow-trickling server, which the ten-second outer bound catches before any per-phase
            # deadline expires. So the tested path went through `TimeoutError` and produced the right
            # answer, while every real per-phase timeout fell to the floor below and came back as
            # `fetcher_error` — a 502 where the contract says 504 (P-15). A test that exercises the
            # generous bound cannot tell you anything about the tight ones.
            self._log_outcome(url, started_at, outcome="timed_out", character_count=None)
            raise SourceTimedOut() from exc
        except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            # The connection never established, or the peer broke the protocol mid-response:
            # refused, reset, a TLS handshake failure, a truncated response (P-13/P-14).
            # `socket.gaierror` is translated at the resolution step in `_resolve`, before any
            # socket is opened.
            #
            # This is a better answer than the floor, and "better" is concrete: both are 502, but
            # the `code` differs, and `code` is the contract a client branches on. `fetcher_error`
            # says "something went wrong inside us"; `source_unreachable` says "that site did not
            # answer" — and only the second is a fact the user can act on.
            self._log_unreachable(url, started_at, exc)
            raise SourceUnreachable() from exc
        except Exception as exc:
            # THE FLOOR (ADR-0012 obligation 10), and it is load-bearing rather than defensive
            # habit. `JobPostingFetcherPort` promises a `JobPostingFetchFailed` subclass on EVERY
            # failure, and the exception surface underneath is httpx's tree (`ConnectError`,
            # `RemoteProtocolError`, `UnsupportedProtocol`, `LocalProtocolError`, `InvalidURL`, …)
            # plus lxml's plus `charset_normalizer`'s plus whatever `trafilatura` lets through — on
            # input a stranger chose. An allow-list is a bet that you enumerated all of it, and that
            # is the bet slice 1.1 lost when a corruption sweep found `KeyError`, `AttributeError`,
            # `ValueError` and `LimitReachedError` escaping the CV extractor into a 500. The
            # specific translations above and inside still run first and still carry the better
            # reason; this is only the floor beneath them.
            #
            # `Exception`, never `BaseException`: `asyncio.CancelledError` must still cancel the
            # request rather than being recorded as a fetch failure (P-26).
            #
            # Two privacy rules, both Constitution §8 rather than fussiness:
            #   1. Only the exception's TYPE is logged. An httpx or lxml message can quote the
            #      fetched document or the entire URL, path and query included.
            #   2. `from None`, not `from exc`. This frame's locals hold the accumulated response
            #      body; a chained exception keeps the original traceback — and therefore that
            #      frame — alive and reachable, and `sentry_sdk` defaults
            #      `include_local_variables=True`, which neither `send_default_pii=False` nor
            #      `max_request_body_size="never"` affects.
            self._log_unexpected_error(url, started_at, exc)
            raise JobPostingFetchFailed(FetchFailureReason.FETCHER_ERROR) from None

        self._log_outcome(
            url, started_at, outcome="success", character_count=fetched.text.character_count
        )
        return fetched

    # -- the guarded request -------------------------------------------------------------------

    async def _fetch_guarded(self, url: SourceUrl) -> FetchedPosting:
        """Steps 1-12: resolve, check, connect, follow redirects by hand, cap the body, extract."""
        timeout = httpx.Timeout(
            connect=self._connect_timeout_seconds,
            read=self._read_timeout_seconds,
            write=self._connect_timeout_seconds,
            pool=self._connect_timeout_seconds,
        )
        async with httpx.AsyncClient(
            # `trust_env=False` (obligation 6). HTTP_PROXY/HTTPS_PROXY/NO_PROXY must not silently
            # reroute egress — and the deeper reason is not routing hygiene: a proxy resolves the
            # hostname ON OUR BEHALF, which moves the DNS lookup outside this process and defeats
            # the entire address check below.
            trust_env=False,
            # Redirects are followed by hand so the whole guard re-runs on every hop (obligation 5).
            follow_redirects=False,
            timeout=timeout,
            headers={"User-Agent": self._user_agent},
        ) as client:
            current = url
            for hop in range(self._max_redirects + 1):
                response = await self._request_one_hop(client, current)

                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        # A 3xx with no Location is a broken server, not a redirect we can follow.
                        raise SourceRejectedRequest()
                    if hop >= self._max_redirects:
                        self._log_event("posting_fetch.too_many_redirects", current, hops=hop + 1)
                        raise SourceTooManyRedirects()
                    # Resolve against the CURRENT url (a Location is often relative), then parse it
                    # through `SourceUrl` so a `Location: file:///etc/passwd` or `gopher://…` dies
                    # on the scheme rule before anything connects. The next loop iteration re-runs
                    # DNS and the address policy in full — THIS is the row people forget, and the
                    # one that is actually exploited: a reputable public host that answers
                    # `302 Location: http://169.254.169.254/latest/meta-data/`.
                    try:
                        current = SourceUrl(urljoin(current.value, location))
                    except Exception as exc:
                        # An unparseable or non-http(s) Location is a blocked target, not a
                        # mysterious failure. `from None` — the Location header is part of the URL
                        # space this module must not leak.
                        del exc
                        self._log_event("posting_fetch.blocked_redirect", current)
                        raise SourceUrlNotAllowed() from None
                    continue

                return await self._read_and_extract(response, current)

            # Unreachable: the loop either returns, raises, or exhausts `max_redirects` and raises
            # above. Kept so the function has no implicit `None` return for mypy to infer.
            raise SourceTooManyRedirects()

    async def _request_one_hop(self, client: httpx.AsyncClient, url: SourceUrl) -> httpx.Response:
        """One request to one already-guarded URL, connecting to a verified address.

        Steps 1-4 and 7-8 of the algorithm run here, in this order, for every hop.
        """
        # Step 1 — re-assert the scheme. `SourceUrl` already guarantees it; this is the second lock
        # on a door the type keeps shut, the same one-line habit as ADR-0011 §6's containment check.
        # It costs nothing and it is what catches a future `SourceUrl` that grows a third scheme.
        scheme = url.value.split(":", 1)[0]
        if scheme not in ("http", "https"):
            raise SourceUrlNotAllowed()

        host = url.host
        port = self._port_for(url, scheme)
        addresses = await self._resolve(host, port)
        if not self._policy.allows(addresses):
            # Logged WITHOUT the resolved address. That address is the one fact an SSRF prober wants
            # confirmed, and "we refused" plus "here is what it resolved to" is a working oracle.
            self._log_event("posting_fetch.blocked_target", url)
            raise SourceUrlNotAllowed()

        # Step 4 — connect to an address this guard just verified (obligation 4). Check-then-connect
        # re-resolves, and a second lookup can return a different answer; that is DNS rebinding, and
        # it is the residual in every naive guard. Requesting the URL with the IP in it removes the
        # second lookup entirely, while `Host:` and `sni_hostname` keep TLS SNI and certificate
        # verification running against the NAME — verified against the installed httpx before this
        # was relied on (ADR-0012 obligation 4 records the three observations).
        pinned = self._pin_to_address(url, addresses[0], scheme, port)
        # `extensions` goes on the REQUEST, not on `send` — that is where httpcore reads
        # `sni_hostname` from when it decides `server_hostname` for the TLS handshake.
        request = client.build_request(
            "GET",
            pinned,
            headers={"Host": self._host_header(host, port, scheme)},
            extensions={"sni_hostname": host},
        )
        return await client.send(request, stream=True)

    async def _resolve(self, host: str, port: int) -> list[IPv4Address | IPv6Address]:
        """Step 2 — DNS through the running loop's `getaddrinfo`.

        Never `socket.getaddrinfo` directly: it is blocking C, and calling it on the event loop
        stalls every concurrent request for the duration of a DNS lookup — the same class of bug as
        parsing a document inline, with the same invisible symptom.
        """
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            self._log_event("posting_fetch.unreachable", None, source_host=host)
            raise SourceUnreachable() from exc

        # De-duplicated but ORDER-PRESERVING: `getaddrinfo` returns the family the resolver prefers
        # first, and connecting to a different one than the OS would have chosen is a surprise
        # nobody needs. A `set` here would make the pinned address non-deterministic run to run.
        seen: dict[str, IPv4Address | IPv6Address] = {}
        for info in infos:
            # `sockaddr[0]` is typed `str | int` because the tuple shape differs by address family;
            # for the SOCK_STREAM AF_INET/AF_INET6 results asked for above it is always the address
            # string, and `str()` keeps mypy honest without changing behaviour.
            literal = str(info[4][0])
            if literal not in seen:
                seen[literal] = ip_address(literal)
        return list(seen.values())

    @staticmethod
    def _port_for(url: SourceUrl, scheme: str) -> int:
        _, _, after_scheme = url.value.partition("://")
        authority = after_scheme.split("/", 1)[0]
        # An IPv6 literal authority is `[::1]:8443`; the port is whatever follows the bracket.
        tail = authority.rsplit("]", 1)[-1] if authority.startswith("[") else authority
        if ":" in tail:
            return int(tail.rsplit(":", 1)[1])
        return 443 if scheme == "https" else 80

    @staticmethod
    def _host_header(host: str, port: int, scheme: str) -> str:
        """The `Host:` the origin server should see — the NAME, not the address we connect to.

        The port is included only when it is non-default, matching what a browser sends: a `Host`
        of `example.com:443` is legal but is enough to make some virtual-host configurations answer
        differently from `example.com`.
        """
        literal = f"[{host}]" if ":" in host else host
        default = 443 if scheme == "https" else 80
        return literal if port == default else f"{literal}:{port}"

    @staticmethod
    def _pin_to_address(
        url: SourceUrl, address: IPv4Address | IPv6Address, scheme: str, port: int
    ) -> str:
        """Rebuild the URL with the verified address in the authority, keeping path and query."""
        _, _, after_scheme = url.value.partition("://")
        _, slash, path_and_query = after_scheme.partition("/")
        literal = f"[{address}]" if isinstance(address, IPv6Address) else str(address)
        return (
            f"{scheme}://{literal}:{port}/{path_and_query}"
            if slash
            else (f"{scheme}://{literal}:{port}")
        )

    # -- reading the body and turning it into a posting ------------------------------------------

    async def _read_and_extract(self, response: httpx.Response, url: SourceUrl) -> FetchedPosting:
        """Steps 8-13: status, content type, the streamed cap, decode, extract, build the VO."""
        try:
            # Step 9 — the Cloudflare 403 and the LinkedIn refusal. This is the case FR-2 exists
            # for, and it must reach the user as "paste it instead", never as "try again".
            if response.status_code >= 400:
                self._log_event("posting_fetch.rejected", url, upstream_status=response.status_code)
                raise SourceRejectedRequest()
            if not response.is_success:
                raise SourceRejectedRequest()

            self._check_content_type(response, url)
            raw = await self._read_capped(response, url)
        finally:
            # `httpx.Response` is not an async context manager, so the connection is released here
            # explicitly. `finally` rather than a trailing call: every branch above raises, and a
            # streamed response whose connection is never released holds a pool slot until GC —
            # which under load is indistinguishable from a leak.
            await response.aclose()

        # Step 11 — decode with the charset the server declared, falling back to UTF-8, replacing
        # anything undecodable rather than raising: a mojibake page still extracts usable text, and
        # a hard failure here would turn an encoding quirk into "we cannot read this site".
        html = raw.decode(response.charset_encoding or "utf-8", errors="replace")

        # Step 12 — extraction is CPU-bound, synchronous, and sized by a REMOTE SERVER rather than
        # by our own user. `asyncio.to_thread` under `wait_for`, never inline (ADR-0009, and
        # CLAUDE.md's 374 ms DOCX stall, which was a smaller version of this problem).
        try:
            extracted = await asyncio.wait_for(
                asyncio.to_thread(self._extract_sync, html),
                timeout=self._extraction_timeout_seconds,
            )
        except TimeoutError as exc:
            raise SourceTimedOut() from exc

        return self._build_posting(extracted, url)

    def _check_content_type(self, response: httpx.Response, url: SourceUrl) -> None:
        """Step 10 — decline anything that is not markup or plain text.

        **A reader will ask why a Content-Type header is trusted here when slice 1.1 refused to
        trust one for an upload, and the distinction is the whole answer.** In 1.1 the header was a
        CLIENT-supplied claim used to GRANT a privilege (accept this file), so it was replaced by
        sniffing the bytes. Here it is a SERVER-supplied claim used only to DECLINE work early. We
        never grant anything on the strength of it: if it lies and the body is really a PDF,
        extraction simply produces nothing usable and P-21 catches it one step later. Trusting a
        claim in order to refuse is not the same act as trusting one in order to permit.
        """
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in _ACCEPTABLE_MEDIA_TYPES:
            self._log_event("posting_fetch.not_html", url, upstream_content_type=media_type)
            raise SourceNotHtml()

    async def _read_capped(self, response: httpx.Response, url: SourceUrl) -> bytes:
        """Step 8 — the byte cap, enforced WHILE streaming, on DECODED bytes.

        `Content-Length` is never the mechanism: it is a claim by the party we are defending
        against, and a chunked response carries none at all. `aiter_bytes()` yields bytes after
        content-decoding, which is the right side of a gzip bomb to measure — a 2 MiB compressed
        body that inflates to 5 GB is stopped here, at the cap, having read 2 MiB of it.
        """
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes(_STREAM_CHUNK_BYTES):
            total += len(chunk)
            if total > self._max_bytes:
                # Abort without draining the rest: `aclose()` on the way out of the `async with`
                # tears the connection down, so a hostile server does not get to keep sending.
                self._log_event("posting_fetch.response_too_large", url, bytes_read=total)
                raise SourceResponseTooLarge()
            chunks.append(chunk)
        return b"".join(chunks)

    def _build_posting(self, extracted: _Extracted, url: SourceUrl) -> FetchedPosting:
        """Step 13 — build the value objects inside the adapter, and translate their errors.

        **This is the exact bug slice 1.1 hit and fixed in its T20, carried forward on purpose so it
        is not rediscovered.** `JobPostingText(...)` raises `EmptyJobPostingText`,
        `JobPostingTextTooShort` or `JobPostingTextTooLong` — all `DomainError`s, none of them a
        `JobPostingFetchFailed` subclass. Left untranslated they would escape this port's contract
        entirely: the use case does not catch them, the router has no mapping for them, and a
        perfectly ordinary "that page had two sentences on it" would surface as a 500.
        """
        try:
            text = JobPostingText(extracted.text)
        except (EmptyJobPostingText, JobPostingTextTooShort) as exc:
            self._log_event("posting_fetch.no_readable_text", url, character_count=0)
            raise SourceHasNoReadableText() from exc
        except JobPostingTextTooLong as exc:
            # Rejected, never truncated (P-24): a tailoring run against text the user did not know
            # was cut is a wrong answer they cannot diagnose. The message names the paste fallback.
            self._log_event("posting_fetch.text_too_long", url)
            raise SourceTextTooLong() from exc

        title: PostingTitle | None = None
        if extracted.title:
            try:
                title = PostingTitle(extracted.title)
            except Exception:
                # A page's metadata title is decoration, not content. If it fails the value
                # object's rules (200 characters of navigation cruft, a control character) the
                # posting is still perfectly usable — losing the label is not worth losing the job
                # description. This is the one place in this module where a failure is swallowed,
                # and it is swallowed because the alternative is worse for the user.
                title = None

        return FetchedPosting(text=text, title=title)

    # -- synchronous work, run only via `asyncio.to_thread` --------------------------------------

    def _extract_sync(self, html: str) -> _Extracted:
        """Runs in a worker thread. Never call this directly from the event loop.

        `favor_precision=True` because a job posting surrounded by navigation, cookie banners and
        "similar jobs" is the normal case, and a prompt padded with site chrome costs tokens and
        dilutes the thing 1.3 is trying to tailor against.
        """
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
            output_format="txt",
        )
        title: str | None = None
        metadata = trafilatura.extract_metadata(html)
        if metadata is not None:
            title = metadata.title
        return _Extracted(text=text or "", title=title or "")

    # -- logging -------------------------------------------------------------------------------

    def _log_outcome(
        self, url: SourceUrl, started_at: float, *, outcome: str, character_count: int | None
    ) -> None:
        log.info(
            "posting_fetch.finished",
            source_host=url.host,
            duration_ms=round((time.monotonic() - started_at) * 1000),
            character_count=character_count,
            outcome=outcome,
        )

    def _log_event(self, event: str, url: SourceUrl | None, **fields: object) -> None:
        """One log line per interesting step, carrying the HOST and never the URL.

        `source_host` may also be passed explicitly for the DNS-failure path, where there is a host
        but the caller is inside `_resolve` and holds no `SourceUrl`.
        """
        if url is not None:
            fields.setdefault("source_host", url.host)
        log.info(event, **fields)

    def _log_unreachable(self, url: SourceUrl, started_at: float, exc: BaseException) -> None:
        """P-13/P-14's log line: the host, the outcome, and the exception's TYPE.

        `error_type` and never `str(exc)` — an httpx connection error's message embeds the full URL
        it was trying to reach, path and query included, which is exactly the disclosure
        Constitution §8 forbids.
        """
        self._log_outcome(url, started_at, outcome="unreachable", character_count=None)
        log.info(
            "posting_fetch.unreachable",
            source_host=url.host,
            error_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
        )

    def _log_unexpected_error(self, url: SourceUrl, started_at: float, exc: BaseException) -> None:
        """The floor's own log line, separate from `posting_fetch.finished` so that line's field set
        stays exactly what AC-18 pins.

        `error_type` is a fully-qualified class name and can carry no document content. `str(exc)`
        and `exc_info` are both absent by design — see the caller.
        """
        self._log_outcome(url, started_at, outcome="fetcher_error", character_count=None)
        log.warning(
            "posting_fetch.unexpected_error",
            source_host=url.host,
            error_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
        )


class _Extracted:
    """What `_extract_sync` hands back across the thread boundary: raw strings, not value objects.

    Deliberately not a `FetchedPosting`. Building the value objects is step 13 and it happens on the
    event-loop side, because `JobPostingText`'s constructor raises domain errors that need
    translating into `JobPostingFetchFailed` subclasses — and doing that inside the worker thread
    would put the translation somewhere `asyncio.wait_for`'s timeout could interrupt half-way.
    """

    __slots__ = ("text", "title")

    def __init__(self, *, text: str, title: str) -> None:
        self.text = text
        self.title = title


if TYPE_CHECKING:
    # Makes mypy prove this satisfies the port structurally rather than by eye.
    def _assert_implements_fetcher(fetcher: HttpxTrafilaturaFetcher) -> None:
        _: JobPostingFetcherPort = fetcher
