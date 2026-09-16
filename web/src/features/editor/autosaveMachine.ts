/**
 * The autosave, as one state machine — the pure half of `useDocumentAutosave` (technical plan,
 * "The editor"; /verify slice 1.4, rounds 1–3).
 *
 * **Why a machine.** The first hook kept six facts in six places — the rendered state in a reducer,
 * and the last-saved text, the in-flight text, "another send is wanted", "a `PUT` is on the wire"
 * and the debounce timer each in a ref — and every review round found a new gap *between* two of
 * them: a comparison against the wrong baseline, a guard reading a state React had not committed
 * yet. Six independent facts have far more combinations than the document has situations. Here
 * the situation *is* the state: a document is `idle`, or `debouncing`, or `inFlight`, and the
 * facts that matter in that situation live on that variant and nowhere else. What was saved is
 * carried everywhere; what was sent exists only while something is in flight; a wish to send
 * again exists only while a flight can answer it. A combination that has no meaning has no type.
 *
 * **How to read it.** `step(machine, event)` is the whole decision: given where the document is
 * and what just happened, it returns where the document is now and the *effects* the hook must
 * perform — `send` a `PUT`, `refetch` the run, `armTimer`, `clearTimer`. It never performs them.
 * That is what makes every rule below decidable in a unit test with no editor, no timer and no
 * network: build a machine, apply an event, read the next machine and the effects.
 *
 * **The states.**
 * - `idle` — the editor holds what the server holds (`lastSaved`).
 * - `debouncing` — typed, not yet sent; the debounce timer is armed.
 * - `inFlight` — one `PUT` carrying `sent` is out. `sendWanted` records that a debounce became
 *   due (or a flush happened) while it was out — the answer decides whether that becomes a send.
 *   `debounceArmed` records a debounce timer armed by typing during the flight.
 * - `resolvingConflict` — the `PUT` was refused as stale, and the run is being re-read to tell a
 *   lost response from a real conflict (E-15b vs E-8/E-17).
 * - `conflict` — the server's text differs from ours; nothing happens without a click (AC-33).
 * - `paused` — a 429; the timer is armed for the server's window, and typing is ignored until it
 *   fires.
 * - `failed` — retries are spent; *Retry* or the next change sends again.
 * - `invalid` — the server refused the text (422); the next change sends again.
 * - `expired` — a 401; terminal (AC-34).
 * - `leaving` — the component is gone while a `PUT` was out. `wish` is the text that was on
 *   screen at that moment if it differed from `sent`, captured *then*, because the editor cannot be
 *   read afterwards; a 200 sends it once and a refusal ends things.
 *
 * **The one timer.** The hook owns a single `setTimeout` handle. The machine is its accountant: a
 * timer is pending exactly in `debouncing`, in `paused`, and in `inFlight` when `debounceArmed` —
 * every transition out of those either hands the timer on or emits `clearTimer`. The same timer
 * serves the 1,500 ms debounce (AC-31) and the 429's wait; `timerDue` is what firing means in the
 * state it fires in.
 *
 * **Never two in flight for one document.** The scope on the mutation serialises the two
 * *documents'* saves (AC-32); it is not used to queue a second save of the same document, because a
 * queued `PUT` that runs after a 409's refetch would carry the fresh version and overwrite the
 * other writer's text with no choice shown. So `inFlight` never emits `send`; it records
 * `sendWanted`, and a 200 sends once — if, and only if, the text still differs from what that 200
 * just saved. A 409 drops the wish, and nothing is sent until a click.
 *
 * **Two events read the editor lazily.** `landed200` and `refetched` arrive from a promise, possibly
 * after the component has gone; they carry `textNow` and the machine calls it only in a state
 * where the editor is still there to read — never in `leaving`.
 */
import { AUTOSAVE_DEBOUNCE_MS } from './saveState';

import type { DocumentProblem } from '@/features/tailoring/types';

