"""The `RequestFitScore` use case: count, reserve, call, store, settle (plan §2, ADR-0040/0041)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import NoReturn, assert_never

from tailorcraft.application.scoring.resolve_scoring_pair import ScoringPair
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.scoring.errors import (
    FitScoreSpendCapReached,
    FitScoringFailed,
    TooManyFitScores,
)
from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.ports import FitScoreRepository, FitScorerPort, SpendLedgerPort
from tailorcraft.domain.scoring.value_objects import (
    FitScoreFailureReason,
    MicroUsd,
    ModelName,
    PromptVersion,
    ScoringPrices,
    ScoringUsage,
)
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.domain.shared.events import EventPublisherPort

_USER_LIMIT_WINDOW = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class RequestFitScoreCommand:
    """Who is asking, and the pair `ResolveScoringPair` already authorized against that owner."""

    owner: Owner
    pair: ScoringPair


@dataclass(frozen=True, slots=True)
class RequestOutcome:
    """`stored` is the row now stored. `created` is `False` for the loser of a two-tab race, whose
    `stored` is the winner's row: 201 or 200 (AC-5)."""

    stored: FitScore
    created: bool


class RequestFitScore:
    """Score a new pair inside the request. The caller has already found no stored hit and passed
    the IP limiter (plan §0.5).

    **The order is the transaction boundary**, since `SpendLedgerPort`'s methods are durable on
    return:

    1. Read the clock once, into locals: `at`, and `day = at.date()`. A call across midnight settles
       the day it reserved, and the row's `scored_at` is this `at`.
    2. `scored_at_since(owner, since)`: `None` for a guest, `at` minus 24 hours for a user. At the
       limit, `TooManyFitScores(count, limit, retry_not_before)`, with no reservation and no call
       (AC-8, AC-12). `retry_not_before` is `None` for a guest. For a user it is
       `scored[count - limit] + 24 h`, `scored` being oldest first: the moment the count drops below
       the limit, which a claim may have left it above (AC-35). At exactly the limit, that is the
       oldest.
    3. A CV over `max_cv_characters` is `FitScoringFailed(INPUTS_TOO_LARGE)` with all-zero usage: no
       reservation, no call, no row (S-9).
    4. `worst = prices.worst_case_attempt(cv chars, posting chars, overhead, max output tokens)`,
       and `try_reserve(day, worst * max_attempts, daily_cap)`. `False` is
       `FitScoreSpendCapReached`, no call. `True` has committed: **nothing is held from here until
       step 6.**
    5. `scorer.assess(pair.cv, pair.posting)`. `CancelledError` passes through with no settle and no
       row: the reservation stands (AC-17, AC-21). `FitScoringFailed` settles to
       `prices.cost_of(failed.usage, worst)` and re-raises.
    6. `FitScore.from_assessment(...)`, then `scores.store(score)`. A `DomainError` from `store`
       (S-25 to S-27: paid for, nothing stored) settles to the actual cost and re-raises.
    7. `settle(day, reserved, cost)`: this commits the row and its cost together (AC-14).
    8. Only if `stored` is this `score`: publish its events. The loser publishes nothing.

    **If a settle fails while another error is on its way out (S-16)**, that error still wins: it is
    the user's answer, the reservation stands, and it is raised `from` the settle's exception. This
    layer has no logger, so `__cause__` is how the router gets the type name to log. A settle
    failing at step 7 has no such error to defer to and propagates, so nothing commits (S-15).

    `prompt_overhead_characters` is an argument because the prompt is infrastructure's: the
    composition root measures it. The limits, prices and cap have no defaults here, since `Settings`
    owns them.
    """

    def __init__(
        self,
        *,
        scores: FitScoreRepository,
        scorer: FitScorerPort,
        ledger: SpendLedgerPort,
        events: EventPublisherPort,
        clock: Clock,
        prices: ScoringPrices,
        daily_cap: MicroUsd,
        max_attempts: int,
        prompt_overhead_characters: int,
        max_output_tokens: int,
        max_per_session: int,
        max_per_user: int,
        max_cv_characters: int,
        model: ModelName,
        prompt_version: PromptVersion,
    ) -> None:
        self._scores = scores
        self._scorer = scorer
        self._ledger = ledger
        self._events = events
        self._clock = clock
        self._prices = prices
        self._daily_cap = daily_cap
        self._max_attempts = max_attempts
        self._prompt_overhead_characters = prompt_overhead_characters
        self._max_output_tokens = max_output_tokens
        self._max_per_session = max_per_session
        self._max_per_user = max_per_user
        self._max_cv_characters = max_cv_characters
        self._model = model
        self._prompt_version = prompt_version

    async def __call__(self, cmd: RequestFitScoreCommand) -> RequestOutcome:
        owner, pair = cmd.owner, cmd.pair
        at = self._clock.now()
        day = at.date()

        await self._refuse_at_the_limit(owner, at)

        # Refuse what the user can fix before what they can't: a full cap must not hide a too-long
        # CV. The adapter's own check stays as the backstop.
        if pair.cv.character_count > self._max_cv_characters:
            raise FitScoringFailed(
                FitScoreFailureReason.INPUTS_TOO_LARGE,
                ScoringUsage(
                    input_tokens=0,
                    output_tokens=0,
                    unmetered_attempts=0,
                    duration_ms=0,
                    model=self._model,
                    prompt_version=self._prompt_version,
                ),
            )

        worst = self._prices.worst_case_attempt(
            pair.cv.character_count,
            pair.posting.character_count,
            self._prompt_overhead_characters,
            self._max_output_tokens,
        )
        reserved = worst * self._max_attempts
        if not await self._ledger.try_reserve(day, reserved, self._daily_cap):
            raise FitScoreSpendCapReached(
                f"the day's ledger cannot take {reserved.value} micro-USD", day
            )

        # Only `FitScoringFailed` is caught: a cancellation must cancel, reservation standing.
        try:
            assessed = await self._scorer.assess(pair.cv, pair.posting)
        except FitScoringFailed as failed:
            cost = self._prices.cost_of(failed.usage, worst)
            await self._settle_then_raise(day, reserved, cost, failed)

        cost = self._prices.cost_of(assessed.usage, worst)
        score = FitScore.from_assessment(
            id=self._scores.next_identity(),
            owner=owner,
            base_cv_id=pair.cv_id,
            job_posting_id=pair.posting_id,
            cv_fingerprint=pair.cv_fingerprint,
            posting_fingerprint=pair.posting_fingerprint,
            assessed=assessed,
            cost=cost,
            at=at,
        )
        # `DomainError` only. Anything else means the transaction is gone, so a settle would be
        # written into nothing: the reservation stands (S-15).
        try:
            stored = await self._scores.store(score)
        except DomainError as refused:
            await self._settle_then_raise(day, reserved, cost, refused)

        await self._ledger.settle(day, reserved, cost)

        created = stored.id == score.id
        if created:
            await self._events.publish(*score.release_events())
        return RequestOutcome(stored=stored, created=created)

    async def _refuse_at_the_limit(self, owner: Owner, at: datetime) -> None:
        since: datetime | None
        match owner:
            case GuestOwner():
                since, limit = None, self._max_per_session
            case UserOwner():
                since, limit = at - _USER_LIMIT_WINDOW, self._max_per_user
            case _:
                assert_never(owner)

        scored = await self._scores.scored_at_since(owner, since)
        count = len(scored)
        if count >= limit:
            retry_not_before = None if since is None else scored[count - limit] + _USER_LIMIT_WINDOW
            raise TooManyFitScores(count, limit, retry_not_before)

    async def _settle_then_raise(
        self, day: date, reserved: MicroUsd, actual: MicroUsd, error: DomainError
    ) -> NoReturn:
        """S-16: `error` is the user's answer whether or not the settle lands. `Exception`, never
        `BaseException`: a cancellation during the settle still cancels."""
        try:
            await self._ledger.settle(day, reserved, actual)
        except Exception as settle_failure:
            raise error from settle_failure
        raise error
