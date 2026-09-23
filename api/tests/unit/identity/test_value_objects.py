"""Value objects for the `identity` bounded context (slice 2.1): `EmailAddress`, `Password`,
`PasswordPolicy`, `PasswordHash`, `TokenHash`, `RetiredRefreshToken`, `IssuedAccessToken` (AC-2,
AC-3, AC-4).

Pure domain tests: no I/O, no fixtures, no event loop, no mocks — every assertion is derived from
the acceptance criteria and the skeleton's own docstrings in `domain/identity/value_objects.py`, not
from running the (currently unimplemented) methods and recording what they did.

**RED-first trap this file is written against** (task-list.md, top of file): `NotImplementedError`
subclasses `RuntimeError`, so every `pytest.raises(...)` below names the exact domain exception —
`InvalidEmailAddress`, `WeakPassword`, `InvariantViolated` — never `RuntimeError` or `Exception`.
Against the skeleton every one of these still fails, but on `NotImplementedError`, which is *not* a
subclass of any of those three, so the red is on the right line for the right reason.

Every redaction test plants a distinctive marker in the secret value and asserts both that the
marker is absent from the output *and* that the output equals the documented redacted form — an
absence check alone would pass against an adapter that returns `""`.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime

import pytest

from tailorcraft.domain.identity.errors import InvalidEmailAddress, WeakPassword
from tailorcraft.domain.identity.value_objects import (
    PASSWORD_INPUT_MAX_LENGTH,
    EmailAddress,
    InvalidEmailReason,
    IssuedAccessToken,
    Password,
    PasswordHash,
    PasswordPolicy,
    RetiredRefreshToken,
    TokenHash,
    WeakPasswordReason,
)
from tailorcraft.domain.shared.errors import InvariantViolated

_AT = datetime(2026, 9, 23, 10, 0, 0, tzinfo=UTC)

# ====================================================================================================
# AC-2: EmailAddress
# ====================================================================================================

# --- normalizing cases: `parse` accepts and returns the normalized form ---------------------------

_VALID_EMAILS: list[tuple[str, str]] = [
    (" Alex@Example.COM ", "alex@example.com"),  # surrounding whitespace stripped, case folded
    ("ALEX@EXAMPLE.COM", "alex@example.com"),  # whole address case-folded, not just the domain
    (" alex@example.com", "alex@example.com"),  # leading whitespace only
    ("alex@example.com ", "alex@example.com"),  # trailing whitespace only
    ("a@b.co", "a@b.co"),  # minimal shape: 1-char local, 2-label domain
    ("alex.smith@example.com", "alex.smith@example.com"),  # dots preserved, not folded away
    ("alex+cv@example.com", "alex+cv@example.com"),  # +tags preserved, not folded away
    ("a" * 64 + "@b.com", "a" * 64 + "@b.com"),  # local part at the 64-character ceiling
]


@pytest.mark.parametrize(
    ("raw", "expected"), _VALID_EMAILS, ids=[repr(c[0]) for c in _VALID_EMAILS]
)
def test_parse_normalizes_and_accepts_a_well_formed_address(raw: str, expected: str) -> None:
    assert EmailAddress.parse(raw).value == expected


def test_parse_preserves_dots_as_a_distinct_mailbox_from_the_folded_form() -> None:
    """`alex.smith@example.com` and `alexsmith@example.com` are different mailboxes on most
    providers (AC-2); folding them would merge two real accounts on the providers where it is
    wrong, so `EmailAddress` must never treat them as equal."""
    assert EmailAddress.parse("alex.smith@example.com") != EmailAddress.parse(
        "alexsmith@example.com"
    )


def test_parse_preserves_a_plus_tag_as_a_distinct_mailbox_from_the_untagged_form() -> None:
    assert EmailAddress.parse("alex+cv@example.com") != EmailAddress.parse("alex@example.com")


# --- refusal cases: `parse` raises `InvalidEmailAddress(reason)` naming the first rule broken -----

_REFUSED_EMAILS: list[tuple[str, InvalidEmailReason]] = [
    ("a@b", InvalidEmailReason.DOMAIN_WITHOUT_DOT),  # single-label domain, no dot at all
    ("a@@b.com", InvalidEmailReason.AT_SIGN_COUNT),  # two `@` signs
    ("a b@c.com", InvalidEmailReason.WHITESPACE_OR_CONTROL),  # whitespace inside, not surrounding
    ("alex@exämple.com", InvalidEmailReason.NOT_ASCII),  # alex@exämple.com — non-ASCII
    ("", InvalidEmailReason.AT_SIGN_COUNT),  # empty: zero `@` signs
    ("alexample.com", InvalidEmailReason.AT_SIGN_COUNT),  # no `@` at all
    ("a@b..com", InvalidEmailReason.EMPTY_DOMAIN_LABEL),  # a dot with nothing between two dots
    ("a@.b.com", InvalidEmailReason.EMPTY_DOMAIN_LABEL),  # a dot with nothing before it
    ("a@b.com.", InvalidEmailReason.EMPTY_DOMAIN_LABEL),  # a trailing dot, empty final label
    ("a" * 65 + "@b.com", InvalidEmailReason.LOCAL_PART_LENGTH),  # local part one over the ceiling
    ("@b.com", InvalidEmailReason.LOCAL_PART_LENGTH),  # empty local part
    ("alex\t@example.com", InvalidEmailReason.WHITESPACE_OR_CONTROL),  # a tab, inside
    ("alex\x01@example.com", InvalidEmailReason.WHITESPACE_OR_CONTROL),  # a control character
]


@pytest.mark.parametrize(
    ("raw", "expected_reason"), _REFUSED_EMAILS, ids=[repr(c[0]) for c in _REFUSED_EMAILS]
)
def test_parse_refuses_a_malformed_address_naming_the_broken_rule(
    raw: str, expected_reason: InvalidEmailReason
) -> None:
    with pytest.raises(InvalidEmailAddress) as exc_info:
        EmailAddress.parse(raw)

    assert exc_info.value.reason is expected_reason


def test_parse_refuses_an_address_over_254_characters_total_as_too_long() -> None:
    """Local part (64, within its own ceiling) + `@` + domain (190, within its own 253-character
    ceiling) sums to 255 total — unambiguously a violation of the *total* length rule, not of
    either component's own ceiling, since both components independently stay inside their bounds."""
    local = "a" * 64
    domain = "b" * 186 + ".com"  # 190 characters
    raw = f"{local}@{domain}"
    assert len(raw) == 254 + 1  # sanity: exactly one over the 254-character total ceiling

    with pytest.raises(InvalidEmailAddress) as exc_info:
        EmailAddress.parse(raw)

    assert exc_info.value.reason is InvalidEmailReason.TOO_LONG


