"""Value objects for the `tailoring` context: `TailoredCv`, `CoverLetter`, `TailoredDocuments`,
`ModelName`, `PromptVersion`, `LlmCallMetrics`.

Pure domain tests: no database, no event loop, no mocks, no network, no fixtures beyond
`parametrize`. These are the cheapest tests in the suite and they should stay that way.

Every assertion comes from the feature spec and the technical plan's "Value objects" section (the
400/200 floors, the 20,000/8,000 ceilings, the `[A-Za-z0-9._-]` grammar, the 64/16 length caps) —
**not** from running the code, which at the time of writing raises `NotImplementedError` in every
`__post_init__` on purpose (docs/sdlc.md §2). The bounds are hard-coded here rather than imported
from `value_objects`'s private module constants: a test that imports the number it is checking
cannot disagree with the code, which is exactly the failure mode CLAUDE.md warns about.

**The most important test in this file is `test_tailored_document_survives_as_markdown_with_every_newline_intact`.**
`TailoredCv` and `CoverLetter` deliberately do NOT collapse whitespace the way `ExtractedText` and
`JobPostingText` do — the newline is semantic in Markdown, not layout noise. If a future reader
"fixes" these two types to match that other family's `" ".join(value.split())`, this is the test
that goes red and tells them why.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError

import pytest

from tailorcraft.domain.tailoring.errors import (
    EmptyTailoredDocument,
    InvalidLlmCallMetrics,
    InvalidModelName,
    InvalidPromptVersion,
    InvalidTailoredDocument,
    TailoredDocumentTooLong,
    TailoredDocumentTooShort,
)
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocuments,
)

# --- TailoredCv / CoverLetter: shared rules, different bounds -------------------------------------
#
# Both types enforce the same four rules (blank, floor, ceiling, control characters) and the same
# two normalizations (CRLF -> LF, per-line trailing-space stripping) — `TailoredCv`'s docstring
# carries the full reasoning and `CoverLetter`'s applies it verbatim except for its own numbers. The
# behaviour tests below are parametrized over both classes; the numeric-bound tests are not, because
# the number is the point of each one and parametrizing it away would hide a 400 written where a
# 200 belonged.

_document_types = pytest.mark.parametrize(
    "document_type", [TailoredCv, CoverLetter], ids=["TailoredCv", "CoverLetter"]
)


@_document_types
@pytest.mark.parametrize("raw", ["", "   \n\t  "], ids=["empty-string", "whitespace-only"])
def test_tailored_document_rejects_blank_content(
    document_type: type[TailoredCv | CoverLetter], raw: str
) -> None:
    """Blank is not "too short" — it is nothing at all, and the model returning nothing usable gets
    its own error rather than colliding with the floor's message."""
    with pytest.raises(EmptyTailoredDocument):
        document_type(raw)


def test_tailored_cv_rejects_399_non_whitespace_characters() -> None:
    """One character short of the 400 floor. Chosen, not measured (OQ-5): a model that stops at
    "Here is your tailored CV:" has failed, and this is where that gets decided."""
    with pytest.raises(TailoredDocumentTooShort):
        TailoredCv("a" * 399)


def test_tailored_cv_accepts_exactly_400_non_whitespace_characters() -> None:
    """The floor is inclusive: 400 is the shortest acceptable CV, not the first rejected one."""
    cv = TailoredCv("a" * 400)

    assert cv.character_count == 400


def test_tailored_cv_accepts_exactly_20000_characters() -> None:
    """The ceiling is inclusive too — the other half of the boundary pair, because a `>=`/`>` slip
    only ever shows up on one side."""
    cv = TailoredCv("a" * 20_000)

    assert cv.character_count == 20_000


def test_tailored_cv_rejects_20001_characters() -> None:
    """One character past the ceiling: it bounds what one paid call can push into Postgres and into
    1.5's renderer, and a runaway generation is not content."""
    with pytest.raises(TailoredDocumentTooLong):
        TailoredCv("a" * 20_001)


