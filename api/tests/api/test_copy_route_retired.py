"""AC-33 (slice 2.4, T26 RED): the copy route is retired, and the readers it left behind are kept.

2.2's `POST /api/base-cvs/copies` put a working copy of a saved CV into a guest workspace. 2.3 made
the signed-in workspace a first-class one and 2.4's claim is the only transfer route left, so the
copy has no caller (OQ-2). This file asserts the *absence* and pairs it with discriminating
positives, because an absence assertion is satisfied by any app that is broken in some other way:

- the 404 is paired with `GET /api/base-cvs` still answering and still naming `origin`;
- the removed names are looked up with `importlib`/`hasattr`, so the red is an assertion and not an
  `ImportError` at collection, and each lookup is paired with a name that must stay importable.

`origin` and `copied_from` stay (2.4 OQ-2): rows made by 2.2's copy still exist on the box until the
purge takes them, and the claim reads `copied_from_base_cv_id` to drop them. The seed below sets
that column in SQL rather than through `BaseCv.copy_from`, which this slice deletes.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

COPIES_URL = "/api/base-cvs/copies"
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cvs"

# (module, attribute) for every name the retirement removes, and for the siblings that must stay.
_REMOVED_MODULE_LEVEL = [
    ("tailorcraft.application.intake.copy_saved_base_cv", "CopySavedBaseCvToWorkspace"),
    ("tailorcraft.domain.intake.events", "BaseCvCopied"),
    ("tailorcraft.domain.intake.errors", "SavedBaseCvNotCopyable"),
    ("tailorcraft.domain.intake.errors", "SavedBaseCvFileMissing"),
    ("tailorcraft.infrastructure.api.schemas.intake", "CopySavedBaseCvRequest"),
]
_KEPT_MODULE_LEVEL = [
    ("tailorcraft.application.intake.upload_base_cv", "UploadBaseCv"),
    ("tailorcraft.domain.intake.events", "BaseCvDeleted"),
    ("tailorcraft.domain.intake.errors", "TooManySavedBaseCvs"),
    ("tailorcraft.domain.intake.value_objects", "BaseCvOrigin"),
]


def _lookup(module_name: str, attribute: str) -> bool:
    """Whether `module_name.attribute` resolves. A missing module counts as 'does not resolve'."""
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        return False
    return hasattr(module, attribute)


@pytest.mark.parametrize(("module_name", "attribute"), _REMOVED_MODULE_LEVEL)
def test_ac33_the_removed_name_no_longer_resolves(module_name: str, attribute: str) -> None:
    assert not _lookup(module_name, attribute), (
        f"{module_name}.{attribute} still exists — AC-33 retires the copy route's creation path"
    )


@pytest.mark.parametrize(("module_name", "attribute"), _KEPT_MODULE_LEVEL)
def test_ac33_control_the_names_beside_the_removed_ones_still_resolve(
    module_name: str, attribute: str
) -> None:
    """The discriminating positive for the test above: the lookup can say 'present'."""
    assert _lookup(module_name, attribute), f"{module_name}.{attribute} must stay (OQ-2)"


def test_ac33_base_cv_no_longer_has_copy_from_but_keeps_its_readers() -> None:
    from tailorcraft.domain.intake.base_cv import BaseCv

    assert not hasattr(BaseCv, "copy_from"), "BaseCv.copy_from must be removed (AC-33)"
    assert hasattr(BaseCv, "copied_from"), "`copied_from` is a reader the claim keeps (OQ-2)"
    assert hasattr(BaseCv, "origin"), "`origin` is a reader the wire keeps (OQ-2)"


async def test_ac33_post_base_cvs_copies_is_404_for_a_bearer_with_a_well_formed_body(
    client: AsyncClient,
) -> None:
    response = await client.post(
        COPIES_URL,
        json={"saved_base_cv_id": str(uuid4())},
        headers={"Authorization": "Bearer not-a-real-token"},
    )

    assert response.status_code == 404, (response.status_code, response.text)


async def test_ac33_post_base_cvs_copies_is_404_for_an_anonymous_caller_and_mints_no_session(
    client: AsyncClient,
) -> None:
    response = await client.post(COPIES_URL, json={"saved_base_cv_id": str(uuid4())})

    assert response.status_code == 404, (response.status_code, response.text)
    assert "set-cookie" not in response.headers, "a route that does not exist mints nothing"


async def test_ac33_get_base_cvs_still_serves_origin_for_a_row_that_has_a_copied_from(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The positive: a working copy that already exists (2.2 made them) is still listed with
    `origin: "copied_from_saved"`, and an uploaded one beside it with `"uploaded"`. Passes today and
    must keep passing after T27."""
    sample = (_FIXTURES / "sample.txt").read_bytes()
    uploaded = await client.post(
        "/api/base-cvs", files={"file": ("sample.txt", sample, "text/plain")}
    )
    assert uploaded.status_code == 201, uploaded.text
    plain_id = uploaded.json()["id"]
    second = await client.post(
        "/api/base-cvs", files={"file": ("second.txt", sample, "text/plain")}
    )
    assert second.status_code == 201, second.text
    copy_id = second.json()["id"]

    # `copied_from_base_cv_id` carries no FK (ADR-0022): any uuid is a legal provenance.
    await session.execute(
        sql_text("UPDATE intake_base_cv SET copied_from_base_cv_id = :src WHERE id = :id"),
        {"src": uuid4(), "id": UUID(copy_id)},
    )
    await session.commit()

    listing = await client.get("/api/base-cvs")

    assert listing.status_code == 200, listing.text
    origins = {row["id"]: row["origin"] for row in listing.json()["items"]}
    assert origins == {plain_id: "uploaded", copy_id: "copied_from_saved"}, origins
