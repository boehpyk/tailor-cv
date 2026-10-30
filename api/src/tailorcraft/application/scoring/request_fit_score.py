"""The `RequestFitScore` use case: count, reserve, call, store, settle (plan §2, ADR-0040/0041)."""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.application.scoring.resolve_scoring_pair import ScoringPair
from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.ports import FitScoreRepository, FitScorerPort, SpendLedgerPort
from tailorcraft.domain.scoring.value_objects import MicroUsd, ScoringPrices
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort


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
    3. `worst = prices.worst_case_attempt(cv chars, posting chars, overhead, max output tokens)`,
       and `try_reserve(day, worst * max_attempts, daily_cap)`. `False` is
       `FitScoreSpendCapReached`, no call. `True` has committed: **nothing is held from here until
       step 5.**
    4. `scorer.assess(pair.cv, pair.posting)`. `CancelledError` passes through with no settle and no
       row: the reservation stands (AC-17, AC-21). `FitScoringFailed` settles to
       `prices.cost_of(failed.usage, worst)` and re-raises.
    5. `FitScore.from_assessment(...)`, then `scores.store(score)`. A `DomainError` from `store`
       (S-25 to S-27: paid for, nothing stored) settles to the actual cost and re-raises.
    6. `settle(day, reserved, cost)`: this commits the row and its cost together (AC-14).
    7. Only if `stored` is this `score`: publish its events. The loser publishes nothing.

    **If a settle fails while another error is on its way out (S-16)**, that error still wins: it is
    the user's answer, the reservation stands, and it is raised `from` the settle's exception. This
    layer has no logger, so `__cause__` is how the router gets the type name to log. A settle
    failing at step 6 has no such error to defer to and propagates, so nothing commits (S-15).

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

    async def __call__(self, cmd: RequestFitScoreCommand) -> RequestOutcome:
        raise NotImplementedError
