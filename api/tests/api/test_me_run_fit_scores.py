"""The HTTP contract of a run's before/after comparison (slice 5.2, T28, **red first**): technical plan
§4 rows 5-6; AC-18 to AC-21, AC-23; S-1 to S-9.

    POST /api/me/tailoring-runs/{id}/fit-scores/{before|after}      -> 201 new / 200 stored
    GET  /api/me/tailoring-runs/{id}/fit-scores                     -> the stored sides and the verdict

Written from the acceptance criteria and the failure contract, not from the handlers (T27's bodies are
`NotImplementedError`; the client re-raises nothing, so a red reads `assert 500 == 201`).

- **A scorer is a fake on `app.state.fit_scorer`; no test calls Gemini.** `SideScorer` answers by what
  it is shown: the base CV's text ("word word ...") is the before side, anything else the after side.
  That is also how a test proves **which text** was scored.
- **Failure rows follow 5.1's translation**, which the feature spec's S-1 row cites ("5.1's
  translation") while summarising it as "503 `scoring_failed`": a rate limit is that translation's 429
  `rate_limited` with `Retry-After`, a refusal its 422 `scoring_refused`. Where the two disagree the
  citation wins, because the comparison goes through 5.1's machinery on purpose (plan §0.8).
- **Responses a handler produces are `no-store` and set no cookie.** A credential 401 and FastAPI's own
  422 are produced before a handler, and `no-store` on them is not in §4's text.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingText
from tailorcraft.domain.scoring.errors import FitScoringFailed
from tailorcraft.domain.scoring.value_objects import (
    AssessedFit,
    FitScoreFailureReason,
    ModelName,
)
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.claim_race_world import World
from tests.api.credits_support import counter_total, fit_scores_url
from tests.api.me_support import (
    ME_RUNS,
    Account,
    Entry,
    error_body,
    error_code,
    mint_guest,
    new_client,
    override_settings,
    register,
    seed_entry,
)
from tests.integration.fakes import RecordingSpendLedger
from tests.integration.owners import extracted_cv, failed_run, pasted_posting, queued_run
from tests.integration.persistence.fit_score_rows import insert_score_row
from tests.integration.scoring.support import THE_GAPS, assessed, usage

RESPONSE_KEYS = {"id", "score_percent", "gaps", "scored_at", "expires_at"}
RUN_SCORES_KEYS = {"before", "after", "comparable", "comparison", "unavailable"}
FORBIDDEN_WORDS = ("fingerprint", "token", "cost", "micro", "model", "prompt", "usage")
SIDES = ["before", "after"]


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """The 5.1 check route's limiters live in Redis, which no rollback reaches."""


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


# ----------------------------------------------------------------------------------------- the rig


class SideScorer:
    """`FitScorerPort`: the before side (the base CV's "word " text) and the after side (the draft)
    each get their own outcome. `calls` is every CV text it was shown, in order."""

    def __init__(
        self,
        before: AssessedFit | FitScoringFailed | None = None,
        after: AssessedFit | FitScoringFailed | None = None,
    ) -> None:
        self.before = before if before is not None else assessed(60)
        self.after = after if after is not None else assessed(75)
        self.calls: list[str] = []

    async def assess(self, cv: object, posting: object) -> AssessedFit:
        value = cv.value  # type: ignore[attr-defined]
        self.calls.append(value)
        outcome = self.before if "word" in value else self.after
        if isinstance(outcome, FitScoringFailed):
            raise outcome
        return outcome


def install(app: FastAPI, **kwargs: AssessedFit | FitScoringFailed | None) -> SideScorer:
    scorer = SideScorer(**kwargs)
    app.state.fit_scorer = scorer
    return scorer


@dataclass
class Rig:
    account: Account
    entry: Entry

    @property
    def run_id(self) -> UUID:
        return self.entry.run_id.value


async def rig(client: AsyncClient, session: AsyncSession, settings: Settings) -> Rig:
    """An account with a saved CV ("word " x 60), a posting and a **succeeded** run over them (its
    draft CV is "c" x 500, so the two sides are told apart by their text)."""
    account = await register(client, settings)
    entry = await seed_entry(
        session,
        settings,
        account.owner,
        at=SystemClock().now() - timedelta(minutes=10),
        ready_formats=(),
    )
    return Rig(account, entry)


