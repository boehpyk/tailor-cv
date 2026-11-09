"""A rig for slice 5.2's tailoring tests: the fakes of one failure path, each logging into one
shared `journal`, so a test can assert the *order* in which the money, the counter, the run and the
events were written. The order is the contract: the refund and the count are written before the run's
save so the worker's one commit carries all three (plan §0.4), and events follow the save.

The fakes have no transaction, so nothing here can show a write *vanishing*. Tests that need that are
in `tests/integration/billing/test_pending_adapters.py`, held until the real adapters exist.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tailorcraft.application.billing.refund_credit_for_run import RefundCreditForRun
from tailorcraft.application.tailoring.record_run_failure import RecordRunFailure
from tailorcraft.domain.billing.credit_account import CreditAccount
from tailorcraft.domain.billing.value_objects import ChargedRunRef
from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.counters import DemandCounter
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tests.integration.billing.support import a_user_id, charge_directly, seed_account
from tests.integration.fakes import (
    FakeCreditAccountRepository,
    FakeTailoringRunRepository,
    RecordingDailyCounter,
    RecordingEventPublisher,
)
from tests.integration.owners import queued_run, running_run


class JournalRuns(FakeTailoringRunRepository):
    def __init__(self, journal: list[str], conflict_on_save: int = 0) -> None:
        super().__init__(conflict_on_save=conflict_on_save)
        self._journal = journal

    async def save(self, run: TailoringRun) -> None:
        await super().save(run)
        self._journal.append(f"runs.save:{run.status.value}")


class JournalCredits(FakeCreditAccountRepository):
    def __init__(self, journal: list[str]) -> None:
        super().__init__()
        self._journal = journal

    async def save(self, account: CreditAccount) -> None:
        await super().save(account)
        self._journal.append("credits.save")


class JournalEvents(RecordingEventPublisher):
    def __init__(self, journal: list[str]) -> None:
        super().__init__()
        self._journal = journal

    async def publish(self, *events: DomainEvent) -> None:
        await super().publish(*events)
        self._journal.append("events.publish")


class Rig:
    """One user with one credit, a journal, and the real `RecordRunFailure` over the fakes."""

    def __init__(
        self,
        at: datetime,
        *,
        extra: int = 0,
        conflict_on_save: int = 0,
        counters: RecordingDailyCounter | None = None,
    ) -> None:
        self.at = at
        self.journal: list[str] = []
        self.runs = JournalRuns(self.journal, conflict_on_save)
        self.credits = JournalCredits(self.journal)
        self.counters = counters or RecordingDailyCounter(journal=self.journal)
        self.events = JournalEvents(self.journal)
        self.refund = RefundCreditForRun(self.credits)
        self.record_failure = RecordRunFailure(self.runs, self.refund, self.counters, self.events)
        self.owner = UserOwner(a_user_id())
        seed_account(self.credits, self.owner.user_id, at - timedelta(hours=2), extra=extra)

    async def balance(self) -> int:
        account = await self.credits.find(self.owner.user_id)
        assert account is not None
        return account.balance

    async def entries(self) -> int:
        account = await self.credits.find(self.owner.user_id)
        assert account is not None
        return len(account.entries)

    async def charge(self, run: TailoringRun) -> None:
        """One charge entry naming `run`, as `RequestTailoringRun` would have written it."""
        await charge_directly(
            self.credits, self.owner.user_id, ChargedRunRef(value=run.id.value), self.at
        )

    async def run(
        self,
        *,
        charged: bool = True,
        running: bool = False,
        age: timedelta = timedelta(0),
        base_cv_id: BaseCvId | None = None,
        job_posting_id: JobPostingId | None = None,
    ) -> TailoringRun:
        """A run of the rig's user, requested `age` before `at`, `running` since the same instant.
        Arrangement only: the journal and the counter are cleared before it returns."""
        requested_at = self.at - age
        run = (running_run if running else queued_run)(
            self.owner, requested_at, base_cv_id=base_cv_id, job_posting_id=job_posting_id
        )
        await self.runs.add(run)
        if charged:
            await self.charge(run)
        self.journal.clear()
        self.counters.calls.clear()
        self.counters.counts.clear()
        self.events.published.clear()
        self.credits.save_calls = 0
        self.runs.save_calls.clear()
        self.runs.saved.clear()
        return run

    def count(self, counter: DemandCounter) -> int:
        return self.counters.count(counter, self.at.date())
