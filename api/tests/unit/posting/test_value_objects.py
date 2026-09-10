"""Value objects for the `posting` context: `SourceUrl`, `JobPostingText`, `PostingTitle`,
`JobPostingId`, and the three types that carry no rules (`PostingSource`, `FetchedPosting`,
`FetchFailureReason`).

Pure domain tests: no database, no event loop, no mocks, no network, no fixtures beyond
`parametrize`. These are the cheapest tests in the suite and they should stay that way.

Every assertion comes from the feature spec (AC-3, AC-4, failure rows P-4 … P-10) and the technical
plan's "Value objects" section — **not** from running the code, which at the time of writing raises
`NotImplementedError` in every `__post_init__` on purpose (docs/sdlc.md §2). Where the spec's prose
is ambiguous, the docstring below says which source won.

**Where `SourceUrl` sits in the SSRF story (ADR-0012).** The type enforces what a job-posting URL
*means* — scheme, host, no credentials, length, no control characters. It does **not** decide where
the URL is allowed to *point*: that needs DNS, DNS is I/O, and I/O is infrastructure. The address
policy is tested separately against the adapter (T20). So a URL passing every table row here is
"well-formed", never "safe to fetch".
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from tailorcraft.domain.posting.errors import (
    EmptyJobPostingText,
    InvalidPostingTitle,
    InvalidSourceUrl,
    JobPostingTextTooLong,
    JobPostingTextTooShort,
)
from tailorcraft.domain.posting.value_objects import (
    FetchedPosting,
    FetchFailureReason,
    JobPostingId,
    JobPostingText,
    PostingSource,
    PostingTitle,
    SourceUrl,
)

# --- SourceUrl: the accept/reject table -----------------------------------------------------------
#
# The table is the point. `SourceUrl` is the codebase's scheme allow-list (AC-4), and an allow-list
# with one example per branch is an allow-list nobody has actually read.


def _url_of_length(total: int) -> str:
    """A syntactically valid `https` URL of exactly `total` characters.

    Computed rather than written out so the 2,048/2,049 boundary pair cannot drift by a
    miscounted literal — the one mistake the boundary test exists to catch.
    """
    prefix = "https://example.com/"
    return prefix + "a" * (total - len(prefix))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(
            "http://example.com/jobs/1",
            "http://example.com/jobs/1",
            id="plain-http-round-trips-unchanged",
        ),
        pytest.param(
            "https://example.com/jobs/1",
            "https://example.com/jobs/1",
            id="plain-https-round-trips-unchanged",
        ),
        pytest.param(
            "HTTPS://EXAMPLE.COM/Path",
            "https://example.com/Path",
            id="scheme-and-host-lowercased-path-case-preserved",
        ),
        pytest.param(
            "https://example.com/jobs?id=42&ref=Alpha",
            "https://example.com/jobs?id=42&ref=Alpha",
            id="query-string-preserved",
        ),
        pytest.param(
            "https://example.com:8443/jobs/1",
            "https://example.com:8443/jobs/1",
            id="explicit-port-preserved",
        ),
        pytest.param(
            "https://x.com/a#frag",
            "https://x.com/a",
            id="fragment-stripped",
        ),
        pytest.param(
            "https://xn--bcher-kva.example.com/stellen/1",
            "https://xn--bcher-kva.example.com/stellen/1",
            id="punycode-idn-host-accepted",
        ),
        pytest.param(
            f"https://{'a' * 63}.example.com/jobs",
            f"https://{'a' * 63}.example.com/jobs",
            id="maximum-length-dns-label-accepted",
        ),
    ],
)
def test_source_url_accepts_and_normalizes(raw: str, expected: str) -> None:
    """Every accepted URL, and exactly what it normalizes to.

    Two normalizations are asserted together because they are one rule with one reason: the scheme
    and the host are case-insensitive per RFC 3986 and the path is **not**, so lowercasing the whole
    string would quietly turn `/Path` into `/path` and fetch a different page (or a 404). The
    fragment is stripped because it is never sent on the wire — keeping one would persist a detail
    the visitor did not mean to hand over, in a row that is already PII-adjacent.

    The punycode and 63-character-label rows are here to prove the validator rejects by *rule*
    rather than by "looks unusual": a German job board and a long tenant subdomain are ordinary
    inputs, and a host regex tightened by guesswork is how they stop working.
    """
    assert SourceUrl(raw).value == expected


@pytest.mark.parametrize(
    "raw",
    [
        # --- P-8: the scheme allow-list. Each of these is a different capability, which is why
        # they get four rows rather than one "not http" row.
        pytest.param("ftp://example.com/jobs", id="ftp-scheme"),
        pytest.param("file:///etc/passwd", id="file-scheme-reads-a-local-file"),
        pytest.param("javascript:alert(1)", id="javascript-scheme"),
        pytest.param("data:text/html,<b>x</b>", id="data-scheme"),
        pytest.param("gopher://example.com/1", id="gopher-scheme"),
        # --- P-9: userinfo. Two rows on purpose — see the test's docstring.
        pytest.param("http://user:pass@example.com/jobs", id="userinfo-with-password"),
        pytest.param("http://user@example.com/jobs", id="userinfo-without-password"),
        # --- P-10: no host, control characters, whitespace, NUL, empty.
        pytest.param("http:///jobs", id="no-host"),
        pytest.param("https://exa\nmple.com/jobs", id="embedded-newline"),
        pytest.param("https://example.com/jo\x00bs", id="embedded-nul"),
        pytest.param("https://example.com/jo bs", id="embedded-space"),
        pytest.param("", id="empty-string"),
    ],
)
def test_source_url_rejects(raw: str) -> None:
    """One `InvalidSourceUrl` for every way a string fails to be a fetchable job-posting URL.

    **The scheme rows are AC-4 and they are a security control, not input hygiene.** Putting the
    allow-list in `__post_init__` is what makes `file:///etc/passwd` *unconstructable*: there is no
    value of this type that holds one, so no call path can reach the fetcher with one. A
    `_is_safe_url()` helper beside the one call site would pass this same table today and fail the
    day a second caller does not know the helper exists.

    **Userinfo gets two rows because a naive check finds only one of them.** Testing for `:` inside
    the netloc catches `user:pass@host` and sails straight past `user@host`, and the bare form is
    the more useful half of the parser-confusion trick anyway: two parsers disagreeing about where
    the host ends is how a guard and an HTTP client end up looking at different hosts. It is also a
    credential in a string we are about to persist — which is why P-9 logs *nothing* about the URL.

    **The newline row is not a duplicate of the space row.** `urllib.parse.urlsplit` silently
    *removes* tab, CR and LF from its input (a CPython fix for exactly this class of injection), so
    a validator that inspects only the parse result sees a perfectly good host and accepts the
    string — while everything downstream still holds the original with the newline in it. The rule
    has to be asserted against the **raw** string, and this row is what forces that.
    """
    with pytest.raises(InvalidSourceUrl):
        SourceUrl(raw)


def test_source_url_accepts_exactly_2048_characters() -> None:
    """The ceiling is inclusive: 2,048 is the longest URL a visitor may submit, not the first one
    refused. Asserted as one half of a pair with the row below, because a length check written as
    `>=` instead of `>` passes every other test in this module."""
    url = _url_of_length(2048)

    assert SourceUrl(url).value == url


def test_source_url_rejects_2049_characters() -> None:
    """One character past the ceiling. The bound is a real limit rather than decoration: it caps
    what a stranger can push into a persisted column and into a log line, and it is the kind of
    number an off-by-one leaves silently wrong in the permissive direction."""
    with pytest.raises(InvalidSourceUrl):
        SourceUrl(_url_of_length(2049))


def test_source_url_host_is_lowercased() -> None:
    """`host` is the **only** part of a URL this codebase may log (Constitution §8): a full URL says
    which job a named person is applying for, while "we could not reach example.com" debugs the
    fetch without recording anyone's job hunt. It must therefore be a property callers reach for
    rather than a slice of `value` they each write themselves."""
    assert SourceUrl("HTTPS://EXAMPLE.COM/Path").host == "example.com"


def test_source_url_host_excludes_the_port() -> None:
    """The host is the host, not the authority. A `host` that returned `example.com:8443` would key
    log lines and any future per-host metric differently for the same site depending on whether the
    visitor typed the default port."""
    assert SourceUrl("https://Example.COM:8443/jobs/1").host == "example.com"


# --- JobPostingText -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty-string"),
        pytest.param("   \n\t ", id="whitespace-only"),
    ],
)
def test_job_posting_text_rejects_blank_input(raw: str) -> None:
    """P-4. Blank is not "too short" — it is nothing at all, and it gets its own error so the
    boundary can say "paste the job description" rather than "paste a longer one" to someone who
    pasted nothing. The whitespace-only row matters separately because it is what an empty
    contenteditable or a stray copy of a blank line actually sends."""
    with pytest.raises(EmptyJobPostingText):
        JobPostingText(raw)


def test_job_posting_text_rejects_99_non_whitespace_characters() -> None:
    """AC-3 / P-5, the lower half of the pair. 100 is provisional but *chosen* (OQ-5) — the test
    asserts the number the spec fixes, and if the fixture corpus later argues for a different floor
    it moves in the spec and the code together, never by a test being relaxed to match."""
    with pytest.raises(JobPostingTextTooShort):
        JobPostingText("a" * 99)


def test_job_posting_text_accepts_exactly_100_non_whitespace_characters() -> None:
    """AC-3, the upper half of the pair: the floor is inclusive."""
    text = JobPostingText("a" * 100)

    assert text.character_count == 100


def test_job_posting_text_floor_counts_non_whitespace_not_length() -> None:
    """99 non-whitespace characters spread over 33 words is 131 characters long — and still too
    short.

    This is the row that makes the floor mean something. A minimum measured on the normalized
    *length* would admit this document, and by extension a genuinely empty posting padded out with
    spaces, which is precisely what a "there must be real content here" floor exists to refuse. The
    skeleton's `character_count` docstring settles the split explicitly: the floor counts
    non-whitespace, the report counts what the user can see.
    """
    raw = " ".join(["abc"] * 33)  # 99 non-whitespace characters, 131 after normalization

    with pytest.raises(JobPostingTextTooShort):
        JobPostingText(raw)


def test_job_posting_text_accepts_exactly_30000_characters() -> None:
    """AC-3 / P-6, the accepted half: the ceiling is inclusive."""
    text = JobPostingText("a" * 30_000)

    assert text.character_count == 30_000


def test_job_posting_text_rejects_30001_characters() -> None:
    """One past the ceiling, and **rejected rather than truncated**: silently trimming would change
    the user's input without telling them, and a tailoring run against text they did not know was
    cut is a wrong answer they have no way to diagnose."""
    with pytest.raises(JobPostingTextTooLong):
        JobPostingText("a" * 30_001)


def test_job_posting_text_ceiling_counts_normalized_length_not_non_whitespace() -> None:
    """30,000 non-whitespace characters in 6,000 words is 35,999 characters of normalized text —
    exactly at the ceiling by one measure, 5,999 over it by the other, and **rejected**.

    This is the case that discriminates the two readings, which is why it earns a test of its own.
    The spec was ambiguous here and the ambiguity was resolved deliberately on 2026-09-09 rather
    than left for the implementation to settle by accident: **the ceiling counts the normalized
    length — the same number `character_count` reports.**

    The argument that decided it came from the UI, which neither source had stated. The technical
    plan specifies a live counter reading `3,184 / 30,000`, fed by `character_count`. Measuring the
    ceiling on non-whitespace would mean accepting this very input and then rendering
    "35,999 / 30,000" underneath it — a limit visibly exceeded by text we had just told the user was
    fine. A bound has to be measured in the unit it is displayed in.

    **The two bounds therefore count different things on purpose**, and the asymmetry is the design
    rather than an oversight: the floor asks "is there real content here?" (whitespace is not
    content — see the floor's own test), while the ceiling asks "is this too big for a prompt, a
    column and a counter?", and a space costs a token, a byte and a column of screen exactly as a
    letter does.
    """
    # 30,000 non-whitespace characters, 35,999 characters after normalization
    raw = " ".join(["aaaaa"] * 6_000)

    with pytest.raises(JobPostingTextTooLong):
        JobPostingText(raw)


def test_job_posting_text_accepts_30000_normalized_characters_including_spaces() -> None:
    """The mirror of the row above, so both units are pinned on the accepting side too.

    5,000 words — 4,999 of five letters and one of six — joined by single spaces are 25,001
    non-whitespace characters and exactly 30,000 characters of normalized text. Under the losing
    reading this sits far below the ceiling and tells us nothing; under the decided one it sits
    precisely *on* it, which is the assertion worth having. It also keeps the accepted boundary
    from being only the trivial `"a" * 30_000`, where the two measures coincide and a ceiling
    implemented against either unit would pass.
    """
    raw = " ".join(["aaaaa"] * 4_999 + ["aaaaaa"])

    assert JobPostingText(raw).character_count == 30_000


def test_job_posting_text_normalizes_whitespace() -> None:
    """Leading/trailing whitespace goes, and every internal run of spaces, tabs and newlines
    collapses to a single space — `" ".join(value.split())`, exactly as `ExtractedText` does it.

    Pasted job descriptions arrive from a browser as ragged text with hard-wrapped lines and
    double-spaced paragraphs. Normalizing at the boundary of the type means every consumer — the
    stored row, the character count, 1.3's prompt — sees one canonical form, instead of each one
    deciding for itself how much of a stranger's formatting matters.
    """
    words = ["python"] * 20  # 120 non-whitespace characters, comfortably past the floor
    raw = "  " + "\n\n".join(words) + "  \t "

    assert JobPostingText(raw).value == " ".join(words)


def test_job_posting_text_character_count_reports_the_normalized_length() -> None:
    """The reported number counts the spaces; the floor did not.

    20 six-letter words are 120 non-whitespace characters and 139 characters of normalized text,
    and `character_count` must be the second one. It is the number the API returns and the number
    `JobPostingCaptured` carries **precisely so that neither has to carry the text** (Constitution
    §8) — so it has to match what a person counting characters in the posting they pasted would
    arrive at, not an internal validation quantity.
    """
    raw = " ".join(["python"] * 20)

    assert JobPostingText(raw).character_count == 139


# --- PostingTitle ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty-string"),
        pytest.param("   \n\t ", id="whitespace-only"),
    ],
)
def test_posting_title_rejects_blank_input(raw: str) -> None:
    """A title is a display label, and a blank one is worse than none: the aggregate already models
    "no title" as `None` (invariant J-4), so an empty-string title would be a second way to say the
    same thing that every renderer then has to handle."""
    with pytest.raises(InvalidPostingTitle):
        PostingTitle(raw)


def test_posting_title_accepts_exactly_200_characters() -> None:
    """The bound is inclusive — 200 is the longest acceptable title, not the first rejected one."""
    raw = "a" * 200

    assert PostingTitle(raw).value == raw


def test_posting_title_rejects_201_characters() -> None:
    """One past the bound. The title comes off a page a stranger wrote, so the length limit is a
    real cap on what a hostile `<title>` can push into a column and onto a screen — the other half
    of the boundary pair, because a `>=`/`>` slip only ever shows up on one side."""
    with pytest.raises(InvalidPostingTitle):
        PostingTitle("a" * 201)


def test_posting_title_length_is_measured_after_normalization() -> None:
    """200 characters wrapped in whitespace is a 200-character title, not a 204-character one.

    Normalization happens first and the bound applies to the result. Checking the raw length would
    reject a perfectly good title for whitespace that is about to be thrown away — and the padding
    is not hypothetical: it is what a `<title>` tag pretty-printed across two lines contains.
    """
    raw = "a" * 200

    assert PostingTitle(f"  {raw}  ").value == raw


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("Senior\x00Engineer", id="embedded-nul"),
        pytest.param("Senior\x01Engineer", id="control-character"),
        pytest.param("Senior\x7fEngineer", id="delete-control-character"),
    ],
)
def test_posting_title_rejects_control_characters_and_nul(raw: str) -> None:
    """The title is untrusted text that arrives from a document a stranger wrote and ends up in a
    log line, a database column and an HTML page. It is validated *because* it came from a `<title>`
    tag, not trusted for it — a NUL alone can truncate a C-side string and quietly change what a
    downstream reader thinks the title is."""
    with pytest.raises(InvalidPostingTitle):
        PostingTitle(raw)


def test_posting_title_normalizes_internal_whitespace() -> None:
    """The same `" ".join(value.split())` the posting text gets, for the same reason: a title
    scraped out of pretty-printed HTML carries the source file's line breaks and indentation, and
    nobody downstream should have to guess which of those the author meant."""
    assert PostingTitle("  Senior\n\tPython   Engineer  ").value == "Senior Python Engineer"


# --- JobPostingId ---------------------------------------------------------------------------------

_A_UUID = UUID("018f3f4a-8f2a-7c3d-9b1e-4c5a6d7e8f90")
_ANOTHER_UUID = UUID("018f3f4a-8f2a-7c3d-9b1e-4c5a6d7e8f91")


def test_job_posting_ids_wrapping_the_same_uuid_are_equal() -> None:
    """Value semantics, not reference semantics. The ownership check this slice depends on is an
    equality comparison, so an id that compared by identity would make every authorization test
    pass for the wrong reason and every real one fail."""
    assert JobPostingId(_A_UUID) == JobPostingId(_A_UUID)


def test_job_posting_ids_wrapping_different_uuids_are_not_equal() -> None:
    """The other half of the equality contract — an `__eq__` that returns `True` too readily is the
    failure mode that matters here, because the thing being compared is an authorization decision."""
    assert JobPostingId(_A_UUID) != JobPostingId(_ANOTHER_UUID)


def test_job_posting_id_is_frozen() -> None:
    """Immutability is the reason a value object can be passed around without defensive copies. An
    id that could be reassigned in place would let one caller's mutation change what a second
    caller — possibly the ownership check — is holding."""
    posting_id = JobPostingId(_A_UUID)

    with pytest.raises(FrozenInstanceError):
        posting_id.value = _ANOTHER_UUID  # type: ignore[misc]


# --- Green on arrival by design -------------------------------------------------------------------
#
# The two enums below have no behaviour to defer, so T1 wrote them whole and these tests pass
# immediately. That is the cycle working, not a hole in it: there is nothing to stub, so there is
# nothing that could have failed.
#
# `FetchedPosting` is the exception in this section. It is a plain carrier with no `__post_init__`
# and nothing of its own to fail — but constructing one requires a real `JobPostingText`, whose
# `__post_init__` is still a skeleton, so its two tests are red until T3 like the rest of the file.


def test_posting_source_wire_values_are_stable() -> None:
    """The API serializes these members straight into JSON and the request body's discriminated
    union parses them straight back, so the *strings* are the contract — renaming `PASTED` is a
    refactor, changing `"pasted"` is a breaking API change. Asserted as a whole mapping rather than
    member by member so that adding a third source without deciding its wire value fails here."""
    assert {source.name: source.value for source in PostingSource} == {
        "PASTED": "pasted",
        "FETCHED": "fetched",
    }


def test_posting_source_compares_equal_to_its_wire_string() -> None:
    """`StrEnum`, not `Enum`: the member *is* the string, which is what lets it cross the boundary
    without a conversion step somebody has to remember.

    The annotation below is half the assertion — `mypy --strict` accepts binding a member to a
    `str` only because `StrEnum` really does subclass `str`, so this test fails at the type check
    as well as at runtime if the base class ever changes to a plain `Enum`.
    """
    wire_value: str = PostingSource.PASTED

    assert wire_value == "pasted"


def test_fetch_failure_reason_wire_values_are_stable() -> None:
    """These ten strings are two contracts at once — the router's reason → status/`code` mapping is
    exhaustive over this set, and the adapter's log line uses the value as a stable `failure_reason`
    to key alerts on. A silently renamed member breaks a dashboard nobody is looking at.

    Asserted as the whole mapping so that an eleventh reason arriving without a decision about its
    wire value and its HTTP status fails here first, rather than in production as an unmapped
    branch.
    """
    assert {reason.name: reason.value for reason in FetchFailureReason} == {
        "BLOCKED_TARGET": "blocked_target",
        "UNREACHABLE": "unreachable",
        "TIMED_OUT": "timed_out",
        "REJECTED": "rejected",
        "TOO_MANY_REDIRECTS": "too_many_redirects",
        "RESPONSE_TOO_LARGE": "response_too_large",
        "NOT_HTML": "not_html",
        "NO_READABLE_TEXT": "no_readable_text",
        "TEXT_TOO_LONG": "text_too_long",
        "FETCHER_ERROR": "fetcher_error",
    }


def test_fetched_posting_accepts_a_missing_title() -> None:
    """A page with no usable `<title>` is an ordinary outcome, not a fetch failure — plenty of job
    boards render the title client-side. The port's return type has to be able to say "text, and no
    title" without the adapter inventing one from the first line of the body, because a confidently
    wrong label is worse than an empty one (invariant J-4)."""
    text = JobPostingText("a" * 150)

    posting = FetchedPosting(text=text, title=None)

    assert posting.title is None
    assert posting.text is text


def test_fetched_posting_carries_the_title_when_the_page_had_one() -> None:
    """The other half of the port's promise. Both fields are already-validated value objects, so
    this carrier has nothing left to check — it is a record, not a rule, and naming it is what keeps
    `JobPostingFetcherPort.fetch`'s signature readable instead of a two-element tuple every caller
    unpacks from memory."""
    text = JobPostingText("a" * 150)
    title = PostingTitle("Senior Python Engineer")

    posting = FetchedPosting(text=text, title=title)

    assert posting.title == title
