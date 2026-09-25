"""Adapter tests for `JwtAccessTokens` (`AccessTokenPort`), written **after** (T25): the real adapter
over the real PyJWT library — hand-built tokens throughout, no mocks.

Covers I-32...I-40 (every refusal `AccessTokenPort.verify` must produce, built as an adversary would
build them — a wrong algorithm, a foreign key, a missing or extra claim, the wrong `typ`, a non-string
`aud`, a boolean `iat`, an uppercase `sub`, a refresh token presented as a bearer) and AC-21 (the
issued token's claim set is exactly `{iss, aud, sub, iat, exp}`, `typ: at+jwt`, `alg: HS256`, and
carries no email and no login id).

Every hand-built token in this file is constructed at the wire level (PyJWT's own `encode`, or raw
base64url segments for the one case PyJWT will not encode through its normal path) rather than
through the adapter's own `issue` — the adapter's `verify` is what is under test, and building the
tokens independently is what keeps this file from being able to agree with a regression in `issue`.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import pytest

from tailorcraft.domain.identity.errors import AccessTokenInvalid
from tailorcraft.domain.identity.value_objects import AccessTokenRefusal, UserId
from tailorcraft.infrastructure.identity.access_tokens import (
    ALGORITHM,
    AUDIENCE,
    ISSUER,
    TOKEN_TYPE,
    JwtAccessTokens,
)

SIGNING_KEY = "a-test-signing-key-at-least-32-bytes-long-xx"
FOREIGN_KEY = "a-completely-different-signing-key-of-its-own"
AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
USER_ID = UserId(UUID("aabbccdd-eeff-1234-5678-90abcdef1234"))
"""Deliberately carries hex letters (`a`-`f`) — a UUID of only digits would make `.upper()` a no-op
and the uppercase-`sub` test below vacuous."""


def _claims(**overrides: object) -> dict[str, object]:
    now = int(AT.timestamp())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(USER_ID.value),
        "iat": now,
        "exp": now + 900,
    }
    claims.update(overrides)
    return claims


def _encode(
    claims: dict[str, object],
    *,
    key: str = SIGNING_KEY,
    algorithm: str = ALGORITHM,
    typ: str | None = TOKEN_TYPE,
) -> str:
    headers = {"typ": typ} if typ is not None else None
    return jwt.encode(claims, key, algorithm=algorithm, headers=headers)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _hand_built_alg_none_token(claims: dict[str, object]) -> str:
    """Built at the wire level — base64url header/payload, empty signature segment — rather than
    through `jwt.encode(..., algorithm="none")`, so this test does not depend on PyJWT's own opinion
    of whether encoding `alg: none` is allowed."""
    header = {"alg": "none", "typ": TOKEN_TYPE}
    return f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}."


def _hand_built_rs256_token(claims: dict[str, object]) -> str:
    """`RS256` with a garbage signature: PyJWT rejects the pinned-`HS256` `algorithms=` allow-list
    before it ever computes or checks a signature (measured against PyJWT 2.15), so no real RSA key
    pair is needed to prove the algorithm is refused."""
    header = {"alg": "RS256", "typ": TOKEN_TYPE}
    signature = _b64url(b"not-a-real-signature")
    return (
        f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}.{signature}"
    )


@pytest.fixture
def adapter() -> JwtAccessTokens:
    return JwtAccessTokens(SIGNING_KEY, timedelta(minutes=15))


def _refuses(
    adapter: JwtAccessTokens, token: str, reason: AccessTokenRefusal, at: datetime = AT
) -> None:
    with pytest.raises(AccessTokenInvalid) as exc_info:
        adapter.verify(token, at)
    assert exc_info.value.reason is reason


# --- I-32...I-40: every refusal, hand-built --------------------------------------------------------


def test_alg_none_built_by_hand_is_refused_as_bad_algorithm(adapter: JwtAccessTokens) -> None:
    token = _hand_built_alg_none_token(_claims())
    _refuses(adapter, token, AccessTokenRefusal.BAD_ALGORITHM)


def test_hs512_signed_with_the_right_key_is_refused_as_bad_algorithm(
    adapter: JwtAccessTokens,
) -> None:
    """The algorithm allow-list is pinned to exactly `HS256` — a *stronger* algorithm signed with the
    correct key is still refused, because the pin is about which algorithm this API speaks, not about
    cryptographic strength."""
    token = _encode(_claims(), algorithm="HS512")
    _refuses(adapter, token, AccessTokenRefusal.BAD_ALGORITHM)


def test_rs256_built_by_hand_is_refused_as_bad_algorithm_before_any_signature_check(
    adapter: JwtAccessTokens,
) -> None:
    token = _hand_built_rs256_token(_claims())
    _refuses(adapter, token, AccessTokenRefusal.BAD_ALGORITHM)


def test_a_token_signed_with_a_foreign_key_is_refused_as_bad_signature(
    adapter: JwtAccessTokens,
) -> None:
    token = _encode(_claims(), key=FOREIGN_KEY)
    _refuses(adapter, token, AccessTokenRefusal.BAD_SIGNATURE)


@pytest.mark.parametrize("missing_claim", ["iss", "aud", "sub", "iat", "exp"])
def test_a_token_missing_any_required_claim_is_refused_as_bad_claims(
    adapter: JwtAccessTokens, missing_claim: str
) -> None:
    claims = _claims()
    del claims[missing_claim]
    token = _encode(claims)
    _refuses(adapter, token, AccessTokenRefusal.BAD_CLAIMS)


def test_a_token_with_no_typ_header_at_all_is_refused_as_bad_claims(
    adapter: JwtAccessTokens,
) -> None:
    """PyJWT never looks at `typ` itself — this adapter does (RFC 9068)."""
    token = _encode(_claims(), typ=None)
    _refuses(adapter, token, AccessTokenRefusal.BAD_CLAIMS)


def test_a_token_with_the_wrong_typ_header_is_refused_as_bad_claims(
    adapter: JwtAccessTokens,
) -> None:
    token = _encode(_claims(), typ="JWT")
    _refuses(adapter, token, AccessTokenRefusal.BAD_CLAIMS)


def test_a_token_with_an_extra_claim_is_refused_as_bad_claims(adapter: JwtAccessTokens) -> None:
    """The claim set must be **exactly** the five keys (AC-21) — PyJWT's `require` only proves
    presence, so a sixth claim sails through PyJWT's own decode and must be caught here."""
    token = _encode(_claims(login_id="some-login-id"))
    _refuses(adapter, token, AccessTokenRefusal.BAD_CLAIMS)


