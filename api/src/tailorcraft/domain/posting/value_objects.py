"""Value objects for the `posting` context: what makes a captured job description a valid one.

The same rule as `domain/intake/value_objects.py`: every field of `JobPosting` that has a rule gets
its own type rather than staying a bare `str`. Here that move carries more weight than it did in
`intake`, because one of these types is a security control — `SourceUrl`'s scheme allow-list is
obligation 1 of ADR-0012, and it works by making `file:///etc/passwd` *unconstructable* rather than
by rejecting it at some call site a second caller will not know about.

All frozen `@dataclass(frozen=True, slots=True)`, validating in `__post_init__`, compared by value.
Never Pydantic (ADR-0002): a `BaseModel` here would drag JSON aliases and `model_config` into
business rules that have nothing to do with the HTTP boundary.

Three of these types validate; three do not. `PostingSource`, `FetchedPosting` and
`FetchFailureReason` have no rule to enforce — a closed enum and a carrier of already-validated
value objects — so they were written whole at the skeleton step and no test of theirs was ever red.
That is the tiered cycle working rather than a hole in it (docs/sdlc.md §2): there was nothing to
stub, so there was nothing that could have failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

# The longest URL a visitor may submit, inclusive. It bounds what a stranger can push into a
# persisted column and into a log line; 2,048 is the de-facto ceiling browsers and proxies have
# converged on, so a URL longer than this would not have survived the trip here anyway.
_MAX_SOURCE_URL_LENGTH = 2048

# The scheme allow-list, and the reason `SourceUrl` is a type at all. See the class docstring.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

# `JobPostingText`'s two bounds, which count two different things on purpose — see the class
# docstring for the decision and the tie-breaker that settled it.
_MIN_POSTING_NON_WHITESPACE_CHARACTERS = 100
_MAX_POSTING_NORMALIZED_LENGTH = 30_000

_MAX_POSTING_TITLE_LENGTH = 200


def _has_control_character(value: str) -> bool:
    """True if `value` holds a C0 control character, DEL, or NUL.

    NUL is the one worth naming: it is a C-side string terminator, so a value carrying one can mean
    two different things to two different readers of the same bytes. `PostingTitle` uses this alone
    — after normalization a title still legitimately contains single spaces, so it must reject the
    controls without rejecting whitespace.
    """
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def _has_control_or_whitespace(value: str) -> bool:
    """True if `value` holds any whitespace *or* a control character — `SourceUrl`'s rule, where a
    URL is one unbroken token and all three are rejections.

    `str.isspace()` does the whitespace half rather than a literal set, because it covers the
    Unicode separators — NEL, NBSP, the line separator — that a copy-paste out of a rendered page
    carries and a hand-written `{" ", "\\t", "\\n"}` misses.
    """
    return any(char.isspace() for char in value) or _has_control_character(value)


@dataclass(frozen=True, slots=True)
class JobPostingId:
    """A `JobPosting`'s identity, typed so a signature cannot silently accept the wrong UUID.

    The authorization rule this slice depends on is `posting.guest_session_id == the resolved
    session id`; with bare `UUID`s on both sides, transposing the two arguments is a runtime bug
    whose only symptom is one guest reading another guest's posting. With `JobPostingId` and
    `GuestSessionId` it is a `mypy --strict` error. Same reasoning as `BaseCvId`.
    """

    value: UUID

    # No `__post_init__` here on purpose: every `UUID` is already a valid `JobPostingId`, so a
    # validation method that does nothing would just be a place a future reader adds a rule that
    # does not belong. `BaseCvId` (`domain/intake/value_objects.py`) and `GuestSessionId`
    # (`domain/identity/value_objects.py`) are the same shape for the same reason.


@dataclass(frozen=True, slots=True)
class SourceUrl:
    """The URL a visitor asked us to fetch a job posting from — the security-critical type here.

    Parsed with `urllib.parse.urlsplit` and validated against five rules:

    - **scheme, lowercased, ∈ {"http", "https"}** — the allow-list;
    - a **non-empty hostname**;
    - **no userinfo**: `http://user:pass@host/` is rejected outright;
    - **length ≤ 2,048 characters**;
    - **no control characters, whitespace or NUL** anywhere in the string.

    Normalized on the way in: the scheme and the host are lowercased, and the **fragment is
    stripped** — a fragment is never sent on the wire, so storing one keeps a detail the user did
    not mean to give us. Raises `InvalidSourceUrl` (P-8, P-9, P-10).

    **Why the allow-list lives in the type** (ADR-0012, obligation 1). The reflex is a
    `_is_safe_url()` helper next to the one call site, and the reason that shape fails is worth
    naming: *the second caller does not know the helper exists.* Putting the check in
    `__post_init__` means `file:///etc/passwd`, `gopher://…`, `javascript:…` and `data:…` are not
    "rejected downstream" — no value of this type can hold one, so no code path exists that could
    reach the fetcher with one. That is the `FileRef` grammar move from ADR-0011 aimed at a
    different attack: make the invalid state unrepresentable and the downstream check becomes a
    second lock rather than the only one. AC-4 tests exactly this.

    Userinfo gets its own rule for two reasons stacked on top of each other: it is a credential
    sitting in a string we are about to persist and log around, and it is the classic
    parser-confusion SSRF trick — two parsers disagreeing about where the host ends is how a guard
    and an HTTP client end up looking at different hosts. P-9 is therefore also the one failure row
    that logs **nothing about the URL at all**, not even the host.

    **What `SourceUrl` deliberately does not do: the IP-range policy.** Deciding whether a host
    resolves to `127.0.0.1`, `169.254.169.254` or `fd00::1` needs DNS, DNS is I/O, and I/O is
    infrastructure — a value object that reached for the network would be a domain type with a
    socket in it. The split is: *the type enforces what a job-posting URL means; the adapter
    enforces where it is allowed to point, at the moment it connects* (ADR-0012 obligations 2-4).
    The adapter re-asserts the scheme on every redirect hop as well, which is duplication on
    purpose.
    """

    value: str

    def __post_init__(self) -> None:
        # Deferred (function-local) import to break a module cycle: `domain/posting/errors.py`
        # imports `FetchFailureReason` from this module, so importing `errors` back at module scope
        # here would make the two modules import each other during collection — whichever loads
        # first would ask for names the other hasn't defined yet. Importing inside the method
        # instead defers the import to call time, by which point both modules have finished
        # loading. This is a local decision about *this* module's import shape, not a domain-purity
        # exception — `errors` is still `tailorcraft.domain`, so the purity test is unaffected.
        # `OriginalFilename.__post_init__` carries the same comment for the same cycle.
        from tailorcraft.domain.posting.errors import InvalidSourceUrl

        raw = self.value

        # --- The raw string is checked BEFORE anything parses it, and that order is the whole
        # point of these three lines. `urllib.parse.urlsplit` *removes* tab, CR and LF from its
        # input before parsing (a CPython hardening fix for header injection), so
        # `urlsplit("https://exa\nmple.com/jobs")` hands back a spotless host of `example.com` and
        # the newline vanishes without a trace. A validator that inspects the parse result would
        # accept that string while every consumer downstream still holds the original — with the
        # newline in it. Validating the raw string first is what closes that gap, and it is exactly
        # the kind of "redundant-looking" line a later reader would tidy away, so: do not move
        # these below the `urlsplit` call.
        if not raw:
            raise InvalidSourceUrl("url must not be empty")
        if len(raw) > _MAX_SOURCE_URL_LENGTH:
            raise InvalidSourceUrl(
                f"url must be at most {_MAX_SOURCE_URL_LENGTH} characters, got {len(raw)}"
            )
        if _has_control_or_whitespace(raw):
            raise InvalidSourceUrl("url must not contain whitespace, control characters or NUL")

        try:
            split = urlsplit(raw)
            scheme = split.scheme.lower()
            hostname = split.hostname
            username = split.username
            password = split.password
            port = split.port
        except ValueError as error:
            # `urlsplit` and its lazily-parsed `hostname`/`port` properties raise `ValueError` on
            # a malformed authority (an unbracketed IPv6 literal, a non-numeric port). Re-raised as
            # the domain's own error `from None`, so a stranger's URL cannot reach a Sentry frame
            # via the original exception's `__context__` — the same discipline the extractor's
            # catch-all follows (CLAUDE.md, the LLM/adapter boundary).
            del error
            raise InvalidSourceUrl("url is not parseable") from None

        if scheme not in _ALLOWED_URL_SCHEMES:
            raise InvalidSourceUrl(f"url scheme must be http or https, not {scheme!r}")
        if not hostname:
            raise InvalidSourceUrl("url must have a hostname")
        if username is not None or password is not None:
            # Deliberately says nothing about the value: the rejected string contains a credential
            # (P-9 is the one failure row that logs *nothing* about the URL, not even the host).
            raise InvalidSourceUrl("url must not contain userinfo")

        # Normalize. `split.hostname` is already lowercased by `urlsplit`; the scheme is lowercased
        # above; the path, query and everything else keep their case, because RFC 3986 makes only
        # the scheme and the host case-insensitive and lowercasing `/Path` would fetch a different
        # page or a 404. The fragment is dropped by passing "" as the last component — it is never
        # sent on the wire, so persisting one would keep a detail the visitor did not mean to give
        # us, in a row that is already PII-adjacent.
        netloc = f"[{hostname}]" if ":" in hostname else hostname  # bracket an IPv6 literal back up
        if port is not None:
            netloc = f"{netloc}:{port}"

        object.__setattr__(self, "value", urlunsplit((scheme, netloc, split.path, split.query, "")))

    @property
    def host(self) -> str:
        """The lowercased hostname — **the only part of a URL this codebase may ever log.**

        A full URL identifies which job a named person is applying for, which is exactly the kind of
        fact Constitution §8 keeps out of log files; the host alone ("we failed to reach
        example.com") is enough to debug a fetch without recording anyone's job hunt. Callers should
        reach for this property rather than slicing `value`, so that "log the host, never the URL"
        is a thing the type makes easy instead of a rule someone has to remember.

        The host, not the authority: any port is excluded. A `host` of `example.com:8443` would key
        log lines and any future per-host metric differently for the same site depending on whether
        the visitor happened to type the port.
        """
        hostname = urlsplit(self.value).hostname
        # `__post_init__` refuses any value without a hostname and rewrites `value` from the parsed
        # parts, so `None` is unreachable on a constructed `SourceUrl`. The branch exists because
        # `.hostname` is typed `str | None` and `mypy --strict` is right to make us say what
        # happens; answering with `""` rather than an exception keeps the logging call site — the
        # only caller — from being where a would-be impossible state surfaces as a second failure.
        return hostname if hostname is not None else ""


@dataclass(frozen=True, slots=True)
class JobPostingText:
    """The job description itself — pasted by the user or extracted from a page by the fetcher.

    Non-blank; whitespace-normalized with `" ".join(value.split())` exactly as `ExtractedText` does
    it; then **at least 100 non-whitespace characters** and **at most 30,000 characters of
    normalized length**. Raises `EmptyJobPostingText`, `JobPostingTextTooShort` or
    `JobPostingTextTooLong` (P-4, P-5, P-6). Invariant J-1 lives here rather than in `JobPosting`:
    the aggregate cannot hold an invalid text because the type that carries it cannot exist in an
    invalid state.

    **The two bounds count different things, and that is the decision, not an inconsistency**
    (DECIDED 2026-09-09 at T2, when the tests found the spec ambiguous and stopped rather than
    letting this file pick a side). The *floor* asks "is there real content here?" — whitespace is
    not content, so it counts non-whitespace and a posting padded with 300 blank lines cannot sneak
    past it. The *ceiling* asks "is this too big for a prompt, a column and a counter?" — a space
    costs a token, a byte and a column of screen just as a letter does, so it counts the normalized
    length, which is exactly what `character_count` reports. The tie-breaker was the UI: the live
    counter reads `3,184 / 30,000` from `character_count`, so measuring the ceiling on the other
    number would let us accept text the counter then renders as "35,999 / 30,000" — a limit
    visibly exceeded by input we had just accepted. A reader who spots the asymmetry and reaches to
    "fix" it should change the counter's source first and see what breaks.

    **Why 100 and not the 200 an `ExtractedText` demands.** The floors measure different documents.
    A genuine two-paragraph job posting — "Senior Python engineer, remote, here is the stack, apply
    here" — is a real thing that a 200-character floor would refuse; a 200-character CV is not a
    thing at all. Provisional and *chosen rather than measured* (OQ-5): verify it against the
    fixture corpus of ten real postings and change it **once, on purpose**, in the spec and the code
    together — never by letting a test ratify whatever the corpus happened to produce.

    **Why a 30,000-character ceiling at all**, when nothing forces one. Two concrete reasons. Slice
    1.3's prompt has a token budget and a 15-second target, and a 200 KB scrape of a page that is
    95 % navigation chrome would blow through both. And it bounds what a single fetch of a
    stranger's URL can push into Postgres and into a prompt — an unbounded ceiling makes the byte
    cap in the adapter the only thing standing between a hostile page and the database. ~30,000
    characters is roughly 7,500 tokens: comfortably inside Gemini's window alongside a CV, and far
    longer than any real posting.

    **Reject, never truncate.** Silently trimming changes the user's input without telling them, and
    a tailoring run against text the user did not know was cut is a wrong answer they have no way to
    diagnose. The considered alternative — truncate and show a notice — is recorded here as
    rejected on that ground, so the next reader finds the reason rather than an omission.
    """

    value: str

    def __post_init__(self) -> None:
        # See `SourceUrl.__post_init__` for why this import is function-local rather than
        # module-level: `errors.py` imports `FetchFailureReason` from this module, so a
        # module-level import in the other direction would be a circular import at collection time.
        from tailorcraft.domain.posting.errors import (
            EmptyJobPostingText,
            JobPostingTextTooLong,
            JobPostingTextTooShort,
        )

        # `str.split()` with no argument already treats any run of whitespace (spaces, tabs,
        # newlines) as one separator and drops leading/trailing whitespace, so re-joining with a
        # single space collapses everything in one pass. Same call `ExtractedText` makes.
        normalized = " ".join(self.value.split())
        non_whitespace_count = sum(1 for char in normalized if not char.isspace())

        if not normalized:
            raise EmptyJobPostingText("job posting text must not be blank")
        # The floor counts non-whitespace; the ceiling counts the normalized length. Two different
        # quantities, on purpose — see the class docstring for the decision and why the UI counter
        # broke the tie. Do not "simplify" these to one measure.
        if non_whitespace_count < _MIN_POSTING_NON_WHITESPACE_CHARACTERS:
            raise JobPostingTextTooShort(
                f"job posting text has only {non_whitespace_count} non-whitespace characters; "
                f"the floor is {_MIN_POSTING_NON_WHITESPACE_CHARACTERS} (OQ-5)"
            )
        if len(normalized) > _MAX_POSTING_NORMALIZED_LENGTH:
            raise JobPostingTextTooLong(
                f"job posting text is {len(normalized)} characters; "
                f"the ceiling is {_MAX_POSTING_NORMALIZED_LENGTH}"
            )

        object.__setattr__(self, "value", normalized)

    @property
    def character_count(self) -> int:
        """The count reported to the user, so nothing downstream needs the text itself to say how
        much of it there is — which matters because a job posting is the other half of a PII pair
        (Constitution §8), and `JobPostingCaptured` carries this number precisely so it need not
        carry the text.

        Carries the same split `ExtractedText.character_count` settled on, and it is worth
        repeating rather than cross-referencing: the **floor** counts non-whitespace characters,
        because a document padded with 300 blank lines must not sneak past a minimum meant to
        guarantee real content; the **report** counts the length of the normalized text, spaces
        included, because that is what a person counting characters in the posting they pasted
        would count. Two different numbers, on purpose, each measuring the thing its own job needs.

        **The ceiling is measured in this number, not in the floor's.** That is what keeps the UI
        honest: the counter renders `character_count / 30,000`, so the limit and the number shown
        against it are the same quantity. See the class docstring for why the tie was broken this
        way — it is the one place the two counts had to agree and could not both win.
        """
        return len(self.value)


@dataclass(frozen=True, slots=True)
class PostingTitle:
    """The job title read off a fetched page — a display label, never an identifier.

    1-200 characters after `strip()` and the same whitespace normalization the text gets; control
    characters and NUL are rejected. Raises `InvalidPostingTitle`.

    It is optional on the aggregate and always `None` for a pasted posting (invariant J-4): a title
    we did not read from a page would have to be *guessed* from the first line of a clipboard paste,
    and a confidently wrong label is worse than an empty one. The title comes from a document a
    stranger wrote, which is why it is validated like untrusted text rather than trusted because it
    came from a `<title>` tag.
    """

    value: str

    def __post_init__(self) -> None:
        # See `SourceUrl.__post_init__` for why this import is function-local rather than
        # module-level: it breaks the `value_objects` ↔ `errors` module cycle.
        from tailorcraft.domain.posting.errors import InvalidPostingTitle

        # Normalize first, then measure. A `<title>` pretty-printed across two indented lines is
        # padded with whitespace that is about to be thrown away, and checking the raw length would
        # reject a perfectly good 200-character title for four spaces it does not keep.
        normalized = " ".join(self.value.split())

        if not normalized:
            raise InvalidPostingTitle("posting title must not be blank")
        if len(normalized) > _MAX_POSTING_TITLE_LENGTH:
            raise InvalidPostingTitle(
                f"posting title must be at most {_MAX_POSTING_TITLE_LENGTH} characters, "
                f"got {len(normalized)}"
            )
        # Controls only, not `_has_control_or_whitespace`: normalization collapses whitespace runs
        # to single spaces rather than deleting them, so a normalized title legitimately contains
        # spaces and the URL predicate would reject every multi-word job title. What must still go
        # is the control characters a `<title>` tag has no business carrying — a NUL above all,
        # which truncates a C-side string and quietly changes what a downstream reader sees.
        if _has_control_character(normalized):
            raise InvalidPostingTitle("posting title must not contain control characters or NUL")

        object.__setattr__(self, "value", normalized)


class PostingSource(StrEnum):
    """How the job description reached us. An enum, not a value object: the set is closed at two,
    the boundary picks the member from a discriminated union rather than parsing a string, and so a
    `__post_init__` would have nothing left to validate — the same call `CvContentType` makes.

    It is also a *field of one fact*, not two facts: `JobPostingCaptured` carries `source` rather
    than the context publishing `JobPostingPasted` and `JobPostingFetched`, so that 1.3's subscriber
    branches on data if it cares and ignores it if it does not.
    """

    PASTED = "pasted"
    FETCHED = "fetched"


@dataclass(frozen=True, slots=True)
class FetchedPosting:
    """What `JobPostingFetcherPort.fetch` promises to return: a posting's text, and its title if the
    page had one.

    A named type rather than a `dict` or a `(text, title)` tuple, and the reason is the port
    signature above it. `async def fetch(self, url: SourceUrl) -> FetchedPosting` reads as a
    sentence; `-> tuple[JobPostingText, PostingTitle | None]` reads as a puzzle whose second element
    every caller has to remember the meaning of, and `-> dict[str, Any]` gives up on types entirely
    at exactly the boundary where an adapter hands untrusted data to the domain. Naming the return
    value is also what lets the fetcher grow a third field later without touching a single caller's
    unpacking.

    No `__post_init__`: both fields are already value objects that validated themselves, so there is
    nothing left for this carrier to check. It is a record, not a rule.
    """

    text: JobPostingText
    title: PostingTitle | None


class FetchFailureReason(StrEnum):
    """Why `JobPostingFetcherPort.fetch` failed. Every member has exactly one
    `JobPostingFetchFailed` subclass binding it in `domain/posting/errors.py`, so the adapter raises
    a named exception and the router maps a closed set.

    `FETCHER_ERROR` is deliberately the residual, and the asymmetry is the point — it is the same
    shape `ExtractionFailureReason.EXTRACTOR_ERROR` has, for the same reason. It is what the
    adapter's `except Exception` floor reaches for when a library fails in a way we have no better
    name for. A named subclass per unknown cause would be a taxonomy of things we specifically
    failed to identify, and the allow-list-of-known-exceptions version of that bet is exactly what
    CLAUDE.md records losing in the extraction sweep.

    **This enum is never persisted.** A failed fetch produces no `JobPosting` and therefore no row
    to persist it on (ADR-0013) — unlike `ExtractionFailureReason`, which is a column. It exists for
    two live purposes: the router's reason → status/`code` mapping has a closed set to be exhaustive
    over, and the adapter's log line has a stable `failure_reason` value to key alerts on. That is
    why it is in the domain at all rather than in the adapter: the router and the adapter must agree
    on the vocabulary, and the domain is the only layer both of them may import.
    """

    BLOCKED_TARGET = "blocked_target"
    UNREACHABLE = "unreachable"
    TIMED_OUT = "timed_out"
    REJECTED = "rejected"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    RESPONSE_TOO_LARGE = "response_too_large"
    NOT_HTML = "not_html"
    NO_READABLE_TEXT = "no_readable_text"
    TEXT_TOO_LONG = "text_too_long"
    FETCHER_ERROR = "fetcher_error"
