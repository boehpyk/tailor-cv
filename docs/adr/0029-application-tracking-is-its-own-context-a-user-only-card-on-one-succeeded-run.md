# ADR-0029: Application tracking is its own context: a user-only card on one succeeded run

- **Status:** Accepted
- **Date:** 2026-10-04
- **Relates to:** ADR-0002 (hexagonal layers), ADR-0007 (persistence conventions), ADR-0014 and
  ADR-0016 (no foreign key across contexts), ADR-0015 (optimistic concurrency by `version`),
  ADR-0022 (a row's owner is a sum type — **deliberately not followed here, and not amended**),
  ADR-0023 decision 8 (*"application tracking is a new aggregate"* — **executed, amended**),
  ADR-0024 (the query-port pattern — **amended (a)**), ADR-0006 (retention — **amended (f)**).
  Constitution §4 gains a `tracking` row. Supersedes nothing.

## Context

Phase 3 opens with a board: a signed-in user moves the applications they tailored here through the
stages of a job search. ADR-0023 decision 8 already said what it is not: *columns on the run*. A
run's status is a fact about a paid call; an application's stage is a fact about the user's life.

Five forces:

1. **`tailoring` owns the paid LLM call.** Every Phase 2 slice proved its privacy claim partly by an
   empty `git diff` over `domain/tailoring`, `application/tailoring` and `infrastructure/llm`. A
   Kanban inside that context would end the proof and start a god context.
2. **Every owned table since 2.2 has two owner columns** (ADR-0022). A card has no meaning for a
   guest: a guest session lives 24 hours, and a job search lasts weeks.
3. **A stage records what the user believes, and belief is corrected** — a misclick, a withdrawn
   offer, a rejection that calls back. `TailoringRun`'s strict transition table exists because each
   of its states records money spent; none of a card's states does.
4. **Contexts' tables are not fused by foreign keys** (ADR-0014, ADR-0016, kept by ADR-0023), and
   2.3 found that `NOT EXISTS` in a `DELETE` cannot see an uncommitted `INSERT`. A card that refers
   to a run is exposed to the same race.
5. **The repository is public and the codebase is a teaching reference** (FR-7). A dependency that
   adds a capability nobody uses teaches the wrong thing.

## Decision

**1. `tracking` is a seventh bounded context.** It owns the user's tracked applications and the
board that lists them. The import direction is one-way, and **`domain/tracking` imports no sibling
context**: the run a card references is held as tracking's own `TrackedRunRef` (a `NewType` over
`UUID`; implemented as a frozen dataclass, see the correction below), and *"only a succeeded run
is trackable"* is enforced by the use case. `tailoring`'s types
(`TailoringRunId`, `TailoringRunStatus`, `GetTailoringRun`) appear only in `application/tracking/`,
which converts at the seam. Nothing in `tailoring` imports `tracking`. An AST allow-list pins
`domain/tracking` to the standard library, `domain/shared` and `domain/identity`.

**2. The aggregate is `TrackedApplication`: one card per succeeded, user-owned run.** A unique index
on `tailoring_run_id`. It holds a stage, an optional title (`ApplicationTitle`: trimmed, 1–120
characters, no control characters), `tracked_at`, `stage_changed_at` and `version` — nothing else.
Notes, salary, contacts and interview dates are out: each is free text or a third party's data, and
together they would make the card the most PII-dense row after the CV. A card references a **run**,
not a posting, because the run is the documents the user sent and the history entry is already the
user-visible unit.

**3. Six stages, and any stage may move to any other.** `to_apply · applied · interviewing · offer ·
rejected · withdrawn`, stored as `VARCHAR(16)` plus a CHECK (not a native enum, for
`tailoring_run.status`'s reason). The invariants are about **time** (`stage_changed_at` is never
earlier than `tracked_at` and never runs backwards) and **concurrency**, not order. Moving to the
current stage is a no-op: no version bump, no event. The aggregate carries a *"why this is not
`TailoringRun`'s table"* paragraph at the point of contradiction.

**4. Optimistic concurrency is ADR-0015's mechanism, without a shared base class.** Every write
carries the `version` the client saw; the aggregate checks it **before** the no-op rule, so a stale
tab is told it is stale. The mapper's `version_id_col` refuses a write that slipped past the
aggregate (409, `current_version: null` when the card was deleted mid-request). `TailoringRun` and
`TrackedApplication` each keep their own six-line guard: their rules differ (a run is editable only
when succeeded, a card always).

