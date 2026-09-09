"""The `JobPosting` aggregate: invariants J-1 … J-4 from technical-plan.md.

Pure domain tests: no I/O, no fixtures, no event loop, no mocks. Every assertion comes from the
invariant table, not from running the (currently unimplemented) constructors and recording what
they did — a test written that way has no source of truth independent of the code it guards.

What the aggregate *records* lives in the sibling `test_events.py`, mirroring the `intake` split
(`test_base_cv.py` / `test_events.py`): the questions "what state did this constructor produce?" and
"what fact did it announce to every subscriber and every log line?" have different reasons to fail,
and AC-19 is a privacy assertion that deserves to be findable on its own.

**Two of the groups below pass on arrival, and that is the design, not a gap in the cycle.** J-2 and
J-4 are enforced by the *shapes of the signatures* — there is no `url` parameter on
`from_pasted_text`, no `title` parameter either, and no `__init__` at all — so T4's skeleton already
made them true and there is no body left to write that could make them false. They are tests of an
absence, and an absence cannot be stubbed. They are grouped together and labelled so nobody reads
their green as the red-first cycle having been skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import (
    FetchedPosting,
    JobPostingId,
    JobPostingText,
    PostingSource,
    PostingTitle,
    SourceUrl,
)

_POSTING_ID = JobPostingId(value=UUID("0192f0a1-89ab-7cde-8123-456789abcdef"))
_SESSION_ID = GuestSessionId(value=UUID("11111111-1111-7111-8111-111111111111"))
# Whole-second, per ADR-0007: the `Clock` port truncates at the source so a database round trip can
# never change a value, and a test double that invented microseconds would fail an equality
# assertion after that round trip on a day nobody has time for it.
_CREATED_AT = datetime(2026, 9, 9, 10, 0, 0, tzinfo=UTC)
_URL = SourceUrl("https://boards.example.com/jobs/4821")
_TITLE = PostingTitle("Senior Python Engineer")


def _posting_text(*, words: int = 25) -> JobPostingText:
    """A minimally valid `JobPostingText`, built rather than written out as a 150-character literal.

    25 repetitions of a six-letter word: 150 non-whitespace characters (clear of the 100 floor) and
    174 characters of normalized length (far under the 30,000 ceiling). The two numbers differ on
    purpose — the bounds measure different quantities, and a helper whose two counts coincided would
    quietly stop being able to tell the event's `character_count` apart from a non-whitespace count.
    """
    return JobPostingText(" ".join(["python"] * words))


def _pasted() -> JobPosting:
    """The pasted happy path, so each test below names only the thing it is varying."""
    return JobPosting.from_pasted_text(
        id=_POSTING_ID,
        guest_session_id=_SESSION_ID,
        text=_posting_text(),
        created_at=_CREATED_AT,
    )


def _fetched(*, title: PostingTitle | None = _TITLE) -> JobPosting:
    """The fetched happy path. `title` is a parameter because a page with no readable title is an
    ordinary outcome, not a failure — plenty of job boards render theirs client-side."""
    return JobPosting.from_fetched_url(
        id=_POSTING_ID,
        guest_session_id=_SESSION_ID,
        url=_URL,
        fetched=FetchedPosting(text=_posting_text(), title=title),
        created_at=_CREATED_AT,
    )


# --- J-1: both constructors produce a complete, owned posting -----------------------------------


def test_from_pasted_text_sets_source_to_pasted() -> None:
    """The source is a field of the fact, not a second fact (see `JobPostingCaptured`), so it has
    to be set by the constructor rather than inferred later from whether a URL happens to be
    present."""
    assert _pasted().source is PostingSource.PASTED


def test_from_pasted_text_leaves_source_url_and_title_none() -> None:
    """J-2 and J-4 as observed state. A pasted posting has no URL because there was none, and no
    title because the only place one could come from is a guess at the first line of a clipboard
    paste — and a confidently wrong label is worse than an empty one."""
    posting = _pasted()

    assert posting.source_url is None
    assert posting.title is None


def test_from_pasted_text_stores_the_identity_owner_text_and_timestamp() -> None:
    """J-1: a `JobPosting` always has exactly one owner session and one valid `JobPostingText`.

    The owner assertion is the one that carries weight beyond this file — `guest_session_id` is the
    entire authorization rule for the two `GET`s in this slice, so a constructor that dropped it or
    stored the wrong one would hand one guest another guest's posting.
    """
    text = _posting_text()

    posting = JobPosting.from_pasted_text(
        id=_POSTING_ID,
        guest_session_id=_SESSION_ID,
        text=text,
        created_at=_CREATED_AT,
    )

    assert posting.id == _POSTING_ID
    assert posting.guest_session_id == _SESSION_ID
    assert posting.text == text
    assert posting.created_at == _CREATED_AT


def test_from_fetched_url_sets_source_to_fetched() -> None:
    assert _fetched().source is PostingSource.FETCHED


def test_from_fetched_url_stores_the_url_it_was_given() -> None:
    """The other half of J-2: `source == FETCHED` **iff** `source_url is not None`. The URL is
    stored as the `SourceUrl` it arrived as — already normalized and already scheme-checked by the
    value object, so the aggregate has nothing left to validate and no reason to re-parse it."""
    assert _fetched().source_url == _URL


def test_from_fetched_url_takes_its_text_and_title_from_the_fetched_posting() -> None:
    """`FetchedPosting` arrives as one value precisely so these two cannot be paired up wrongly:
    the port promises the text and the page's title together, so they travel together instead of
    as two parameters a call site could transpose."""
    fetched = FetchedPosting(text=_posting_text(), title=_TITLE)

    posting = JobPosting.from_fetched_url(
        id=_POSTING_ID,
        guest_session_id=_SESSION_ID,
        url=_URL,
        fetched=fetched,
        created_at=_CREATED_AT,
    )

    assert posting.text == fetched.text
    assert posting.title == _TITLE


def test_from_fetched_url_accepts_a_fetched_posting_with_no_title() -> None:
    """A page with no usable `<title>` still yields a perfectly good posting — the title is a
    display label, never an identifier, and `None` is how the aggregate already says "there isn't
    one". The row that would be wrong is a FETCHED posting *without a URL*, and that one is
    unconstructable; a missing title is not.
    """
    posting = _fetched(title=None)

    assert posting.title is None
    assert posting.source is PostingSource.FETCHED
    assert posting.source_url == _URL


# --- J-3: immutability after creation ------------------------------------------------------------


@pytest.mark.parametrize(
    "attribute",
    [
        "id",
        "guest_session_id",
        "source",
        "source_url",
        "title",
        "text",
        "created_at",
    ],
)
def test_job_posting_properties_are_read_only(attribute: str) -> None:
    """J-3, asserted over the whole public surface rather than one representative property.

    Editing a posting's text arrives in slice 1.4 as the browser editor, and it will arrive as a
    *named method* with its own event and its own answer to "what does an edit do to a tailoring run
    derived from the old text?". Someone adding a bare setter here in the meantime would take that
    decision for us silently — which is exactly why this test enumerates all seven properties: a
    setter added to any one of them turns a test red instead of shipping.
    """
    posting = _pasted()

    with pytest.raises(AttributeError):
        setattr(posting, attribute, "a bare setter would let this through")


# --- Green on arrival: the invariants enforced by a signature ------------------------------------
#
# J-2 and J-4 are not checks, they are absences — no `url` parameter, no `title` parameter, no
# `__init__` — so T4's skeleton already satisfies them and these five tests pass immediately. That
# is the point rather than a hole in the cycle: an invariant that has become a shape cannot be
# broken by a body, so there is nothing here that a `NotImplementedError` could have failed. What
# these tests defend against is a *future* edit that relaxes the shape, which is when they go red.


def test_from_pasted_text_rejects_a_url_argument() -> None:
    """**J-2, and the most important test in this file.** There must be no way to construct a
    PASTED posting that carries a URL.

    Asserted structurally — the constructor has no `url` parameter, so Python refuses the call
    before any body runs — rather than by passing one and catching a validation error. The
    difference is what the two versions prove. A runtime check proves that *this* constructor
    currently rejects the combination; an absent parameter proves there is no call site anywhere
    that can express it, which is the same "make the invalid state unconstructable" move
    `SourceUrl`'s scheme allow-list makes. It is also why the aggregate has two named constructors
    instead of one `create(source, url=None, …)`: that version would push J-2 into a runtime `if`
    and invite every caller to get the combination wrong.
    """
    with pytest.raises(TypeError):
        JobPosting.from_pasted_text(
            id=_POSTING_ID,
            guest_session_id=_SESSION_ID,
            text=_posting_text(),
            created_at=_CREATED_AT,
            url=_URL,  # type: ignore[call-arg]
        )


def test_from_pasted_text_rejects_a_title_argument() -> None:
    """J-4 written as a signature. There is no argument to pass, so there is no call site that can
    pass one, so there is no branch anywhere that has to decide what a pasted posting's title
    should be. Letting a user *name* a posting is a plausible 1.4 feature — it will arrive as an
    explicit method, and this test is what makes "relax the constructor instead" go red first."""
    with pytest.raises(TypeError):
        JobPosting.from_pasted_text(
            id=_POSTING_ID,
            guest_session_id=_SESSION_ID,
            text=_posting_text(),
            created_at=_CREATED_AT,
            title=_TITLE,  # type: ignore[call-arg]
        )


def test_from_fetched_url_requires_a_url() -> None:
    """The other direction of J-2: `from_fetched_url` cannot omit the URL. Required-ness is the
    type, not a check — which is what makes the pair of constructors *unable* to disagree about
    `source == FETCHED iff source_url is not None`."""
    with pytest.raises(TypeError):
        JobPosting.from_fetched_url(  # type: ignore[call-arg]
            id=_POSTING_ID,
            guest_session_id=_SESSION_ID,
            fetched=FetchedPosting(text=_posting_text(), title=None),
            created_at=_CREATED_AT,
        )


def test_job_posting_cannot_be_constructed_directly() -> None:
    """The rule that there are exactly two constructors is enforced by an **absence**, and this
    test is what guards the absence.

    `JobPosting` defines no `__init__`, so a direct call falls through to `object.__init__`, which
    refuses arguments. That is the mechanism behind J-2: a third way to build one would be a way to
    set `source` and `source_url` independently, and every invariant argument above rests on there
    not being one. Someone adding an `__init__` later — for a test fixture, for SQLAlchemy, for
    convenience — would dismantle that quietly, and this assertion is the thing that notices.
    (The imperative mapping does not need one: it instruments `__dict__` directly and never calls
    `__init__` on the load path, ADR-0007.)
    """
    with pytest.raises(TypeError):
        JobPosting(  # type: ignore[call-arg]
            id=_POSTING_ID,
            guest_session_id=_SESSION_ID,
            text=_posting_text(),
            created_at=_CREATED_AT,
        )


def test_job_posting_takes_no_positional_arguments_either() -> None:
    """The same absence from the other side. Both constructors are keyword-only, so a positional
    call is refused too — which matters here because the two share four of five parameter meanings
    and differ in the fifth, the exact shape where a positional call site reads correctly and means
    something else."""
    with pytest.raises(TypeError):
        JobPosting(_POSTING_ID, _SESSION_ID)  # type: ignore[call-arg]