export type AutosaveMachine =
  | { readonly kind: 'idle'; readonly lastSaved: string }
  | { readonly kind: 'debouncing'; readonly lastSaved: string }
  | {
      readonly kind: 'inFlight';
      readonly lastSaved: string;
      readonly sent: string;
      readonly sendWanted: boolean;
      readonly debounceArmed: boolean;
    }
  | { readonly kind: 'resolvingConflict'; readonly lastSaved: string }
  | { readonly kind: 'conflict'; readonly lastSaved: string }
  | { readonly kind: 'paused'; readonly lastSaved: string; readonly retryAfterSeconds: number }
  | { readonly kind: 'failed'; readonly lastSaved: string }
  | { readonly kind: 'invalid'; readonly lastSaved: string; readonly problem: DocumentProblem }
  | { readonly kind: 'expired' }
  | {
      readonly kind: 'leaving';
      readonly lastSaved: string;
      readonly sent: string;
      readonly wish: string | null;
    };

/** How a refused `PUT` was refused — the hook translates the HTTP answer into this. */
export type SaveFailure =
  /** 409 `document_version_conflict`: the run must be re-read before this is a resolution. */
  | { readonly kind: 'conflict' }
  /** 401: the session is gone. */
  | { readonly kind: 'expired' }
  /** 422 `document_invalid`. */
  | { readonly kind: 'invalid'; readonly problem: DocumentProblem }
  /** 429, with the window the server named. */
  | { readonly kind: 'rateLimited'; readonly retryAfterSeconds: number }
  /** Everything else, after the retries. */
  | { readonly kind: 'failed' };

export type AutosaveEvent =
  /** The editor's `update` event; `text` is the document now. */
  | { readonly type: 'change'; readonly text: string }
  /** The hook's one timer fired. */
  | { readonly type: 'timerDue'; readonly text: string }
  /** A tab switch or `visibilitychange → hidden`: a pending debounce becomes a save now (AC-31). */
  | { readonly type: 'flush'; readonly text: string }
  /** The *Retry* button. */
  | { readonly type: 'retry'; readonly text: string }
  /** *Keep my version* (AC-33). */
  | { readonly type: 'keepMine'; readonly text: string }
  /** *Load the latest version*, after the hook has re-seeded the editor; `text` is the new document. */
  | { readonly type: 'loadLatest'; readonly text: string }
  /** The component is going away; `text` is read now, while the editor still exists. */
  | { readonly type: 'unmount'; readonly text: string }
  /** The `PUT` answered 200. */
  | { readonly type: 'landed200'; readonly textNow: () => string }
  /** The `PUT` was refused, after any retries. */
  | { readonly type: 'landedError'; readonly failure: SaveFailure }
  /** The `refetch` effect completed; `serverText` is the server's text for this document. */
  | {
      readonly type: 'refetched';
      readonly serverText: string | null;
      readonly textNow: () => string;
    }
  /** The `refetch` effect failed. */
  | { readonly type: 'refetchFailed' };

export type AutosaveEffect =
  | { readonly type: 'send'; readonly content: string }
  | { readonly type: 'refetch' }
  | { readonly type: 'armTimer'; readonly ms: number }
  | { readonly type: 'clearTimer' };

export interface Transition {
  readonly next: AutosaveMachine;
  readonly effects: readonly AutosaveEffect[];
}

/** The state the indicator shows — `SaveState` before the hook attaches the callbacks. */
export type AutosaveView =
  | { readonly kind: 'saved' }
  | { readonly kind: 'dirty' }
  | { readonly kind: 'saving' }
  | { readonly kind: 'failed' }
  | { readonly kind: 'conflict' }
  | { readonly kind: 'paused'; readonly retryAfterSeconds: number }
  | { readonly kind: 'invalid'; readonly problem: DocumentProblem }
  | { readonly kind: 'expired' };

const ARM_DEBOUNCE: AutosaveEffect = { type: 'armTimer', ms: AUTOSAVE_DEBOUNCE_MS };
const CLEAR_TIMER: AutosaveEffect = { type: 'clearTimer' };

function stay(machine: AutosaveMachine): Transition {
  return { next: machine, effects: [] };
}

