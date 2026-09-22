"""Use cases for the `retention` bounded context: the two jobs that make FR-6's promise true.

This package is the thinnest in the application layer, and the shape of it is the argument ADR-0018
decision 1 makes. There is no aggregate to load, no state transition to drive and no event to
publish — what lives here is a *policy* applied to whatever the store of record says has aged out:

- `purge_expired_guest_sessions` — the scheduled path (Celery beat) and the operator's path (the
  CLI), sharing one use case. Rows first, **committed**, then files, per session; bounded per tick;
  deliberately partial in the face of one bad row.
- `reclaim_orphaned_files` — the second half of the same promise from the volume's side: a file that
  outlived the row that named it. It fails **closed**, in the opposite direction from the purge's
  lock, and its own docstring says why (ADR-0018 decision 6).

**Neither takes a command dataclass**, for the reason `AbandonStaleExportJobs` states: every use case
that takes one acts on behalf of a caller who names what to act on, and these two name nothing. They
act on whatever is expired *now*, and their bounds are configuration fixed at construction. A
zero-field command would be a contract with nothing in it.

**Neither names a transaction, and neither may** (ADR-0002). "Rows first, committed, then files" is a
sentence about durability, and the only honest way to say it from here is one committed transaction
per session — supplied by the committing adapter in the composition root, exactly as
`CommittingExportJobRepository` and `CommittingTailoringRunRepository` do one layer along.

This package re-exports nothing, as every other application package does.
"""
