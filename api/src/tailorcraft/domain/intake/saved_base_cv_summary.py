"""`SavedBaseCvSummary` — the read side of the saved-CV list, and nothing else (slice 2.2, T13b).

**Why a read model exists at all.** `GET /api/me/base-cvs` pins `character_count` (= the length of
the extracted text) on every entry, and AC-52 says `list_for_user` never selects `extracted_text`.
A list of `BaseCv` aggregates cannot satisfy both: the count lives only in the text, and the text is
exactly what the list must not load — up to tens of kilobytes of someone's employment history per
row, one `repr()` away from a log line. The owner's resolution (technical plan §3, amendment
2026-09-25) is to stop pretending the list is a collection of aggregates. The adapter computes the
count in SQL (`char_length`, code points, equal to Python's `len`) and hands back this.

**It is deliberately not a `BaseCv`, and not a base class of one.** It carries no text, no
`FileRef`, no owner and no behaviour; it cannot be renamed, deleted, copied or tailored. Rename,
delete and copy still load the aggregate through `get`, because they change it and its invariants
must hold. Only the list is a read side. A caller that needs the text of a listed CV must `get` it,
which is the point.

The field types are the aggregate's own value objects rather than primitives, so the list says the
same thing the aggregate does in the same language, and the API layer maps both with one vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    BaseCvLabel,
    BaseCvStatus,
    CvContentType,
    ExtractionFailureReason,
    OriginalFilename,
)


@dataclass(frozen=True, slots=True)
class SavedBaseCvSummary:
    """One entry of a registered user's saved-CV list, as `list_for_user` returns it.

    `character_count` is `None` exactly when there is no extracted text (the CV is not `EXTRACTED`);
    `failure_reason` is `None` unless it is `EXTRACTION_FAILED`. Those mirror the aggregate's I-2,
    but they are **not re-checked here**: this is a projection of rows the aggregate already
    validated on the way in, not a second place those rules live.
    """

    id: BaseCvId
    label: BaseCvLabel | None
    original_filename: OriginalFilename
    content_type: CvContentType
    size_bytes: int
    status: BaseCvStatus
    character_count: int | None
    failure_reason: ExtractionFailureReason | None
    uploaded_at: datetime