/** One `PUT` goes out carrying `content`; `lastSaved` is what the server held before it. */
function send(
  lastSaved: string,
  content: string,
  before: readonly AutosaveEffect[] = [],
): Transition {
  return {
    next: { kind: 'inFlight', lastSaved, sent: content, sendWanted: false, debounceArmed: false },
    effects: [...before, { type: 'send', content }],
  };
}

/** The wait for a 429's window, never shorter than the debounce (a bare 429 would otherwise spin). */
function pausedFor(retryAfterSeconds: number): AutosaveEffect {
  return { type: 'armTimer', ms: Math.max(retryAfterSeconds * 1000, AUTOSAVE_DEBOUNCE_MS) };
}

/**
 * Where a refused `PUT` leaves a document that was `inFlight`. A 409 is not yet a resolution — the
 * run has to be re-read first — so it becomes `resolvingConflict` with the `refetch` effect.
 */
function refused(lastSaved: string, failure: SaveFailure): Transition {
  switch (failure.kind) {
    case 'conflict':
      return { next: { kind: 'resolvingConflict', lastSaved }, effects: [{ type: 'refetch' }] };
    case 'expired':
      return stay({ kind: 'expired' });
    case 'invalid':
      return stay({ kind: 'invalid', lastSaved, problem: failure.problem });
    case 'rateLimited':
      return {
        next: { kind: 'paused', lastSaved, retryAfterSeconds: failure.retryAfterSeconds },
        effects: [pausedFor(failure.retryAfterSeconds)],
      };
    case 'failed':
      return stay({ kind: 'failed', lastSaved });
  }
}

function stepIdle(machine: AutosaveMachine & { kind: 'idle' }, event: AutosaveEvent): Transition {
  if (event.type === 'change' && event.text !== machine.lastSaved) {
    return { next: { kind: 'debouncing', lastSaved: machine.lastSaved }, effects: [ARM_DEBOUNCE] };
  }
  return stay(machine);
}

function stepDebouncing(
  machine: AutosaveMachine & { kind: 'debouncing' },
  event: AutosaveEvent,
): Transition {
  const { lastSaved } = machine;
  switch (event.type) {
    case 'change':
      // Every keystroke restarts the window; typing back to the saved text closes it.
      return event.text === lastSaved
        ? { next: { kind: 'idle', lastSaved }, effects: [CLEAR_TIMER] }
        : { next: machine, effects: [ARM_DEBOUNCE] };
    case 'timerDue':
      return event.text === lastSaved
        ? stay({ kind: 'idle', lastSaved })
        : send(lastSaved, event.text);
    case 'flush':
      return event.text === lastSaved
        ? { next: { kind: 'idle', lastSaved }, effects: [CLEAR_TIMER] }
        : send(lastSaved, event.text, [CLEAR_TIMER]);
    case 'unmount':
      // The editor is about to be destroyed: the text is captured now and sent now.
      return event.text === lastSaved
        ? { next: { kind: 'idle', lastSaved }, effects: [CLEAR_TIMER] }
        : {
            next: { kind: 'leaving', lastSaved, sent: event.text, wish: null },
            effects: [CLEAR_TIMER, { type: 'send', content: event.text }],
          };
    default:
      return stay(machine);
  }
}

