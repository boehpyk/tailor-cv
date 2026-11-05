"""Shared arrangement for slice 5.1's application tests (T15): one `World` of fakes, builders for the
scoring values, and the **hand-computed** money constants every arithmetic assertion leans on.

The constants are written out, not derived from `ScoringPrices`: a test that computed its expectation
with the code under test could not disagree with it. The arithmetic, at $0.10 / $0.40 per million
tokens (`MicroUsd(100_000)` / `MicroUsd(400_000)` per million), rounded up each direction:

    one attempt, a 1,000-character CV and 500-character posting, 2,500 of prompt overhead, 512 output:
        input  (1000 + 500 + 2500) = 4000 tokens -> 4000 * 100_000 / 1e6 = 400 exactly
        output 512 tokens                        -> 512  * 400_000 / 1e6 = 204.8 -> 205
        = 605 micro-USD; two attempts reserved   = 1210
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from uuid import uuid4

from tailorcraft.application.intake.get_base_cv import GetBaseCv
from tailorcraft.application.posting.get_job_posting import GetJobPosting
from tailorcraft.application.scoring.find_fit_score_for_pair import FindFitScoreForPair
from tailorcraft.application.scoring.request_fit_score import (
    RequestFitScore,
    RequestFitScoreCommand,
    RequestOutcome,
)
from tailorcraft.application.scoring.resolve_scoring_pair import ResolveScoringPair, ScoringPair
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import BaseCvId, ExtractedText
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.scoring.errors import FitScoringFailed
from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.value_objects import (
    AssessedFit,
    CvFingerprint,
    CvForScoring,
    FitAssessment,
    FitGap,
    FitScoreId,
    MicroUsd,
    ModelName,
    PostingFingerprint,
    PostingForScoring,
    PromptVersion,
    ScoredCvRef,
    ScoredPostingRef,
    ScorePercent,
    ScoringPrices,
    ScoringUsage,
)
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    CountingClock,
    FakeBaseCvRepository,
    FakeFitScorer,
    FakeFitScoreRepository,
    FakeGuestSessionRepository,
    FakeJobPostingRepository,
    FakeUserRepository,
    RecordingEventPublisher,
    RecordingSpendLedger,
    create_active_session,
)
from tests.integration.owners import pasted_posting, seed_user, uploaded_cv

PRICES = ScoringPrices(input_per_million=MicroUsd(100_000), output_per_million=MicroUsd(400_000))
DAILY_CAP = MicroUsd(2_000_000)
OVERHEAD = 2500
MAX_OUTPUT_TOKENS = 512
MAX_ATTEMPTS = 2
MAX_PER_SESSION = 3
MAX_PER_USER = 10
MAX_CV_CHARACTERS = 25_000  # the settings default

WORST_ATTEMPT = 605  # see the module docstring
RESERVED = 1210  # WORST_ATTEMPT * MAX_ATTEMPTS
USUAL_COST = 230  # 1,700 in -> 170; 150 out -> 60

DAY = timedelta(hours=24)
THE_GAPS = ("Kubernetes experience", "AWS certification")


class LedgerDown(Exception):
    """The database failing under the ledger. Its own class, so `pytest.raises` can never be
    satisfied by a skeleton's `NotImplementedError`."""


class StoreDown(Exception):
    """The database failing under `store`: not a `DomainError`, so the use case must not settle."""


def usage(
    input_tokens: int = 1700,
    output_tokens: int = 150,
    unmetered: int = 0,
    duration_ms: int = 900,
) -> ScoringUsage:
    return ScoringUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        unmetered_attempts=unmetered,
        duration_ms=duration_ms,
        model=ModelName("gemini-test"),
        prompt_version=PromptVersion("1"),
    )


def assessed(
    score: int = 62, gaps: tuple[str, ...] = THE_GAPS, used: ScoringUsage | None = None
) -> AssessedFit:
    return AssessedFit(
        assessment=FitAssessment(ScorePercent(score), tuple(FitGap(g) for g in gaps)),
        usage=used if used is not None else usage(),
    )


def pair_of(
    cv_text: str = "c" * 1000, posting_text: str = "p" * 500, *, cv_marker: str = ""
) -> ScoringPair:
    """A `ScoringPair` built by hand, so a request test needs no CV rows. `cv_marker` makes a
    different CV (a different fingerprint) without changing its length."""
    cv = CvForScoring(cv_marker + cv_text)
    posting = PostingForScoring(posting_text)
    return ScoringPair(
        cv_id=ScoredCvRef(uuid4()),
        posting_id=ScoredPostingRef(uuid4()),
        cv=cv,
        posting=posting,
        cv_fingerprint=cv.fingerprint,
        posting_fingerprint=posting.fingerprint,
    )


def a_guest_owner() -> GuestOwner:
    """A guest owner with no session row: `RequestFitScore` resolves nobody (the pair is authorized)."""
    return GuestOwner(GuestSessionId(value=uuid4()))


def a_stored_score(
    owner: Owner,
    at: datetime,
    *,
    n: int = 1000,
    pair: ScoringPair | None = None,
    gaps: tuple[str, ...] = ("An unrelated requirement",),
) -> FitScore:
    """A score already on file. Without `pair`, a distinct fingerprint pair per `n`, so seeded rows
    never collide with the pair under test."""
    cv_fp = pair.cv_fingerprint if pair else CvFingerprint(f"{n:064x}")
    posting_fp = pair.posting_fingerprint if pair else PostingFingerprint(f"{n:064x}")
    score = FitScore.from_assessment(
        id=FitScoreId(value=uuid4()),
        owner=owner,
        base_cv_id=pair.cv_id if pair else ScoredCvRef(uuid4()),
        job_posting_id=pair.posting_id if pair else ScoredPostingRef(uuid4()),
        cv_fingerprint=cv_fp,
        posting_fingerprint=posting_fp,
        assessed=assessed(gaps=gaps),
        cost=MicroUsd(USUAL_COST),
        at=at,
    )
    score.release_events()
    return score