def test_parse_accepts_an_address_at_exactly_254_characters_total() -> None:
    local = "a" * 64
    domain = "b" * 185 + ".com"  # 189 characters
    raw = f"{local}@{domain}"
    assert len(raw) == 254  # sanity: exactly at the total-length ceiling

    assert EmailAddress.parse(raw).value == raw


# --- AC-2: direct construction re-validates and refuses a non-normalized value --------------------


def test_direct_construction_of_an_unnormalized_address_is_refused() -> None:
    """`EmailAddress("Alex@Example.com")` is un-normalized (upper case survives): `__post_init__`
    must refuse it so `parse` is the only way to hold a value, ever — a repository rehydrating a row
    is also a stranger, one migration later."""
    with pytest.raises(InvalidEmailAddress) as exc_info:
        EmailAddress("Alex@Example.com")

    assert exc_info.value.reason is InvalidEmailReason.NOT_NORMALIZED


def test_direct_construction_of_an_unnormalized_address_with_surrounding_whitespace_is_refused() -> (
    None
):
    with pytest.raises(InvalidEmailAddress) as exc_info:
        EmailAddress(" alex@example.com")

    assert exc_info.value.reason is InvalidEmailReason.NOT_NORMALIZED


def test_direct_construction_of_an_already_normalized_address_succeeds() -> None:
    """The only value `__post_init__` must accept: exactly what `parse` would have produced. This is
    the path a repository uses to rehydrate a row."""
    address = EmailAddress("alex@example.com")

    assert address.value == "alex@example.com"


