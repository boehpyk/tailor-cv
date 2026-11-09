"""Application tests for `RequestTailoringRun`'s charge (slice 5.2, T14 RED, AC-6, AC-7, AC-8, F-1,
F-2, F-22 .. F-25, plan §0.3).

Plan §0.3's order for an account: ownership and `EXTRACTED` (404 / 409) -> the soft active-run check
-> the per-owner cap -> `runs.add` -> **charge under the account lock** -> the **hard** active-run
re-check (`find_active_for_owner(owner, excluding=run.id)`) -> publish. Every refusal after `runs.add`
is rolled back by the handler (run and charge vanish together), which the fakes cannot do: what is
asserted here is that the refusal is *raised*, nothing is published, and, for refusals before the
charge, that the lock was never taken and no credit moved.

**The hard re-check (F-2)** is staged by `_CompetitorCommitsWhileWaiting`: its `lock_for_charge`
inserts a rival queued run before returning, which is what a second request sees on waking from the
first's lock under READ COMMITTED. A re-check placed *before* the lock cannot see that run, so the
test goes red under that mutation. The concurrent, real-lock version is in `test_pending_adapters.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from tailorcraft.application.billing.charge_credit_for_run import ChargeCreditForRun
from tailorcraft.application.intake.get_base_cv import GetBaseCv
from tailorcraft.application.posting.get_job_posting import GetJobPosting
from tailorcraft.application.tailoring.request_tailoring_run import (
    RequestTailoringRun,
    RequestTailoringRunCommand,
)
from tailorcraft.domain.billing.credit_account import CreditAccount
from tailorcraft.domain.billing.errors import NoCreditsLeft
from tailorcraft.domain.billing.events import CreditCharged
from tailorcraft.domain.billing.value_objects import ChargedRunRef, CreditEntryKind
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.tailoring.errors import (
    AccountRequired,
    BaseCvNotReadyForTailoring,
    TailoringAlreadyRunning,
    TooManyTailoringRuns,
)
from tailorcraft.domain.tailoring.events import TailoringRunRequested
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringRunId, TailoringRunStatus
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.billing.support import seed_account
from tests.integration.fakes import (
    FakeBaseCvRepository,
    FakeCreditAccountRepository,
    FakeGuestSessionRepository,
    FakeJobPostingRepository,
    FakeTailoringRunRepository,
    FakeUserRepository,
    RecordingEventPublisher,
    create_active_session,
)
from tests.integration.owners import (
    extracted_cv,
    failed_run,
    pasted_posting,
    queued_run,
    running_run,
    seed_user,
    uploaded_cv,
)


class _CompetitorCommitsWhileWaiting(FakeCreditAccountRepository):
    """`lock_for_charge` returns only after a rival request's queued run is visible, as it would be
    to a request that waited on the first one's account lock."""

    def __init__(self, runs: FakeTailoringRunRepository, requested_at: datetime) -> None:
        super().__init__()
        self._runs = runs
        self._requested_at = requested_at
        self.rival: TailoringRun | None = None

    async def lock_for_charge(self, user_id: UserId) -> CreditAccount:
        self.rival = queued_run(UserOwner(user_id), self._requested_at)
        await self._runs.add(self.rival)
        return await super().lock_for_charge(user_id)


class _RunVisibleAtLock(FakeCreditAccountRepository):
    """Records which runs existed at the instant the account lock was taken."""

    def __init__(self, runs: FakeTailoringRunRepository) -> None:
        super().__init__()
        self._runs = runs
        self.runs_at_lock: list[set[TailoringRunId]] = []

    async def lock_for_charge(self, user_id: UserId) -> CreditAccount:
        self.runs_at_lock.append({run.id for run in self._runs.all()})
        return await super().lock_for_charge(user_id)


