# ADR-0024: A read model that joins contexts is a query port, keyset-paginated

- **Status:** Accepted
- **Date:** 2026-09-26
- **Relates to:** ADR-0002 (hexagonal layers — a port is a `Protocol` in the domain, an adapter in
  infrastructure), ADR-0007 (persistence conventions — one repository per aggregate, every column
  named), ADR-0023 (the history this read model lists), ADR-0014 amendment (the dangling
  `base_cv_id` whose deletion this read model derives). Supersedes nothing.

## Context

Until slice 2.3 every list in the codebase read one table. The guest lists (1.1–1.5) load
aggregates — at most twenty, a session's lifetime. Slice 2.2's saved-CV list returns
`SavedBaseCvSummary` from `BaseCvRepository.list_for_user`, with `char_length(extracted_text)`
computed in SQL so the text column is never selected. That was right: one table, one aggregate, and
the read model is a narrower view of the thing the repository already persists.

History (ADR-0023) is the first list that is not like that. One history entry needs **three
contexts' tables**: the run's status and timestamps (`tailoring`), the posting's title and a short
preview (`posting`), and the saved CV's label and filename (`intake`) — or, when the CV has been
deleted, the fact that it is gone. And it is unbounded in the way the guest lists are not: a user may
hold up to 500 runs, kept indefinitely, so it must page.

Three forces:

1. **A repository is the persistence of one aggregate.** A `TailoringRunRepository.list_for_user`
   that joins `posting_job_posting` and `intake_base_cv` would make `tailoring`'s write-side port
   know two other contexts' schemas — the coupling the bounded contexts exist to prevent.
2. **The list must never load a document.** A tailored CV and a cover letter are the most sensitive
   text the product holds, and a list response has no use for them. The posting's full text is the
   same: a preview is 140 characters, not 30,000.
3. **The list changes under the reader.** Entries are created and deleted while a user pages
   through them; the `Clock` is whole-second, so two runs in one second are ordinary in a test and
   possible in life.

## Decision

**1. A read model that joins more than one context's tables is a query port.** A `Protocol` in the
**reading** context's domain — here `TailoringHistoryQuery` in `domain/tailoring/ports.py`, with one
method, `page_for_user(user_id, after, size) -> HistoryPage` — returning flat, frozen **read-model**
values (`TailoringHistoryEntry`, `HistoryPage`), never an aggregate. Its adapter lives in a new
directory, `infrastructure/persistence/queries/`, and is **Core SQL with every column named**. That
is the one place a cross-context join is legitimate: a projection nobody writes through. The port's
docstring says it is read-side, returns no aggregate, and never selects a document.

**A read model over one aggregate's table may stay on its repository.** 2.2's `SavedBaseCvSummary` on
`BaseCvRepository.list_for_user` is the example, and it is correct as it is: the line is *"does this
projection join another context's table?"*, not *"is this a read model?"*.

**2. Values in a read model are plain, not re-validated.** Titles, labels and filenames come back as
`str`, not as their value objects: a read model is not re-validated on the way out (it was validated
on the way in), and constructing value objects per row is the cost 2.2 measured (T30). Derived
boundary facts — `retryable`, for one — stay at the boundary, as in 1.3.

**3. The query is one statement, and every join to another context is a `LEFT JOIN`.**

- The saved-CV join is how *"CV deleted"* is **derived**: no row, no label, and nothing stored
  (ADR-0014 amendment).
- The posting join is defensive: a missing posting should be impossible, but an `INNER JOIN` would
  make such an entry **invisible to its owner, and therefore undeletable**. A warning line names it
  instead.
- **The owner is in each join condition** (`p.user_id = r.user_id`), not only in the `WHERE`. A
  dangling id that one day collides with another owner's row can never lend its label to the wrong
  history. Defense in depth that costs nothing.
- **No document column is selected.** The posting preview is `left(p.text, 140)` in SQL, as 2.2's
  character count was, so the full text never enters a result set. A statement-capture test asserts
  it.

**4. Pages are keyset, never `OFFSET`.** Ordered by `(requested_at DESC, id DESC)` with a composite
index on `(user_id, requested_at DESC, id DESC)`; the next page is `WHERE (requested_at, id) <
(:cursor_at, :cursor_id)`; the query fetches `size + 1` rows to know whether a next page exists
without a count. `id` breaks ties because the `Clock` is whole-second.