function stepInFlight(
  machine: AutosaveMachine & { kind: 'inFlight' },
  event: AutosaveEvent,
): Transition {
  const { lastSaved, sent, sendWanted, debounceArmed } = machine;
  // The effect a transition out of this state owes for the timer typing may have armed.
  const owed: readonly AutosaveEffect[] = debounceArmed ? [CLEAR_TIMER] : [];
  switch (event.type) {
    case 'change':
      // "Differs" is measured against the text on the wire: that is what the server will hold if
      // the flight lands, so typing back to the *previous* save is a change that needs sending,
      // and typing back to exactly what was sent is not.
      if (event.text !== sent) {
        return { next: { ...machine, debounceArmed: true }, effects: [ARM_DEBOUNCE] };
      }
      return { next: { ...machine, debounceArmed: false }, effects: owed };
    case 'timerDue':
      return stay({ ...machine, sendWanted: true, debounceArmed: false });
    case 'flush':
      // Only a pending debounce has anything to flush; a flush with nothing armed is nothing.
      return debounceArmed
        ? { next: { ...machine, sendWanted: true, debounceArmed: false }, effects: [CLEAR_TIMER] }
        : stay(machine);
    case 'landed200': {
      // What was sent is now what is saved; the question is what the editor holds against that.
      const text = event.textNow();
      if (text === sent) {
        return { next: { kind: 'idle', lastSaved: sent }, effects: owed };
      }
      if (sendWanted) {
        // The one send a wish becomes: the latest text, against the version this 200 returned.
        return send(sent, text, owed);
      }
      if (debounceArmed) {
        // The timer typing armed keeps running and sends when it is due.
        return stay({ kind: 'debouncing', lastSaved: sent });
      }
      return { next: { kind: 'debouncing', lastSaved: sent }, effects: [ARM_DEBOUNCE] };
    }
    case 'landedError': {
      // A refusal drops the wish: nothing is sent until the next change or a click.
      const resolution = refused(lastSaved, event.failure);
      return { next: resolution.next, effects: [...owed, ...resolution.effects] };
    }
    case 'unmount':
      return {
        next: { kind: 'leaving', lastSaved, sent, wish: event.text === sent ? null : event.text },
        effects: owed,
      };
    default:
      return stay(machine);
  }
}

function stepResolvingConflict(
  machine: AutosaveMachine & { kind: 'resolvingConflict' },
  event: AutosaveEvent,
): Transition {
  const { lastSaved } = machine;
  switch (event.type) {
    case 'refetched': {
      // E-15b: the server already holds our text, so the 200 was lost on the way back — adopt
      // the version (the refetch put it in the cache) and there is nothing to send.
      const text = event.textNow();
      return event.serverText === text
        ? stay({ kind: 'idle', lastSaved: text })
        : stay({ kind: 'conflict', lastSaved });
    }
    case 'refetchFailed':
      return stay({ kind: 'failed', lastSaved });
    case 'unmount':
      // The save was refused and nobody is left to choose between the two texts.
      return stay({ kind: 'failed', lastSaved });
    default:
      return stay(machine);
  }
}

function stepConflict(
  machine: AutosaveMachine & { kind: 'conflict' },
  event: AutosaveEvent,
): Transition {
  switch (event.type) {
    case 'keepMine':
      // Against the version the refetch revealed — the hook reads it from the cache at send time.
      return send(machine.lastSaved, event.text);
    case 'loadLatest':
      return stay({ kind: 'idle', lastSaved: event.text });
    default:
      // A keystroke is not a choice (AC-33), and neither is a timer, a flush or an unmount.
      return stay(machine);
  }
}

function stepPaused(
  machine: AutosaveMachine & { kind: 'paused' },
  event: AutosaveEvent,
): Transition {
  const { lastSaved } = machine;
  switch (event.type) {
    case 'timerDue':
      // The window is over: one more attempt, reading the editor as it is now.
      return event.text === lastSaved
        ? stay({ kind: 'idle', lastSaved })
        : send(lastSaved, event.text);
    case 'unmount':
      // The person is leaving with text the 429 kept from being saved. It is sent now, inside the
      // window the server named, so this one `PUT` may be refused again — and then the text is
      // gone: `leaving` has no timer and nobody to show *Retry* to. Sending is still the better
      // odds, because not sending loses the text for certain. If a paused document has to survive
      // its author leaving, the model is a `leaving` variant that keeps the timer and sends when
      // the window closes; nothing here is built for that yet.
      return event.text === lastSaved
        ? { next: { kind: 'idle', lastSaved }, effects: [CLEAR_TIMER] }
        : {
            next: { kind: 'leaving', lastSaved, sent: event.text, wish: null },
            effects: [CLEAR_TIMER, { type: 'send', content: event.text }],
          };
    default:
      // Typing and flushing wait for the window; a flush that ignored a 429 would earn another.
      return stay(machine);
  }
}

