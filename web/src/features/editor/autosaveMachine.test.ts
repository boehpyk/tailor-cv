import { describe, expect, it } from 'vitest';

import { AUTOSAVE_DEBOUNCE_MS } from './saveState';
import { step } from './autosaveMachine';

import type {
  AutosaveEffect,
  AutosaveEvent,
  AutosaveMachine,
  SaveFailure,
} from './autosaveMachine';
import type { DocumentProblem } from '@/features/tailoring/types';

/**
 * The table against `step`, TailorCraft's pure autosave decision (technical plan, "The editor";
 * `autosaveMachine.ts`'s own docstring, which is the source of truth here — there is no prior
 * spec to write this from, because the machine *is* the re-modelling of three review rounds' worth
 * of hook bugs into one place). Test-after, per CLAUDE.md: this is a "shape discovered against the
 * library" only in the sense that the shape was discovered against three rounds of bugs, not a
 * library, but the module already exists and this pins it rather than driving it red-first.
 *
 * Every case is `{ name, machine, event, next, effects }`; `next` and `effects` are asserted with
 * `toEqual` against `step(machine, event)`'s two fields separately, so a mismatched field is easy
 * to see in a failure. The whole matrix — 10 states × 11 events, hand-enumerated below because
 * `AutosaveMachine['kind']` and `AutosaveEvent['type']` are TypeScript types with nothing to
 * introspect at runtime — is covered: the meaningful transitions are pinned explicitly below by
 * state, and a closing `describe` computes every (state, event) pair this file did NOT already
 * exercise and asserts it is the machine's default branch — stay in place, no effects. That default
 * assertion is only true if the pair really is a no-op; if a transition was forgotten above, this
 * is where it turns red instead of silently passing by omission (TailorCraft's "no default case"
 * rule, applied to a hand enumeration instead of a compiler).
 *
 * One documented disagreement, resolved in favour of the code (which agrees with its own
 * docstring): `inFlight + change` does NOT set `sendWanted`. It sets `debounceArmed`. `sendWanted`
 * is set only by `timerDue` or by a `flush` that finds a debounce armed — exactly what the
 * docstring says ("`sendWanted` records that a debounce became due (or a flush happened) while it
 * was out... `debounceArmed` records a debounce timer armed by typing during the flight"). The
 * cases below test the module's actual, self-consistent rule.
 */

const A = 'The quick CV.';
const B = 'The quick, revised CV.';
const C = 'The quick, re-revised CV.';

const SEND_B: AutosaveEffect = { type: 'send', content: B };
const SEND_C: AutosaveEffect = { type: 'send', content: C };
const ARM_DEBOUNCE: AutosaveEffect = { type: 'armTimer', ms: AUTOSAVE_DEBOUNCE_MS };
const CLEAR_TIMER: AutosaveEffect = { type: 'clearTimer' };
const REFETCH: AutosaveEffect = { type: 'refetch' };

const PROBLEM: DocumentProblem = 'too_long';

interface Case {
  readonly name: string;
  readonly machine: AutosaveMachine;
  readonly event: AutosaveEvent;
  readonly next: AutosaveMachine;
  readonly effects: readonly AutosaveEffect[];
}

function run(cases: readonly Case[]): void {
  it.each(cases)('$name', ({ machine, event, next, effects }) => {
    const result = step(machine, event);
    expect(result.next).toEqual(next);
    expect(result.effects).toEqual(effects);
  });
}

// ---------------------------------------------------------------------------------------------
// idle
// ---------------------------------------------------------------------------------------------

const idleCases: readonly Case[] = [
  {
    name: 'idle + change(differs) arms the debounce',
    machine: { kind: 'idle', lastSaved: A },
    event: { type: 'change', text: B },
    next: { kind: 'debouncing', lastSaved: A },
    effects: [ARM_DEBOUNCE],
  },
  {
    name: 'idle + change(same as lastSaved) stays idle, no effects',
    machine: { kind: 'idle', lastSaved: A },
    event: { type: 'change', text: A },
    next: { kind: 'idle', lastSaved: A },
    effects: [],
  },
];

describe('idle', () => {
  run(idleCases);
});

