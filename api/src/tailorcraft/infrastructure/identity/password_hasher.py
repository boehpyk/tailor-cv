"""`PasswordHasherPort` over argon2id — **the only module in the codebase that imports `argon2`**.

Four decisions, each from ADR-0021, each written at the line that enforces it:

1. **The parameters are module constants, not settings** (AC-19). `m=65536 KiB, t=3, p=4` is RFC
   9106's low-memory profile. A setting would be a knob that weakens a control, and the codebase's
   rule is that a control gets no knob (ADR-0012). The constructor's `parameters` argument is the
   *testing seam*, with a strict default — the same shape as the egress's address policy: nothing
   in production passes it, and no environment variable reaches it.
2. **Every hash and every verify runs on a dedicated, bounded `ThreadPoolExecutor`** handed in by the
   application's lifespan — never on the event loop, and never on the loop's *default* executor.
   argon2 at these parameters is ~50 ms of CPU and 64 MiB of memory per call; on the loop that stalls
   every concurrent request and logs nothing (I-42). The default executor is shared with CV
   extraction and the posting parser, so a login burst there would starve an upload, and it is
   unbounded against memory. **The executor's size is the memory cap**: 2 workers x 64 MiB per
   process. argon2-cffi calls into C through cffi, which releases the GIL, so two hashes genuinely
   run in parallel.
3. **The decoy** (AC-9, AC-28). `verify(password, None)` means "no such account", and it must cost
   exactly what a wrong password costs, or a stopwatch answers "is this email registered?". So a
   decoy hash is made once, here, with the live parameters, and the unknown-email path verifies
   against it and returns `MISMATCH` whatever the outcome.
4. **An `except Exception` floor** (CLAUDE.md: a port that promises to translate every failure needs a
   catch-all, not an allow-list). Anything the library raises that the specific mappings below do not
   name becomes `PasswordHashingFailed`, `from None`, with a log line carrying the exception's *type*
   and nothing else: the frame holds the password, and a library message can quote its input.
   `Exception`, never `BaseException`, so `asyncio.CancelledError` still cancels.
"""

from __future__ import annotations

import asyncio
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from tailorcraft.domain.identity.errors import PasswordHashingFailed
from tailorcraft.domain.identity.ports import PasswordHasherPort
from tailorcraft.domain.identity.value_objects import Password, PasswordHash, PasswordVerdict

log = structlog.get_logger(__name__)

# AC-19's numbers, as argon2-cffi names them. They equal argon2-cffi's own defaults today and are
# passed explicitly anyway: a library default is a value somebody else can change in a minor release,
# and "our parameters" should be a fact readable in this file, not an inference from a changelog.
MEMORY_COST_KIB: Final = 65536
TIME_COST: Final = 3
PARALLELISM: Final = 4
HASH_LEN: Final = 32
SALT_LEN: Final = 16

_ENCODING: Final = "utf-8"


@dataclass(frozen=True, slots=True)
class Argon2Parameters:
    """One argon2id cost profile. `PRODUCTION_PARAMETERS` is the only one production ever builds.

    Exists as a type so the testing seam is one argument rather than five, and so a test that wants
    cheap parameters has to *name* them — `Argon2Parameters(memory_cost=8, …)` in a fixture is
    visible in review in a way that an environment variable would not be.
    """

    memory_cost: int
    time_cost: int
    parallelism: int
    hash_len: int = HASH_LEN
    salt_len: int = SALT_LEN


PRODUCTION_PARAMETERS: Final = Argon2Parameters(
    memory_cost=MEMORY_COST_KIB,
    time_cost=TIME_COST,
    parallelism=PARALLELISM,
    hash_len=HASH_LEN,
    salt_len=SALT_LEN,
)


