"""Value objects for the `tailoring` context: what makes a tailored document a valid one.

The same rule `intake` and `posting` follow: every field of `TailoringRun` that has a rule gets its
own type rather than staying a bare `str` or `int`. All frozen `@dataclass(frozen=True,
slots=True)`, validating in `__post_init__`, compared by value. Never Pydantic (ADR-0002): a
`BaseModel` here would drag JSON aliases and `model_config` into business rules that have nothing to
do with the HTTP boundary — and this is the one context where the temptation is strongest, because
the model's answer arrives as JSON. It is parsed and re-validated in `infrastructure/llm/parsing.py`
and only then does it become one of these types.

Five of these types validate; five do not. `TailoredCv`, `CoverLetter`, `ModelName`, `PromptVersion`
and `LlmCallMetrics` each enforce their rule in `__post_init__`, and every one of those rules was
watched failing on its own assertion before it was written (docs/sdlc.md §2) — the skeleton step
fixed the signatures, the field types and the bounds so that the red step could fail on a `raises`
assertion rather than on an `ImportError`.

The other five — `TailoringRunId`, `TailoredDocuments`, `TailoringRunStatus`,
`TailoringFailureReason` and `TailoredDraft` — have nothing to enforce: a typed UUID, two carriers of
already-validated value objects, and two closed enums. There was nothing to defer, so no test of
theirs was ever red. That is the tiered cycle working rather than a hole in it, exactly as
`PostingSource`, `FetchedPosting` and `FetchFailureReason` were in slice 1.2.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

# `TailoredCv`'s two bounds. They count two different things, and that asymmetry is inherited from
# `JobPostingText` on purpose: the *floor* asks "is there real content here?" and whitespace is not
# content, so it counts non-whitespace characters and a document padded with blank lines cannot sneak
# past it; the *ceiling* asks "is this too big for a column, a renderer and a screen?" and a newline
# costs a byte and a line of screen just as a letter does, so it counts the length of the normalized
# text — which is exactly what `character_count` reports to the user.
#
# 400 non-whitespace characters is roughly a third of a page: a model that answered "Here is your
# tailored CV:" has failed, and this is where that is decided. **Chosen, not measured** (OQ-5):
# verify against `api/eval/corpus/` and change it once, on purpose, in the spec and the code
# together — never by letting a test ratify whatever the corpus happened to produce.
_MIN_TAILORED_CV_NON_WHITESPACE_CHARACTERS = 400
_MAX_TAILORED_CV_LENGTH = 20_000

# `CoverLetter`'s two bounds, same split for the same reasons. The floor is half the CV's because a
# genuine cover letter is three paragraphs and a CV is not; the ceiling is well under half because a
# 30,000-character "cover letter" is a runaway generation, not content.
_MIN_COVER_LETTER_NON_WHITESPACE_CHARACTERS = 200
_MAX_COVER_LETTER_LENGTH = 8_000

# `ModelName`: 1-64 characters. Long enough for the longest vendor identifier anyone ships
# (`gemini-2.5-flash-preview-09-2025` is 32) and short enough that a provenance column cannot become
# somewhere a stranger's string lands.
_MAX_MODEL_NAME_LENGTH = 64

# `PromptVersion`: 1-16 characters from `[A-Za-z0-9._-]`. A closed grammar rather than a length
# check, because this value is a *key* — it keys log lines, a persisted column and any future
# regression comparison — and a key with a space or a slash in it is a key that renders differently
# in two of those three places.
_MAX_PROMPT_VERSION_LENGTH = 16
_PROMPT_VERSION_ALLOWED_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)

# The document types' two legal control characters. Named rather than written inline in a `not in
# "\n\t"` test, because that literal form silently also accepts the empty string and reads as a
# substring check when it is meant as a membership one.
_ALLOWED_DOCUMENT_CONTROL_CHARACTERS = frozenset({"\n", "\t"})


def _has_disallowed_control_character(value: str) -> bool:
    """True if `value` holds a C0 control character, DEL or NUL that is not `\\n` or `\\t`.

    The document types' rule. It is `domain/posting/value_objects.py`'s `_has_control_character` with
    two characters carved out, and the carve-out is the whole difference between the two families: a
    job posting's newlines are layout noise that gets collapsed away, while these types hold Markdown
    where a newline is a paragraph break and a tab is indentation inside a line. Everything else in
    the C0 range stays refused — a NUL above all, which terminates a C-side string and so lets the
    database, the renderer and the browser disagree about where the document ends.
    """
    return any(
        (ord(char) < 0x20 or ord(char) == 0x7F) and char not in _ALLOWED_DOCUMENT_CONTROL_CHARACTERS
        for char in value
    )


def _has_control_or_whitespace(value: str) -> bool:
    """True if `value` holds any whitespace *or* a control character — `ModelName`'s rule, where a
    model id is one unbroken token and all three are rejections.

    `str.isspace()` does the whitespace half rather than a literal set, because it covers the Unicode
    separators — NEL, NBSP, the line separator — that a copy-paste out of a rendered docs page carries
    and a hand-written `{" ", "\\t", "\\n"}` misses. `SourceUrl` applies the identical predicate to a
    URL for the identical reason, and the duplication is deliberate: importing a private helper across
    two bounded contexts would couple `tailoring` to `posting` for four characters of code.
    """
    return any(char.isspace() for char in value) or any(
        ord(char) < 0x20 or ord(char) == 0x7F for char in value
    )


@dataclass(frozen=True, slots=True)
class TailoringRunId:
    """A `TailoringRun`'s identity, typed so a signature cannot silently accept the wrong UUID.

    The reason is the same one `BaseCvId` and `JobPostingId` give, only louder here: this aggregate
    holds **three** foreign ids — its owning session, its base CV and its job posting — plus its own.
    With bare `UUID`s on all four, transposing two arguments in `TailoringRun.request(...)` is a
    runtime bug whose only symptom is a run tailoring the wrong person's CV. With four distinct types
    it is a `mypy --strict` error before the code ever runs.
    """

    value: UUID

    # No `__post_init__` here on purpose: every `UUID` is already a valid `TailoringRunId`, so a
    # validation method that does nothing would just be a place a future reader adds a rule that does
    # not belong. `BaseCvId`, `JobPostingId` and `GuestSessionId` are the same shape for the same
    # reason. This type is therefore complete as written — there was nothing for T2 to turn red.


@dataclass(frozen=True, slots=True)
class TailoredCv:
    """The tailored CV the model produced, as Markdown.

    A separate type from `CoverLetter` rather than one `TailoredDocument(kind, text)`, for two
    reasons: the two carry different bounds, and `TailoredDocuments(cv=…, cover_letter=…)` cannot
    have its arguments swapped the way a pair of same-typed strings can. **No shared base class**
    either, per CLAUDE.md — the two happen to have the same *shape*, which is not the same as having
    shared behaviour, and a base class here would guess at rules that legitimately differ.

    Refuses to be constructed with:

    - blank or whitespace-only text → `EmptyTailoredDocument`;
    - fewer than 400 non-whitespace characters → `TailoredDocumentTooShort`;
    - more than 20,000 characters of normalized length → `TailoredDocumentTooLong`;
    - any control character other than `\\n` and `\\t`, or a NUL → `InvalidTailoredDocument`.

    Normalized on the way in: `\\r\\n` becomes `\\n`, and trailing whitespace is stripped from each
    line (a model that ends every bullet with a space should not change the value's identity).

    **A lone `\\r` — one not part of a `\\r\\n` pair — is rejected, not normalized**, and that falls
    out of the two rules above in that order rather than being a third rule: the pair is rewritten
    first, so anything still carrying a bare carriage return afterwards is a control character that
    is neither `\\n` nor `\\t`, and the last rule refuses it. Settled here on purpose rather than
    left to whichever test happened to land first. A classic-Mac line ending is not a thing a 2026
    language model emits, and silently rewriting one would mean guessing at what a model meant in
    the one place this codebase has decided not to guess.

    **Why this type does NOT collapse whitespace, when `ExtractedText` and `JobPostingText` both do.**
    Both of those call `" ".join(value.split())`, and a reader who spots that this one does not is
    entitled to the reason rather than a "fix" (CLAUDE.md: *where a new type deliberately contradicts
    an existing one, comment why at the point of contradiction*). The two families hold different
    kinds of text. `ExtractedText` and `JobPostingText` hold text whose **layout is noise** — a PDF's
    column breaks, a scraped page's indentation, the ragged spacing of a `<div>` soup — so flattening
    it loses nothing and makes two copies of the same posting compare equal. This type holds
    **Markdown, where the newline is semantic**: a blank line is a paragraph break and a leading `- `
    is a bullet. Collapsing whitespace here would turn a bulleted, sectioned CV into one unreadable
    paragraph — and it would do so to the document we just paid Google to produce. Same class of
    operation, opposite correct answer, because the inputs mean different things.

    **Neither this type nor `CoverLetter` strips or rejects HTML, and that absence is deliberate.**
    A value object that rejected `<` would reject "C++ → C# migration" and "throughput > 10k/s",
    which are things real CVs say. A value object that *sanitized* would be a rendering policy — an
    infrastructure concern — hiding inside a domain type, and it would sanitize for HTML even when
    the caller is about to render a PDF or a plain-text download. The rule stated instead: **the
    model's output is untrusted text, and whoever renders it into HTML sanitizes it there** (1.4's
    editor, 1.5's PDF path). This slice renders it as text; G-33/AC-31 pin that.
    """

    value: str

    def __post_init__(self) -> None:
        # Deferred (function-local) import to break a module cycle: `domain/tailoring/errors.py`
        # imports `TailoringFailureReason` and `TailoringRunId` from this module, so importing
        # `errors` back at module scope here would make the two modules import each other during
        # collection — whichever loads first would ask for names the other hasn't defined yet.
        # Importing inside the method instead defers the import to call time, by which point both
        # modules have finished loading. `OriginalFilename.__post_init__`
        # (`domain/intake/value_objects.py`) and `SourceUrl.__post_init__` carry the same comment for
        # the same cycle. This is a local decision about *this* module's import shape, not a
        # domain-purity exception — `errors` is still `tailorcraft.domain`, so the purity test is
        # unaffected.
        from tailorcraft.domain.tailoring.errors import (
            EmptyTailoredDocument,
            InvalidTailoredDocument,
            TailoredDocumentTooLong,
            TailoredDocumentTooShort,
        )

        # Normalize in this order and no other. `\r\n` collapses to `\n` **first**, so a Windows line
        # ending is a line ending rather than a control character the rule below would refuse; what
        # is left is then stripped of trailing spaces and tabs **per line**, which is the point of
        # splitting and re-joining rather than calling `self.value.rstrip()` — the latter reaches
        # only the last line, and a model that pads every bullet with a space pads every line.
        #
        # `rstrip(" \t")`, deliberately NOT the bare `rstrip()`. The bare form strips *every* trailing
        # whitespace character, and `\r` and `\x0b` are whitespace: it would therefore delete, at the
        # end of a line, precisely the control characters the next rule exists to refuse. A lone `\r`
        # in the middle of a line would raise while the same `\r` at the end of one vanished
        # silently, leaving the control-character rule true everywhere except the one position a
        # stray control character is most likely to occupy. Stripping only the two characters that
        # legitimately pad a line's end keeps that rule unconditional.
        normalized = "\n".join(
            line.rstrip(" \t") for line in self.value.replace("\r\n", "\n").split("\n")
        )
        non_whitespace_count = sum(1 for char in normalized if not char.isspace())

        if not normalized.strip():
            raise EmptyTailoredDocument("tailored cv must not be blank")
        # Control characters are judged before the bounds, so that a document carrying a NUL is
        # reported as malformed rather than as too short. The two failures mean different things to
        # whoever reads them — "the model returned junk" against "the model returned too little" —
        # and a control character is a malformed document at any length.
        if _has_disallowed_control_character(normalized):
            raise InvalidTailoredDocument(
                "tailored cv must not contain control characters or NUL other than newline and tab"
            )
        # The floor counts non-whitespace characters and the ceiling counts the normalized length:
        # two different quantities, on purpose, for the reasons recorded above the module constants.
        # Do not "simplify" them to one measure.
        if non_whitespace_count < _MIN_TAILORED_CV_NON_WHITESPACE_CHARACTERS:
            raise TailoredDocumentTooShort(
                f"tailored cv has only {non_whitespace_count} non-whitespace characters; "
                f"the floor is {_MIN_TAILORED_CV_NON_WHITESPACE_CHARACTERS} (OQ-5)"
            )
        if len(normalized) > _MAX_TAILORED_CV_LENGTH:
            raise TailoredDocumentTooLong(
                f"tailored cv is {len(normalized)} characters; "
                f"the ceiling is {_MAX_TAILORED_CV_LENGTH}"
            )

        object.__setattr__(self, "value", normalized)

    @property
    def character_count(self) -> int:
        """The length of the normalized text — **the number every consumer reports instead of the
        text itself.**

        This exists for the same reason `ExtractedText.character_count` does, and here the reason is
        sharper: a tailored CV is a person's employment history rewritten, so `TailoringRunSucceeded`
        carries `cv_character_count` precisely so that it need not carry the CV (Constitution §8,
        AC-22). A count in a log line is a size; the string it counts is a stranger's address.

        It reports the *normalized length*, spaces and newlines included, not the non-whitespace
        count the floor is measured in. Two different numbers on purpose: the floor asks whether
        there is real content, the report answers what a person counting characters in the document
        in front of them would count.
        """
        return len(self.value)


@dataclass(frozen=True, slots=True)
class CoverLetter:
    """The tailored cover letter the model produced, as Markdown.

    Refuses to be constructed with:

    - blank or whitespace-only text → `EmptyTailoredDocument`;
    - fewer than 200 non-whitespace characters → `TailoredDocumentTooShort`;
    - more than 8,000 characters of normalized length → `TailoredDocumentTooLong`;
    - any control character other than `\\n` and `\\t`, or a NUL → `InvalidTailoredDocument`.

    Normalizes `\\r\\n` to `\\n` and strips trailing whitespace per line, and — like `TailoredCv` and
    unlike `ExtractedText`/`JobPostingText` — **does not collapse whitespace**. The full reasoning is
    in `TailoredCv`'s docstring and it applies verbatim: this is Markdown, the newline is semantic,
    and flattening it destroys the paragraph structure of the letter. It does not strip or reject
    HTML either, for the reasons recorded there.

    Its bounds are its own rather than inherited from a shared base class: a 400-character floor
    would reject a perfectly good three-paragraph letter, and a 20,000-character ceiling would admit
    a letter nobody would read. Shared shape, different rules — which is exactly the case CLAUDE.md
    names as the one where a base class does harm.
    """

    value: str

    def __post_init__(self) -> None:
        # See `TailoredCv.__post_init__` for why this import is function-local rather than
        # module-level: `errors.py` imports `TailoringFailureReason` and `TailoringRunId` from this
        # module, so a module-level import in the other direction is a circular import at collection
        # time.
        from tailorcraft.domain.tailoring.errors import (
            EmptyTailoredDocument,
            InvalidTailoredDocument,
            TailoredDocumentTooLong,
            TailoredDocumentTooShort,
        )

        # The same two normalizations in the same order, and `TailoredCv.__post_init__` carries the
        # full reasoning for both — why `\r\n` is rewritten before anything judges a control
        # character, why the strip is per line, and why it is `rstrip(" \t")` rather than the bare
        # `rstrip()` that would swallow a trailing `\r`. Written out rather than shared through a
        # helper because the four checks below differ in their numbers and their messages, and the
        # numbers are the thing a reader of this class came here for.
        normalized = "\n".join(
            line.rstrip(" \t") for line in self.value.replace("\r\n", "\n").split("\n")
        )
        non_whitespace_count = sum(1 for char in normalized if not char.isspace())

        if not normalized.strip():
            raise EmptyTailoredDocument("cover letter must not be blank")
        if _has_disallowed_control_character(normalized):
            raise InvalidTailoredDocument(
                "cover letter must not contain control characters or NUL other than newline and tab"
            )
        if non_whitespace_count < _MIN_COVER_LETTER_NON_WHITESPACE_CHARACTERS:
            raise TailoredDocumentTooShort(
                f"cover letter has only {non_whitespace_count} non-whitespace characters; "
                f"the floor is {_MIN_COVER_LETTER_NON_WHITESPACE_CHARACTERS} (OQ-5)"
            )
        if len(normalized) > _MAX_COVER_LETTER_LENGTH:
            raise TailoredDocumentTooLong(
                f"cover letter is {len(normalized)} characters; "
                f"the ceiling is {_MAX_COVER_LETTER_LENGTH}"
            )

        object.__setattr__(self, "value", normalized)

    @property
    def character_count(self) -> int:
        """The length of the normalized text, reported so that nothing downstream needs the letter
        itself to say how much of it there is. See `TailoredCv.character_count`."""
        return len(self.value)


@dataclass(frozen=True, slots=True)
class TailoredDocuments:
    """The pair the model was asked for, named: a tailored CV and a cover letter.

    This is the Constitution's `TailoredDocument` concept realized as a **value object held by the
    run** rather than as the second aggregate §4.3 names. The reasoning is in the technical plan's
    "Why `TailoredDocument` is not an aggregate", and so is the named trigger that would change it:
    the day a document gets an independent lifecycle — versioning, a per-document export, re-use
    across runs. Until then, a consistency boundary nothing ever crosses independently is not a
    boundary; it is a join.

    Requiring **both** fields is how invariant TR-5 is enforced: "succeeded with no cover letter" is
    not a state that gets checked and rejected, it is a state that cannot be constructed. Half a
    result is a failure, and the type says so.

    No `__post_init__`: both fields are value objects that validated themselves, so there is nothing
    left for this carrier to check. It is a record, not a rule — the same shape `FetchedPosting` has.
    """

    cv: TailoredCv
    cover_letter: CoverLetter


class TailoringRunStatus(StrEnum):
    """Where a `TailoringRun` stands. Four values, and the transition table between them (AC-3) is
    the aggregate's whole behaviour.

    An enum, not a value object: the set is closed, the boundary never parses one out of user input,
    and so a `__post_init__` would have nothing left to validate — the same call `BaseCvStatus` and
    `PostingSource` make.

    `QUEUED` and `RUNNING` are the two non-terminal states, which is what
    `TailoringRunRepository.find_active_for_session` filters on and what the client's poller keeps
    polling through; `SUCCEEDED` and `FAILED` are terminal and decided exactly once (TR-3).
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TailoringFailureReason(StrEnum):
    """Why a `TailoringRun` ended in `FAILED`. Nine values, closed, exhaustive over every way a run
    that reached a worker can end without two documents.

    **This enum is persisted**, and that is the deliberate contrast with `FetchFailureReason`, which
    is not. The line that decides it is ADR-0014 §2 and it is worth carrying here in full: *was
    anything spent, and is there an artifact to own?* Before the enqueue nothing has been spent, so a
    rejected request leaves no row at all. After the enqueue the user is waiting and we are about to
    pay Google on their behalf, so **every** outcome is a row — `succeeded`, and equally `failed`
    with one of these nine reasons. A run that reached a worker and produced nothing still owns the
    fact that it happened, the money it cost and the twelve seconds of a person's afternoon. That
    single line reconciles ADR-0004 with ADR-0013 rather than choosing between them.

    Seven of the nine have exactly one `TailoringFailed` subclass binding them in
    `domain/tailoring/errors.py`, so the adapter raises a named exception and the use case records a
    closed set. `LLM_ERROR` is the residual within that seven — the same role `FETCHER_ERROR` and
    `EXTRACTOR_ERROR` play, reached by the adapter's `except Exception` floor when a library fails in
    a way we have no better name for.

    **`NOT_QUEUED` and `ABANDONED` have no exception subclass, and the asymmetry is the point.**
    Nothing *raises* them, because nothing outside this process produces them: they are recorded by
    our own orchestration. `NOT_QUEUED` is written by the router when the row committed and the
    broker then refused the enqueue (G-14) — the run can never run, and saying so is more honest than
    leaving it `queued` forever. `ABANDONED` is written by the worker when a redelivered task finds a
    run that has been `running` past the stale window (G-25) — nobody is coming back for it. Both are
    facts about our own plumbing, and inventing a `TailoringNotQueuedFailure(TailoringFailed)` for
    them would be an exception type that no `raise` statement ever mentions. This is the mirror image
    of `FetchFailureReason.FETCHER_ERROR`'s asymmetry: there, one reason with no *specific* cause;
    here, two causes with no *raise*.

    (`TailoringNotQueued` in `errors.py` is a different animal and is not this exception: it is what
    the queue adapter raises at the broker, before any reason is recorded, and it is a plain
    `DomainError` because nothing was spent.)
    """

    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_RATE_LIMITED = "llm_rate_limited"
    LLM_REFUSED = "llm_refused"
    LLM_TIMED_OUT = "llm_timed_out"
    LLM_OUTPUT_INVALID = "llm_output_invalid"
    INPUTS_TOO_LARGE = "inputs_too_large"
    LLM_ERROR = "llm_error"
    NOT_QUEUED = "not_queued"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True)