// ---------------------------------------------------------------------------------------------
// debouncing
// ---------------------------------------------------------------------------------------------

const debouncingCases: readonly Case[] = [
  {
    name: 'debouncing + change(differs from lastSaved) re-arms the debounce',
    machine: { kind: 'debouncing', lastSaved: A },
    event: { type: 'change', text: B },
    next: { kind: 'debouncing', lastSaved: A },
    effects: [ARM_DEBOUNCE],
  },
  {
    name: 'debouncing + change(back to lastSaved) closes the window, clears the timer',
    machine: { kind: 'debouncing', lastSaved: A },
    event: { type: 'change', text: A },
    next: { kind: 'idle', lastSaved: A },
    effects: [CLEAR_TIMER],
  },
  {
    name: 'debouncing + timerDue(differs) sends and goes inFlight',
    machine: { kind: 'debouncing', lastSaved: A },
    event: { type: 'timerDue', text: B },
    next: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    effects: [SEND_B],
  },
  {
    name: 'debouncing + timerDue(equal to lastSaved) resolves to idle with no send',
    machine: { kind: 'debouncing', lastSaved: A },
    event: { type: 'timerDue', text: A },
    next: { kind: 'idle', lastSaved: A },
    effects: [],
  },
  {
    name: 'debouncing + flush(differs) sends immediately and clears the timer',
    machine: { kind: 'debouncing', lastSaved: A },
    event: { type: 'flush', text: B },
    next: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    effects: [CLEAR_TIMER, SEND_B],
  },
  {
    name: 'debouncing + flush(equal to lastSaved) closes the window with no send',
    machine: { kind: 'debouncing', lastSaved: A },
    event: { type: 'flush', text: A },
    next: { kind: 'idle', lastSaved: A },
    effects: [CLEAR_TIMER],
  },
  {
    name: 'debouncing + unmount(differs) sends now, captured text becomes leaving.sent',
    machine: { kind: 'debouncing', lastSaved: A },
    event: { type: 'unmount', text: B },
    next: { kind: 'leaving', lastSaved: A, sent: B, wish: null },
    effects: [CLEAR_TIMER, SEND_B],
  },
  {
    name: 'debouncing + unmount(equal to lastSaved) is a quiet close, no send',
    machine: { kind: 'debouncing', lastSaved: A },
    event: { type: 'unmount', text: A },
    next: { kind: 'idle', lastSaved: A },
    effects: [CLEAR_TIMER],
  },
];

describe('debouncing', () => {
  run(debouncingCases);
});

// ---------------------------------------------------------------------------------------------
// inFlight
// ---------------------------------------------------------------------------------------------