async def post(client: AsyncClient, r: Rig, side: str, run_id: object = None) -> Response:
    return await client.post(fit_scores_url(run_id or r.run_id, side), headers=r.account.headers)


async def get(client: AsyncClient, r: Rig, run_id: object = None) -> Response:
    return await client.get(fit_scores_url(run_id or r.run_id), headers=r.account.headers)


def answered(response: Response, status_code: int, code: str | None = None) -> Response:
    assert response.status_code == status_code, response.text
    assert response.headers.get("cache-control") == "no-store", dict(response.headers)
    assert "set-cookie" not in response.headers, dict(response.headers)
    if code is not None:
        assert error_code(response) == code
    return response


def failing(reason: FitScoreFailureReason) -> FitScoringFailed:
    return FitScoringFailed(reason, usage())


async def add_run(session: AsyncSession, run: TailoringRun) -> None:
    """A real posting for the run first (`add` refuses a run whose posting is gone), then the run."""
    await SqlAlchemyJobPostingRepository(session).add(
        JobPosting.from_pasted_text(
            id=run.job_posting_id,
            owner=run.owner,
            text=JobPostingText("posting " * 40),
            created_at=run.requested_at,
        )
    )
    await SqlAlchemyTailoringRunRepository(session).add(run)
    await session.commit()


async def undecided_run(
    client: AsyncClient, session: AsyncSession, settings: Settings, *, failed: bool
) -> Rig:
    account = await register(client, settings)
    at = SystemClock().now() - timedelta(minutes=10)
    cv = extracted_cv(account.owner, at)
    await SqlAlchemyBaseCvRepository(session).add(cv)
    run = (failed_run if failed else queued_run)(account.owner, at, base_cv_id=cv.id)
    run_id = run.id
    await add_run(session, run)
    entry = Entry(
        cv_id=cv.id,
        posting_id=run.job_posting_id,
        posting_preview="",
        run_id=run_id,
        run_version=1,
        jobs=[],
        run=run,
    )
    return Rig(account, entry)


async def stored_rows(session: AsyncSession, user_id: UUID) -> list[tuple[UUID | None, UUID]]:
    rows = await session.execute(
        text(
            "SELECT tailoring_run_id, base_cv_id FROM scoring_fit_score "
            "WHERE user_id = :u ORDER BY scored_at, id"
        ),
        {"u": user_id},
    )
    return [(r[0], r[1]) for r in rows]


# -------------------------------------------------------------------- AC-18: score, then the same


@pytest.mark.parametrize("side", SIDES)
async def test_ac18_a_new_side_is_201_with_a_whole_score_and_the_second_ask_is_200_byte_identical(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, side: str
) -> None:
    scorer = install(app)
    r = await rig(client, session, settings)

    first = answered(await post(client, r, side), 201)
    second = answered(await post(client, r, side), 200)

    body = first.json()
    assert set(body) == RESPONSE_KEYS
    assert body["gaps"] == list(THE_GAPS)
    assert body["expires_at"] is None
    datetime.fromisoformat(body["scored_at"])
    assert body["score_percent"] == (60 if side == "before" else 75)
    assert second.content == first.content
    assert len(scorer.calls) == 1, "the stored side is served without a second model call"