class ModelName:
    """Which model wrote this document — a **provenance fact**, not a configuration knob.

    The configured model id is a setting read by the adapter; this is the answer to "what produced
    the words in this row", recorded on the run at the moment of the call. The two are the same
    string today and will differ the first time the setting changes, which is exactly when 2.3's
    history and every prompt regression will need the recorded one.

    1-64 characters, no whitespace, no control characters, not blank → `InvalidModelName`. Whitespace
    is rejected rather than normalized because a model id is one unbroken token, the same rule
    `SourceUrl` applies for the same reason.
    """

    value: str

    def __post_init__(self) -> None:
        # See `TailoredCv.__post_init__` for why this import is function-local rather than
        # module-level: it breaks the `value_objects` ↔ `errors` module cycle.
        from tailorcraft.domain.tailoring.errors import InvalidModelName

        # No normalization at all here, and the absence is the rule rather than an omission. The two
        # document types above rewrite what they are given; this one refuses it. `strip()`ing a model
        # id would accept `" gemini-2.5-flash "` and quietly record something the caller never
        # configured, and the value is a provenance fact — what it says must be what the adapter was
        # handed, not our tidied-up guess at it. So `value` is stored exactly as it arrived.
        if not self.value:
            raise InvalidModelName("model name must not be empty")
        if len(self.value) > _MAX_MODEL_NAME_LENGTH:
            raise InvalidModelName(
                f"model name must be at most {_MAX_MODEL_NAME_LENGTH} characters, "
                f"got {len(self.value)}"
            )
        if _has_control_or_whitespace(self.value):
            raise InvalidModelName(
                "model name must not contain whitespace, control characters or NUL"
            )


