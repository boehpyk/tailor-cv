import { describe, expect, it } from 'vitest';

import { reduce } from './useDocumentAutosave';

import type { AutosaveAction, AutosaveState } from './useDocumentAutosave';

/**
 * Test-after, pure unit tests for `reduce` (commit ca2eed1 exported it exactly so this could be a
 * pure test — no editor, no timer, no network). The union of `(AutosaveState, AutosaveAction)`
 * pairs is small enough to cover exhaustively rather than sample, per the reducer's own docstring
 * ("Exported so it can be pinned in a pure test: every rule above is decidable from a state and an
 * action").
 *
 * The rules pinned here, read off `useDocumentAutosave.ts`'s `reduce` and its docstring
 * (lines 72-98):
 *
 * 1. `expired` is terminal — every action, of every type, leaves it `expired`.
 * 2. `changed` is a no-op in `conflict`, `saving` and `paused` — a keystroke is not a choice
 *    (`conflict`), a `PUT` already on the wire decides the next state (`saving`), and a 429's
 *    window is being waited out (`paused`).
 * 3. Everywhere else `changed` reads only `differs`, not the source state: `differs: true` always
 *    lands on `dirty` and `differs: false` always lands on `saved` — from `saved`, `dirty`,
 *    `failed` and `invalid` alike. The docstring narrates only the `saved|failed|invalid -> dirty`
 *    and `dirty -> saved` cases by name; it does not say `failed`/`invalid` behave differently for
 *    `differs: false`, and they do not — the code's rule is source-state-agnostic outside the three
 *    ignored kinds. Docstring and code agree; the docstring is just less exhaustive than the code.
 * 4. `sent` always moves to `saving`, from any non-`expired` state — including `saving` itself,
 *    `conflict`, and `paused`. Nothing in the docstring restricts `sent`'s reach the way it
 *    restricts `changed`'s, and the `switch` doesn't either.
 * 5. `resolved{to}` always adopts `to` verbatim, from any non-`expired` state, regardless of what
 *    `to` is — this is how `onSuccess`/`onError`/`loadLatest`/`send` each land the reducer on a
 *    concrete outcome without the reducer knowing anything about mutations or the network.
 */

const SAVED: AutosaveState = { kind: 'saved' };
const DIRTY: AutosaveState = { kind: 'dirty' };
const SAVING: AutosaveState = { kind: 'saving' };
const FAILED: AutosaveState = { kind: 'failed' };
const CONFLICT: AutosaveState = { kind: 'conflict' };
const PAUSED: AutosaveState = { kind: 'paused', retryAfterSeconds: 30 };
const INVALID: AutosaveState = { kind: 'invalid', problem: 'too_short' };
const EXPIRED: AutosaveState = { kind: 'expired' };

function changed(differs: boolean): AutosaveAction {
  return { type: 'changed', differs };
}

const SENT: AutosaveAction = { type: 'sent' };

function resolved(to: AutosaveState): AutosaveAction {
  return { type: 'resolved', to };
}

describe('reduce — expired is terminal', () => {
  it.each([
    ['changed{differs:true}', changed(true)],
    ['changed{differs:false}', changed(false)],
    ['sent', SENT],
    ['resolved{to: dirty}', resolved(DIRTY)],
    ['resolved{to: saved}', resolved(SAVED)],
    ['resolved{to: expired}', resolved(EXPIRED)],
  ] as const)('%s leaves expired unchanged', (_label, action) => {
    expect(reduce(EXPIRED, action)).toEqual(EXPIRED);
  });
});

describe('reduce — changed is ignored while conflict, saving or paused', () => {
  const IGNORING_STATES: readonly (readonly [string, AutosaveState])[] = [
    ['conflict', CONFLICT],
    ['saving', SAVING],
    ['paused', PAUSED],
  ];

  it.each(IGNORING_STATES)('%s + changed{differs:true} is a no-op', (_label, state) => {
    expect(reduce(state, changed(true))).toEqual(state);
  });

  it.each(IGNORING_STATES)('%s + changed{differs:false} is a no-op', (_label, state) => {
    expect(reduce(state, changed(false))).toEqual(state);
  });
});

describe('reduce — changed moves every other state by differs alone', () => {
  const RESPONSIVE_STATES: readonly (readonly [string, AutosaveState])[] = [
    ['saved', SAVED],
    ['dirty', DIRTY],
    ['failed', FAILED],
    ['invalid', INVALID],
  ];

  it.each(RESPONSIVE_STATES)('%s + changed{differs:true} -> dirty', (_label, state) => {
    expect(reduce(state, changed(true))).toEqual(DIRTY);
  });

  it.each(RESPONSIVE_STATES)('%s + changed{differs:false} -> saved', (_label, state) => {
    expect(reduce(state, changed(false))).toEqual(SAVED);
  });
});

describe('reduce — sent always moves to saving, from any non-expired state', () => {
  const NON_EXPIRED_STATES: readonly (readonly [string, AutosaveState])[] = [
    ['saved', SAVED],
    ['dirty', DIRTY],
    ['saving', SAVING],
    ['failed', FAILED],
    ['conflict', CONFLICT],
    ['paused', PAUSED],
    ['invalid', INVALID],
  ];

  it.each(NON_EXPIRED_STATES)('%s + sent -> saving', (_label, state) => {
    expect(reduce(state, SENT)).toEqual(SAVING);
  });
});

describe('reduce — resolved{to} always adopts `to`, from any non-expired state', () => {
  const NON_EXPIRED_STATES: readonly (readonly [string, AutosaveState])[] = [
    ['saved', SAVED],
    ['dirty', DIRTY],
    ['saving', SAVING],
    ['failed', FAILED],
    ['conflict', CONFLICT],
    ['paused', PAUSED],
    ['invalid', INVALID],
  ];
  const RESOLUTION_TARGETS: readonly (readonly [string, AutosaveState])[] = [
    ['saved', SAVED],
    ['dirty', DIRTY],
    ['failed', FAILED],
    ['conflict', CONFLICT],
    ['paused', PAUSED],
    ['invalid', INVALID],
    ['expired', EXPIRED],
  ];

  describe.each(NON_EXPIRED_STATES)('from %s', (_sourceLabel, source) => {
    it.each(RESOLUTION_TARGETS)('resolved{to: %s} lands on exactly that state', (_toLabel, to) => {
      // `toBe`, not `toEqual`: the reducer returns `action.to` itself, with no copy.
      expect(reduce(source, resolved(to))).toBe(to);
    });
  });
});