**5. The owner is a `UserId`, not ADR-0022's `Owner`.** The table has `user_id NOT NULL →
identity_user ON DELETE CASCADE` and **no `guest_session_id` column**. A guest arm the use case always
refuses would be an impossible variant in the type and a table the purge must be *trusted* not to
reach. Without it the purge cannot reach a card, the claim (ADR-0025) has nothing to re-key, and
account erasure is the cascade under the existing user-row lock. Authorization stays one equality:
`card.user_id == requester`.

**6. Deleting a history entry deletes its card, in the same transaction; there is no foreign key.**
Retention's history-entry adapter runs a separate `DELETE FROM tracking_application … RETURNING id`
after the run's `DELETE … RETURNING`. The race a foreign key would close — a track request landing
after the run's deletion — is closed by **2.3's two-lock pattern**: the repository's `add` INSERTs,
**then** takes the run `FOR KEY SHARE` and refuses if it is gone (the INSERT's FK check has already
taken the user row, so account erasure meets it there and no lock cycle exists); the history
deletion's run `DELETE` waits on that lock, and its card `DELETE`, a separate statement, takes a
fresh snapshot after the wait. Both orders are tested on real connections.

**7. Moving a card is a command with two ways to issue it, and no dependency.** An always-visible
**Move to** control (a native `<select>`) serves keyboard, screen-reader and touch users and
satisfies WCAG 2.2 SC 2.5.7; native HTML5 drag-and-drop serves the mouse and sends the identical
request. The board has no ordering within a column, so the only drop target is a column — the case
native drag-and-drop handles well.

## Alternatives

- **Inside `tailoring`.** One fewer package. Rejected: it makes the context that owns the paid call
  also own a job search, and it ends the empty-diff privacy proof (force 1).
- **Inside `identity`, as account data.** Rejected: saved CVs and runs are account data too, and
  live in their own contexts. Ownership is a property, not a context.
- **Domain imports of `tailoring`'s value objects.** The planner's first draft, with
  `EraseHistoryEntry` as precedent. Rejected at approval: that precedent is application-layer only,
  and 2.3 already fixed the same shape in `retention`'s domain.
- **Reference a posting, or a free-standing card.** Rejected: postings are not a user-visible unit,
  and a posting does not say which documents were sent. A free-standing card (company and role typed
  by hand) is a different data shape and a larger privacy surface. **Trigger:** the friend group asks
  for it during the Phase 3 gate.
- **A strict stage machine** (`rejected` terminal, forward-only). Rejected: it invents rules the
  user's life does not follow and turns every correction into a support request.
- **ADR-0022's sum type with a refused guest arm.** Rejected: an impossible variant in the type and
  a guest column the purge must be trusted to leave alone (decision 5).
- **A foreign key with `ON DELETE CASCADE` to `tailoring_run`.** Simpler deletion. Rejected: it fuses
  two contexts' schemas, which ADR-0014 and ADR-0016 declined; the two locks close the same race.
- **Refusing a history deletion while the run is tracked.** Rejected: an unexpected 409 on a privacy
  action, which 2.3 keeps unconditional for anything not in flight.
- **`@dnd-kit`.** The serious option: keyboard dragging, touch sensors, announcements. Rejected for
  now: the keyboard and touch problem it solves is already solved, more plainly, by the Move control,
  and the board has no within-column ordering. **Trigger:** within-column reordering is approved.

## Consequences

- **Constitution §4 gains a row**, and `retention`'s row now names tracked applications among what
  history-entry deletion and account erasure take.
- **The guest purge and the guest-work claim never see the table**, by schema rather than by care.
  2.3's exactly-one-owner catalogue test and 2.4's `guest_session_id` catalogue test are unaffected,
  and a test asserts the table has no guest column.
- **History deletion is one statement longer**, and its report gains `tracked_application_deleted`;
  `erase-account`'s dry run counts cards.
- **The board makes no LLM call and queues no task.** It works with the worker stopped.
- **This is the codebase's first optimistic update** (a move shows at once and rolls back on
  refusal). Retitle, remove and add are not optimistic.
- **Notes, salary and free-standing cards are each a future decision with a privacy check**, not a
  column someone adds.
- **What to watch:** the board is unpaginated and bounded by the 500-card cap (ADR-0024 amendment
  (a)). If the cap rises, or the measured payload passes 400 KB, page it.

## Correction: 2026-10-06, from the implementation of slice 3.1 (`tracking-application-board`)

Decision 1 calls `TrackedRunRef` a `NewType` over `UUID`. The implementation uses a **frozen
dataclass with one `value: UUID` field** instead, for `TrackedApplicationId` and `TrackedRunRef`
alike, the same shape as `TailoringRunId` and every other id in the codebase. A `NewType` would have
been the only id of its kind, and it is erased at runtime (`TrackedRunRef(u)` would simply be `u`).

The decision itself is unchanged: `domain/tracking` still imports no sibling context, and the two
contexts' types still meet at exactly one seam in `application/tracking/`, which is now spelled
`TrackedRunRef(run.id.value)`. The column holding it maps through its own `TrackedRunRefType`
decorator, not tailoring's `TailoringRunId` one.
