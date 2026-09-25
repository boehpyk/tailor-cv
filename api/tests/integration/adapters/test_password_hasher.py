"""Adapter tests for `Argon2PasswordHasher` (`PasswordHasherPort`), written **after** (T25): the
real adapter over the real `argon2-cffi` library, on its own dedicated executor — no fakes, no
mocks (CLAUDE.md: a mocked adapter tests the mock).

Covers AC-19 (production parameters are constants, parsed out of a real PHC string; the decoy is
made with the same parameters; a test-parameter hasher's decoy carries test parameters), the
`verify` mapping (`InvalidHashError` and a garbled stored hash both -> `MISMATCH`; `None` -> the
decoy path, exactly one underlying `argon2.PasswordHasher.verify` call; `MATCH_NEEDS_REHASH` when
the stored hash's own parameters differ from the instance's), the floor (an unexpected exception
becomes exactly `PasswordHashingFailed`, chained `from None`, logging the exception's *type* and
never the password), that hashing and verifying genuinely run on the injected executor's thread
(AC-20), and the `TypeError` a caller gets for skipping the executor.

One real production-parameter hash is made exactly once (`test_production_parameters_...`) — ~50-250
ms is fine for a single call; every other test uses `TEST_ARGON2_PARAMETERS` via the constructor seam
(`conftest.py`), never an environment variable.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from argon2 import PasswordHasher as Argon2Library

from tailorcraft.domain.identity.errors import PasswordHashingFailed
from tailorcraft.domain.identity.value_objects import Password, PasswordHash, PasswordVerdict
from tailorcraft.infrastructure.identity.password_hasher import (
    PARALLELISM,
    PRODUCTION_PARAMETERS,
    TIME_COST,
    Argon2Parameters,
    Argon2PasswordHasher,
)

_CHEAP_PARAMETERS = Argon2Parameters(memory_cost=8, time_cost=1, parallelism=1)


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="argon2-adapter-test")
    try:
        yield pool
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def _phc_params(phc: str) -> str:
    """Pull the `m=...,t=...,p=...` segment out of a PHC string, so a test asserts on the parsed
    numbers rather than on the whole string's shape (which would also pass if the salt happened to
    contain the right substring by chance — vanishingly unlikely, but the parse is the honest check)."""
    match = re.search(r"\$m=(\d+),t=(\d+),p=(\d+)\$", phc)
    assert match is not None, f"not a PHC string with m=,t=,p= parameters: {phc!r}"
    return f"m={match.group(1)},t={match.group(2)},p={match.group(3)}"


# --- AC-19: production parameters are constants, and the decoy carries them too ------------------


async def test_the_default_adapter_hashes_with_exactly_the_production_parameters(
    executor: ThreadPoolExecutor,
) -> None:
    """One real production-cost hash (no `parameters=` override — the strict default), parsed for
    exactly `m=65536,t=3,p=4` and `argon2id`. Slow on purpose: this is the one test in the suite that
    proves the constant nobody may weaken with a setting."""
    hasher = Argon2PasswordHasher(executor)  # PRODUCTION_PARAMETERS, the strict default.

    phc = (await hasher.hash(Password.from_input("a throwaway registration password"))).value

    assert phc.startswith("$argon2id$")
    assert _phc_params(phc) == "m=65536,t=3,p=4"
    assert PRODUCTION_PARAMETERS.time_cost == TIME_COST == 3
    assert PRODUCTION_PARAMETERS.parallelism == PARALLELISM == 4


async def test_the_decoys_parameters_equal_the_instances_live_parameters(
    executor: ThreadPoolExecutor,
) -> None:
    """The production-default hasher's decoy is made with the same live parameters (AC-19) — a
    cheaper decoy would reopen the timing channel it exists to close."""
    hasher = Argon2PasswordHasher(executor)

    assert _phc_params(hasher.decoy_hash.value) == "m=65536,t=3,p=4"


async def test_a_test_parameter_hasher_produces_a_decoy_with_test_parameters(
    executor: ThreadPoolExecutor,
) -> None:
    """The same guarantee holds at cheap parameters too — the decoy always mirrors `self._parameters`,
    whatever they are, not a hard-coded production value."""
    hasher = Argon2PasswordHasher(executor, parameters=_CHEAP_PARAMETERS)

    assert _phc_params(hasher.decoy_hash.value) == "m=8,t=1,p=1"
    assert hasher.parameters == _CHEAP_PARAMETERS


# --- `verify`'s mapping -----------------------------------------------------------------------------


async def test_invalid_hash_error_from_a_corrupt_stored_hash_is_a_mismatch(
    executor: ThreadPoolExecutor,
) -> None:
    """A stored hash argon2 cannot parse at all (`InvalidHashError`) must not become a 500 or lock the
    loop — the corruption sweep's rule one level over: the only safe answer is MISMATCH."""
    hasher = Argon2PasswordHasher(executor, parameters=_CHEAP_PARAMETERS)
    garbled = PasswordHash("$argon2id$not-a-real-phc-string-at-all$$$")

    verdict = await hasher.verify(Password.from_input("whatever the visitor typed"), garbled)

    assert verdict is PasswordVerdict.MISMATCH