@dataclass(frozen=True, slots=True)
class PromptVersion:
    """Which prompt produced this document, persisted on the run so a document can be traced back to
    it.

    A prompt edit is a behaviour change (ADR-0004) and gets a commit message that says so; without
    this column, "the output got worse last Tuesday" is a question with no answer, because the prompt
    that produced last Tuesday's documents is not in the row and not in the log.

    1-16 characters from `[A-Za-z0-9._-]` → `InvalidPromptVersion`. A closed grammar rather than a
    bare length check, because this value is a *key*: it keys a persisted column, a log field and any
    future regression comparison, and a key with a space or a slash in it renders differently in at
    least one of those three.
    """

    value: str

    def __post_init__(self) -> None:
        # See `TailoredCv.__post_init__` for why this import is function-local rather than
        # module-level: it breaks the `value_objects` ↔ `errors` module cycle.
        from tailorcraft.domain.tailoring.errors import InvalidPromptVersion

        # An allow-list, never a deny-list. Enumerating the characters a key may contain is a closed
        # question with a checkable answer; enumerating the ones it may not is a bet that nobody
        # invents a new way to break a log field, and that is the same bet CLAUDE.md records losing
        # at the extractor's exception allow-list. The empty string fails this loop vacuously, so the
        # length check below is what names it — worth keeping separate, because "blank" and "has a
        # slash in it" are different mistakes and deserve different messages.
        if not self.value:
            raise InvalidPromptVersion("prompt version must not be empty")
        if len(self.value) > _MAX_PROMPT_VERSION_LENGTH:
            raise InvalidPromptVersion(
                f"prompt version must be at most {_MAX_PROMPT_VERSION_LENGTH} characters, "
                f"got {len(self.value)}"
            )
        if any(char not in _PROMPT_VERSION_ALLOWED_CHARACTERS for char in self.value):
            # Says what the grammar is, never which character offended: the value is a key we are
            # about to persist and log, and quoting the rejected byte back into an exception message
            # puts a stranger's string in every frame that touches it.
            raise InvalidPromptVersion("prompt version must contain only [A-Za-z0-9._-]")


