"""`TypeDecorator`s round-tripping the `scoring` context's value objects (ADR-0007, slice 5.1).

One class per value object, written out, as in every other `types/` module. `ScoredCvRefType` and
`ScoredPostingRefType` exist because `domain/scoring` imports no sibling (ADR-0040 decision 1), and
lending `BaseCvIdType` to those columns would load an id `==` never matches (`TrackedRunRefType`'s
reason).

**Everything re-validates on load**, so a row a hand-written `UPDATE` bent fails loudly rather than
reaching a page. `FitGapsType` is the one that matters: a gap is derived from a CV, so PII, and nothing
here logs it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, SmallInteger, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from tailorcraft.domain.scoring.value_objects import (
    CvFingerprint,
    FitGap,
    FitScoreId,
    MicroUsd,
    ModelName,
    PostingFingerprint,
    PromptVersion,
    ScoredCvRef,
    ScoredPostingRef,
    ScorePercent,
)


class FitScoreIdType(TypeDecorator[FitScoreId]):
    """`scoring_fit_score.id`: a native `UUID` carrying a `FitScoreId`."""

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def process_bind_param(self, value: FitScoreId | None, dialect: Dialect) -> UUID | None:
        return None if value is None else value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> FitScoreId | None:
        return None if value is None else FitScoreId(value)


class ScoredCvRefType(TypeDecorator[ScoredCvRef]):
    """`scoring_fit_score.base_cv_id`: a native `UUID`, no foreign key (ADR-0040 decision 8)."""

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def process_bind_param(self, value: ScoredCvRef | None, dialect: Dialect) -> UUID | None:
        return None if value is None else value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> ScoredCvRef | None:
        return None if value is None else ScoredCvRef(value)


class ScoredPostingRefType(TypeDecorator[ScoredPostingRef]):
    """`scoring_fit_score.job_posting_id`: a native `UUID`, no foreign key. See `ScoredCvRefType`."""

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def process_bind_param(self, value: ScoredPostingRef | None, dialect: Dialect) -> UUID | None:
        return None if value is None else value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> ScoredPostingRef | None:
        return None if value is None else ScoredPostingRef(value)


class CvFingerprintType(TypeDecorator[CvFingerprint]):
    """`scoring_fit_score.cv_fingerprint`: `TEXT`, its grammar held by `CvFingerprint`."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: CvFingerprint | None, dialect: Dialect) -> str | None:
        return None if value is None else value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> CvFingerprint | None:
        return None if value is None else CvFingerprint(value)


class PostingFingerprintType(TypeDecorator[PostingFingerprint]):
    """`scoring_fit_score.posting_fingerprint`: `TEXT`. See `CvFingerprintType`."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: PostingFingerprint | None, dialect: Dialect) -> str | None:
        return None if value is None else value.value

    def process_result_value(
        self, value: Any | None, dialect: Dialect
    ) -> PostingFingerprint | None:
        return None if value is None else PostingFingerprint(value)


class ScorePercentType(TypeDecorator[ScorePercent]):
    """`scoring_fit_score.score`: `SMALLINT` plus `ck_scoring_fit_score_score_in_range`."""

    impl = SmallInteger
    cache_ok = True

    def process_bind_param(self, value: ScorePercent | None, dialect: Dialect) -> int | None:
        return None if value is None else value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> ScorePercent | None:
        return None if value is None else ScorePercent(value)


class FitGapsType(TypeDecorator[tuple[FitGap, ...]]):
    """`scoring_fit_score.gaps`: `JSONB`, a list of strings, an empty list when no gap was named."""

    impl = postgresql.JSONB
    cache_ok = True

    def process_bind_param(
        self, value: tuple[FitGap, ...] | None, dialect: Dialect
    ) -> list[str] | None:
        return None if value is None else [gap.value for gap in value]

    def process_result_value(
        self, value: Any | None, dialect: Dialect
    ) -> tuple[FitGap, ...] | None:
        return None if value is None else tuple(FitGap(gap) for gap in value)


class ModelNameType(TypeDecorator[ModelName]):
    """`scoring_fit_score.model`: `TEXT`, carrying scoring's own `ModelName` (ADR-0040 decision 1)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: ModelName | None, dialect: Dialect) -> str | None:
        return None if value is None else value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> ModelName | None:
        return None if value is None else ModelName(value)


class PromptVersionType(TypeDecorator[PromptVersion]):
    """`scoring_fit_score.prompt_version`: `TEXT`. See `ModelNameType`."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: PromptVersion | None, dialect: Dialect) -> str | None:
        return None if value is None else value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> PromptVersion | None:
        return None if value is None else PromptVersion(value)


class MicroUsdType(TypeDecorator[MicroUsd]):
    """`scoring_fit_score.cost_microusd`: `BIGINT`, whole micro-USD (ADR-0041 decision 1)."""

    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value: MicroUsd | None, dialect: Dialect) -> int | None:
        return None if value is None else value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> MicroUsd | None:
        return None if value is None else MicroUsd(value)