async def test_a_garbled_argon2_string_that_looks_shaped_is_still_a_mismatch(
    executor: ThreadPoolExecutor,
) -> None:
    """A second, differently-garbled stored value — parses as *shaped* like PHC but not like argon2's
    own grammar — hits the same `(InvalidHashError, VerificationError)` branch and the same MISMATCH,
    never an exception escaping to the router."""
    hasher = Argon2PasswordHasher(executor, parameters=_CHEAP_PARAMETERS)
    garbled = PasswordHash("$argon2id$v=19$m=8,t=1,p=1$####$####")

    verdict = await hasher.verify(Password.from_input("whatever the visitor typed"), garbled)

    assert verdict is PasswordVerdict.MISMATCH


async def test_verify_against_none_is_a_mismatch_after_exactly_one_underlying_verify(
    monkeypatch: pytest.MonkeyPatch, executor: ThreadPoolExecutor
) -> None:
    """The decoy path (I-9): `against=None` must cost one real argon2 verify — no more, no fewer — and
    must always answer MISMATCH, regardless of what the underlying library decides."""
    hasher = Argon2PasswordHasher(executor, parameters=_CHEAP_PARAMETERS)
    calls: list[tuple[str, bytes]] = []
    real_verify = Argon2Library.verify

    def counting_verify(self: Argon2Library, stored: str, secret: bytes) -> None:
        calls.append((stored, secret))
        real_verify(self, stored, secret)

    # `argon2.PasswordHasher` is a slotted class, so its *instance* attributes are read-only —
    # patch the class instead, which is safe here because every affected instance's decoy was
    # already computed in `__init__`, before this patch is installed.
    monkeypatch.setattr(Argon2Library, "verify", counting_verify)

    verdict = await hasher.verify(Password.from_input("nobody has this password"), None)

    assert verdict is PasswordVerdict.MISMATCH
    assert len(calls) == 1
    assert calls[0][0] == hasher.decoy_hash.value


async def test_a_hash_with_older_parameters_needs_a_rehash_on_match(
    executor: ThreadPoolExecutor,
) -> None:
    """AC-9's `MATCH_NEEDS_REHASH`: the stored hash matches, but its own PHC parameters are not
    today's — made here by hashing with cheaper parameters than the instance verifying it uses."""
    stale_hasher = Argon2PasswordHasher(
        executor, parameters=Argon2Parameters(memory_cost=8, time_cost=1, parallelism=1)
    )
    current_hasher = Argon2PasswordHasher(
        executor, parameters=Argon2Parameters(memory_cost=8, time_cost=2, parallelism=1)
    )
    password = Password.from_input("a perfectly good password, just hashed a while ago")
    stale_hash = await stale_hasher.hash(password)

    verdict = await current_hasher.verify(password, stale_hash)

    assert verdict is PasswordVerdict.MATCH_NEEDS_REHASH


async def test_a_matching_hash_with_current_parameters_needs_no_rehash(
    executor: ThreadPoolExecutor,
) -> None:
    """The contrast case for the test above: identical parameters, so `check_needs_rehash` is False
    and the verdict is a plain MATCH — proving the rehash verdict is about the parameters, not merely
    about matching."""
    hasher = Argon2PasswordHasher(executor, parameters=_CHEAP_PARAMETERS)
    password = Password.from_input("a perfectly good and perfectly current password")
    current_hash = await hasher.hash(password)

    verdict = await hasher.verify(password, current_hash)

    assert verdict is PasswordVerdict.MATCH


# --- The floor: `except Exception` -> `PasswordHashingFailed`, `from None`, type-only log ----------


