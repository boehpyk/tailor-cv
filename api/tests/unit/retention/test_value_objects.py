"""Value objects for `retention`: `RetentionWindow`'s refusal and cutoff arithmetic (AC-3), the
expired/not-expired boundary (R-18), the five value objects' exact field sets (AC-4), and the part of
`FileRef.for_export`'s determinism guarantee (AC-9) that 1.6, not 1.5, newly depends on.

Pure domain tests: no I/O, no event loop, no mocks, no fixtures beyond a plain `datetime` literal —
never `FixedClock`, which is an infrastructure double this package may not import (technical plan
§0's import table). Every expected value here is copied from the spec, the technical plan or the
skeleton's own docstring — never from running the (currently `NotImplementedError`) code and
recording what it did.

A note on `expiry_cutoff`, because it is easy to misread. The technical plan's field sketch
(`RetentionWindow.expiry_cutoff(now) -> datetime  # the instant a session must be at or before to be
expired`) and the skeleton's docstring both describe the *comparison instant the purge hands to the
port*, not "`now` minus the window". The window is applied exactly once, at `GuestSession.start`, and
is already frozen into `expires_at` by the time the purge ever runs (ubiquitous-language contrast 1:
*"Changing it moves no existing session"*). Since the predicate is `expires_at <= as_of`
(`GuestSession.is_expired`'s own `at >= expires_at`, mirrored), the `as_of` the purge must pass is
`now` itself — `RetentionWindow.hours` plays no further arithmetic role in this one method call.
`ReclaimOrphanedFiles` is the use case that actually subtracts `window.hours + grace` from `now`, and
it does that arithmetic itself in the application layer (technical plan §2), not through this method.

**This reading is the one place in this file I had to decide rather than transcribe**, because
neither the feature-spec's AC-3 nor R-18's row states the arithmetic in so many words — they state
the *comparison's inclusivity*, not what `expiry_cutoff` computes from `hours`. I am holding the
skeleton's own docstring to its literal words ("the instant... must be at or before", matching
`is_expired`'s `at >= expires_at` one-for-one) rather than the more common meaning of "retention
window" as something you subtract. If GREEN disagrees and treats `hours` as a subtrahend here, that
is a real disagreement between the test and the implementer's reading, not a bug in either — flag it
back to the spec rather than editing this test to match.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tailorcraft.domain.export.errors import ExportFormatNotQueued
from tailorcraft.domain.export.value_objects import ExportDelivery, ExportFormat, ExportJobId
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.retention.value_objects import (
    ExpiringGuestSession,
    OrphanScanReport,
    PurgeReport,
    RetentionWindow,
    ScannedFile,
)
from tailorcraft.domain.shared.errors import InvariantViolated
from tailorcraft.domain.shared.files import FileRef

_SESSION_ID = GuestSessionId(value=UUID("11111111-1111-7111-8111-111111111111"))
_TOKEN_HASH = "a" * 64  # a SHA-256 hex digest is 64 characters; the exact value never matters here
_JOB_ID = ExportJobId(value=UUID("0192f0a1-89ab-7cde-8123-456789abcdef"))

# ADR-0011's grammar, written out independently of `domain.shared.files._KEY_GRAMMAR` (which is
# private to that module) so this assertion has a source of truth other than "the code didn't raise".
_KEY_GRAMMAR = r"^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f-]{36}\.(pdf|docx|txt)$"


# --- AC-3: RetentionWindow's refusal ------------------------------------------------------------


@pytest.mark.parametrize("hours", [0, -1], ids=["zero", "negative"])
def test_retention_window_rejects_a_non_positive_number_of_hours(hours: int) -> None:
    """Zero or negative hours is not a shorter policy, it is a rule that expires a session at or
    before the instant it was created — a configuration typo, not a valid retention window."""
    with pytest.raises(InvariantViolated):
        RetentionWindow(hours=hours)


def test_retention_window_constructs_with_a_positive_number_of_hours() -> None:
    window = RetentionWindow(hours=24)

    assert window.hours == 24


# --- AC-3 / R-18: expiry_cutoff's arithmetic and the inclusive boundary ------------------------


@pytest.mark.parametrize("hours", [1, 24, 999], ids=["1h", "24h", "999h"])
def test_expiry_cutoff_returns_the_instant_handed_to_it_regardless_of_the_windows_hours(
    hours: int,
) -> None:
    """The window is already frozen into every session's `expires_at` at `GuestSession.start`
    (ubiquitous-language contrast 1); by the time the purge runs, the comparison instant it must hand
    to the port is `now` itself, not `now` adjusted by `hours`. Three different windows over the
    identical `now` must therefore produce the identical cutoff — the arithmetic the purge leans on
    is `expires_at <= now`, and nothing here subtracts a window a second time."""
    now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)

    cutoff = RetentionWindow(hours=hours).expiry_cutoff(now)

    assert cutoff == now


def test_expiry_cutoff_selects_a_session_whose_expires_at_exactly_equals_it() -> None:
    """R-18: the boundary is inclusive. The purge's predicate is `GuestSession.is_expired`
    (`at >= expires_at`) fed the cutoff as `at` — reused here rather than reimplemented, so this test
    and `test_guest_session.py`'s own boundary tests cannot drift apart from each other."""
    now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
    cutoff = RetentionWindow(hours=24).expiry_cutoff(now)
    session = GuestSession.start(
        _SESSION_ID, _TOKEN_HASH, at=cutoff - timedelta(hours=1), ttl_hours=1
    )

    assert session.expires_at == cutoff
    assert session.is_expired(cutoff) is True