class _World:
    def __init__(
        self,
        clock: FixedClock,
        credits_over: Callable[[FakeTailoringRunRepository], FakeCreditAccountRepository]
        | None = None,
    ) -> None:
        self.clock = clock
        self.earlier = clock.now() - timedelta(hours=1)
        self.sessions = FakeGuestSessionRepository()
        self.users = FakeUserRepository()
        self.cvs = FakeBaseCvRepository()
        self.postings = FakeJobPostingRepository()
        self.runs = FakeTailoringRunRepository()
        self.events = RecordingEventPublisher(self.runs)
        self.credits = credits_over(self.runs) if credits_over else FakeCreditAccountRepository()

    def use_case(self) -> RequestTailoringRun:
        return RequestTailoringRun(
            self.runs,
            GetBaseCv(self.cvs, self.sessions, self.users, self.clock),
            GetJobPosting(self.postings, self.sessions, self.users, self.clock),
            self.events,
            self.clock,
            charge=ChargeCreditForRun(self.credits),
        )

    async def user(self, *, extra: int = 0) -> UserOwner:
        owner = await seed_user(self.users, "alex@example.com", self.earlier)
        seed_account(self.credits, owner.user_id, self.earlier, extra=extra)
        return owner

    async def command(
        self, owner: UserOwner, *, cv_extracted: bool = True
    ) -> RequestTailoringRunCommand:
        cv = (extracted_cv if cv_extracted else uploaded_cv)(owner, self.earlier)
        posting = pasted_posting(owner, self.earlier)
        await self.cvs.add(cv)
        await self.postings.add(posting)
        return RequestTailoringRunCommand(owner=owner, base_cv_id=cv.id, job_posting_id=posting.id)

    async def account(self, owner: UserOwner) -> CreditAccount:
        account = await self.credits.find(owner.user_id)
        assert account is not None
        return account


@pytest.mark.parametrize("balance", [1, 2, 5])
async def test_a_request_with_credit_commits_a_queued_run_and_one_charge_naming_it(
    balance: int, clock: FixedClock
) -> None:
    w = _World(clock)
    owner = await w.user(extra=balance - 1)

    result = await w.use_case()(await w.command(owner))

    assert result.status is TailoringRunStatus.QUEUED
    account = await w.account(owner)
    assert account.balance == balance - 1
    charge = account.entries[-1]
    assert (charge.kind, charge.credits, charge.at) == (CreditEntryKind.CHARGE, -1, clock.now())
    assert charge.run == ChargedRunRef(value=result.tailoring_run_id.value)
    assert len([e for e in account.entries if e.kind is CreditEntryKind.CHARGE]) == 1


async def test_the_runs_own_events_are_published_then_the_charges(clock: FixedClock) -> None:
    w = _World(clock)
    owner = await w.user()

    await w.use_case()(await w.command(owner))

    assert [type(e) for e in w.events.published] == [TailoringRunRequested, CreditCharged]


async def test_the_run_is_inserted_before_the_account_lock_is_taken(clock: FixedClock) -> None:
    """Plan §0.3 step 4 before 5: the lock is held for as little as possible, and the run is already
    there for a waiting request to find."""
    w = _World(clock, _RunVisibleAtLock)
    spy = w.credits
    assert isinstance(spy, _RunVisibleAtLock)
    owner = await w.user()

    result = await w.use_case()(await w.command(owner))

    assert len(spy.runs_at_lock) == 1
    assert result.tailoring_run_id in spy.runs_at_lock[0]


async def test_at_balance_zero_the_request_is_refused_with_no_charge_and_nothing_published(
    clock: FixedClock,
) -> None:
    """F-1. (That no run row survives is the handler's rollback, proven in the HTTP tests.)"""
    w = _World(clock)
    owner = await w.user()
    first = await w.use_case()(await w.command(owner))
    w.runs.discard(first.tailoring_run_id)  # the handler's rollback, so the soft check passes
    w.events.published.clear()
    entries_before = len((await w.account(owner)).entries)

    with pytest.raises(NoCreditsLeft):
        await w.use_case()(await w.command(owner))

    account = await w.account(owner)
    assert account.balance == 0
    assert len(account.entries) == entries_before
    assert w.events.published == []


