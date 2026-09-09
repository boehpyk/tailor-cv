"""The `JobPosting` aggregate: a captured job description, and how it got here.

Composes `RecordsEvents` (`domain/shared/events.py`) rather than inheriting a shared aggregate base
class, and shares **no** base class with `BaseCv` — CLAUDE.md is explicit that two aggregates with
the same shape do not get one. The shape really is similar (an id, an owner session, a created-at, a
chunk of validated text), and that similarity is exactly the trap: the *rules* differ, and a base
class would have to guess which set it enforces. `BaseCv` records a failure as a state of itself;
`JobPosting` cannot exist at all unless it succeeded (ADR-0013). No supertype could hold both.

**SKELETON (T4).** The two constructors raise `NotImplementedError`; their bodies arrive at T6,
after `qa` records the red. Everything else here is real — the attribute annotations, the property
projections, and the invariant reasoning — because none of it has behaviour that could fail an
assertion, and a skeleton exists so that a test fails on its *assertion* rather than on an
`ImportError` (docs/sdlc.md §2).
"""

from __future__ import annotations

from datetime import datetime

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.posting.value_objects import (
    FetchedPosting,
    JobPostingId,
    JobPostingText,
    PostingSource,
    PostingTitle,
    SourceUrl,
)
from tailorcraft.domain.shared.events import RecordsEvents


