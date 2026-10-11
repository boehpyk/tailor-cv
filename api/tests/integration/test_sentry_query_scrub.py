"""Commit 9984003: no query string reaches Sentry, in an error event or a transaction.

Drives the REAL sentry_sdk through `configure_sentry` (fake DSN, so production's init options apply),
a capturing transport swapped onto the live client, and a real ASGI request whose query carries a
planted marker. The route also makes an outgoing httpx call (a mock transport, no network) whose
URL carries a marker query, then raises: that puts a breadcrumb on the error event and a span on the
transaction. Sampling is forced to 1.0 after init via `client.options["traces_sample_rate"]` (the
sampler reads the options dict per transaction); the fixture restores the hub/client afterwards.

Positive controls: an error event AND a transaction were captured, the path is present, and the
outgoing URL's path is present, so "marker absent" cannot pass on an empty capture.

Mutation (recorded 2026-10-11): removing `before_send=` / `before_send_transaction=` from
`configure_sentry` turns this red: `AssertionError: assert 'query_string' not in {'method': 'GET',
..., 'query_string': 'q=reqmarker-9f3a', 'url': 'http://t/probe'}` (the planted marker is in the
captured error event); source restored byte-exact (`git diff --stat -- api/src` empty).
Second mutation (span/breadcrumb/trace branch of `strip_query_strings` deleted, request scrubbing
kept): red on `assert 'outmarker-71bc' not in '{"level":"e...'` with the payload showing
`query":"q=outmarker-71bc","http.fragment":""` (the httpx breadcrumb's `http.query`); restored.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
import sentry_sdk
from fastapi import FastAPI
from sentry_sdk.transport import Transport

from tailorcraft.infrastructure.observability import configure_sentry
from tailorcraft.infrastructure.settings import Settings

REQUEST_MARKER = "reqmarker-9f3a"
OUTGOING_MARKER = "outmarker-71bc"


class _Capture(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[tuple[str, str]] = []

    def capture_envelope(self, envelope) -> None:  # type: ignore[no-untyped-def]
        for item in envelope.items:
            self.items.append((str(item.type), item.payload.get_bytes().decode()))


@pytest.fixture
def captured(settings: Settings) -> Iterator[_Capture]:
    previous = sentry_sdk.get_global_scope().client
    configure_sentry(settings.model_copy(update={"sentry_dsn": "https://k@o0.ingest.invalid/1"}))
    client = sentry_sdk.get_client()
    transport = _Capture()
    client.transport = transport
    client.options["traces_sample_rate"] = 1.0
    try:
        yield transport
    finally:
        client.close()
        sentry_sdk.get_global_scope().set_client(previous)


async def test_query_strings_reach_neither_error_events_nor_transactions(
    captured: _Capture,
) -> None:
    app = FastAPI()  # built after init, so the SDK's Starlette integration wraps it

    @app.get("/probe")
    async def probe() -> None:
        upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200)), trust_env=False
        )
        async with upstream:
            await upstream.get(f"http://upstream.test/fetched?q={OUTGOING_MARKER}")
        raise RuntimeError("boom")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
    ) as client:
        assert (await client.get(f"/probe?q={REQUEST_MARKER}")).status_code == 500
    sentry_sdk.get_client().flush()

    kinds = {kind for kind, _ in captured.items}
    assert {"event", "transaction"} <= kinds, kinds
    blob = "\n".join(payload for _, payload in captured.items)
    assert "/probe" in blob
    assert "/fetched" in blob  # the outgoing call was recorded (crumb / span), minus its query
    for item in (json.loads(p) for k, p in captured.items if k in ("event", "transaction")):
        assert "query_string" not in item.get("request", {})
    assert REQUEST_MARKER not in blob
    assert OUTGOING_MARKER not in blob
