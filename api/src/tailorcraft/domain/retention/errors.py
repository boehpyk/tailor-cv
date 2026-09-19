"""Errors the `retention` context raises when a rule about deleting aged-out guest data breaks.

There are two of them, and the shortness of this file is the design. **No floor, no catch-all and no
`except Exception` anywhere in this context** (R-15). The distinction is worth stating because it
contradicts a rule that holds two modules away: a *port* promises to translate every failure into the
domain's language and therefore needs an `except Exception` floor underneath its named translations
(ADR-0012 obligation 10, and `LocalFileStore` does exactly that). A **use case promises no such
thing** — and a floor here would convert a bug in a job that *deletes things* into a green exit code
and a tidy-looking report. An unexpected exception in the purge must escape, fail the task, and leave
whatever had already committed committed (R-1, R-15).

As in `intake`, `posting`, `tailoring` and `export`: an error class *is* its contract, so both get a
real body here rather than a `NotImplementedError` stub even though this module lands in the SKELETON
step. An error the RED test has to construct must be constructible.

**Neither carries text, and neither ever will.** Not a filename, not a storage key, not a path, not a
driver message — the two things this context touches are a pile of PII and a volume where an
unrecognised file may be named after a person (Constitution §8, R-37). The domain never raises
`HTTPException` and carries no status code; the entry points here are a CLI and a Celery task, where
there is no request to fail at all.
"""

from __future__ import annotations

from tailorcraft.domain.shared.errors import DomainError


class RetentionError(DomainError):
    """Base class for every error this context raises.

    It exists so an entry point can say "retention refused" in one `except` without reaching for
    `DomainError` and catching every other context's rules along with it — and so the next error
    this context grows arrives inside a category the CLI already handles, rather than as a new clause
    somebody has to remember to add.
    """


class OrphanScanAborted(RetentionError):
    """The database cross-check could not run, so **nothing is deleted** (R-33, AC-23).

    This is the fail-**closed** half of a pair whose two directions are deliberate and opposite
    (ADR-0018 decision 6). The purge lock fails *open* — Redis being down must not stop a purge,
    because a skipped purge breaks a privacy promise while an overlap merely duplicates idempotent
    work. This one fails closed, because deleting a file on the grounds that we could not ask whether
    anything references it is the one irreversible mistake this tool can make. Put a mechanism's
    failure on the side whose loss is recoverable: duplicated work in one case, a file in the other.

    The sweep exits non-zero on this, so an operator learns the check did not run instead of reading
    a report of zero reclaimed files as a clean volume.

    Carries nothing — not the keys it was asking about, not the underlying error's message. The
    entry point logs `error_type` and the count of keys it never got an answer for, and the entry
    point already has both.
    """
