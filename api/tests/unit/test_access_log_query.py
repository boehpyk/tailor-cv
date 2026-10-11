"""uvicorn's access-log line carries no query string (slice 4.2, T26 finding; owner decision).

An after-test of the adapter-level filter, written by its implementer (`api-dev`), not `qa`.
Mutation-proven (2026-10-11): `install_access_log_query_filter()` removed from `configure_logging`
-> the app-setup test (the `?q=` line kept) and the install-once test (`assert 0 == 1`) go red; the
`record.args` rewrite removed from the filter -> the filter test and the app-setup test go red.
"""

from __future__ import annotations

import logging

import pytest

from tailorcraft.infrastructure.observability import StripAccessLogQuery, configure_logging
from tailorcraft.infrastructure.settings import Settings

# uvicorn 0.52's own format and argument shape (`protocols/http/h11_impl.py`, `httptools_impl.py`).
_UVICORN_ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'
_MARKER = "t26-user-k7q2xm"


def _record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        _UVICORN_ACCESS_FORMAT,
        ("172.18.0.5:51234", "GET", path, "1.1", 200),
        None,
    )


def test_the_filter_drops_the_query_and_keeps_the_rest_of_the_line() -> None:
    record = _record(f"/api/admin/users?q={_MARKER}&limit=20")

    assert StripAccessLogQuery().filter(record) is True
    assert record.getMessage() == '172.18.0.5:51234 - "GET /api/admin/users HTTP/1.1" 200'


def test_a_path_without_a_query_and_a_record_of_another_shape_pass_unchanged() -> None:
    plain = _record("/api/auth/me")
    other = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "x %s", ("?q=1",), None)

    assert StripAccessLogQuery().filter(plain)
    assert StripAccessLogQuery().filter(other)
    assert plain.getMessage() == '172.18.0.5:51234 - "GET /api/auth/me HTTP/1.1" 200'
    assert other.getMessage() == "x ?q=1"


def test_the_apps_logging_setup_strips_the_query_from_a_uvicorn_access_record(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    configure_logging(settings)  # what `create_app` runs in every uvicorn process
    access = logging.getLogger("uvicorn.access")

    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        access.info(
            _UVICORN_ACCESS_FORMAT,
            "172.18.0.5:51234",
            "GET",
            f"/api/admin/users?q={_MARKER}",
            "1.1",
            200,
        )

    lines = [r.getMessage() for r in caplog.records if r.name == "uvicorn.access"]
    assert lines == ['172.18.0.5:51234 - "GET /api/admin/users HTTP/1.1" 200']  # positive control
    assert _MARKER not in caplog.text
    assert "?q=" not in caplog.text


def test_installing_twice_adds_one_filter(settings: Settings) -> None:
    configure_logging(settings)
    configure_logging(settings)

    filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(f, StripAccessLogQuery) for f in filters) == 1