const inFlightCases: readonly Case[] = [
  // --- change: arms debounceArmed, never sendWanted, and never sends -------------------------
  {
    name: 'inFlight + change(differs from sent) arms debounceArmed, does not send, does not set sendWanted',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    event: { type: 'change', text: C },
    next: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: true },
    effects: [ARM_DEBOUNCE],
  },
  {
    name: 'inFlight + change(back to exactly what was sent), no debounce previously armed: nothing owed',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    event: { type: 'change', text: B },
    next: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    effects: [],
  },
  {
    name: 'inFlight + change(back to exactly what was sent), a debounce WAS armed: clears it',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: true },
    event: { type: 'change', text: B },
    next: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    effects: [CLEAR_TIMER],
  },
  // --- timerDue: only ever records the wish; never emits clearTimer, never sends -------------
  {
    name: 'inFlight + timerDue sets sendWanted, clears debounceArmed, sends nothing',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: true },
    event: { type: 'timerDue', text: C },
    next: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: true, debounceArmed: false },
    effects: [],
  },
  // --- flush: only a debounce that is actually armed has anything to flush -------------------
  {
    name: 'inFlight + flush with a debounce armed records the wish and clears the timer',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: true },
    event: { type: 'flush', text: C },
    next: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: true, debounceArmed: false },
    effects: [CLEAR_TIMER],
  },
  {
    name: 'inFlight + flush with nothing armed is nothing',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    event: { type: 'flush', text: C },
    next: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    effects: [],
  },
  // --- landed200: resend-on-200 ----------------------------------------------------------------
  {
    name: 'landed200, editor text === sent, sendWanted false: settles idle at sent, no clearTimer owed',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    event: { type: 'landed200', textNow: () => B },
    next: { kind: 'idle', lastSaved: B },
    effects: [],
  },
  {
    name: 'landed200, editor text === sent, sendWanted TRUE: still settles idle — text matching wins over the wish',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: true, debounceArmed: false },
    event: { type: 'landed200', textNow: () => B },
    next: { kind: 'idle', lastSaved: B },
    effects: [],
  },
  {
    name: 'landed200, editor text === sent, a debounce was armed: settling idle clears it (owed)',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: true },
    event: { type: 'landed200', textNow: () => B },
    next: { kind: 'idle', lastSaved: B },
    effects: [CLEAR_TIMER],
  },
  {
    name: 'landed200, text differs, sendWanted true, no debounce armed: resends the latest text against the new baseline',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: true, debounceArmed: false },
    event: { type: 'landed200', textNow: () => C },
    next: { kind: 'inFlight', lastSaved: B, sent: C, sendWanted: false, debounceArmed: false },
    effects: [SEND_C],
  },
  {
    name: 'landed200, text differs, sendWanted true, a debounce was ALSO armed: clearTimer precedes the resend',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: true, debounceArmed: true },
    event: { type: 'landed200', textNow: () => C },
    next: { kind: 'inFlight', lastSaved: B, sent: C, sendWanted: false, debounceArmed: false },
    effects: [CLEAR_TIMER, SEND_C],
  },
  {
    name: 'landed200, text differs, sendWanted false, debounceArmed true: the running timer keeps ticking, no new arm',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: true },
    event: { type: 'landed200', textNow: () => C },
    next: { kind: 'debouncing', lastSaved: B },
    effects: [],
  },
  {
    name: 'landed200, text differs, sendWanted false, debounceArmed false: a fresh debounce is armed',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    event: { type: 'landed200', textNow: () => C },
    next: { kind: 'debouncing', lastSaved: B },
    effects: [ARM_DEBOUNCE],
  },
  // --- landedError: every kind, with and without a debounce owed ----------------------------
  {
    name: 'landedError(conflict) moves to resolvingConflict and refetches',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    event: { type: 'landedError', failure: { kind: 'conflict' } },
    next: { kind: 'resolvingConflict', lastSaved: A },
    effects: [REFETCH],
  },
  {
    name: 'landedError(conflict), a debounce was armed: clearTimer precedes the refetch',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: true },
    event: { type: 'landedError', failure: { kind: 'conflict' } },
    next: { kind: 'resolvingConflict', lastSaved: A },
    effects: [CLEAR_TIMER, REFETCH],
  },
  {
    name: 'landedError(expired) is terminal, no effects',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    event: { type: 'landedError', failure: { kind: 'expired' } },
    next: { kind: 'expired' },
    effects: [],
  },
  {
    name: 'landedError(invalid) carries the problem through, no effects',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    event: { type: 'landedError', failure: { kind: 'invalid', problem: PROBLEM } },
    next: { kind: 'invalid', lastSaved: A, problem: PROBLEM },
    effects: [],
  },
  {
    name: 'landedError(failed) settles failed, no effects',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    event: { type: 'landedError', failure: { kind: 'failed' } },
    next: { kind: 'failed', lastSaved: A },
    effects: [],
  },
  {
    name: 'landedError(failed), a debounce was armed: clearTimer is the only effect',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: true },
    event: { type: 'landedError', failure: { kind: 'failed' } },
    next: { kind: 'failed', lastSaved: A },
    effects: [CLEAR_TIMER],
  },
  {
    name: 'landedError(rateLimited, 30s) pauses for the window named, in ms',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    event: { type: 'landedError', failure: { kind: 'rateLimited', retryAfterSeconds: 30 } },
    next: { kind: 'paused', lastSaved: A, retryAfterSeconds: 30 },
    effects: [{ type: 'armTimer', ms: 30_000 }],
  },
  {
    name: 'landedError(rateLimited, 1s): the wait floors at AUTOSAVE_DEBOUNCE_MS, never shorter than the debounce',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    event: { type: 'landedError', failure: { kind: 'rateLimited', retryAfterSeconds: 1 } },
    next: { kind: 'paused', lastSaved: A, retryAfterSeconds: 1 },
    effects: [{ type: 'armTimer', ms: AUTOSAVE_DEBOUNCE_MS }],
  },
  {
    name: 'landedError(rateLimited), a debounce was armed: clearTimer precedes the pause timer',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: true },
    event: { type: 'landedError', failure: { kind: 'rateLimited', retryAfterSeconds: 30 } },
    next: { kind: 'paused', lastSaved: A, retryAfterSeconds: 30 },
    effects: [CLEAR_TIMER, { type: 'armTimer', ms: 30_000 }],
  },
  // --- unmount: captures the wish, never reads the editor again ------------------------------
  {
    name: 'unmount, editor text === sent: no wish',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    event: { type: 'unmount', text: B },
    next: { kind: 'leaving', lastSaved: A, sent: B, wish: null },
    effects: [],
  },
  {
    name: 'unmount, editor text differs from sent: the text is the wish, captured now',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    event: { type: 'unmount', text: C },
    next: { kind: 'leaving', lastSaved: A, sent: B, wish: C },
    effects: [],
  },
  {
    name: 'unmount, a debounce was armed: clears it (owed)',
    machine: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: true },
    event: { type: 'unmount', text: C },
    next: { kind: 'leaving', lastSaved: A, sent: B, wish: C },
    effects: [CLEAR_TIMER],
  },
];