# NOT `slots=True`: this aggregate is later mapped by SQLAlchemy's *imperative* mapping
# (`registry.map_imperatively`), which instruments attributes on the instance `__dict__` — a
# `__slots__` class has none, so mapping would fail at wiring time rather than at import time, which
# is a much worse place to discover it. This is a deliberate departure from the value objects one
# module over, which are all `slots=True` because nothing ever maps them directly: a reader who has
# just written `slots=True` on five value objects in `value_objects.py` and arrives here should find
# the reason rather than "fix" the inconsistency. `BaseCv` carries the same comment for the same
# reason, and the duplication is on purpose — a reader lands on one class or the other, not on both.
# The private-attribute names below are exactly what
# `infrastructure/persistence/mapping/posting/job_posting.py` will target, so renaming one here is a
# breaking change to that module too (ADR-0007).
class JobPosting(RecordsEvents):
    """A job description a visitor captured: its text, its owner session, and — when it was fetched
    rather than pasted — the URL it came from and the title read off the page.

    Invariants (technical-plan.md):

    - **J-1** — A `JobPosting` always has exactly one owner session and one valid `JobPostingText`
      (≥ 100 non-whitespace characters, ≤ 30,000 characters of normalized length). Both
      constructors require both, and the length rules live in the value object rather than here, so
      an invalid text cannot exist to be held. This is the invariant that makes 1.3 simple: the
      tailoring use case never asks "is this posting's text actually there?" — the type answers it.
    - **J-2** — `source == FETCHED` **iff** `source_url is not None`. Protected by there being two
      named constructors and **no third way to build one**: `from_pasted_text` cannot set a URL and
      `from_fetched_url` cannot omit one, so the pair is unable to disagree. This is precisely why
      it is two constructors rather than one `create(source, url=None, title=None, …)` — that
      version would push the invariant into a runtime `if` inside the constructor and invite every
      caller to get the combination wrong, which is the same argument `ExtractedText` makes for a
      short string: make the invalid state unconstructable rather than checked. The rule is also
      mirrored as a database `CHECK` (AC-21), because two classmethods do not bind a hand-written
      backfill or a `psql` session at 2 a.m.
    - **J-3** — A `JobPosting` is **immutable after creation**. There is no `edit()` and there is no
      setter; the properties below are read-only projections. Recording *why* there is nothing here
      is the point of this row: editing the text arrives in slice 1.4 as the browser editor, and it
      will arrive as a named method with its own event and its own rules about what an edit does to
      a tailoring run derived from the old text. Someone adding a bare `text` setter in the meantime
      would silently take that decision for us.
    - **J-4** — `source == PASTED` ⇒ `title is None`. Enforced by a signature: `from_pasted_text`
      does not accept a title at all. A title we did not read from a page would have to be invented
      from the first line of a clipboard paste, and a confidently wrong label is worse than an empty
      one — the aggregate already models "no title" as `None`, so a guessed one would be a second,
      untrustworthy way of saying the same thing. Letting a user *name* a posting is a plausible
      1.4 feature and will arrive as an explicit method, not by relaxing this.

    **Deliberately not invariants of `JobPosting`:**

    - The "at most 10 postings per session" cap. It spans every `JobPosting` a session owns, which
      is a fact no single instance has access to — reaching for it from inside a constructor would
      mean a repository call in a constructor. It lives in the `CaptureJobPosting` use case with a
      comment there saying why, exactly as `TooManyBaseCvs` does.
    - Every fetch bound: the timeout, the response byte cap, the redirect limit. Those are
      configuration read by an adapter, and the domain must not read settings (CLAUDE.md:
      `os.environ` is read in exactly one place). This aggregate has no opinion about how its text
      was obtained beyond `source`.
    """

    # Class-level annotations only (no assignment): with no `__init__` override, this is how
    # `mypy --strict` learns the types of the attributes the two constructors set directly on the
    # instance and the properties below read back. SQLAlchemy's imperative mapping targets these
    # exact names.
    _id: JobPostingId
    _guest_session_id: GuestSessionId
    _source: PostingSource
    _source_url: SourceUrl | None
    _title: PostingTitle | None
    _text: JobPostingText
    _created_at: datetime

    # No `__init__` override, and the absence is load-bearing. `from_pasted_text` and
    # `from_fetched_url` are the only ways application code builds a valid instance — calling
    # `JobPosting(...)` directly falls through to `object.__init__`, which rejects any keyword
    # argument, so "there are exactly two constructors" (the mechanism behind J-2) is enforced by
    # there being no constructor here rather than by convention or a comment. `GuestSession` uses
    # the same absence for the same purpose. SQLAlchemy's imperative mapping does not need
    # `__init__` either: it rehydrates a mapped instance by instrumenting `__dict__` directly and
    # never calls it on the load path (ADR-0007).

    @classmethod
    def from_pasted_text(
        cls,
        *,
        id: JobPostingId,
        guest_session_id: GuestSessionId,
        text: JobPostingText,
        created_at: datetime,
    ) -> JobPosting:
        """Build a posting from text the visitor pasted in. Sets `source = PASTED`,
        `source_url = None`, `title = None`, and records exactly one `JobPostingCaptured` (with
        `source=PASTED` and `character_count` taken from `text`).

        Keyword-only, like its sibling: the two constructors share four of five parameter meanings
        and differ in the fifth, which is precisely the shape where a positional call site reads
        correctly and means something else.

        **This deliberately contradicts `BaseCv.upload`, which takes its seven parameters
        positionally**, and the contradiction is recorded here rather than resolved because the two
        situations are not the same one. `BaseCv` has a single constructor, so there is no sibling
        for a positional call to be confused with; `JobPosting` has two that overlap in four
        arguments, so `JobPosting.from_fetched_url(a, b, c, d)` and
        `JobPosting.from_pasted_text(a, b, c, d)` would both type-check while meaning different
        things. A reader who notices the inconsistency should find this paragraph rather than
        "fix" one of the two to match the other (CLAUDE.md).

        **Takes no title, and that is J-4 written as a signature** rather than as a check. There is
        no argument to pass, so there is no call site that can pass one, so there is no branch
        anywhere that has to decide what a pasted posting's title should be.
        """
        raise NotImplementedError

    @classmethod
    def from_fetched_url(
        cls,
        *,
        id: JobPostingId,
        guest_session_id: GuestSessionId,
        url: SourceUrl,
        fetched: FetchedPosting,
        created_at: datetime,
    ) -> JobPosting:
        """Build a posting from a page `JobPostingFetcherPort` retrieved. Sets `source = FETCHED`,
        `source_url = url`, `text` and `title` from `fetched`, and records exactly one
        `JobPostingCaptured` (with `source=FETCHED` and `character_count` taken from the text).

        `fetched: FetchedPosting` carries the extracted text and the page's title-or-`None` as one
        value, which is what keeps this signature from growing a `text` and a `title` parameter that
        a caller could pair up wrongly — the port promises them together, so they arrive together.

        The URL is required and cannot be `None`: that half of J-2 is the type, not a check.
        """
        raise NotImplementedError

    @property
    def id(self) -> JobPostingId:
        return self._id

    @property
    def guest_session_id(self) -> GuestSessionId:
        return self._guest_session_id

    @property
    def source(self) -> PostingSource:
        return self._source

    @property
    def source_url(self) -> SourceUrl | None:
        return self._source_url

    @property
    def title(self) -> PostingTitle | None:
        return self._title

    @property
    def text(self) -> JobPostingText:
        return self._text

    @property
    def created_at(self) -> datetime:
        return self._created_at