def test_expiry_cutoff_does_not_select_a_session_whose_expires_at_is_one_second_later() -> None:
    """The other half of R-18's boundary: a session that expires one second after the cutoff is not
    yet expired *as of* that cutoff."""
    now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
    cutoff = RetentionWindow(hours=24).expiry_cutoff(now)
    one_second_late_start = cutoff - timedelta(hours=1) + timedelta(seconds=1)
    session = GuestSession.start(_SESSION_ID, _TOKEN_HASH, at=one_second_late_start, ttl_hours=1)

    assert session.expires_at == cutoff + timedelta(seconds=1)
    assert session.is_expired(cutoff) is False


# --- AC-4: the five value objects' field sets, exactly, in order, with no defaults --------------


def test_expiring_guest_session_field_set_is_exactly_the_agreed_fields_with_no_defaults() -> None:
    """Three fields, and nowhere among them to put a CV, a filename or a path (AC-4, AC-16)."""
    fields = dataclasses.fields(ExpiringGuestSession)

    assert tuple(field.name for field in fields) == ("session_id", "expires_at", "files")
    for field in fields:
        assert field.default is dataclasses.MISSING
        assert field.default_factory is dataclasses.MISSING


def test_purge_report_field_set_is_exactly_the_agreed_fields_with_no_defaults() -> None:
    """Counts and a flag only. No default on any field — a default on a count is how a second
    construction site forgets one and reports a confident zero (the 1.4 lesson on `conflicts`)."""
    fields = dataclasses.fields(PurgeReport)

    assert tuple(field.name for field in fields) == (
        "examined",
        "sessions_deleted",
        "sessions_failed",
        "files_unlinked",
        "files_failed",
        "duration_ms",
        "dry_run",
    )
    for field in fields:
        assert field.default is dataclasses.MISSING
        assert field.default_factory is dataclasses.MISSING


def test_orphan_scan_report_field_set_is_exactly_the_agreed_fields_with_no_defaults() -> None:
    fields = dataclasses.fields(OrphanScanReport)

    assert tuple(field.name for field in fields) == (
        "scanned",
        "referenced",
        "too_young",
        "unrecognized",
        "reclaimed",
        "failed",
        "dry_run",
    )
    for field in fields:
        assert field.default is dataclasses.MISSING
        assert field.default_factory is dataclasses.MISSING


def test_scanned_file_field_set_is_exactly_the_agreed_fields_with_no_defaults() -> None:
    """No name field, anywhere — that absence is R-37's whole control: an unrecognised file on the
    uploads volume may be named after a person, so a string field here would make that name
    loggable. `ref` is `None` for exactly that case."""
    fields = dataclasses.fields(ScannedFile)

    assert tuple(field.name for field in fields) == ("ref", "created_at", "is_partial")
    for field in fields:
        assert field.default is dataclasses.MISSING
        assert field.default_factory is dataclasses.MISSING