**5. The cursor is a domain value; its encoding is the boundary's, and it is not signed.**
`HistoryCursor(requested_at, tailoring_run_id)` is a frozen dataclass validating a tz-aware,
whole-second instant. The router encodes it as base64url of `"<epoch_seconds>.<uuid>"` and decodes it
at the boundary, refusing anything malformed with 422 `invalid_cursor`. It is never logged. It is not
signed, because it carries nothing the requester could not already see, and a forged cursor can only
page **their own** history differently: the `WHERE r.user_id = :u` is not in it.

## Alternatives

- **A repository method** (`TailoringRunRepository.list_for_user`) — 2.2's shape. Rejected here and
  kept there: right for one table, wrong for three, because `tailoring`'s write-side port would learn
  `posting`'s and `intake`'s schemas.
- **Three queries joined in the use case** — the runs from `tailoring`'s repository, then postings and
  CVs by id from theirs. Rejected: either N+1 or three round trips per page, and *"CV deleted"*
  computed in Python from an absence in a dictionary — the same derivation, further from the data and
  easier to get wrong.
- **`OFFSET` pagination.** Rejected: `OFFSET 400` makes Postgres read and discard 400 rows per
  request, and it **skips or repeats rows** when an entry is inserted or deleted between two page
  loads. Keyset paging on values is O(page) with the index and stable under concurrent change: a
  cursor whose own run was deleted between pages still continues correctly, because it compares
  values, not a row's position.
- **A signed (HMAC) cursor.** Rejected: signing protects a cursor's contents from the person holding
  it, and there is nothing in this one they may not see or may not change. A key, a rotation story and
  a verification path for no protected property.

## Consequences

- **The read side is not the write side, visibly.** Commands go through aggregates and repositories;
  a list that joins contexts goes through a query port. A reviewer can tell which one a new list
  needs by one question, and the directory says which it is.
- **Phase 3's tracking board reads this way.** A tracked application (ADR-0023 decision 8) listed
  beside its run and posting is a cross-context projection: a query port in its context, an adapter
  under `infrastructure/persistence/queries/`, keyset pages. This ADR is the pattern it copies.
- **A query port needs a binding like any port.** 1.3's "a port with no binding is a bug" check
  extends to it; the in-memory double used by application tests must honour the same ordering and
  cursor semantics, or the tests prove the double.
- **The index is part of the decision.** Keyset without `(user_id, requested_at DESC, id DESC)` is a
  sort over the user's whole history per page. The migration creates it, and a measured `EXPLAIN` on
  500 runs is the proof, not the plan's word.
- **A read model can drift from the aggregates it projects.** It is not re-validated, so a column
  renamed on a write-side mapping must be renamed here too. The statement-capture test and the
  mapping round-trip tests are what catch it; nothing in the type system will.

## Amendment: 2026-10-04, from the plan of slice 3.1 (`tracking-application-board`)

Slice 3.1's board (ADR-0029) is a cross-context projection, so decisions 1–3 apply to it as written:
`ApplicationBoardQuery` in `domain/tracking/ports.py`, Core SQL under
`infrastructure/persistence/queries/`, three `LEFT JOIN`s with the owner in every join condition, no
document column, the posting preview `left(text, 140)` in SQL. Decision 4 does not fit it, and this
amendment says when it need not.

**(a) A bounded projection that must be seen whole may be returned whole.** Keyset paging (decision 4)
is the default. A cross-context read model may instead return every row in one response when **all
three** conditions hold:

1. **It is bounded at write time** — a cap enforced by the use case that creates the rows (here
   `MAX_TRACKED_APPLICATIONS_PER_USER = 500`), not a `LIMIT` on the read that silently hides rows.
2. **It is measured at the bound** — a p95 and a response size at the cap, recorded in the slice
   (here ≤ 150 ms and ≤ 400 KB for 500 cards), with an `EXPLAIN` showing an index scan.
3. **The reader needs it whole** — a Kanban shows every column at once, and a drag target that is
   not loaded cannot be dropped on. Paging it per column means six cursors for one screen.

A list that is merely *usually* small does not qualify; history (decision 4's case) does not, because
it is read a page at a time. **Trigger for paging after all:** the cap rises above its measured value,
or the measured payload exceeds the recorded size.

The Consequence *"Phase 3's tracking board reads this way … keyset pages"* is corrected: the board
reads through a query port, **whole, under (a)**.
