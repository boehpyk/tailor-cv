"""The five event names the guest purge logs, defined once because **two** entry points emit them.

AC-21 asks for one structured line on every run, and slice 1.6 ships two runners of the same use
case: the Celery task on beat and `purge-guests` at an operator's terminal. The whole value of the
line is that one log search finds a job's skips and failures next to its successes — across both
runners, since an operator's manual `make purge` and the 03:00 tick are the same job. Two copies of
a string that must be identical is a search that silently misses half its answer the day one of them
is reworded.

They live in this package rather than in `tasks/retention.py` for the reason the lock's TTL does not
live in the lock: `infrastructure/tasks/app.py` builds a Celery application at import, and
`purge-guests` publishes no task, needs no broker and must not inherit an unrelated startup refusal
to find out what a log line is called.

`retention.orphan_scan_*` is deliberately absent. The orphan sweep has exactly one entry point — the
CLI, never beat (ADR-0018) — so its names have one home and belong in it.
"""

from __future__ import annotations

from typing import Final

EVENT_PURGE_COMPLETED: Final = "retention.purge_completed"
"""AC-21's line: eight fields, every one a count, a duration or a flag. Emitted on **every**
completed run including the ones that deleted nothing, because a job that does nothing and logs
nothing is indistinguishable from a job that never ran — and for this job "nothing to do" is the
healthy outcome."""

EVENT_PURGE_SKIPPED: Final = "retention.purge_skipped"
"""The lock was held by another run (R-8). The task returns without raising; the CLI exits 3."""

EVENT_PURGE_FAILED: Final = "retention.purge_failed"
"""The run failed (R-1, R-15). `error_type` only — never the exception's message and never
`exc_info`: a driver error quotes the row it refused, and here the row is a guest session. No
heartbeat is written on this path, because a heartbeat says "the job last finished at"."""

EVENT_SESSION_PURGE_FAILED: Final = "retention.session_purge_failed"
"""R-3: one session's `DELETE` was refused and the batch carried on without it. One line **per
failed session**, carrying `guest_session_id` and `error_type` and nothing else.

This is the line whose absence `/verify` found. Without it a permanently-refused `DELETE` — a
constraint nobody predicted, a row lock that never clears — is retried every hour for ever, always
fails, stops the CLI's batch loop as soon as it is the only candidate left, and the run still exits
0 while `overdue` sits above zero permanently. The counts say *how many*; only this line says
**which** session and **what kind** of refusal, which is the operator's next move.

`error_type` is a class name **by convention, kept at the call sites** — both production
constructions go through `SessionPurgeFailure.from_exception`, which reads `type(exc).__name__` and
cannot reach `str(exc)` or `exc_info`. The dataclass constructor is public, so this is a convention a
reviewer enforces rather than a property the type guarantees; `value_objects.py`'s own docstring
argues it in full, including why validating the string in `__post_init__` was rejected
(Constitution §8, AC-38)."""

EVENT_FILE_UNLINK_FAILED: Final = "retention.file_unlink_failed"
"""R-4: the row is already deleted and committed, and the store refused to unlink one of its files —
so that file is now an orphan, the survivor ADR-0006 §2 chose. One line **per failed unlink**,
carrying `error_type` alone.

**Never the key, never the path**, which R-4 says in so many words and which `FileUnlinkFailure`
enforces by having no field to put one in. The `errno` was already logged by the adapter, at the
point where it still knew one. Recovery is `purge-guests --orphans`."""