describe('inFlight', () => {
  run(inFlightCases);
});

// ---------------------------------------------------------------------------------------------
// resolvingConflict
// ---------------------------------------------------------------------------------------------

const resolvingConflictCases: readonly Case[] = [
  {
    name: 'refetched: server text matches the editor now — the 200 was only lost on the way back, adopt it',
    machine: { kind: 'resolvingConflict', lastSaved: A },
    event: { type: 'refetched', serverText: B, textNow: () => B },
    next: { kind: 'idle', lastSaved: B },
    effects: [],
  },
  {
    name: 'refetched: server text differs from the editor now — a real conflict',
    machine: { kind: 'resolvingConflict', lastSaved: A },
    event: { type: 'refetched', serverText: B, textNow: () => C },
    next: { kind: 'conflict', lastSaved: A },
    effects: [],
  },
  {
    name: 'refetchFailed settles failed',
    machine: { kind: 'resolvingConflict', lastSaved: A },
    event: { type: 'refetchFailed' },
    next: { kind: 'failed', lastSaved: A },
    effects: [],
  },
  {
    name: 'unmount while resolving a conflict: nobody is left to choose, settles failed (NOT a no-op)',
    machine: { kind: 'resolvingConflict', lastSaved: A },
    event: { type: 'unmount', text: C },
    next: { kind: 'failed', lastSaved: A },
    effects: [],
  },
];

describe('resolvingConflict', () => {
  run(resolvingConflictCases);
});

// ---------------------------------------------------------------------------------------------
// conflict — nothing happens without a click (AC-33)
// ---------------------------------------------------------------------------------------------

const conflictCases: readonly Case[] = [
  {
    name: 'conflict + timerDue is not a choice: no send',
    machine: { kind: 'conflict', lastSaved: A },
    event: { type: 'timerDue', text: C },
    next: { kind: 'conflict', lastSaved: A },
    effects: [],
  },
  {
    name: 'conflict + change is not a choice: no send',
    machine: { kind: 'conflict', lastSaved: A },
    event: { type: 'change', text: C },
    next: { kind: 'conflict', lastSaved: A },
    effects: [],
  },
  {
    name: 'conflict + flush is not a choice: no send',
    machine: { kind: 'conflict', lastSaved: A },
    event: { type: 'flush', text: C },
    next: { kind: 'conflict', lastSaved: A },
    effects: [],
  },
  {
    name: 'conflict + keepMine sends the editor text against the conflict baseline',
    machine: { kind: 'conflict', lastSaved: A },
    event: { type: 'keepMine', text: C },
    next: { kind: 'inFlight', lastSaved: A, sent: C, sendWanted: false, debounceArmed: false },
    effects: [SEND_C],
  },
  {
    name: 'conflict + loadLatest adopts the given text as the new baseline, idle',
    machine: { kind: 'conflict', lastSaved: A },
    event: { type: 'loadLatest', text: C },
    next: { kind: 'idle', lastSaved: C },
    effects: [],
  },
];