class Argon2PasswordHasher:
    """`PasswordHasherPort` over argon2id, run on a dedicated executor (ADR-0021).

    Constructed once per process, in `main.py`'s lifespan, and kept on `app.state` beside the engine
    — the decoy is computed here and must not be recomputed per request.
    """

    def __init__(
        self,
        executor: ThreadPoolExecutor,
        parameters: Argon2Parameters = PRODUCTION_PARAMETERS,
    ) -> None:
        # A runtime refusal on top of the annotation, because the failure it prevents is silent:
        # `loop.run_in_executor(None, …)` is legal and means the loop's *default* executor, which is
        # exactly the shared, memory-unbounded pool decision 2 exists to avoid. A caller that passes
        # `None` (or forgets the lifespan wiring and passes something else) must fail at startup, not
        # quietly hash on the wrong pool for ever.
        if not isinstance(executor, ThreadPoolExecutor):
            raise TypeError("Argon2PasswordHasher needs a dedicated ThreadPoolExecutor")
        self._executor = executor
        self._parameters = parameters
        self._hasher = PasswordHasher(
            time_cost=parameters.time_cost,
            memory_cost=parameters.memory_cost,
            parallelism=parameters.parallelism,
            hash_len=parameters.hash_len,
            salt_len=parameters.salt_len,
            encoding=_ENCODING,
            type=Type.ID,
        )
        # The decoy (decision 3). Built from a random throwaway password nobody knows, with the very
        # `PasswordHasher` the live path uses, so its cost is the live cost by construction and its
        # PHC string carries the live parameters (asserted by AC-19). Computed synchronously because
        # `__init__` runs once, in the lifespan, before the application accepts a request — one hash
        # on the loop at startup stalls nobody.
        self._decoy = self._hasher.hash(secrets.token_urlsafe(32))

    @property
    def parameters(self) -> Argon2Parameters:
        """The cost profile this instance hashes and verifies with."""
        return self._parameters

    @property
    def decoy_hash(self) -> PasswordHash:
        """The decoy's PHC string, exposed so AC-19 can assert it was made with `parameters` —
        a decoy with cheaper parameters would reopen the timing channel it exists to close."""
        return PasswordHash(self._decoy)

    async def hash(self, password: Password) -> PasswordHash:
        """Hash `password` under today's parameters, off the loop. Raises `PasswordHashingFailed`."""
        loop = asyncio.get_running_loop()
        try:
            phc = await loop.run_in_executor(
                self._executor, self._hasher.hash, password.value.encode(_ENCODING)
            )
            return PasswordHash(phc)
        except Exception as exc:
            raise _hashing_failed("hash", exc) from None

    async def verify(self, password: Password, against: PasswordHash | None) -> PasswordVerdict:
        """Compare `password` with `against`, off the loop.

        `against=None` verifies against the decoy **and returns `MISMATCH` whatever that verify
        says** — do not "optimise" the `None` case into an early return (the port's docstring: a
        reviewer who sees one is looking at the bug).
        """
        loop = asyncio.get_running_loop()
        stored = self._decoy if against is None else against.value
        try:
            verdict = await loop.run_in_executor(
                self._executor, self._verify_sync, stored, password.value.encode(_ENCODING)
            )
        except Exception as exc:
            raise _hashing_failed("verify", exc) from None
        return PasswordVerdict.MISMATCH if against is None else verdict

    def _verify_sync(self, stored: str, secret: bytes) -> PasswordVerdict:
        """The whole comparison, in the executor's thread — including `check_needs_rehash`, which is
        string parsing but belongs with the verify it qualifies rather than a second hop."""
        try:
            self._hasher.verify(stored, secret)
        except VerifyMismatchError:
            return PasswordVerdict.MISMATCH
        except (InvalidHashError, VerificationError):
            # A stored hash we cannot read — not a PHC string argon2 understands (`InvalidHashError`),
            # or one whose fields do not decode (`VerificationError` with "Decoding failed", measured
            # against argon2-cffi 25.1). Either is a corrupt or foreign row, not a server fault, and
            # the only safe answer is "this password does not match it": a 503 would say something
            # about this account that a wrong password does not, and an exception would make a
            # corrupt row a way to turn a login into a 500. Ordered after `VerifyMismatchError`,
            # which is a `VerificationError` subclass. Such a row verifies faster than a real one;
            # that is a fact about a corrupt row, never about whether the email exists.
            return PasswordVerdict.MISMATCH
        if self._hasher.check_needs_rehash(stored):
            return PasswordVerdict.MATCH_NEEDS_REHASH
        return PasswordVerdict.MATCH


def _hashing_failed(operation: str, exc: Exception) -> PasswordHashingFailed:
    """The floor's one log line and its translation (decision 4). The exception's *type* only:
    never its message, never `exc_info` — the caller re-raises `from None` so the frame holding the
    password is unreachable from a Sentry report."""
    log.error(
        "identity.password_hasher_failed",
        operation=operation,
        error_type=type(exc).__name__,
    )
    return PasswordHashingFailed()


if TYPE_CHECKING:
    # Makes mypy prove the class structurally satisfies the port. Never executed.
    def _assert_implements_password_hasher_port(hasher: Argon2PasswordHasher) -> None:
        _: PasswordHasherPort = hasher
