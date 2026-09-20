"""The three event names the guest purge logs, defined once because **two** entry points emit them.

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