def test_cover_letter_rejects_199_non_whitespace_characters() -> None:
    """One character short of the letter's 200 floor — half the CV's, because a genuine cover
    letter is three paragraphs and a CV is not."""
    with pytest.raises(TailoredDocumentTooShort):
        CoverLetter("a" * 199)


def test_cover_letter_accepts_exactly_200_non_whitespace_characters() -> None:
    letter = CoverLetter("a" * 200)

    assert letter.character_count == 200


def test_cover_letter_accepts_exactly_8000_characters() -> None:
    letter = CoverLetter("a" * 8_000)

    assert letter.character_count == 8_000


def test_cover_letter_rejects_8001_characters() -> None:
    """One character past the ceiling: well under half the CV's, because a 30,000-character "cover
    letter" is a runaway generation, not content."""
    with pytest.raises(TailoredDocumentTooLong):
        CoverLetter("a" * 8_001)


@_document_types
def test_tailored_document_rejects_an_embedded_nul(
    document_type: type[TailoredCv | CoverLetter],
) -> None:
    """A NUL is a C-side string terminator: a document carrying one can mean two different things
    to the database, the renderer and the browser, so it is refused rather than passed through."""
    with pytest.raises(InvalidTailoredDocument):
        document_type("a" * 250 + "\x00" + "a" * 250)


@_document_types
def test_tailored_document_reports_control_character_before_length_floor(
    document_type: type[TailoredCv | CoverLetter],
) -> None:
    """**Addendum, not part of the red cycle.** Pins an ordering the fixtures above cannot see: every
    malformed fixture in this file is exactly 500 non-whitespace characters (250 + 1 + 250), so each
    one clears the 400/200 floor regardless of which check runs first, and none of them can tell
    "control-before-bounds" apart from "bounds-before-control". This fixture is short — 50 characters,
    comfortably under both floors — precisely so the two orderings disagree: bounds-first would
    report `TailoredDocumentTooShort`, control-first reports `InvalidTailoredDocument`.

    The decision, settled here on purpose because the spec left it open: **control-character before
    bounds.** "This is not a document at all" outranks "this document is the wrong size" — a NUL
    means the model's output is corrupt at any length, and a corrupt 50-character string is not
    usefully described as "37 characters too short". This test passes immediately against the current
    implementation (`TailoredCv.__post_init__` and `CoverLetter.__post_init__` both call
    `_has_disallowed_control_character` before either bound check) — it is a test-after guard on a
    decision already made, not a red-first assertion, and it is expected to be green the moment it is
    committed.
    """
    with pytest.raises(InvalidTailoredDocument):
        document_type("a" * 50 + "\x00")


@_document_types
def test_tailored_document_rejects_a_bare_vertical_tab(
    document_type: type[TailoredCv | CoverLetter],
) -> None:
    """`\\x0b` is a control character other than `\\n` or `\\t`, and the type refuses every one of
    those regardless of which specific byte it is."""
    with pytest.raises(InvalidTailoredDocument):
        document_type("a" * 250 + "\x0b" + "a" * 250)


@_document_types
def test_tailored_document_rejects_a_lone_carriage_return(
    document_type: type[TailoredCv | CoverLetter],
) -> None:
    """A `\\r` not immediately followed by `\\n` is rejected, not normalized — settled deliberately
    in `TailoredCv`'s docstring rather than left for whichever test happened to land first.

    `\\r\\n` is rewritten to `\\n` **first**, so anything still carrying a bare `\\r` afterwards is a
    control character that is neither `\\n` nor `\\t`, and the control-character rule refuses it. A
    classic-Mac line ending is not a thing a 2026 language model emits, and silently rewriting one
    would mean guessing at what the model meant in the one place this codebase has decided not to
    guess.
    """
    with pytest.raises(InvalidTailoredDocument):
        document_type("a" * 250 + "\r" + "a" * 250)