describe('conflict', () => {
  run(conflictCases);
});

// ---------------------------------------------------------------------------------------------
// paused — a 429's window
// ---------------------------------------------------------------------------------------------

const pausedCases: readonly Case[] = [
  {
    name: 'paused + timerDue(differs from lastSaved): one more attempt',
    machine: { kind: 'paused', lastSaved: A, retryAfterSeconds: 30 },
    event: { type: 'timerDue', text: B },
    next: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    effects: [SEND_B],
  },
  {
    name: 'paused + timerDue(equal to lastSaved): the window ends with nothing to send',
    machine: { kind: 'paused', lastSaved: A, retryAfterSeconds: 30 },
    event: { type: 'timerDue', text: A },
    next: { kind: 'idle', lastSaved: A },
    effects: [],
  },
  {
    name: 'paused + flush is nothing: a flush cannot jump the 429 window',
    machine: { kind: 'paused', lastSaved: A, retryAfterSeconds: 30 },
    event: { type: 'flush', text: B },
    next: { kind: 'paused', lastSaved: A, retryAfterSeconds: 30 },
    effects: [],
  },
  {
    name: "paused + unmount(differs): still sends — the window is the server's to enforce again",
    machine: { kind: 'paused', lastSaved: A, retryAfterSeconds: 30 },
    event: { type: 'unmount', text: B },
    next: { kind: 'leaving', lastSaved: A, sent: B, wish: null },
    effects: [CLEAR_TIMER, SEND_B],
  },
  {
    name: 'paused + unmount(equal to lastSaved): quiet close, timer cleared',
    machine: { kind: 'paused', lastSaved: A, retryAfterSeconds: 30 },
    event: { type: 'unmount', text: A },
    next: { kind: 'idle', lastSaved: A },
    effects: [CLEAR_TIMER],
  },
];

describe('paused', () => {
  run(pausedCases);
});

// ---------------------------------------------------------------------------------------------
// failed / invalid — "the next change re-arms; Retry sends now" (failed only)
// ---------------------------------------------------------------------------------------------

const failedOrInvalidStarts: readonly {
  readonly name: string;
  readonly machine: AutosaveMachine;
}[] = [
  { name: 'failed', machine: { kind: 'failed', lastSaved: A } },
  { name: 'invalid', machine: { kind: 'invalid', lastSaved: A, problem: PROBLEM } },
];

const failedInvalidChangeCases: readonly Case[] = failedOrInvalidStarts.flatMap(
  ({ name, machine }) => [
    {
      name: `${name} + change(differs from lastSaved) re-arms the debounce`,
      machine,
      event: { type: 'change', text: B },
      next: { kind: 'debouncing', lastSaved: A },
      effects: [ARM_DEBOUNCE],
    },
    {
      name: `${name} + change(back to lastSaved) settles idle`,
      machine,
      event: { type: 'change', text: A },
      next: { kind: 'idle', lastSaved: A },
      effects: [],
    },
  ],
);

const retryCases: readonly Case[] = [
  {
    name: 'failed + retry(differs from lastSaved) sends again',
    machine: { kind: 'failed', lastSaved: A },
    event: { type: 'retry', text: B },
    next: { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false },
    effects: [SEND_B],
  },
  {
    name: 'failed + retry(equal to lastSaved) settles idle without a wasted send',
    machine: { kind: 'failed', lastSaved: A },
    event: { type: 'retry', text: A },
    next: { kind: 'idle', lastSaved: A },
    effects: [],
  },
  {
    name: 'invalid + retry is a no-op: only failed exposes a Retry button (SaveState has no retry() on invalid)',
    machine: { kind: 'invalid', lastSaved: A, problem: PROBLEM },
    event: { type: 'retry', text: B },
    next: { kind: 'invalid', lastSaved: A, problem: PROBLEM },
    effects: [],
  },
];

