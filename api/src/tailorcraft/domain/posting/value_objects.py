"""Value objects for the `posting` context: what makes a captured job description a valid one.

The same rule as `domain/intake/value_objects.py`: every field of `JobPosting` that has a rule gets
its own type rather than staying a bare `str`. Here that move carries more weight than it did in
`intake`, because one of these types is a security control — `SourceUrl`'s scheme allow-list is
obligation 1 of ADR-0012, and it works by making `file:///etc/passwd` *unconstructable* rather than
by rejecting it at some call site a second caller will not know about.

All frozen `@dataclass(frozen=True, slots=True)`, validating in `__post_init__`, compared by value.
Never Pydantic (ADR-0002): a `BaseModel` here would drag JSON aliases and `model_config` into
business rules that have nothing to do with the HTTP boundary.

**SKELETON (T1).** Every `__post_init__` and every property here raises `NotImplementedError` on
purpose. The signatures and field types are real so that `qa`'s tests fail on their *assertion*
rather than on an `ImportError` — an import red proves a file is absent, not that the assertion
discriminates (docs/sdlc.md §2). The bodies arrive in T3, after the red is recorded. The three types
that have nothing to defer — two enums and one plain carrier — are written whole here, for the same
reason `CvContentType` was: there is no behaviour to fail, so there is nothing to stub.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


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
        # SKELETON (T1). T3 fills this in. When it does, the errors it raises must be imported
        # *inside* this method, not at module scope: `domain/posting/errors.py` imports
        # `FetchFailureReason` from this module, so a module-level import back the other way would
        # make the two modules import each other during collection and whichever loaded first would
        # ask for names the other has not defined yet. `OriginalFilename.__post_init__` in
        # `domain/intake/value_objects.py` carries the same comment for the same cycle. That is a
        # local decision about this module's import shape, not a domain-purity exception —
        # `errors` is still `tailorcraft.domain`.
        raise NotImplementedError

    @property
    def host(self) -> str:
        """The lowercased hostname — **the only part of a URL this codebase may ever log.**

        A full URL identifies which job a named person is applying for, which is exactly the kind of
        fact Constitution §8 keeps out of log files; the host alone ("we failed to reach
        example.com") is enough to debug a fetch without recording anyone's job hunt. Callers should
        reach for this property rather than slicing `value`, so that "log the host, never the URL"
        is a thing the type makes easy instead of a rule someone has to remember.
        """
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class JobPostingText:
    """The job description itself — pasted by the user or extracted from a page by the fetcher.

    Non-blank; whitespace-normalized with `" ".join(value.split())` exactly as `ExtractedText` does
    it; **at least 100 and at most 30,000 non-whitespace characters** after normalization. Raises
    `EmptyJobPostingText`, `JobPostingTextTooShort` or `JobPostingTextTooLong` (P-4, P-5, P-6).
    Invariant J-1 lives here rather than in `JobPosting`: the aggregate cannot hold an invalid text
    because the type that carries it cannot exist in an invalid state.

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
        # SKELETON (T1). See `SourceUrl.__post_init__` for why T3's error imports go inside the
        # method body rather than at module scope.
        raise NotImplementedError

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
        """
        raise NotImplementedError


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
        # SKELETON (T1). See `SourceUrl.__post_init__` for why T3's error imports go inside the
        # method body rather than at module scope.
        raise NotImplementedError


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
