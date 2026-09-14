/**
 * The autosave's state, as the indicator shows it and the workspace acts on it (AC-31, AC-33,
 * AC-34).
 *
 * A **discriminated union**, not a `status` string beside a set of optional fields: `retry` exists
 * only on `failed`, the two conflict choices only on `conflict`, `retryAfterSeconds` only on
 * `paused`, `problem` only on `invalid`. A shape where all four were optional would let a
 * component render a Retry button in the `saving` state — the exact "still working vs failed"
 * confusion AC-31 forbids for writes.
 *
 * This is **client state about a client process** (the save), not a copy of server state: the
 * run itself stays in TanStack Query under `tailoringRunQueryKey(runId)`, and nothing here holds a
 * document, a version or a run.
 */
import type { DocumentProblem } from '@/features/tailoring/types';

export type SaveState =
  /** The editor holds what the server holds. */
  | { readonly kind: 'saved' }
  /** Typed, not yet sent — the debounce window is open. */
  | { readonly kind: 'dirty' }
  /** A `PUT` is in flight (or queued behind another for the same run, AC-32). */
  | { readonly kind: 'saving' }
  /** Retries are spent (network error or 5xx, E-15a). `retry` sends again. */
  | { readonly kind: 'failed'; readonly retry: () => void }
  /** 409 and the server's text differs from ours (E-8, E-17). Nothing happens without a click. */
  | { readonly kind: 'conflict'; readonly loadLatest: () => void; readonly keepMine: () => void }
  /** 429 — the save limiter said wait. */
  | { readonly kind: 'paused'; readonly retryAfterSeconds: number }
  /** 422 `document_invalid` — the server refused the text; waits for the next change. */
  | { readonly kind: 'invalid'; readonly problem: DocumentProblem }
  /** 401 — the session is gone; the editor is read-only and the text stays on screen (AC-34). */
  | { readonly kind: 'expired' };

/**
 * The debounce after the last change before a save is sent (AC-31). Also flushed early by a tab
 * switch and by `visibilitychange → hidden`.
 */
export const AUTOSAVE_DEBOUNCE_MS = 1500;

/**
 * One sentence per state — the eight strings of AC-31. Typed as a `Record` over the union's
 * discriminant so a ninth state is a compile error at the one place that must name it.
 *
 * Skeleton (F8): every value is empty. F10c fills them; the test asserts the strings, and that
 * *Saving…* is not a substring of *Couldn't save*.
 */
export const saveStateCopy: Readonly<Record<SaveState['kind'], string>> = {
  saved: '',
  dirty: '',
  saving: '',
  failed: '',
  conflict: '',
  paused: '',
  invalid: '',
  expired: '',
};