# --- AC-4: every one of the five is frozen and uses __slots__ -----------------------------------
#
# `__dataclass_params__` is not in typeshed's `dataclasses` stubs, so it is read through `getattr`
# rather than as a direct attribute access — the two-argument form of `getattr` is typed `Any` in
# typeshed, which is the justified, narrow use of `Any` CLAUDE.md asks for rather than a blanket
# `# type: ignore` over the whole line.


def _is_frozen(cls: type) -> bool:
    params = getattr(cls, "__dataclass_params__", None)
    assert params is not None, f"{cls.__name__} is not a dataclass"
    frozen: bool = params.frozen
    return frozen


def test_retention_window_is_frozen_and_uses_slots() -> None:
    assert _is_frozen(RetentionWindow) is True
    assert "__slots__" in vars(RetentionWindow)


def test_expiring_guest_session_is_frozen_and_uses_slots() -> None:
    assert _is_frozen(ExpiringGuestSession) is True
    assert "__slots__" in vars(ExpiringGuestSession)


def test_purge_report_is_frozen_and_uses_slots() -> None:
    assert _is_frozen(PurgeReport) is True
    assert "__slots__" in vars(PurgeReport)


def test_orphan_scan_report_is_frozen_and_uses_slots() -> None:
    assert _is_frozen(OrphanScanReport) is True
    assert "__slots__" in vars(OrphanScanReport)


def test_scanned_file_is_frozen_and_uses_slots() -> None:
    assert _is_frozen(ScannedFile) is True
    assert "__slots__" in vars(ScannedFile)


# --- AC-9: FileRef.for_export's determinism, generalised over ExportFormat.delivery -------------
#
# `tests/unit/export/test_value_objects.py` already carries 1.5's proof: a hand-computed key for
# PDF and for DOCX (`test_for_export_matches_a_hand_computed_key_for_pdf` / `_for_docx`),
# determinism for PDF alone (`test_for_export_is_deterministic`), and refusal for both named inline
# members (`test_for_export_refuses_an_inline_format`, parametrized over MD and TXT). This file does
# not repeat any of that. What 1.6 newly depends on — because the SQL adapter derives
# `FileRef.for_export(id, format)` for *any* row whose `file_key IS NULL`, driven off
# `ExportFormat.delivery` rather than a hardcoded pair of members (technical plan §3) — is that the
# guarantee holds across every member the enum currently classifies as queued or inline, not only
# the two of each 1.5 happened to name. These tests enumerate `ExportFormat` itself rather than
# listing members, so a fifth format is covered by construction the day it is added.

_QUEUED_FORMATS = tuple(fmt for fmt in ExportFormat if fmt.delivery is ExportDelivery.QUEUED)
_INLINE_FORMATS = tuple(fmt for fmt in ExportFormat if fmt.delivery is ExportDelivery.INLINE)


@pytest.mark.parametrize("format", _QUEUED_FORMATS, ids=lambda fmt: fmt.value)
def test_for_export_key_matches_the_storage_grammar_for_every_queued_format(
    format: ExportFormat,
) -> None:
    ref = FileRef.for_export(_JOB_ID, format)

    assert re.fullmatch(_KEY_GRAMMAR, ref.key)


@pytest.mark.parametrize("format", _QUEUED_FORMATS, ids=lambda fmt: fmt.value)
def test_for_export_is_deterministic_for_every_queued_format(format: ExportFormat) -> None:
    first = FileRef.for_export(_JOB_ID, format)
    second = FileRef.for_export(_JOB_ID, format)

    assert first == second


@pytest.mark.parametrize("format", _INLINE_FORMATS, ids=lambda fmt: fmt.value)
def test_for_export_refuses_every_inline_format(format: ExportFormat) -> None:
    """The purge's derivation must never be asked to build a key for an inline format: there is no
    queued `ExportJob` row for `md`/`txt` to have a `NULL file_key` in the first place."""
    with pytest.raises(ExportFormatNotQueued):
        FileRef.for_export(_JOB_ID, format)