@dataclass(frozen=True, slots=True)
class LlmCallMetrics:
    """What one call to the model cost: which model, which prompt, how many tokens each way, and how
    long it took.

    **This is the only thing about an LLM call this codebase may log** (Constitution §8), and having
    it as one named type is what makes that rule easy to obey rather than merely easy to state:
    `log.info("tailoring.succeeded", **metrics_fields(metrics))` cannot accidentally include a prompt
    or a completion, because neither is in the type. The alternative — logging fields picked out of
    the response by hand at each call site — is one careless `**response` away from putting a
    stranger's employment history in a file nobody thinks of as a database.

    All three integers must be `>= 0`; zero is legal (a provider that reports no usage metadata is a
    gap in observability, not a reason to fail a run that produced two good documents). Anything
    negative raises `InvalidLlmCallMetrics`.

    **Why `duration_ms` is measured in the adapter with `time.perf_counter()` and not from the
    `Clock` port.** This looks like a second source of time sneaking past the port, and a later
    reader will reach to "fix" it, so: the `Clock` port is **whole-second by contract** (ADR-0007,
    and the system implementation truncates at the source so a database round trip can never change a
    value). That makes `completed_at - started_at` accurate to ±1 second, which cannot defend a
    15-second budget — the measurement error is 7 % of the thing being measured. `perf_counter`
    measures a **duration**, not a "now": it cannot be used to date anything, it never reaches an
    aggregate's timestamp field, and it answers a question the `Clock` port was never asked. The two
    coexist on purpose, and the run carries both — whole-second timestamps for *when*, and this for
    *how long*.
    """

    model: ModelName
    prompt_version: PromptVersion
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int

    def __post_init__(self) -> None:
        # See `TailoredCv.__post_init__` for why this import is function-local rather than
        # module-level: it breaks the `value_objects` ↔ `errors` module cycle.
        from tailorcraft.domain.tailoring.errors import InvalidLlmCallMetrics

        # `model` and `prompt_version` are not checked here, and that is the point of them being
        # value objects: each refused every invalid value of its own at its own construction, so
        # re-checking them would be a second lock on a door that cannot open. The three bare `int`s
        # are the only fields with a rule left to enforce — `>= 0`, with zero legal, because a
        # provider that reports no usage metadata is a gap in accounting rather than a reason to fail
        # a run that produced two good documents.
        if self.prompt_tokens < 0:
            raise InvalidLlmCallMetrics(f"prompt_tokens must not be negative: {self.prompt_tokens}")
        if self.completion_tokens < 0:
            raise InvalidLlmCallMetrics(
                f"completion_tokens must not be negative: {self.completion_tokens}"
            )
        if self.duration_ms < 0:
            raise InvalidLlmCallMetrics(f"duration_ms must not be negative: {self.duration_ms}")


@dataclass(frozen=True, slots=True)
class TailoredDraft:
    """What `LlmPort.tailor` promises to return: the two documents, and the metrics of the call that
    produced them.

    A named type rather than a `tuple[TailoredDocuments, LlmCallMetrics]`, for the same reason
    `FetchedPosting` is one. `async def tailor(self, cv: ExtractedText, posting: JobPostingText) ->
    TailoredDraft` reads as a sentence; the tuple version reads as a puzzle whose second element
    every caller has to remember the meaning of. Naming it is also what lets the adapter grow a third
    field later without touching a single caller's unpacking.

    No `__post_init__`: both fields are already validated. A record, not a rule.

    "Draft" rather than "result" is the ubiquitous language doing its job — what comes back from the
    model is a draft the user is expected to edit (1.4), not a finished document.
    """

    documents: TailoredDocuments
    metrics: LlmCallMetrics