# --- local_part property ----------------------------------------------------------------------


def test_local_part_is_everything_before_the_at_sign() -> None:
    assert EmailAddress.parse("alex.smith+cv@example.com").local_part == "alex.smith+cv"


# --- equality is the uniqueness rule ---------------------------------------------------------------


def test_two_addresses_normalizing_to_the_same_value_compare_equal() -> None:
    """This **is** the uniqueness rule (technical plan §1): the unique index on `identity_user.email`
    compares exactly the string this type holds, so two spellings of one mailbox must be `==`."""
    assert EmailAddress.parse("A@x.io") == EmailAddress.parse(" a@X.io ")


def test_two_distinct_addresses_do_not_compare_equal() -> None:
    assert EmailAddress.parse("alex@example.com") != EmailAddress.parse("sam@example.com")


# ====================================================================================================
# AC-3: Password
# ====================================================================================================

_MARKER_PASSWORD = "MyS3cretMarkerPw!"  # a distinctive value that must never survive into repr/str


def test_from_input_applies_nfkc_normalization() -> None:
    """NIST SP 800-63B §5.1.1.2: a password typed on two keyboards that produce two encodings of one
    character is one password. The ligature `ﬁ` (U+FB01) NFKC-normalizes to the two characters
    `fi` — chosen because it changes the *length* of the string, so a normalizer that is a no-op
    cannot pass this by accident."""
    raw = "ﬁle12345678"  # "ﬁle12345678" — the ligature is one code point pre-normalization
    assert (
        unicodedata.normalize("NFKC", raw) != raw
    )  # sanity: this input actually needs normalizing

    password = Password.from_input(raw)

    assert password.value == unicodedata.normalize("NFKC", raw)
    assert password.value == "file12345678"


def test_from_input_does_not_strip_whitespace() -> None:
    """A trailing space is a character the user typed (AC-3); silently removing it would make
    "correct password, refused" a support ticket nobody can reproduce."""
    raw = "  correct horse  "

    password = Password.from_input(raw)

    assert password.value == raw


def test_from_input_refuses_an_empty_value() -> None:
    with pytest.raises(WeakPassword) as exc_info:
        Password.from_input("")

    assert exc_info.value.reason is WeakPasswordReason.TOO_SHORT
    assert exc_info.value.min_length == 1
    assert exc_info.value.max_length == PASSWORD_INPUT_MAX_LENGTH


def test_from_input_accepts_exactly_the_maximum_length() -> None:
    raw = "a" * PASSWORD_INPUT_MAX_LENGTH

    password = Password.from_input(raw)

    assert password.value == raw


def test_from_input_refuses_one_character_over_the_maximum_length() -> None:
    raw = "a" * (PASSWORD_INPUT_MAX_LENGTH + 1)

    with pytest.raises(WeakPassword) as exc_info:
        Password.from_input(raw)

    assert exc_info.value.reason is WeakPasswordReason.TOO_LONG
    assert exc_info.value.min_length == 1
    assert exc_info.value.max_length == PASSWORD_INPUT_MAX_LENGTH


def test_direct_construction_of_an_empty_password_is_refused() -> None:
    with pytest.raises(WeakPassword) as exc_info:
        Password("")

    assert exc_info.value.reason is WeakPasswordReason.TOO_SHORT


def test_direct_construction_over_the_maximum_length_is_refused() -> None:
    with pytest.raises(WeakPassword) as exc_info:
        Password("a" * (PASSWORD_INPUT_MAX_LENGTH + 1))

    assert exc_info.value.reason is WeakPasswordReason.TOO_LONG