/** `failed` and `invalid` behave alike: the next change re-arms the debounce; *Retry* sends now. */
function stepSettledRefusal(
  machine: AutosaveMachine & { kind: 'failed' | 'invalid' },
  event: AutosaveEvent,
): Transition {
  const { lastSaved } = machine;
  switch (event.type) {
    case 'change':
      return event.text === lastSaved
        ? stay({ kind: 'idle', lastSaved })
        : { next: { kind: 'debouncing', lastSaved }, effects: [ARM_DEBOUNCE] };
    case 'retry':
      if (machine.kind !== 'failed') {
        return stay(machine);
      }
      return event.text === lastSaved
        ? stay({ kind: 'idle', lastSaved })
        : send(lastSaved, event.text);
    default:
      return stay(machine);
  }
}

function stepLeaving(
  machine: AutosaveMachine & { kind: 'leaving' },
  event: AutosaveEvent,
): Transition {
  const { sent, wish } = machine;
  switch (event.type) {
    case 'landed200':
      // `wish` was captured on unmount and differs from `sent` by construction, so it is sent
      // without reading an editor that no longer exists.
      return wish === null
        ? stay({ kind: 'idle', lastSaved: sent })
        : {
            next: { kind: 'leaving', lastSaved: sent, sent: wish, wish: null },
            effects: [{ type: 'send', content: wish }],
          };
    case 'landedError':
      // No refetch, no timer, no choice: there is no one to show any of them to.
      return stay(
        event.failure.kind === 'expired'
          ? { kind: 'expired' }
          : { kind: 'failed', lastSaved: machine.lastSaved },
      );
    default:
      return stay(machine);
  }
}

/**
 * The autosave's whole decision: where the document is now, and what the hook must do about it.
 * Pure — it reads the editor only through the `text` an event carries (or `textNow`, lazily), and
 * performs nothing.
 */
export function step(machine: AutosaveMachine, event: AutosaveEvent): Transition {
  switch (machine.kind) {
    case 'idle':
      return stepIdle(machine, event);
    case 'debouncing':
      return stepDebouncing(machine, event);
    case 'inFlight':
      return stepInFlight(machine, event);
    case 'resolvingConflict':
      return stepResolvingConflict(machine, event);
    case 'conflict':
      return stepConflict(machine, event);
    case 'paused':
      return stepPaused(machine, event);
    case 'failed':
    case 'invalid':
      return stepSettledRefusal(machine, event);
    case 'expired':
      // Terminal: nothing after a 401 can change it (AC-34).
      return stay(machine);
    case 'leaving':
      return stepLeaving(machine, event);
  }
}

/** The machine as the indicator shows it. `leaving` is `saving`: a `PUT` is out, for a page that is gone. */
export function viewOf(machine: AutosaveMachine): AutosaveView {
  switch (machine.kind) {
    case 'idle':
      return { kind: 'saved' };
    case 'debouncing':
      return { kind: 'dirty' };
    case 'inFlight':
    case 'resolvingConflict':
    case 'leaving':
      return { kind: 'saving' };
    case 'conflict':
      return { kind: 'conflict' };
    case 'paused':
      return { kind: 'paused', retryAfterSeconds: machine.retryAfterSeconds };
    case 'failed':
      return { kind: 'failed' };
    case 'invalid':
      return { kind: 'invalid', problem: machine.problem };
    case 'expired':
      return { kind: 'expired' };
  }
}

/**
 * Whether two views would render the same thing — so a step that changes the machine without
 * changing what is shown (a wish recorded during a flight) hands React the same snapshot and
 * causes no render.
 */
export function sameView(a: AutosaveView, b: AutosaveView): boolean {
  if (a.kind !== b.kind) {
    return false;
  }
  if (a.kind === 'paused' && b.kind === 'paused') {
    return a.retryAfterSeconds === b.retryAfterSeconds;
  }
  if (a.kind === 'invalid' && b.kind === 'invalid') {
    return a.problem === b.problem;
  }
  return true;
}