async def test_ac18_before_finds_the_score_an_earlier_check_of_the_same_pair_stored(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    scorer = install(app)
    r = await rig(client, session, settings)
    checked = await client.post(
        "/api/me/fit-scores",
        json={
            "base_cv_id": str(r.entry.cv_id.value),
            "job_posting_id": str(r.entry.posting_id.value),
        },
        headers=r.account.headers,
    )
    assert checked.status_code == 201, checked.text

    response = answered(await post(client, r, "before"), 200)

    assert response.json() == checked.json()
    assert len(scorer.calls) == 1


async def test_ac18_after_scores_the_draft_as_plain_text_and_never_the_base_cv(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    scorer = install(app)
    r = await rig(client, session, settings)

    answered(await post(client, r, "after"), 201)

    assert scorer.calls == ["c" * 500], "the model is shown the draft, as plain text"


async def test_ac18_after_scores_the_draft_not_the_users_revision_of_it(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    scorer = install(app)
    r = await rig(client, session, settings)
    revised = await client.put(
        f"{ME_RUNS}/{r.run_id}/documents/cv",
        json={
            "content": "Revised curriculum vitae line. " * 30,
            "expected_version": r.entry.run_version,
        },
        headers=r.account.headers,
    )
    assert revised.status_code == 200, revised.text

    answered(await post(client, r, "after"), 201)

    assert scorer.calls == ["c" * 500], "ADR-0015: the revision is never what a score judges"


async def test_ac18_each_side_is_stored_against_the_runs_saved_cv_and_only_the_after_names_the_run(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    install(app)
    r = await rig(client, session, settings)

    answered(await post(client, r, "before"), 201)
    answered(await post(client, r, "after"), 201)

    rows = await stored_rows(session, r.account.user_id.value)
    assert sorted(rows, key=lambda row: row[0] is not None) == [
        (None, r.entry.cv_id.value),
        (r.run_id, r.entry.cv_id.value),
    ]


@pytest.mark.parametrize("side", SIDES)
async def test_no_response_carries_a_fingerprint_a_token_count_a_cost_or_a_model(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, side: str
) -> None:
    install(app)
    r = await rig(client, session, settings)

    texts = [
        answered(await post(client, r, side), 201).text,
        answered(await post(client, r, side), 200).text,
        answered(await get(client, r), 200).text,
    ]

    for body in texts:
        for word in FORBIDDEN_WORDS:
            assert word not in body.lower(), word


# ------------------------------------------------------------ AC-19: limits, the cap, the counter


@pytest.mark.parametrize("side", SIDES)
async def test_ac19_a_side_ignores_the_owner_limit_and_counts_no_fit_score(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    side: str,
) -> None:
    override_settings(app, settings, fit_score_max_per_user_per_day=1)
    install(app)
    r = await rig(client, session, settings)
    await insert_score_row(session, scored_at=SystemClock().now(), user_id=r.account.user_id.value)
    await session.commit()
    # The control: the limit is live for the ordinary check of a different pair.
    other = pasted_posting(r.account.owner, SystemClock().now() - timedelta(minutes=5), marker="q")
    other_id = str(other.id.value)
    await SqlAlchemyJobPostingRepository(session).add(other)
    await session.commit()
    refused = await client.post(
        "/api/me/fit-scores",
        json={"base_cv_id": str(r.entry.cv_id.value), "job_posting_id": other_id},
        headers=r.account.headers,
    )
    assert refused.status_code == 409, refused.text
    before = await counter_total(session, "fit_scores")

    response = await post(client, r, side)

    answered(response, 201)
    assert await counter_total(session, "fit_scores") == before


@pytest.mark.parametrize("side", SIDES)
async def test_s4_a_full_cap_is_503_scoring_busy_with_no_call_no_row_and_one_busy_count(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    side: str,
) -> None:
    override_settings(app, settings, fit_score_daily_spend_cap_usd=Decimal(0))
    scorer = install(app)
    r = await rig(client, session, settings)
    busy_before = await counter_total(session, "fit_scores_busy")

    answered(await post(client, r, side), 503, "scoring_busy")

    assert scorer.calls == []
    assert await stored_rows(session, r.account.user_id.value) == []
    assert await counter_total(session, "fit_scores_busy") - busy_before == 1


@pytest.mark.parametrize("side", SIDES)
async def test_a_stored_side_is_served_with_the_cap_full(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, side: str
) -> None:
    install(app)
    r = await rig(client, session, settings)
    answered(await post(client, r, side), 201)
    override_settings(app, settings, fit_score_daily_spend_cap_usd=Decimal(0))
    busy_before = await counter_total(session, "fit_scores_busy")

    answered(await post(client, r, side), 200)

    assert await counter_total(session, "fit_scores_busy") == busy_before


# ---------------------------------------------------------------------------- S-1, S-2, S-3: failures


@pytest.mark.parametrize(
    ("reason", "status_code", "code"),
    [
        (FitScoreFailureReason.LLM_UNAVAILABLE, 503, "scoring_failed"),
        (FitScoreFailureReason.LLM_TIMED_OUT, 503, "scoring_failed"),
        (FitScoreFailureReason.LLM_OUTPUT_INVALID, 503, "scoring_failed"),
        (FitScoreFailureReason.LLM_ERROR, 503, "scoring_failed"),
        (FitScoreFailureReason.LLM_RATE_LIMITED, 429, "rate_limited"),
        (FitScoreFailureReason.LLM_REFUSED, 422, "scoring_refused"),
        (FitScoreFailureReason.INPUTS_TOO_LARGE, 422, "inputs_too_large"),
    ],
)
@pytest.mark.parametrize("side", SIDES)
async def test_s1_a_failed_side_is_5_1s_refusal_leaves_no_row_and_leaves_the_other_side_working(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    side: str,
    reason: FitScoreFailureReason,
    status_code: int,
    code: str,
) -> None:
    other = "after" if side == "before" else "before"
    failure = failing(reason)
    install(app, **{side: failure})
    r = await rig(client, session, settings)

    refused = answered(await post(client, r, side), status_code, code)

    if status_code == 429:
        assert int(refused.headers["retry-after"]) > 0
    assert await stored_rows(session, r.account.user_id.value) == []
    answered(await post(client, r, other), 201)
    assert len(await stored_rows(session, r.account.user_id.value)) == 1


async def test_a_failure_leaves_the_run_and_the_credit_untouched(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    install(app, after=failing(FitScoreFailureReason.LLM_TIMED_OUT))
    r = await rig(client, session, settings)
    balance_sql = text(
        "SELECT COALESCE(SUM(credits), 0) FROM billing_credit_entry WHERE user_id = :u"
    )
    balance = (await session.execute(balance_sql, {"u": r.account.user_id.value})).scalar_one()

    answered(await post(client, r, "after"), 503, "scoring_failed")

    run = await client.get(f"{ME_RUNS}/{r.run_id}", headers=r.account.headers)
    assert run.status_code == 200, run.text
    assert run.json()["status"] == "succeeded"
    assert (
        await session.execute(balance_sql, {"u": r.account.user_id.value})
    ).scalar_one() == balance


# ------------------------------------------------------------------ S-5, S-6, AC-21: refusals


@pytest.mark.parametrize("failed", [False, True], ids=["queued", "failed"])
@pytest.mark.parametrize("side", SIDES)
async def test_s5_a_run_that_did_not_succeed_is_409_tailoring_run_not_succeeded_with_no_call(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    side: str,
    failed: bool,
) -> None:
    scorer = install(app)
    r = await undecided_run(client, session, settings, failed=failed)

    answered(await post(client, r, side), 409, "tailoring_run_not_succeeded")

    assert scorer.calls == []


@pytest.mark.parametrize("failed", [False, True], ids=["queued", "failed"])
async def test_s5_the_read_of_a_run_that_did_not_succeed_says_not_succeeded(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, failed: bool
) -> None:
    install(app)
    r = await undecided_run(client, session, settings, failed=failed)

    body = answered(await get(client, r), 200).json()

    assert body == {
        "before": None,
        "after": None,
        "comparable": None,
        "comparison": None,
        "unavailable": "not_succeeded",
    }


@pytest.mark.parametrize("side", SIDES)
async def test_s6_a_deleted_base_cv_is_409_base_cv_deleted_on_both_sides_with_no_call(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, side: str
) -> None:
    scorer = install(app)
    r = await rig(client, session, settings)
    await session.execute(
        text("DELETE FROM intake_base_cv WHERE id = :i"), {"i": r.entry.cv_id.value}
    )
    await session.commit()

    answered(await post(client, r, side), 409, "base_cv_deleted")

    assert scorer.calls == []


async def test_s6_the_read_after_the_cv_is_deleted_says_base_cv_deleted_with_both_sides_null(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    install(app)
    r = await rig(client, session, settings)
    await session.execute(
        text("DELETE FROM intake_base_cv WHERE id = :i"), {"i": r.entry.cv_id.value}
    )
    await session.commit()

    body = answered(await get(client, r), 200).json()

    assert body["unavailable"] == "base_cv_deleted"
    assert (body["before"], body["after"], body["comparable"], body["comparison"]) == (
        None,
        None,
        None,
        None,
    )


async def test_ac21_deleting_the_saved_cv_takes_the_runs_stored_scores_with_it(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    install(app)
    r = await rig(client, session, settings)
    answered(await post(client, r, "before"), 201)
    answered(await post(client, r, "after"), 201)
    deleted = await client.delete(
        f"/api/me/base-cvs/{r.entry.cv_id.value}", headers=r.account.headers
    )
    assert deleted.status_code == 204, deleted.text

    assert await stored_rows(session, r.account.user_id.value) == []


@pytest.mark.parametrize("side", SIDES)
async def test_ac21_another_users_run_and_a_nonexistent_run_are_the_same_404(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, side: str
) -> None:
    scorer = install(app)
    mine = await rig(client, session, settings)
    theirs = await rig(client, session, settings)

    stranger = await post(client, mine, side, run_id=theirs.run_id)
    missing = await post(client, mine, side, run_id=uuid4())

    for response in (stranger, missing):
        answered(response, 404, "tailoring_run_not_found")
    assert stranger.json()["error"]["message"] == missing.json()["error"]["message"]
    assert scorer.calls == []
    assert await stored_rows(session, theirs.account.user_id.value) == []


async def test_ac21_the_read_of_another_users_run_is_the_same_404_as_a_missing_one(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    install(app)
    mine = await rig(client, session, settings)
    theirs = await rig(client, session, settings)

    stranger = await get(client, mine, run_id=theirs.run_id)
    missing = await get(client, mine, run_id=uuid4())

    for response in (stranger, missing):
        answered(response, 404, "tailoring_run_not_found")
    assert error_body(stranger) == error_body(missing)


@pytest.mark.parametrize("path", ["bogus", "BEFORE", "both", "0"])
async def test_an_unknown_side_is_422_validation_error(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings, path: str
) -> None:
    scorer = install(app)
    r = await rig(client, session, settings)

    response = await post(client, r, path)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"
    assert scorer.calls == []


async def test_a_malformed_run_id_is_422_on_both_routes(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    install(app)
    r = await rig(client, session, settings)

    posted = await post(client, r, "before", run_id="not-a-uuid")
    read = await get(client, r, run_id="not-a-uuid")

    for response in (posted, read):
        assert response.status_code == 422, response.text
        assert error_code(response) == "validation_error"


@pytest.mark.parametrize(("method", "side"), [("POST", "before"), ("POST", "after"), ("GET", None)])
async def test_a_guest_cookie_is_not_a_credential_here(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    method: str,
    side: str | None,
) -> None:
    install(app)
    r = await rig(client, session, settings)
    await mint_guest(client, session)

    response = await client.request(method, fit_scores_url(r.run_id, side))

    assert response.status_code == 401, response.text
    assert error_code(response) == "invalid_access_token"


# ----------------------------------------------------------------- AC-20, AC-23, S-9: the read


async def test_ac20_before_anything_is_scored_the_read_is_all_null_and_calls_no_model(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    scorer = install(app)
    r = await rig(client, session, settings)

    body = answered(await get(client, r), 200).json()

    assert set(body) == RUN_SCORES_KEYS
    assert body == {
        "before": None,
        "after": None,
        "comparable": None,
        "comparison": None,
        "unavailable": None,
    }
    assert scorer.calls == []


async def test_ac20_with_one_side_stored_the_read_shows_it_and_is_not_comparable_yet(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    install(app)
    r = await rig(client, session, settings)
    created = answered(await post(client, r, "before"), 201).json()

    body = answered(await get(client, r), 200).json()

    assert body["before"] == created
    assert (body["after"], body["comparable"], body["comparison"]) == (None, None, None)


@pytest.mark.parametrize(
    ("before", "after", "delta", "verdict"),
    [
        (60, 66, 6, "higher"),
        (60, 65, 5, "about_the_same"),
        (60, 60, 0, "about_the_same"),
        (60, 55, -5, "about_the_same"),
        (60, 54, -6, "lower"),
        (20, 95, 75, "higher"),
    ],
)
async def test_ac20_both_sides_give_a_delta_and_the_domains_verdict_with_no_model_call(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    before: int,
    after: int,
    delta: int,
    verdict: str,
) -> None:
    scorer = install(app, before=assessed(before), after=assessed(after))
    r = await rig(client, session, settings)
    answered(await post(client, r, "before"), 201)
    answered(await post(client, r, "after"), 201)
    calls = len(scorer.calls)

    body = answered(await get(client, r), 200).json()

    assert body["comparable"] is True
    assert body["comparison"] == {"delta": delta, "verdict": verdict}
    assert (body["before"]["score_percent"], body["after"]["score_percent"]) == (before, after)
    assert body["unavailable"] is None
    assert len(scorer.calls) == calls, "the read never reaches the scorer"


async def test_ac23_s9_a_before_from_an_older_scorer_is_not_comparable_and_nothing_is_rescored(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    older = replace(usage(), model=ModelName("gemini-older"))
    scorer = install(app, before=assessed(60, used=older), after=assessed(80))
    r = await rig(client, session, settings)
    answered(await post(client, r, "before"), 201)
    answered(await post(client, r, "after"), 201)
    calls = len(scorer.calls)

    body = answered(await get(client, r), 200).json()

    assert body["comparable"] is False
    assert body["comparison"] is None
    assert body["before"] is not None
    assert body["after"] is not None
    assert len(scorer.calls) == calls


# ------------------------------------------------------------------------- S-8: two tabs at once


@pytest.mark.parametrize("side", SIDES)
async def test_s8_two_tabs_comparing_one_side_at_once_store_one_row_and_both_carry_it(
    settings: Settings,
    engine: AsyncEngine,
    password_hasher: object,
    tmp_path: Path,
    side: str,
) -> None:
    class _BothInside:
        """Holds each call until the other has arrived, so both really are in `assess` at once."""

        def __init__(self) -> None:
            self.barrier = asyncio.Barrier(2)
            self.calls = 0

        async def assess(self, cv: object, posting: object) -> AssessedFit:
            self.calls += 1
            await asyncio.wait_for(self.barrier.wait(), timeout=20)
            return assessed(62)

    world = World(settings, engine, password_hasher, tmp_path / "uploads")
    try:
        both = _BothInside()
        world.app.state.fit_scorer = both
        account = await world.account()
        async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
            entry = await seed_entry(
                seeding,
                world.settings,
                account.owner,
                at=SystemClock().now() - timedelta(minutes=5),
                ready_formats=(),
            )
        url = fit_scores_url(entry.run_id.value, side)
        async with new_client(world.app) as one, new_client(world.app) as two:
            first, second = await asyncio.gather(
                one.post(url, headers=account.headers), two.post(url, headers=account.headers)
            )
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT count(*) FROM scoring_fit_score WHERE user_id = :u"),
                    {"u": account.user_id.value},
                )
            ).scalar_one()

        assert sorted([first.status_code, second.status_code]) == [200, 201], (
            first.text,
            second.text,
        )
        assert first.json() == second.json()
        assert both.calls == 2
        assert rows == 1
    finally:
        await world.cleanup()
        async with engine.begin() as conn:
            assert "_test" in settings.database_url
            await conn.execute(text("DELETE FROM scoring_daily_spend"))


async def test_the_spend_ledger_is_untouched_by_a_stored_side(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    """A stored hit is served before any reservation (plan §4's error order: stored hit, then cap)."""
    from tests.api.test_fit_scores import ledger_of

    install(app)
    r = await rig(client, session, settings)
    answered(await post(client, r, "before"), 201)
    ledger: RecordingSpendLedger = ledger_of(app)

    answered(await post(client, r, "before"), 200)

    assert ledger.reservations == []
