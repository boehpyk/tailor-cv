"""Fit-score rows written as SQL, for the tests of what *moves* or *deletes* them (slice 5.1, T21).

No CV or posting row is needed behind a score (neither carries a foreign key, ADR-0014), so a claim
test can aim a score at a CV by id alone. The fingerprints are the identity, per owner: two rows with
the same pair under one owner are a twin and the unique constraint refuses the second.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def fingerprint(n: int) -> str:
    return f"{n:064x}"


async def insert_score_row(
    session: AsyncSession,
    *,
    scored_at: datetime,
    user_id: UUID | None = None,
    guest_session_id: UUID | None = None,
    base_cv_id: UUID | None = None,
    job_posting_id: UUID | None = None,
    pair: int = 1,
    score: int = 50,
) -> UUID:
    """One success. `pair` picks the (cv, posting) fingerprints, so equal `pair` under one owner is a
    twin. Flushed, never committed."""
    row_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO scoring_fit_score (id, guest_session_id, user_id, base_cv_id, "
            "job_posting_id, cv_fingerprint, posting_fingerprint, score, gaps, model, "
            "prompt_version, input_tokens, output_tokens, unmetered_attempts, llm_duration_ms, "
            "cost_microusd, scored_at) VALUES (:id, :g, :u, :cv, :p, :cfp, :pfp, :score, "
            "CAST('[]' AS jsonb), 'm', '1', 1, 1, 0, 1, 1, :at)"
        ),
        {
            "id": row_id,
            "g": guest_session_id,
            "u": user_id,
            "cv": base_cv_id or uuid4(),
            "p": job_posting_id or uuid4(),
            "cfp": fingerprint(pair),
            "pfp": fingerprint(pair + 1000),
            "score": score,
            "at": scored_at,
        },
    )
    return row_id


async def owner_of(session: AsyncSession, row_id: UUID) -> tuple[UUID | None, UUID | None] | None:
    """`(guest_session_id, user_id)` of one row, or `None` when the row does not exist."""
    row = (
        await session.execute(
            text("SELECT guest_session_id, user_id FROM scoring_fit_score WHERE id = :i"),
            {"i": row_id},
        )
    ).first()
    return None if row is None else (row[0], row[1])


async def score_of(session: AsyncSession, row_id: UUID) -> int | None:
    row = (
        await session.execute(
            text("SELECT score FROM scoring_fit_score WHERE id = :i"), {"i": row_id}
        )
    ).first()
    return None if row is None else int(row[0])