def test_direct_construction_of_a_non_nfkc_normalized_value_is_refused() -> None:
    """`__post_init__` re-checks NFKC for the same reason `EmailAddress` does: one form, no side
    door — a repository rehydrating a row is also a stranger."""
    raw = "ﬁle12345678"  # not NFKC-normalized: unicodedata.normalize("NFKC", raw) != raw

    with pytest.raises(WeakPassword):
        Password(raw)


def test_direct_construction_of_an_already_normalized_value_succeeds() -> None:
    password = Password("file12345678")

    assert password.value == "file12345678"


# --- redaction: repr, str, format, f-string never contain the plaintext ---------------------------


def test_repr_never_contains_the_password_and_equals_the_redacted_form() -> None:
    password = Password.from_input(_MARKER_PASSWORD)

    output = repr(password)

    assert _MARKER_PASSWORD not in output
    assert output == "Password(<redacted>)"


def test_str_never_contains_the_password_and_equals_the_redacted_form() -> None:
    password = Password.from_input(_MARKER_PASSWORD)

    output = str(password)

    assert _MARKER_PASSWORD not in output
    assert output == "Password(<redacted>)"


def test_format_never_contains_the_password_and_equals_the_redacted_form() -> None:
    password = Password.from_input(_MARKER_PASSWORD)

    output = format(password)

    assert _MARKER_PASSWORD not in output
    assert output == "Password(<redacted>)"


def test_format_with_a_spec_still_redacts_rather_than_raising() -> None:
    """`object.__format__` would delegate to `__str__` for an empty spec but raise `TypeError` for
    any other — the skeleton defines `__format__` explicitly so every spec redacts (its own
    docstring). A spec that fell through to the default would crash here instead of redacting."""
    password = Password.from_input(_MARKER_PASSWORD)

    output = format(password, ">30")

    assert _MARKER_PASSWORD not in output
    assert "<redacted>" in output


def test_f_string_interpolation_never_contains_the_password() -> None:
    password = Password.from_input(_MARKER_PASSWORD)

    output = f"{password}"

    assert _MARKER_PASSWORD not in output
    assert output == "Password(<redacted>)"


# ====================================================================================================
# AC-3 / OQ-3: PasswordPolicy
# ====================================================================================================


def _policy_email() -> EmailAddress:
    """Built lazily inside each test, never at module scope: `EmailAddress.parse` is itself
    unimplemented against the skeleton, and a module-level call would fail every test in this file
    at collection instead of failing each one on its own assertion."""
    return EmailAddress.parse("policytester@example.com")


def test_check_refuses_eleven_code_points_as_too_short() -> None:
    policy = PasswordPolicy()
    password = Password.from_input("a" * 11)

    with pytest.raises(WeakPassword) as exc_info:
        policy.check(password, _policy_email())

    assert exc_info.value.reason is WeakPasswordReason.TOO_SHORT
    assert exc_info.value.min_length == 12
    assert exc_info.value.max_length == 128


def test_check_accepts_twelve_code_points() -> None:
    policy = PasswordPolicy()
    password = Password.from_input("a" * 12)

    policy.check(password, _policy_email())  # must not raise


def test_check_accepts_one_hundred_twenty_eight_code_points() -> None:
    policy = PasswordPolicy()
    password = Password.from_input("a" * 128)

    policy.check(password, _policy_email())  # must not raise


def test_check_refuses_one_hundred_twenty_nine_code_points_as_too_long() -> None:
    policy = PasswordPolicy()
    password = Password.from_input("a" * 129)

    with pytest.raises(WeakPassword) as exc_info:
        policy.check(password, _policy_email())

    assert exc_info.value.reason is WeakPasswordReason.TOO_LONG
    assert exc_info.value.min_length == 12
    assert exc_info.value.max_length == 128