def test_a_list_audience_containing_the_right_value_is_refused_as_bad_claims(
    adapter: JwtAccessTokens,
) -> None:
    """PyJWT accepts a *list* `aud` that merely contains the expected audience (measured) — this API
    never issues one, and `aud` must be exactly our string."""
    token = _encode(_claims(aud=[AUDIENCE, "some-other-audience"]))
    _refuses(adapter, token, AccessTokenRefusal.BAD_CLAIMS)


def test_a_boolean_iat_is_refused_as_bad_claims(adapter: JwtAccessTokens) -> None:
    """With PyJWT's own time checks off, it no longer type-checks `iat`/`exp` either (measured:
    `"iat": true` decodes cleanly) — `bool` is an `int` subclass in Python, so the adapter's own
    `_is_int` must reject it explicitly."""
    token = _encode(_claims(iat=True))
    _refuses(adapter, token, AccessTokenRefusal.BAD_CLAIMS)


def test_an_uppercase_sub_is_refused_as_bad_claims(adapter: JwtAccessTokens) -> None:
    """`UUID()` itself accepts upper case and normalizes it; a token whose `sub` is not already the
    canonical lower-case form was not issued by this adapter, and is refused rather than normalised."""
    token = _encode(_claims(sub=str(USER_ID.value).upper()))
    _refuses(adapter, token, AccessTokenRefusal.BAD_CLAIMS)


def test_exp_equal_to_now_is_refused_as_expired(adapter: JwtAccessTokens) -> None:
    """No leeway on `exp`: `exp == now` is already expired (AC-21)."""
    now = int(AT.timestamp())
    token = _encode(_claims(exp=now))
    _refuses(adapter, token, AccessTokenRefusal.EXPIRED)


