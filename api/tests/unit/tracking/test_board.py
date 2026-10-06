"""AC-7: the board read model is frozen, slotted, behaviour-free, and carries plain `str` for text.

`board.py` is complete in the skeleton (pure data), so these are green on arrival and guard it
against drift: a document body column, a `title: ApplicationTitle`, or a method creeping in.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tailorcraft.domain.tracking.board import (
    ApplicationBoard,
    BoardBaseCv,
    BoardCard,
    BoardPosting,
    BoardRun,
)
from tailorcraft.domain.tracking.value_objects import (
    ApplicationStage,
    TrackedApplicationId,
    TrackedRunRef,
)

_AT = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)

_ALL = [BoardCard, BoardPosting, BoardBaseCv, BoardRun, ApplicationBoard]


def _card() -> BoardCard:
    return BoardCard(
        id=TrackedApplicationId(uuid4()),
        tailoring_run_id=TrackedRunRef(uuid4()),
        stage=ApplicationStage.APPLIED,
        title="Staff Engineer",
        tracked_at=_AT,
        stage_changed_at=_AT,
        version=1,
        run=None,
        posting=None,
        base_cv=None,
    )


@pytest.mark.parametrize("cls", _ALL, ids=lambda c: c.__name__)
def test_every_board_type_is_a_frozen_slotted_dataclass(cls: type) -> None:
    assert dataclasses.is_dataclass(cls)
    assert cls.__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert hasattr(cls, "__slots__")


def test_a_board_card_cannot_be_mutated() -> None:
    card = _card()

    with pytest.raises(dataclasses.FrozenInstanceError):
        card.stage = ApplicationStage.OFFER  # type: ignore[misc]


@pytest.mark.parametrize("cls", _ALL, ids=lambda c: c.__name__)
def test_no_board_type_defines_a_method_of_its_own(cls: type) -> None:
    own = {
        name
        for name, value in vars(cls).items()
        if callable(value) and not (name.startswith("__") and name.endswith("__"))
    }
    assert own == set()


def test_board_card_field_set_is_exactly_the_agreed_fields() -> None:
    assert [f.name for f in dataclasses.fields(BoardCard)] == [
        "id",
        "tailoring_run_id",
        "stage",
        "title",
        "tracked_at",
        "stage_changed_at",
        "version",
        "run",
        "posting",
        "base_cv",
    ]


def test_board_posting_field_set_has_a_preview_and_no_full_text() -> None:
    assert {f.name for f in dataclasses.fields(BoardPosting)} == {
        "job_posting_id",
        "source",
        "title",
        "source_url",
        "preview",
    }


def test_board_base_cv_carries_label_and_filename_only() -> None:
    assert {f.name for f in dataclasses.fields(BoardBaseCv)} == {
        "base_cv_id",
        "label",
        "original_filename",
    }


def test_board_run_carries_no_document_body() -> None:
    assert {f.name for f in dataclasses.fields(BoardRun)} == {
        "tailoring_run_id",
        "requested_at",
        "edited",
    }


def test_a_board_card_title_is_a_plain_str() -> None:
    assert type(_card().title) is str


def test_an_application_board_holds_its_cards_as_a_tuple() -> None:
    board = ApplicationBoard(cards=(_card(),))

    assert isinstance(board.cards, tuple)
    assert len(board.cards) == 1