describe('failed / invalid', () => {
  run([...failedInvalidChangeCases, ...retryCases]);
});

// ---------------------------------------------------------------------------------------------
// expired — terminal under every event (AC-34)
// ---------------------------------------------------------------------------------------------

const expiredEvents: readonly AutosaveEvent[] = [
  { type: 'change', text: B },
  { type: 'timerDue', text: B },
  { type: 'flush', text: B },
  { type: 'retry', text: B },
  { type: 'keepMine', text: B },
  { type: 'loadLatest', text: B },
  { type: 'unmount', text: B },
  { type: 'landed200', textNow: () => B },
  { type: 'landedError', failure: { kind: 'failed' } },
  { type: 'refetched', serverText: B, textNow: () => B },
  { type: 'refetchFailed' },
];

const expiredCases: readonly Case[] = expiredEvents.map((event) => ({
  name: `expired + ${event.type} stays expired, no effects`,
  machine: { kind: 'expired' },
  event,
  next: { kind: 'expired' },
  effects: [],
}));

describe('expired (terminal, AC-34)', () => {
  run(expiredCases);
});

// ---------------------------------------------------------------------------------------------
// leaving
// ---------------------------------------------------------------------------------------------

const leavingCases: readonly Case[] = [
  {
    name: 'leaving + landed200, no wish: settles idle at what was sent',
    machine: { kind: 'leaving', lastSaved: A, sent: B, wish: null },
    event: { type: 'landed200', textNow: () => B },
    next: { kind: 'idle', lastSaved: B },
    effects: [],
  },
  {
    name: 'leaving + landed200, a wish recorded before unmount: sends it once, without reading the destroyed editor',
    machine: { kind: 'leaving', lastSaved: A, sent: B, wish: C },
    event: { type: 'landed200', textNow: () => C },
    next: { kind: 'leaving', lastSaved: B, sent: C, wish: null },
    effects: [SEND_C],
  },
  {
    name: 'leaving + landedError(expired): terminal, no refetch, no choice',
    machine: { kind: 'leaving', lastSaved: A, sent: B, wish: C },
    event: { type: 'landedError', failure: { kind: 'expired' } },
    next: { kind: 'expired' },
    effects: [],
  },
  {
    name: 'leaving + landedError(conflict): no refetch — nobody is left to resolve it, settles failed at lastSaved',
    machine: { kind: 'leaving', lastSaved: A, sent: B, wish: C },
    event: { type: 'landedError', failure: { kind: 'conflict' } },
    next: { kind: 'failed', lastSaved: A },
    effects: [],
  },
  {
    name: 'leaving + landedError(invalid): settles failed at lastSaved, not invalid — no one to show the reason to',
    machine: { kind: 'leaving', lastSaved: A, sent: B, wish: C },
    event: { type: 'landedError', failure: { kind: 'invalid', problem: PROBLEM } },
    next: { kind: 'failed', lastSaved: A },
    effects: [],
  },
  {
    name: 'leaving + landedError(rateLimited): settles failed at lastSaved, no timer armed for a page that is gone',
    machine: { kind: 'leaving', lastSaved: A, sent: B, wish: C },
    event: { type: 'landedError', failure: { kind: 'rateLimited', retryAfterSeconds: 30 } },
    next: { kind: 'failed', lastSaved: A },
    effects: [],
  },
  {
    name: 'leaving + landedError(failed): settles failed at lastSaved',
    machine: { kind: 'leaving', lastSaved: A, sent: B, wish: C },
    event: { type: 'landedError', failure: { kind: 'failed' } },
    next: { kind: 'failed', lastSaved: A },
    effects: [],
  },
];

describe('leaving', () => {
  run(leavingCases);
});

// ---------------------------------------------------------------------------------------------
// Full matrix coverage: every (state, event) pair not exercised above must be the machine's
// default branch — stay, no effects. This is the "no default case" check for a hand enumeration:
// forgetting a real transition above does not silently pass, because the generic assertion below
// only holds for pairs that are genuinely no-ops.
// ---------------------------------------------------------------------------------------------