@_document_types
def test_tailored_document_normalizes_crlf_to_lf(
    document_type: type[TailoredCv | CoverLetter],
) -> None:
    raw = ("a" * 250) + "\r\n" + ("a" * 250)
    expected = ("a" * 250) + "\n" + ("a" * 250)

    assert document_type(raw).value == expected


@_document_types
def test_tailored_document_strips_trailing_spaces_per_line(
    document_type: type[TailoredCv | CoverLetter],
) -> None:
    """Every line's trailing whitespace is stripped, not only the document's — a model that pads
    every bullet with a trailing space before its newline should not change the value's identity,
    and a check applied only at the end of the string would miss every line but the last."""
    raw = "a" * 250 + "   \n" + "b" * 250 + "  \t \n" + "c" * 250
    expected = "a" * 250 + "\n" + "b" * 250 + "\n" + "c" * 250

    assert document_type(raw).value == expected


@_document_types
def test_tailored_document_keeps_tabs_and_newlines(
    document_type: type[TailoredCv | CoverLetter],
) -> None:
    """`\\t` and `\\n` are the two control characters this type declares legal, and they must
    survive unchanged — the tab sits mid-line here, not at a line's end, so the trailing-space rule
    cannot be the thing that happens to preserve it."""
    raw = "a" * 200 + "\t" + "b" * 200 + "\n" + "c" * 200

    assert document_type(raw).value == raw


# The Markdown fixture below is deliberately not built from a repeated character: the whole point
# of this test is that the *structure* — headings, blank paragraph breaks, a bulleted list — must
# come out the other side unchanged, and a fixture of "a" * n cannot show that. 597 non-whitespace
# characters and 711 total, comfortably past both floors (400/200) and nowhere near either ceiling
# (20,000/8,000), so no boundary rule is what would reject or admit it.
_MARKDOWN_DOCUMENT = (
    "# Senior Backend Engineer\n"
    "\n"
    "## Summary\n"
    "\n"
    "A backend engineer with eight years of experience building payment systems that process "
    "millions of transactions a day, with a focus on reliability, observability and mentoring "
    "engineers earlier in their careers.\n"
    "\n"
    "## Experience\n"
    "\n"
    "- Led the payments team through a period of rapid growth, redesigning the ledger service to "
    "handle a tenfold increase in transaction volume without downtime.\n"
    "- Reduced checkout latency by forty percent across three quarters by rewriting the hot path "
    "in the settlement pipeline and removing two synchronous network calls.\n"
    "- Mentored four engineers who were later promoted to senior roles.\n"
    "\n"
    "## Skills\n"
    "\n"
    "- Python\n"
    "- PostgreSQL\n"
    "- Distributed systems"
)


@_document_types
def test_tailored_document_survives_as_markdown_with_every_newline_intact(
    document_type: type[TailoredCv | CoverLetter],
) -> None:
    """**The contradiction test.** `ExtractedText` and `JobPostingText` both collapse whitespace
    with `" ".join(value.split())` because their text's layout is noise — a PDF's column breaks, a
    scraped page's indentation. `TailoredCv` and `CoverLetter` hold Markdown, where the newline is
    semantic: a blank line is a paragraph break and a leading `- ` is a bullet. If someone "fixes"
    either type to match the other family, every assertion below goes red, and that is the point —
    it is the one test in this file that a whitespace-collapsing "cleanup" cannot survive.

    The fixture has no trailing whitespace on any line and no `\\r`, so normalization is a no-op:
    the value must come back byte-for-byte identical to what went in, not merely "close enough".
    """
    document = document_type(_MARKDOWN_DOCUMENT)

    assert document.value == _MARKDOWN_DOCUMENT
    assert document.value.count("\n") == 16

    lines = document.value.split("\n")
    assert len(lines) == 17
    assert sum(1 for line in lines if line == "") == 6  # the blank paragraph breaks
    assert sum(1 for line in lines if line.startswith("- ")) == 6  # the bulleted lines