class JournalingPublisher(RecordingEventPublisher):
    """Records `publish` in the shared journal, so events are placed in the call order too."""

    def __init__(self, journal: list[str]) -> None:
        super().__init__()
        self._journal = journal

    async def publish(self, *events: DomainEvent) -> None:
        self._journal.append("publish")
        await super().publish(*events)


def extracted_cv_with(owner: Owner, at: datetime, text: str) -> BaseCv:
    """An `EXTRACTED` CV holding exactly `text` (which must clear `ExtractedText`'s floor)."""
    cv = uploaded_cv(owner, at)
    cv.mark_extracted(ExtractedText(text), at)
    cv.release_events()
    return cv


class World:
    """Every fake the three scoring use cases need, sharing one `journal` and one clock.

    `clock` is the test's `FixedClock`; the use cases see a `CountingClock` over it, so "read once"
    is checkable and seeding through `fixed` does not inflate the count.
    """

    def __init__(
        self,
        fixed: FixedClock,
        *,
        outcome: AssessedFit | FitScoringFailed | None = None,
        on_assess: Callable[[], None] | None = None,
        block: bool = False,
        reserve_raises: Exception | None = None,
        settle_raises: Exception | None = None,
        store_raises: Exception | None = None,
    ) -> None:
        self.fixed = fixed
        self.clock = CountingClock(fixed)
        self.earlier = fixed.now() - timedelta(hours=1)
        self.journal: list[str] = []
        self.outcome = outcome if outcome is not None else assessed()
        self.at_assess: dict[str, object] = {}

        self.sessions = FakeGuestSessionRepository()
        self.users = FakeUserRepository()
        self.cvs = FakeBaseCvRepository()
        self.postings = FakeJobPostingRepository()
        self.scores = FakeFitScoreRepository(journal=self.journal, store_raises=store_raises)
        self.ledger = RecordingSpendLedger(
            journal=self.journal, reserve_raises=reserve_raises, settle_raises=settle_raises
        )
        self.events = JournalingPublisher(self.journal)

        def snapshot_then(hook: Callable[[], None] | None) -> Callable[[], None]:
            def run() -> None:
                # What the world looked like the instant the model was asked.
                self.at_assess["spent"] = dict(self.ledger.spent)
                self.at_assess["settlements"] = list(self.ledger.settlements)
                self.at_assess["rows"] = self.scores.all()
                if hook is not None:
                    hook()

            return run

        self.scorer = FakeFitScorer(
            self.outcome, journal=self.journal, on_call=snapshot_then(on_assess), block=block
        )

    # --- the use cases ---

    def resolve(self) -> ResolveScoringPair:
        return ResolveScoringPair(
            GetBaseCv(self.cvs, self.sessions, self.users, self.fixed),
            GetJobPosting(self.postings, self.sessions, self.users, self.fixed),
        )

    def find(self) -> FindFitScoreForPair:
        return FindFitScoreForPair(self.scores, self.resolve())

    def request(
        self,
        *,
        daily_cap: MicroUsd = DAILY_CAP,
        max_attempts: int = MAX_ATTEMPTS,
        overhead: int = OVERHEAD,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
        max_cv_characters: int = MAX_CV_CHARACTERS,
    ) -> RequestFitScore:
        return RequestFitScore(
            scores=self.scores,
            scorer=self.scorer,
            ledger=self.ledger,
            events=self.events,
            clock=self.clock,
            prices=PRICES,
            daily_cap=daily_cap,
            max_attempts=max_attempts,
            prompt_overhead_characters=overhead,
            max_output_tokens=max_output_tokens,
            max_per_session=MAX_PER_SESSION,
            max_per_user=MAX_PER_USER,
            max_cv_characters=max_cv_characters,
            model=ModelName("gemini-test"),
            prompt_version=PromptVersion("1"),
        )

    async def ask(self, owner: Owner, pair: ScoringPair, **config: object) -> RequestOutcome:
        return await self.request(**config)(RequestFitScoreCommand(owner=owner, pair=pair))  # type: ignore[arg-type]

    # --- people and rows ---

    async def guest(self, label: str = "guest") -> GuestOwner:
        session = await create_active_session(
            self.sessions, self.fixed, token_hash=label.ljust(64, "0")
        )
        return GuestOwner(session.id)

    async def user(self, email: str = "alex@example.com") -> UserOwner:
        return await seed_user(self.users, email, self.earlier)

    async def inputs(
        self, owner: Owner, *, cv_text: str | None = None, posting_marker: str = "p"
    ) -> tuple[BaseCvId, JobPostingId]:
        cv = (
            extracted_cv_with(owner, self.earlier, cv_text)
            if cv_text is not None
            else extracted_cv_with(owner, self.earlier, "word " * 60)
        )
        posting = pasted_posting(owner, self.earlier, marker=posting_marker)
        await self.cvs.add(cv)
        await self.postings.add(posting)
        return cv.id, posting.id

    def seed_scores(self, owner: Owner, instants: list[datetime]) -> None:
        for n, at in enumerate(instants):
            self.scores.seed(a_stored_score(owner, at, n=1000 + n))

    @property
    def day(self) -> date:
        return self.fixed.now().date()