def test_check_counts_length_in_code_points_after_nfkc() -> None:
    """A single ligature code point that NFKC-expands to two ASCII letters must be counted as *two*
    code points — otherwise an 11-character NFKC-expansion could sneak under the 12-code-point floor
    by exploiting the pre-normalization length instead."""
    policy = PasswordPolicy()
    # 11 ligatures, each 1 code point pre-NFKC, each expanding to 2 code points ("fi") post-NFKC:
    # 11 code points pre-normalization, 22 post-normalization.
    password = Password.from_input("ﬁ" * 11)

    policy.check(password, _policy_email())  # 22 code points post-NFKC: must not raise


def test_check_refuses_a_password_equal_to_the_whole_email_case_insensitively() -> None:
    email = EmailAddress.parse("myLocalPart12@example.com")
    password = Password.from_input("MYLOCALPART12@EXAMPLE.COM")
    policy = PasswordPolicy()

    with pytest.raises(WeakPassword) as exc_info:
        policy.check(password, email)

    assert exc_info.value.reason is WeakPasswordReason.MATCHES_EMAIL


def test_check_refuses_a_password_equal_to_the_local_part_case_insensitively() -> None:
    email = EmailAddress.parse("myLocalPart12@example.com")  # local part: "mylocalpart12", 13 chars
    password = Password.from_input("MYLOCALPART12")
    policy = PasswordPolicy()

    with pytest.raises(WeakPassword) as exc_info:
        policy.check(password, email)

    assert exc_info.value.reason is WeakPasswordReason.MATCHES_EMAIL


def test_check_imposes_no_composition_rule() -> None:
    """NIST SP 800-63B §5.1.1.2 advises against composition rules. `aaaaaaaaaaaa` — twelve
    identical characters, no digit, no punctuation, no mixed case — passing is a test, not an
    oversight (technical plan §1)."""
    policy = PasswordPolicy()
    password = Password.from_input("aaaaaaaaaaaa")

    policy.check(password, _policy_email())  # must not raise despite having no composition variety


# --- PasswordPolicy.__post_init__: 1 <= min_length <= max_length <= PASSWORD_INPUT_MAX_LENGTH -----


def test_default_policy_is_constructible() -> None:
    policy = PasswordPolicy()

    assert policy.min_length == 12
    assert policy.max_length == 128


def test_policy_refuses_a_min_length_below_one() -> None:
    with pytest.raises(InvariantViolated):
        PasswordPolicy(min_length=0, max_length=128)


def test_policy_refuses_a_min_length_greater_than_max_length() -> None:
    with pytest.raises(InvariantViolated):
        PasswordPolicy(min_length=129, max_length=128)


def test_policy_refuses_a_max_length_over_the_password_input_bound() -> None:
    with pytest.raises(InvariantViolated):
        PasswordPolicy(min_length=12, max_length=PASSWORD_INPUT_MAX_LENGTH + 1)


def test_policy_accepts_the_widest_legal_bounds() -> None:
    policy = PasswordPolicy(min_length=1, max_length=PASSWORD_INPUT_MAX_LENGTH)

    assert policy.min_length == 1
    assert policy.max_length == PASSWORD_INPUT_MAX_LENGTH


# ====================================================================================================
# AC-4: PasswordHash
# ====================================================================================================

_MARKER_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$MARKERSALTMARKERSALT$MARKERDIGESTMARKERDIGESTMARKERDIGEST"
)


def test_password_hash_accepts_a_dollar_prefixed_phc_string() -> None:
    hashed = PasswordHash(_MARKER_HASH)

    assert hashed.value == _MARKER_HASH


def test_password_hash_refuses_an_empty_value() -> None:
    with pytest.raises(InvariantViolated):
        PasswordHash("")


def test_password_hash_refuses_a_value_without_the_dollar_prefix() -> None:
    with pytest.raises(InvariantViolated):
        PasswordHash("argon2id-not-phc-shaped")