def test_tailored_cv_character_count_reports_normalized_length_not_non_whitespace_count() -> None:
    """450 non-whitespace characters spread across 90 space-separated words is comfortably past
    the 400 floor (measured in non-whitespace characters) — and, once the 89 separating spaces are
    counted too, a different and larger number: 539. `character_count` must report the second one,
    the number a person counting characters in the document in front of them would arrive at, not
    the internal validation quantity the floor uses.
    """
    raw = " ".join(["aaaaa"] * 90)  # 450 non-whitespace characters, 539 characters total

    cv = TailoredCv(raw)

    assert cv.character_count == 539


def test_cover_letter_character_count_reports_normalized_length_not_non_whitespace_count() -> None:
    """The same asymmetry, pinned for `CoverLetter` too: 225 non-whitespace characters (past the
    200 floor) and 269 characters once the 44 separating spaces are counted."""
    raw = " ".join(["aaaaa"] * 45)  # 225 non-whitespace characters, 269 characters total

    letter = CoverLetter(raw)

    assert letter.character_count == 269


# --- ModelName -------------------------------------------------------------------------------------


def test_model_name_accepts_a_realistic_model_id() -> None:
    assert ModelName("gemini-2.5-flash").value == "gemini-2.5-flash"


def test_model_name_rejects_a_blank_string() -> None:
    with pytest.raises(InvalidModelName):
        ModelName("")


def test_model_name_rejects_internal_whitespace() -> None:
    """A model id is one unbroken token — the same rule `SourceUrl` applies to a scheme, for the
    same reason: whitespace inside it means two parsers can disagree about where it ends."""
    with pytest.raises(InvalidModelName):
        ModelName("gemini 2.5 flash")


def test_model_name_rejects_a_control_character() -> None:
    with pytest.raises(InvalidModelName):
        ModelName("gemini\x012.5-flash")


def test_model_name_accepts_exactly_64_characters() -> None:
    value = "a" * 64

    assert ModelName(value).value == value


def test_model_name_rejects_65_characters() -> None:
    with pytest.raises(InvalidModelName):
        ModelName("a" * 65)


# --- PromptVersion -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("1", id="bare-integer"),
        pytest.param("1.2", id="dotted-pair"),
        pytest.param("v1_a-b.c", id="mixed-grammar"),
    ],
)
def test_prompt_version_accepts_the_grammar(raw: str) -> None:
    assert PromptVersion(raw).value == raw


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("v1 a", id="space"),
        pytest.param("v1/a", id="slash"),
        pytest.param("v1#a", id="hash"),
        pytest.param("", id="empty-string"),
    ],
)
def test_prompt_version_rejects_characters_outside_the_grammar(raw: str) -> None:
    """A prompt version is a key — it keys a persisted column, a log field and any future
    regression comparison — and a key with a space or a slash in it renders differently in at
    least one of those three."""
    with pytest.raises(InvalidPromptVersion):
        PromptVersion(raw)


def test_prompt_version_accepts_exactly_16_characters() -> None:
    value = "a" * 16

    assert PromptVersion(value).value == value


def test_prompt_version_rejects_17_characters() -> None:
    with pytest.raises(InvalidPromptVersion):
        PromptVersion("a" * 17)


# --- LlmCallMetrics ----------------------------------------------------------------------------------


def test_llm_call_metrics_constructs_with_valid_values() -> None:
    metrics = LlmCallMetrics(
        model=ModelName("gemini-2.5-flash"),
        prompt_version=PromptVersion("v1"),
        prompt_tokens=1_200,
        completion_tokens=800,
        duration_ms=4_300,
    )

    assert metrics.prompt_tokens == 1_200
    assert metrics.completion_tokens == 800
    assert metrics.duration_ms == 4_300