async def test_an_unexpected_exception_during_hash_becomes_exactly_password_hashing_failed(
    monkeypatch: pytest.MonkeyPatch,
    executor: ThreadPoolExecutor,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_fragment = "SECRET-PASSWORD-must-never-reach-a-log-line-77qz"
    hasher = Argon2PasswordHasher(executor, parameters=_CHEAP_PARAMETERS)

    def boom(self: Argon2Library, secret: bytes) -> str:
        raise ValueError(secret_fragment)

    monkeypatch.setattr(Argon2Library, "hash", boom)

    with caplog.at_level(logging.INFO), pytest.raises(PasswordHashingFailed) as exc_info:
        await hasher.hash(Password.from_input(secret_fragment))

    # The floor's promise: `raise ... from None`, so the frame holding the password is unreachable.
    assert type(exc_info.value) is PasswordHashingFailed
    assert exc_info.value.__cause__ is None, "raise ... from None must leave __cause__ unset"
    assert exc_info.value.__suppress_context__ is True

    # The privacy half: the exception's fully-qualified TYPE is loggable; its MESSAGE — which here
    # carries the whole password — must never travel anywhere near a log line.
    assert secret_fragment not in caplog.text
    assert "ValueError" in caplog.text, "the exception's fully-qualified type should be logged"


async def test_an_unexpected_exception_during_verify_becomes_exactly_password_hashing_failed(
    monkeypatch: pytest.MonkeyPatch,
    executor: ThreadPoolExecutor,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hasher = Argon2PasswordHasher(executor, parameters=_CHEAP_PARAMETERS)
    stored = await hasher.hash(Password.from_input("whatever was stored"))

    def boom(self: Argon2Library, stored: str, secret: bytes) -> None:
        raise RuntimeError("argon2-cffi exploded")

    monkeypatch.setattr(Argon2Library, "verify", boom)

    with caplog.at_level(logging.INFO), pytest.raises(PasswordHashingFailed) as exc_info:
        await hasher.verify(Password.from_input("a guess"), stored)

    assert type(exc_info.value) is PasswordHashingFailed
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True
    assert "argon2-cffi exploded" not in caplog.text
    assert "RuntimeError" in caplog.text


# --- AC-20: hash and verify genuinely run off the loop, on the injected executor -------------------


async def test_hash_runs_on_the_injected_executors_thread(
    monkeypatch: pytest.MonkeyPatch, executor: ThreadPoolExecutor
) -> None:
    hasher = Argon2PasswordHasher(executor, parameters=_CHEAP_PARAMETERS)
    observed_thread_names: list[str] = []
    real_hash = Argon2Library.hash

    def recording_hash(self: Argon2Library, secret: bytes) -> str:
        observed_thread_names.append(threading.current_thread().name)
        return real_hash(self, secret)

    monkeypatch.setattr(Argon2Library, "hash", recording_hash)

    await hasher.hash(Password.from_input("does this run on the right thread"))

    assert observed_thread_names, "the hash never ran"
    assert observed_thread_names[0].startswith("argon2-adapter-test")
    assert observed_thread_names[0] != threading.current_thread().name


async def test_verify_runs_on_the_injected_executors_thread(
    monkeypatch: pytest.MonkeyPatch, executor: ThreadPoolExecutor
) -> None:
    hasher = Argon2PasswordHasher(executor, parameters=_CHEAP_PARAMETERS)
    stored = await hasher.hash(Password.from_input("a stored password"))
    observed_thread_names: list[str] = []
    real_verify = Argon2Library.verify

    def recording_verify(self: Argon2Library, stored_phc: str, secret: bytes) -> None:
        observed_thread_names.append(threading.current_thread().name)
        real_verify(self, stored_phc, secret)

    monkeypatch.setattr(Argon2Library, "verify", recording_verify)

    await hasher.verify(Password.from_input("a stored password"), stored)

    assert observed_thread_names, "the verify never ran"
    assert observed_thread_names[0].startswith("argon2-adapter-test")
    assert observed_thread_names[0] != threading.current_thread().name


# --- The constructor's runtime refusal ---------------------------------------------------------------


def test_constructing_without_a_dedicated_executor_raises_type_error() -> None:
    """`loop.run_in_executor(None, ...)` is legal and means the loop's own default executor — exactly
    the shared, memory-unbounded pool ADR-0021 exists to avoid. `None` must fail loudly at
    construction, not quietly hash on the wrong pool forever."""
    with pytest.raises(TypeError):
        Argon2PasswordHasher(None)  # type: ignore[arg-type]


def test_constructing_with_something_that_is_not_a_thread_pool_executor_raises_type_error() -> None:
    with pytest.raises(TypeError):
        Argon2PasswordHasher("not-an-executor")  # type: ignore[arg-type]