async def test_a_rival_run_that_became_visible_under_the_lock_refuses_the_request(
    clock: FixedClock,
) -> None:
    """F-2, AC-8: the hard re-check. The soft check ran first and saw nothing; the rival committed
    while this request waited for the lock; the re-check, run under the lock, finds it."""
    w = _World(
        clock, lambda runs: _CompetitorCommitsWhileWaiting(runs, clock.now() - timedelta(seconds=1))
    )
    rival_source = w.credits
    assert isinstance(rival_source, _CompetitorCommitsWhileWaiting)
    owner = await w.user(extra=1)

    with pytest.raises(TailoringAlreadyRunning) as refused:
        await w.use_case()(await w.command(owner))

    assert rival_source.rival is not None
    assert refused.value.active_run_id == rival_source.rival.id
    assert w.events.published == []


async def test_the_hard_recheck_does_not_mistake_the_request_for_its_own_rival(
    clock: FixedClock,
) -> None:
    """The re-check excludes the run it just inserted; without that, every request refuses itself."""
    w = _World(clock)
    owner = await w.user()

    result = await w.use_case()(await w.command(owner))

    assert result.status is TailoringRunStatus.QUEUED


@pytest.mark.parametrize("active", [queued_run, running_run], ids=["queued", "running"])
async def test_an_active_run_is_refused_before_any_lock_is_taken(
    active: Callable[..., TailoringRun], clock: FixedClock
) -> None:
    w = _World(clock)
    owner = await w.user(extra=1)
    existing = active(owner, w.earlier)
    await w.runs.add(existing)

    with pytest.raises(TailoringAlreadyRunning) as refused:
        await w.use_case()(await w.command(owner))

    assert refused.value.active_run_id == existing.id
    assert w.credits.lock_calls == 0
    assert (await w.account(owner)).balance == 2


async def test_a_user_at_the_run_cap_is_refused_before_any_lock_is_taken(
    clock: FixedClock,
) -> None:
    w = _World(clock)
    owner = await w.user()
    for _ in range(500):
        await w.runs.add(failed_run(owner, w.earlier))

    with pytest.raises(TooManyTailoringRuns):
        await w.use_case()(await w.command(owner))

    assert w.credits.lock_calls == 0
    assert (await w.account(owner)).balance == 1


async def test_a_cv_not_yet_extracted_is_refused_and_nothing_is_charged(clock: FixedClock) -> None:
    """F-24."""
    w = _World(clock)
    owner = await w.user()

    with pytest.raises(BaseCvNotReadyForTailoring):
        await w.use_case()(await w.command(owner, cv_extracted=False))

    assert w.credits.lock_calls == 0
    assert (await w.account(owner)).balance == 1


async def test_a_cv_that_is_not_the_users_is_not_found_and_nothing_is_charged(
    clock: FixedClock,
) -> None:
    w = _World(clock)
    owner = await w.user()
    command = await w.command(owner)
    stranger = await seed_user(w.users, "other@example.com", w.earlier)

    with pytest.raises(BaseCvNotFound):
        await w.use_case()(
            RequestTailoringRunCommand(
                owner=stranger,
                base_cv_id=command.base_cv_id,
                job_posting_id=command.job_posting_id,
            )
        )

    assert w.credits.lock_calls == 0
    assert (await w.account(owner)).balance == 1


async def test_a_guest_cannot_request_a_run_and_nothing_is_written(clock: FixedClock) -> None:
    """F-25 at the use-case level: the closed boundary's last line of defence."""
    w = _World(clock)
    session = await create_active_session(w.sessions, clock)
    guest = GuestOwner(session.id)
    cv, posting = extracted_cv(guest, w.earlier), pasted_posting(guest, w.earlier)
    await w.cvs.add(cv)
    await w.postings.add(posting)

    with pytest.raises(AccountRequired):
        await w.use_case()(
            RequestTailoringRunCommand(owner=guest, base_cv_id=cv.id, job_posting_id=posting.id)
        )

    assert w.runs.all() == []
    assert w.credits.lock_calls == 0
    assert w.events.published == []