def test_llm_call_metrics_accepts_zero_for_all_three_integers() -> None:
    """Zero is legal, not a boundary violation: a provider that reports no usage metadata is a gap
    in accounting, not a reason to fail a run that produced two good documents."""
    metrics = LlmCallMetrics(
        model=ModelName("gemini-2.5-flash"),
        prompt_version=PromptVersion("v1"),
        prompt_tokens=0,
        completion_tokens=0,
        duration_ms=0,
    )

    assert metrics.prompt_tokens == 0
    assert metrics.completion_tokens == 0
    assert metrics.duration_ms == 0


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "duration_ms"])
def test_llm_call_metrics_rejects_a_negative_value(field: str) -> None:
    """A negative token count or duration is a bug in the adapter's arithmetic, and it should
    surface where it was made rather than as an impossible number in a latency percentile three
    weeks later."""
    kwargs: dict[str, object] = {
        "model": ModelName("gemini-2.5-flash"),
        "prompt_version": PromptVersion("v1"),
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "duration_ms": 1_200,
    }
    kwargs[field] = -1

    with pytest.raises(InvalidLlmCallMetrics):
        LlmCallMetrics(**kwargs)  # type: ignore[arg-type]


# --- TailoredDocuments -------------------------------------------------------------------------------
#
# `TailoredDocuments` itself has no `__post_init__` — both fields are already-validated value
# objects, so there is nothing left for this carrier to check. But building one for this test still
# requires a real `TailoredCv`, whose `__post_init__` is still a skeleton, so this test is red until
# T3 for the same reason `test_fetched_posting_*` was red until 1.2's T3: the type under test is
# complete, the fixture it needs is not. That is the cycle working, not a hole in it.


def test_tailored_documents_cannot_be_constructed_with_only_one_document() -> None:
    """This is how invariant TR-5 is enforced: "succeeded with no cover letter" is not a state that
    gets checked and rejected at runtime, it is a state this type cannot represent at all. The
    ordinary dataclass `TypeError` for a missing required argument **is** the enforcement — there is
    no custom domain error here because there is nothing left for one to say."""
    with pytest.raises(TypeError):
        TailoredDocuments(cv=TailoredCv("a" * 400))  # type: ignore[call-arg]


# --- Value semantics: compare by value, frozen ------------------------------------------------------

_value_object_builders: list[tuple[Callable[[], object], str, str]] = [
    (lambda: TailoredCv("a" * 400), "value", "TailoredCv"),
    (lambda: CoverLetter("a" * 200), "value", "CoverLetter"),
    (lambda: ModelName("gemini-2.5-flash"), "value", "ModelName"),
    (lambda: PromptVersion("v1"), "value", "PromptVersion"),
    (
        lambda: LlmCallMetrics(
            model=ModelName("gemini-2.5-flash"),
            prompt_version=PromptVersion("v1"),
            prompt_tokens=100,
            completion_tokens=50,
            duration_ms=1_200,
        ),
        "prompt_tokens",
        "LlmCallMetrics",
    ),
    (
        lambda: TailoredDocuments(cv=TailoredCv("a" * 400), cover_letter=CoverLetter("a" * 200)),
        "cv",
        "TailoredDocuments",
    ),
]


@pytest.mark.parametrize(
    ("build", "attribute"),
    [pytest.param(build, attribute, id=name) for build, attribute, name in _value_object_builders],
)
def test_value_object_compares_equal_to_a_separately_built_equivalent(
    build: Callable[[], object], attribute: str
) -> None:
    """Two independently constructed instances with the same fields are equal — proving this is
    value comparison rather than identity, since two calls to `build()` never return the same
    object. The authorization and idempotency checks this codebase relies on depend on exactly this.
    """
    del attribute  # unused here; shared parametrize table with the frozen test below
    assert build() == build()


@pytest.mark.parametrize(
    ("build", "attribute"),
    [pytest.param(build, attribute, id=name) for build, attribute, name in _value_object_builders],
)
def test_value_object_is_frozen(build: Callable[[], object], attribute: str) -> None:
    """Immutability is what lets a value object be passed around without a defensive copy. A field
    that could be reassigned in place would let one caller's mutation change what a second caller —
    possibly an authorization check holding the same instance — is looking at."""
    instance = build()

    with pytest.raises(FrozenInstanceError):
        setattr(instance, attribute, getattr(instance, attribute))