def test_exp_one_second_after_now_is_accepted(adapter: JwtAccessTokens) -> None:
    now = int(AT.timestamp())
    token = _encode(_claims(exp=now + 1))

    user_id = adapter.verify(token, AT)

    assert user_id == USER_ID


def test_iat_thirty_seconds_in_the_future_is_accepted(adapter: JwtAccessTokens) -> None:
    now = int(AT.timestamp())
    token = _encode(_claims(iat=now + 30))

    user_id = adapter.verify(token, AT)

    assert user_id == USER_ID


def test_iat_thirty_one_seconds_in_the_future_is_refused_as_issued_in_future(
    adapter: JwtAccessTokens,
) -> None:
    now = int(AT.timestamp())
    token = _encode(_claims(iat=now + 31))
    _refuses(adapter, token, AccessTokenRefusal.ISSUED_IN_FUTURE)


def test_a_refresh_token_string_presented_as_a_bearer_is_refused_as_malformed(
    adapter: JwtAccessTokens,
) -> None:
    """I-40: a 43-character `secrets.token_urlsafe(32)` value is not shaped like a JWT at all (no
    dot-separated segments) — PyJWT's own `DecodeError` ("Not enough segments") floors to MALFORMED,
    never attempting a lookup with it."""
    refresh_shaped_token = "x" * 43
    _refuses(adapter, refresh_shaped_token, AccessTokenRefusal.MALFORMED)


def test_junk_bytes_are_refused_as_malformed(adapter: JwtAccessTokens) -> None:
    _refuses(adapter, "not a token at all", AccessTokenRefusal.MALFORMED)


def test_the_empty_string_is_refused_as_malformed(adapter: JwtAccessTokens) -> None:
    _refuses(adapter, "", AccessTokenRefusal.MALFORMED)


def test_two_segments_only_is_refused_as_malformed(adapter: JwtAccessTokens) -> None:
    header = _b64url(json.dumps({"alg": "HS256", "typ": TOKEN_TYPE}).encode())
    payload = _b64url(json.dumps(_claims()).encode())
    _refuses(adapter, f"{header}.{payload}", AccessTokenRefusal.MALFORMED)


def test_unparsable_base64_is_refused_as_malformed(adapter: JwtAccessTokens) -> None:
    _refuses(adapter, "not-base64!!.not-base64!!.not-base64!!", AccessTokenRefusal.MALFORMED)


def test_a_header_segment_that_is_not_json_is_refused_as_malformed(
    adapter: JwtAccessTokens,
) -> None:
    header = _b64url(b"this is not json at all")
    payload = _b64url(json.dumps(_claims()).encode())
    signature = _b64url(b"whatever")
    _refuses(adapter, f"{header}.{payload}.{signature}", AccessTokenRefusal.MALFORMED)


# --- AC-21: the issued token's exact shape ----------------------------------------------------------


def test_issue_then_verify_round_trips_to_the_same_user(adapter: JwtAccessTokens) -> None:
    issued = adapter.issue(USER_ID, AT)

    user_id = adapter.verify(issued.token, AT + timedelta(minutes=1))

    assert user_id == USER_ID


def test_the_issued_tokens_header_and_claim_set_are_exactly_ac21s_shape(
    adapter: JwtAccessTokens,
) -> None:
    issued = adapter.issue(USER_ID, AT)

    header = jwt.get_unverified_header(issued.token)
    payload = jwt.decode(issued.token, options={"verify_signature": False})

    assert header["alg"] == "HS256"
    assert header["typ"] == "at+jwt"
    assert set(payload) == {"iss", "aud", "sub", "iat", "exp"}
    assert payload["iss"] == "tailorcraft"
    assert payload["aud"] == "tailorcraft-api"
    assert payload["sub"] == str(USER_ID.value)


def test_the_issued_token_carries_no_email_and_no_login_id(adapter: JwtAccessTokens) -> None:
    """A JWT is base64, not encryption, and sits in devtools — no claim beyond the five may exist,
    and in particular nothing that looks like an email address or a login id."""
    issued = adapter.issue(USER_ID, AT)
    payload = jwt.decode(issued.token, options={"verify_signature": False})

    assert "email" not in payload
    assert "login_id" not in payload
    assert "@" not in json.dumps(payload)
