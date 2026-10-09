"""AC-3 (slice 3.3, T6 proof): every 429 a router raises names `Retry-After` and `rate_limited`.

Discovery, not a list: an AST walk of `infrastructure/api/routers/*.py` finds every call whose
status argument is `status.HTTP_429_TOO_MANY_REQUESTS` (the `responses={...}` dict keys are not
calls and are ignored), so a new limiter site is checked the day it is written. The client holds
a refused request for `Retry-After` seconds; a 429 without it is a hold nobody can size.

Mutation (recorded 2026-10-09): deleting `headers={"Retry-After": str(retry_after)},` from
`routers/_upload.py` turned this red:
    AssertionError: 429 sites missing Retry-After or `rate_limited`:
    ['_upload.py:97']
Source restored byte-exact (`git diff` empty).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROUTERS = Path(__file__).parents[2] / "src/tailorcraft/infrastructure/api/routers"
MINIMUM_SITES = 9  # today's number; fewer means the scan stopped seeing something


def _is_429(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "HTTP_429_TOO_MANY_REQUESTS"


def _names_retry_after(call: ast.Call) -> bool:
    headers = next((k.value for k in call.keywords if k.arg == "headers"), None)
    return isinstance(headers, ast.Dict) and any(
        isinstance(k, ast.Constant) and k.value == "Retry-After" for k in headers.keys
    )


def _has_rate_limited_code(call: ast.Call) -> bool:
    return any(
        isinstance(n, ast.Constant) and n.value == "rate_limited"
        for k in call.keywords
        if k.arg == "detail"
        for n in ast.walk(k.value)
    )


def _sites() -> list[tuple[str, ast.Call]]:
    found = []
    for path in sorted(ROUTERS.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and (
                any(_is_429(a) for a in node.args[:1])
                or any(k.arg == "status_code" and _is_429(k.value) for k in node.keywords)
            ):
                found.append((f"{path.name}:{node.lineno}", node))
    return found


def test_every_429_raised_by_a_router_carries_retry_after_and_rate_limited() -> None:
    sites = _sites()
    print(f"429 sites found: {len(sites)}")
    assert len(sites) >= MINIMUM_SITES
    bad = [
        where
        for where, call in sites
        if not (_names_retry_after(call) and _has_rate_limited_code(call))
    ]
    assert not bad, f"429 sites missing Retry-After or `rate_limited`: {bad}"
