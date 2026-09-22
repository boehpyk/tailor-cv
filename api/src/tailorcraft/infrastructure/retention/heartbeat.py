"""The guest purge's heartbeat: one Redis hash saying when the job last *completed*, and how it went.

Key `retention:guest_purge:last_run`, five fields: `at` (RFC 3339, whole-second), `outcome`
(`ok` | `failed`), `sessions_deleted`, `files_unlinked`, `duration_ms`. `/health/ready` reads it and
publishes `last_run`, `last_run_age_seconds`, `last_outcome` and `stale` from it.

**There is no TTL on the key, and the omission is the design.** An expiring heartbeat would
manufacture the exact `last_run: null` the runbook tells an operator to investigate — *"the job has
never run, or Redis was flushed; both are worth knowing"* (R-26). A key that quietly disappears three
hours after a perfectly healthy run turns "the job is fine and idle" into "the job has never run",
which is the one reading of that field an operator is told to act on. A heartbeat that expires is a
false alarm generator with a cron of its own.

**The heartbeat is not the signal to trust, and this module should not be read as if it were**
(ADR-0018 decision 4). A run row, a log line and this hash can all be written by a job that is not
actually working; the *backlog* — `count_expired`, the same predicate the purge selects on — cannot
be faked by one. The heartbeat answers "when did it last finish", which is a different and weaker
question. That is also why a failed write here does not fail the run: see `record`.

Nothing in here can carry PII. The five fields are an instant, a word and three counts; there is no
session id, no key, no filename and no path, so no probe, log line or Sentry frame downstream can
leak one from this hash (Constitution §8).
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, cast

import redis.asyncio as aioredis
import structlog

log = structlog.get_logger(__name__)

# The one key, written once. A heartbeat written under one name and read under another is a job that
# reports "never run" for ever while working perfectly.
HEARTBEAT_KEY: Final = "retention:guest_purge:last_run"

_FIELD_AT: Final = "at"
_FIELD_OUTCOME: Final = "outcome"
_FIELD_SESSIONS_DELETED: Final = "sessions_deleted"
_FIELD_FILES_UNLINKED: Final = "files_unlinked"
_FIELD_DURATION_MS: Final = "duration_ms"

PurgeOutcome = Literal["ok", "failed"]
"""How a completed run ended. `failed` is still a *completed* run in this module's sense — the job
ran, something went wrong, and that is a fact worth recording. A run that never started writes
nothing at all, which is the `last_run: null` case."""


@dataclass(frozen=True, slots=True)
class PurgeHeartbeat:
    """One heartbeat, read back.

    **Every field but `at` is optional, and that is how this type degrades rather than a shrug at
    validation.** The hash is written by one caller in one place, so a partial or malformed one means
    somebody edited Redis by hand, a write was interrupted between fields, or a future version
    changed the shape — and in all three cases the health probe's job is to report what it can, not
    to return 500 because a count would not parse. An unparseable `sessions_deleted` becomes `None`
    and `last_run` is still published.

    `at` is the exception because it is the field every derived answer rests on: `last_run`,
    `last_run_age_seconds` and `stale` are all computed from it. A heartbeat with no usable `at` can
    answer none of them, so `read` returns `None` for it — see that method for why that reading is
    the safe one.
    """

    at: datetime
    outcome: PurgeOutcome | None
    sessions_deleted: int | None
    files_unlinked: int | None
    duration_ms: int | None


class RedisPurgeHeartbeat:
    """Reads and writes the purge's heartbeat hash.

    Constructed with a client rather than a URL: the entry points and the health probe each already
    have one, and a second client per call would open a connection pool per purge tick.
    """

    def __init__(self, redis: aioredis.Redis, *, key: str = HEARTBEAT_KEY) -> None:
        self._redis = redis
        self._key = key

    async def record(
        self,
        *,
        at: datetime,
        outcome: PurgeOutcome,
        sessions_deleted: int,
        files_unlinked: int,
        duration_ms: int,
    ) -> None:
        """Write the heartbeat for a run that has just finished. **Never raises.**

        Called on **every completed run, including the ones that deleted nothing** (AC-21, R-14): a
        job that does nothing and records nothing is indistinguishable from a job that never ran, and
        "nothing was overdue" is the healthiest outcome this job has. It is **never** called by a dry
        run (R-13) — a dry run deletes nothing, so recording it as a run would tell the operator the
        purge is current when no data has moved. That branch belongs to the entry point: this method
        has no `dry_run` parameter to get wrong.

        **A write failure is logged and swallowed** (R-25), and the direction is the same one
        ADR-0018 decision 6 argues for the lock: put a mechanism's failure on the side whose loss is
        recoverable. By the time this runs the rows are deleted and the files are unlinked — the
        irreversible part is done and it succeeded. Failing the run over a bookkeeping write would
        mark a successful purge as failed, and on the Celery path it would hand Sentry an error for
        work that completed. What the operator loses is one stale `last_run`, and the runbook already
        points at the falling **backlog** as the signal to trust precisely because this one can lie.

        The counts are five explicit keyword arguments with **no defaults**, for `PurgeReport`'s
        reason: a default on a count is how a second call site forgets one and records a confident
        zero. A failed run that has no report passes its zeros deliberately.

        No `EXPIRE`, ever — see the module docstring.
        """
        try:
            # redis-py types `hset` as `ResponseT` — `Awaitable[T] | T`, one signature covering both
            # the sync and the async client — so a bare `await` on it does not type-check. A `cast`
            # at the call site rather than an `ignore`: it names which half of that union the async
            # client returns, and everything around it stays checked.
            hset = self._redis.hset(
                self._key,
                mapping={
                    # `timespec="seconds"` rather than a bare `isoformat()`. The `Clock` port is
                    # whole-second by contract, so on a conforming clock this is the identity — but
                    # writing the precision explicitly means a test double that lies about the
                    # contract cannot get a microsecond field into a value `/health/ready` echoes.
                    _FIELD_AT: at.isoformat(timespec="seconds"),
                    _FIELD_OUTCOME: outcome,
                    _FIELD_SESSIONS_DELETED: sessions_deleted,
                    _FIELD_FILES_UNLINKED: files_unlinked,
                    _FIELD_DURATION_MS: duration_ms,
                },
            )
            await cast("Awaitable[int]", hset)
        except Exception as exc:
            # Broader than `rate_limit.py`'s `RedisError`, on purpose. That module's failure costs
            # one HTTP request and a narrow `except` there keeps a genuine bug visible; here the
            # destructive work is already done and committed, so *anything* that escapes this line
            # would convert a successful purge into a failed one. `Exception`, never
            # `BaseException` — a cancelled run must still cancel.
            #
            # `error_type` and nothing else: the value being written carries no PII, but the habit of
            # never formatting an exception's *message* into a log line is what keeps that true the
            # day somebody adds a field (Constitution §8).
            log.warning("retention.heartbeat_unavailable", error_type=type(exc).__name__)

    async def read(self) -> PurgeHeartbeat | None:
        """The last heartbeat, or `None` when there is not a usable one.

        `None` means exactly what the runbook's row says: *the job has never run, or Redis was
        flushed* (R-26). `/health/ready` publishes `last_run: null`, and — when the schedule is on —
        `stale: true`, which is a 200 and never influences `ready` (AC-32).

        **How this degrades, decided rather than discovered:**

        - **Key absent** → `None`. The never-run case above.
        - **`at` missing, unparseable, or naive** → `None`, i.e. treated as never-run. Every answer
          derived from a heartbeat is derived from `at`, and a naive timestamp is the sharp one: the
          probe subtracts it from an aware `now`, and `aware - naive` raises `TypeError` — a health
          endpoint returning 500 because a timestamp lost its offset is a worse failure than one
          reporting the state an operator is already told to investigate.
        - **Any other field missing or unparseable** → that field is `None` and the rest is
          published. A count that will not parse is not a reason to withhold `last_run`.
        - **Redis unreachable** → this **raises**, unlike `record`. The two directions are not an
          inconsistency: a failed *write* loses bookkeeping about work that already succeeded, while
          a failed *read* is the probe not knowing — and "I could not ask" must not be reported as
          "it has never run". `probe_guest_purge` catches it and sets `detail` (R-29), which is the
          shape the readiness contract already uses for a field it could not compute.
        """
        # The same `ResponseT` cast as in `record`, and the weakest true claim about the shape:
        # `dict[object, object]`, because what the values are is decided by a client built three
        # modules away — see `_as_text`.
        raw: dict[object, object] = await cast(
            "Awaitable[dict[object, object]]", self._redis.hgetall(self._key)
        )
        if not raw:
            return None

        # The client is built without `decode_responses`, so values come back as `bytes`; a client
        # configured the other way hands back `str`. Both are accepted rather than assuming one,
        # because which of them arrives is a property of a connection built three modules away.
        fields = {
            text_key: _as_text(value)
            for key, value in raw.items()
            if (text_key := _as_text(key)) is not None
        }

        at = _as_instant(fields.get(_FIELD_AT))
        if at is None:
            return None
        return PurgeHeartbeat(
            at=at,
            outcome=_as_outcome(fields.get(_FIELD_OUTCOME)),
            sessions_deleted=_as_int(fields.get(_FIELD_SESSIONS_DELETED)),
            files_unlinked=_as_int(fields.get(_FIELD_FILES_UNLINKED)),
            duration_ms=_as_int(fields.get(_FIELD_DURATION_MS)),
        )


def _as_text(value: object) -> str | None:
    """Decode one hash key or value. Never raises: an undecodable byte string is not a field."""
    if isinstance(value, bytes):
        try:
            return value.decode()
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        return value
    return None


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _as_outcome(value: str | None) -> PurgeOutcome | None:
    """Written out rather than a membership test, because the return type is a `Literal` and an
    unknown word must come back as `None` rather than as a third outcome the probe has no branch
    for."""
    if value == "ok":
        return "ok"
    if value == "failed":
        return "failed"
    return None


def _as_instant(value: str | None) -> datetime | None:
    """Parse `at`. `None` for anything the probe could not safely do arithmetic with — see `read`."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed
