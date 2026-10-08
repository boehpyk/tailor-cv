"""AC-27 (slice 3.2, T20, test-after) — the OpenAPI document publishes `layout_template` as the enum
of the three layout ids, in order, on the request and on the response schema.

The ids are written out here, not read from `LayoutTemplate`: the contract is the wire value.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

IDS = ["classic", "modern", "formal"]


def _accepted_values(app: FastAPI, schema: str) -> tuple[list[str], bool]:
    """The enum ids a property accepts and whether `null` is among them, resolving `$ref`s."""
    doc = app.openapi()
    schemas = doc["components"]["schemas"]
    prop: dict[str, Any] = schemas[schema]["properties"]["layout_template"]
    ids: list[str] = []
    nullable = False
    for branch in prop.get("anyOf", [prop]):
        if branch.get("type") == "null":
            nullable = True
        elif "$ref" in branch:
            ids = schemas[branch["$ref"].rsplit("/", 1)[-1]]["enum"]
        else:
            ids = branch["enum"]
    return ids, nullable


def test_the_create_request_publishes_the_three_ids_and_is_optional(app: FastAPI) -> None:
    ids, nullable = _accepted_values(app, "CreateExportRequest")

    assert ids == IDS
    assert nullable, "omitted or null is Classic for a PDF, so the field accepts null"
    assert "layout_template" not in app.openapi()["components"]["schemas"][
        "CreateExportRequest"
    ].get("required", []), "the field is optional"


def test_the_job_response_publishes_the_three_ids_and_is_nullable(app: FastAPI) -> None:
    ids, nullable = _accepted_values(app, "ExportJobResponse")

    assert ids == IDS
    assert nullable, "a DOCX job has no layout"