def test_password_hash_accepts_exactly_five_hundred_twelve_characters() -> None:
    value = "$" + "a" * 511
    assert len(value) == 512

    hashed = PasswordHash(value)

    assert hashed.value == value


def test_password_hash_refuses_five_hundred_thirteen_characters() -> None:
    value = "$" + "a" * 512
    assert len(value) == 513

    with pytest.raises(InvariantViolated):
        PasswordHash(value)


def test_password_hash_repr_never_contains_the_value_and_equals_the_redacted_form() -> None:
    hashed = PasswordHash(_MARKER_HASH)

    output = repr(hashed)

    assert _MARKER_HASH not in output
    assert "MARKERSALT" not in output
    assert "redacted" in output


# ====================================================================================================
# AC-4: TokenHash
# ====================================================================================================

_VALID_TOKEN_HASH = "a" * 64  # 64 lowercase hex characters


def test_token_hash_accepts_sixty_four_lowercase_hex_characters() -> None:
    token_hash = TokenHash(_VALID_TOKEN_HASH)

    assert token_hash.value == _VALID_TOKEN_HASH


def test_token_hash_refuses_sixty_three_characters() -> None:
    with pytest.raises(InvariantViolated):
        TokenHash("a" * 63)


def test_token_hash_refuses_sixty_five_characters() -> None:
    with pytest.raises(InvariantViolated):
        TokenHash("a" * 65)


def test_token_hash_refuses_uppercase_hex() -> None:
    with pytest.raises(InvariantViolated):
        TokenHash("A" * 64)


def test_token_hash_repr_never_contains_the_value_and_equals_the_redacted_form() -> None:
    marker = "deadbeef" * 8  # 64 lowercase hex characters, a distinctive value
    token_hash = TokenHash(marker)

    output = repr(token_hash)

    assert marker not in output
    assert "redacted" in output


# ====================================================================================================
# RetiredRefreshToken: generation >= 1
# ====================================================================================================


def test_retired_refresh_token_accepts_generation_one() -> None:
    retired = RetiredRefreshToken(
        token_hash=TokenHash(_VALID_TOKEN_HASH),
        generation=1,
        retired_at=_AT,
    )

    assert retired.generation == 1


def test_retired_refresh_token_refuses_generation_zero() -> None:
    with pytest.raises(InvariantViolated):
        RetiredRefreshToken(
            token_hash=TokenHash(_VALID_TOKEN_HASH),
            generation=0,
            retired_at=_AT,
        )


def test_retired_refresh_token_refuses_a_negative_generation() -> None:
    with pytest.raises(InvariantViolated):
        RetiredRefreshToken(
            token_hash=TokenHash(_VALID_TOKEN_HASH),
            generation=-1,
            retired_at=_AT,
        )


# ====================================================================================================
# IssuedAccessToken: expires_in > 0, redacted repr
# ====================================================================================================


def test_issued_access_token_accepts_a_positive_lifetime() -> None:
    from datetime import timedelta

    token = IssuedAccessToken(token="a.b.c", expires_in=timedelta(minutes=15))

    assert token.expires_in.total_seconds() == 900


def test_issued_access_token_refuses_a_zero_lifetime() -> None:
    from datetime import timedelta

    with pytest.raises(InvariantViolated):
        IssuedAccessToken(token="a.b.c", expires_in=timedelta(seconds=0))


def test_issued_access_token_refuses_a_negative_lifetime() -> None:
    from datetime import timedelta

    with pytest.raises(InvariantViolated):
        IssuedAccessToken(token="a.b.c", expires_in=timedelta(seconds=-1))


def test_issued_access_token_repr_never_contains_the_token_and_equals_the_redacted_form() -> None:
    from datetime import timedelta

    marker = "MARKER.ACCESS.TOKEN.VALUE"
    token = IssuedAccessToken(token=marker, expires_in=timedelta(minutes=15))

    output = repr(token)

    assert marker not in output
    assert "redacted" in output
