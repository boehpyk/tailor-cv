"""Specifications that need slice 5.2's real adapters, written now and **held with a skip** (T14).

Each test names the task whose adapter makes it meaningful. They are specifications, not yet tests:
the bodies are intentionally empty, because a body written against an adapter that does not exist
is a guess at its constructor, and an assertion on a fake would prove the fake. The fakes in
`tests/integration/fakes.py` have no transaction and no lock, so none of these can be shown on them.

When the adapter lands, the owner of that task un-skips its tests here (or moves them to T28 / T30
if they are really HTTP or persistence tests) and writes the body from the docstring: that is the
acceptance criterion, not whatever the adapter was observed doing.
"""

from __future__ import annotations

import pytest

_NEEDS_T21 = "needs SqlAlchemyCreditAccountRepository (T21)"
_NEEDS_T22 = "needs SqlAlchemyDailyCounter (T22)"
_NEEDS_T23 = "needs the worker's whole-transaction rollback on a failed save (T23)"


@pytest.mark.skip(reason=f"{_NEEDS_T21}; {_NEEDS_T22}; {_NEEDS_T23}")
@pytest.mark.parametrize(
    "site",
    ["llm_failure", "base_cv_deleted", "cv_text_missing", "abandoned_redelivery", "not_queued"],
)
async def test_a_fault_after_the_refund_write_leaves_neither_the_failure_nor_the_refund(
    site: str,
) -> None:
    """AC-9. For each of the failure sites reachable from the worker and the handler (the sweep's two
    are the next two tests): a charged run, real credit and run repositories and the real counter on
    one session; inject a database failure *after* the refund is written (patch the library call
    below the repositories, not the repository); roll back as the caller does. Assert that the run is
    **not** `failed`, **no** refund entry exists, and `runs_failed` did not move."""
    ...


@pytest.mark.skip(reason=f"{_NEEDS_T21}; {_NEEDS_T22}; {_NEEDS_T23}")
@pytest.mark.parametrize("list_name", ["stale_running", "lost_queued"])
async def test_a_fault_after_the_sweeps_refund_write_leaves_neither_failure_nor_refund(
    list_name: str,
) -> None:
    """AC-9, the sweep's `abandoned` and `not_queued` sites, same shape as above, through the real
    `CommittingTailoringRunRepository`."""
    ...


@pytest.mark.skip(reason=f"{_NEEDS_T21}; {_NEEDS_T22}; {_NEEDS_T23}")
async def test_the_loser_of_a_sweep_against_redelivery_race_loses_its_refund_and_count_too() -> (
    None
):
    """F-14. Stage a version conflict on the failure write with `concurrent_app` at the moment the
    spec names (sweep and a redelivered task both holding the same `running` row). Exactly one side
    commits `failed`; afterwards the account holds **one** refund and `runs_failed` is **1**. The
    loser's whole transaction, refund and count included, has rolled back. The sweep carries on to
    its next run, which keeps its loaded state (ids first, re-read by id)."""
    ...


@pytest.mark.skip(reason=_NEEDS_T21)
async def test_a_refund_racing_a_charge_loses_nothing() -> None:
    """AC-13, F-15. Two sessions on one account (`concurrent_app`): a charge holds the account lock,
    a refund for a different charged run commits meanwhile (it takes no lock). After both commit the
    balance equals the sum of the entries, and the entries hold one charge and one refund. A charge
    that read the balance just before the refund committed may answer `no_credits` once (accepted)."""
    ...


@pytest.mark.skip(reason=_NEEDS_T21)
async def test_two_concurrent_requests_from_one_account_make_one_run_and_one_charge() -> None:
    """AC-8, F-2. Two requests at once from an account with balance >= 2 (`concurrent_app`, a real
    session per request), staged so both pass the soft check before either takes the lock. Exactly one
    run and one charge exist afterwards; the other answers 409 `tailoring_already_running` with the
    first run's id. With balance 1: still one run and one charge; the other gets 409 (either code).
    **Mutation**: move the hard re-check before `lock_for_charge`; this test must go red. Likely
    belongs in T28 (it is an HTTP-level race)."""
    ...


@pytest.mark.skip(reason=_NEEDS_T21)
async def test_a_request_commits_the_run_and_the_charge_together_or_neither() -> None:
    """AC-6, AC-7. On a real session: success commits the `queued` run and the `charge` entry in one
    commit (balance N becomes N-1); at balance 0 the rollback leaves **no run row**, no entry and no
    enqueue (the application test can only show the refusal is raised)."""
    ...


@pytest.mark.skip(reason=f"{_NEEDS_T21}; {_NEEDS_T22}")
async def test_confirming_commits_the_user_the_welcome_entry_and_the_sign_up_count_together() -> (
    None
):
    """AC-1, AC-27 `sign_ups`. Confirm on a real session and roll back after `open_account`: no user,
    no account, no entry, no count. Commit: all three."""
    ...


@pytest.mark.skip(reason=_NEEDS_T22)
@pytest.mark.parametrize(
    "counter", ["runs_failed", "runs_succeeded", "pack_clicks", "pack_clickers"]
)
async def test_a_counter_rolled_back_with_its_fact_counts_nothing(counter: str) -> None:
    """AC-27: one test per counter, each driven through the use case that owns it on a real session
    with its fact rolled back afterwards. (`fit_scores`, `fit_scores_busy` and `sign_ups` are asserted
    in T24 and the test above.) The counter row must not exist after the rollback."""
    ...