/** The ten states, exactly as `autosaveMachine.ts`'s docstring and `AutosaveMachine` list them. */
const STATE_KINDS: readonly AutosaveMachine['kind'][] = [
  'idle',
  'debouncing',
  'inFlight',
  'resolvingConflict',
  'conflict',
  'paused',
  'failed',
  'invalid',
  'expired',
  'leaving',
];

/** The eleven events, exactly as `AutosaveEvent` lists them. */
const EVENT_TYPES: readonly AutosaveEvent['type'][] = [
  'change',
  'timerDue',
  'flush',
  'retry',
  'keepMine',
  'loadLatest',
  'unmount',
  'landed200',
  'landedError',
  'refetched',
  'refetchFailed',
];

function canonicalMachine(kind: AutosaveMachine['kind']): AutosaveMachine {
  switch (kind) {
    case 'idle':
      return { kind: 'idle', lastSaved: A };
    case 'debouncing':
      return { kind: 'debouncing', lastSaved: A };
    case 'inFlight':
      return { kind: 'inFlight', lastSaved: A, sent: B, sendWanted: false, debounceArmed: false };
    case 'resolvingConflict':
      return { kind: 'resolvingConflict', lastSaved: A };
    case 'conflict':
      return { kind: 'conflict', lastSaved: A };
    case 'paused':
      return { kind: 'paused', lastSaved: A, retryAfterSeconds: 30 };
    case 'failed':
      return { kind: 'failed', lastSaved: A };
    case 'invalid':
      return { kind: 'invalid', lastSaved: A, problem: PROBLEM };
    case 'expired':
      return { kind: 'expired' };
    case 'leaving':
      return { kind: 'leaving', lastSaved: A, sent: B, wish: null };
  }
}

function canonicalEvent(type: AutosaveEvent['type']): AutosaveEvent {
  const failure: SaveFailure = { kind: 'failed' };
  switch (type) {
    case 'change':
      return { type: 'change', text: C };
    case 'timerDue':
      return { type: 'timerDue', text: C };
    case 'flush':
      return { type: 'flush', text: C };
    case 'retry':
      return { type: 'retry', text: C };
    case 'keepMine':
      return { type: 'keepMine', text: C };
    case 'loadLatest':
      return { type: 'loadLatest', text: C };
    case 'unmount':
      return { type: 'unmount', text: C };
    case 'landed200':
      return { type: 'landed200', textNow: () => C };
    case 'landedError':
      return { type: 'landedError', failure };
    case 'refetched':
      return { type: 'refetched', serverText: C, textNow: () => C };
    case 'refetchFailed':
      return { type: 'refetchFailed' };
  }
}

const ALL_PAIRS: readonly (readonly [AutosaveMachine['kind'], AutosaveEvent['type']])[] =
  STATE_KINDS.flatMap((kind) => EVENT_TYPES.map((type) => [kind, type] as const));

/** Every (state, event) pair the tables above already exercise, regardless of the case's outcome. */
const exercisedPairs = new Set<string>(
  [
    ...idleCases,
    ...debouncingCases,
    ...inFlightCases,
    ...resolvingConflictCases,
    ...conflictCases,
    ...pausedCases,
    ...failedInvalidChangeCases,
    ...retryCases,
    ...expiredCases,
    ...leavingCases,
  ].map((c) => `${c.machine.kind}:${c.event.type}`),
);

const residualPairs = ALL_PAIRS.filter(([kind, type]) => !exercisedPairs.has(`${kind}:${type}`));

describe("every remaining (state, event) pair is the machine's default branch", () => {
  it('the hand-enumerated lists match the docstring\'s "ten states" and "eleven events"', () => {
    expect(STATE_KINDS).toHaveLength(10);
    expect(EVENT_TYPES).toHaveLength(11);
    expect(ALL_PAIRS).toHaveLength(110);
  });

  it.each(residualPairs)('%s + %s is a no-op: same machine, no effects', (kind, type) => {
    const machine = canonicalMachine(kind);
    const event = canonicalEvent(type);
    const result = step(machine, event);
    expect(result.next).toEqual(machine);
    expect(result.effects).toEqual([]);
  });
});
