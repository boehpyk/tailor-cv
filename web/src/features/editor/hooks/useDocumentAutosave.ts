import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react';

import { ApiError } from '@/api/client';
import { reviseTailoredDocument } from '@/api/tailoringRuns';
import {
  tailoringRunQueryKey,
  tailoringRunQueryOptions,
} from '@/features/tailoring/hooks/useTailoringRun';
import { tailoringRunsQueryKey } from '@/features/tailoring/hooks/useTailoringRuns';

import { AUTOSAVE_DEBOUNCE_MS, documentProblemCopy } from '../saveState';

import type { DocumentEditorHandle } from './useDocumentEditor';
import type { SaveState } from '../saveState';
import type {
  DocumentProblem,
  TailoredDocumentKind,
  TailoringRun,
} from '@/features/tailoring/types';

export interface DocumentAutosaveOptions {
  /**
   * Whether this document's pane is the one on screen. Going from `true` to `false` — the tab
   * switch — flushes a pending save at once instead of waiting out the debounce (AC-31).
   */
  readonly visible: boolean;
}

/** How many times a transient failure is retried before the save resolves to `failed` (E-15a). */
const MAX_TRANSIENT_RETRIES = 3;

/**
 * How long a 429 without a usable `Retry-After` pauses saving. Every TailorCraft 429 carries the
 * header, so this is the answer for a proxy's or a future endpoint's bare 429 — and a minute is
 * long enough not to hammer a limiter and short enough that a person still typing gets their
 * save.
 */
const RATE_LIMIT_FALLBACK_SECONDS = 60;

/**
 * The reducer's state: `SaveState` with its callbacks stripped. The callbacks (`retry`,
 * `loadLatest`, `keepMine`) close over the hook's refs and are attached in render; what the
 * reducer holds is the plain fact of where the save stands, so the reducer stays pure and the
 * state stays comparable.
 */
type Resolution =
  | { readonly kind: 'saved' }
  | { readonly kind: 'dirty' }
  | { readonly kind: 'saving' }
  | { readonly kind: 'failed' }
  | { readonly kind: 'conflict' }
  | { readonly kind: 'paused'; readonly retryAfterSeconds: number }
  | { readonly kind: 'invalid'; readonly problem: DocumentProblem }
  | { readonly kind: 'expired' };

type Action =
  /** The editor's `update` event: `differs` is whether the text now differs from the last save. */
  | { readonly type: 'changed'; readonly differs: boolean }
  /** A `PUT` was handed to the mutation (it may be queued behind another for this run, AC-32). */
  | { readonly type: 'sent' }
  /** The mutation settled, or a choice was made; `to` is where that leaves the document. */
  | { readonly type: 'resolved'; readonly to: Resolution };

/**
 * `expired` is terminal: nothing after a 401 can change it, because the session that owned the
 * run is gone (AC-34). `conflict` ignores typing: the person must choose between the two texts,
 * and a keystroke is not a choice (AC-33). `saving` ignores typing too — a `PUT` is on the wire
 * and its resolution decides whether the document is `saved` or `dirty` again, by comparing what
 * was sent with what is there now.
 */
function reduce(state: Resolution, action: Action): Resolution {
  if (state.kind === 'expired') {
    return state;
  }
  switch (action.type) {
    case 'changed':
      if (state.kind === 'conflict' || state.kind === 'saving' || state.kind === 'paused') {
        return state;
      }
      return action.differs ? { kind: 'dirty' } : { kind: 'saved' };
    case 'sent':
      return { kind: 'saving' };
    case 'resolved':
      return action.to;
  }
}

/** The `TailoringRun` field that holds `kind`'s current text. */
function textFieldOf(kind: TailoredDocumentKind): 'tailored_cv' | 'cover_letter' {
  return kind === 'cv' ? 'tailored_cv' : 'cover_letter';
}

/**
 * A 4xx `ApiError` is an answer the server will repeat; a network failure or a 5xx may not be.
 * The same line `useTailoringRun` draws for reads, drawn here for the write (E-15a: "network
 * errors and 5xx only, never 4xx").
 */
function isTransient(error: Error): boolean {
  return !(error instanceof ApiError && error.status >= 400 && error.status < 500);
}

/** Narrow the 422 envelope's `problem` towards the closed set, as `activeTailoringRunId` does. */
function isDocumentProblem(value: unknown): value is DocumentProblem {
  return typeof value === 'string' && Object.hasOwn(documentProblemCopy, value);
}

/**
 * A save's variables. `content` is captured **only** when the editor is about to go away (an
 * unmount flush); every other save reads the document at send time, because a `PUT` queued
 * behind another for the same run should carry the text as it is when its turn comes.
 */
interface SaveVariables {
  readonly content?: string;
}

/**
 * Keep one document saved — the slice's teaching hook (technical plan, "The editor").
 *
 * **What it owns, and where each thing lives.** The `PUT` is a `useMutation` **scoped per run**
 * (`scope: { id: 'revise:<runId>' }`): TanStack runs mutations in one scope serially, which is the
 * whole of AC-32 — with both documents dirty, the letter's `PUT` waits for the CV's to settle and
 * then reads the version *that one returned*, because `expected_version` is read from the query
 * cache **at send time** inside `mutationFn`, never captured in a closure at render. The debounce
 * timer is a ref (outside React); the last-saved text is a ref (a fact taken at one moment); the
 * save state is a `useReducer`, because it is *client* state about a *client* process and not a
 * copy of anything the server holds. The run itself stays in TanStack Query under
 * `tailoringRunQueryKey(runId)`.
 *
 * **This hook observes the run's key** (`useQuery` with `enabled: false`) without ever fetching
 * it. The poller on the run page is the one that fetches; this observer says "the run must stay
 * cached while this document is open", which is exactly true, and a query with no observer is
 * garbage-collected on its `gcTime` regardless of who intends to read it later.
 *
 * **The one sanctioned cache write.** `onSuccess` calls `setQueryData` with the response —
 * contrast `useCreateJobPosting`, whose docstring forbids exactly this, and the difference is the
 * argument: there, the `POST` returns a full `JobPosting` while the list cache holds
 * `JobPostingSummary`, so a write would put the wrong shape in the cache. Here the `PUT` returns
 * **the whole run in the same shape the poller reads**, under the same key: the response *is* the
 * resource the key holds. The run list, whose items are summaries with counts that just changed,
 * is invalidated rather than written, for `useCreateJobPosting`'s reason.
 *
 * **The `useEffect`s are the legitimate kind, each synchronizing with something outside React:**
 * the editor's `update` event (an instance with its own event emitter), `visibilitychange` on the
 * document, the pane's visibility changing under a URL the router owns, and the timer's cleanup.
 * None fetches, none derives.
 *
 * **Resolutions** (AC-33, AC-34): 200 → `saved`, or `dirty` if the text moved on during the
 * flight; 409 `document_version_conflict` → re-read the run, and if the server's text for this
 * document equals ours the response was simply lost (E-15b): adopt the version, `saved`, no second
 * `PUT` — otherwise `conflict`, with the two choices and nothing until one is clicked; 401 →
 * `expired`, terminal, and the container makes both editors read-only from it (the session is
 * the run's, not this document's); 422 `document_invalid` → `invalid(problem)`, waiting
 * for the next change; 429 → `paused` for the header's `Retry-After`, then one more attempt;
 * everything else, after three retries with backoff for the transient cases → `failed` with
 * *Retry*. Whatever happens, **the text is never touched**: the document lives in the editor, and
 * only `loadLatest` — a click — replaces it.
 */
export function useDocumentAutosave(
  runId: string,
  kind: TailoredDocumentKind,
  handle: DocumentEditorHandle,
  options: DocumentAutosaveOptions = { visible: true },
): SaveState {
  const queryClient = useQueryClient();
  const queryKey = tailoringRunQueryKey(runId);
  const textField = textFieldOf(kind);

  useQuery({ ...tailoringRunQueryOptions(runId), enabled: false });

  const [state, dispatch] = useReducer(reduce, { kind: 'saved' });
  // The latest state, for callbacks that run outside a render (the timer, the event handlers).
  const stateRef = useRef<Resolution>(state);
  stateRef.current = state;

  // What the server holds for this document, as far as this client knows — initially the seed's
  // own serialization, so that opening is not dirtying (AC-29) even where the bridge normalised.
  const lastSavedRef = useRef<string | null>(null);
  lastSavedRef.current ??= handle.serialize();
  // The text of the `PUT` in flight, so its success can move `lastSavedRef` to exactly that.
  const sentRef = useRef<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const { mutate } = useMutation({
    scope: { id: `revise:${runId}` },
    mutationFn: (variables: SaveVariables) => {
      const run = queryClient.getQueryData<TailoringRun>(queryKey);
      if (run === undefined) {
        // Unreachable while this hook's own observer is mounted; a plain failure rather than a
        // request with a guessed version, which the server would rightly refuse.
        return Promise.reject(new Error('useDocumentAutosave: the run is not in the cache.'));
      }
      const content = variables.content ?? handle.serialize();
      sentRef.current = content;
      return reviseTailoredDocument(runId, kind, { content, expected_version: run.version });
    },
    retry: (failureCount, error) => isTransient(error) && failureCount < MAX_TRANSIENT_RETRIES,
    onSuccess: (run) => {
      lastSavedRef.current = sentRef.current;
      queryClient.setQueryData(queryKey, run);
      void queryClient.invalidateQueries({ queryKey: tailoringRunsQueryKey });
      const differs = handle.serialize() !== lastSavedRef.current;
      dispatch({ type: 'resolved', to: differs ? { kind: 'dirty' } : { kind: 'saved' } });
    },
    onError: async (error) => {
      if (!(error instanceof ApiError)) {
        dispatch({ type: 'resolved', to: { kind: 'failed' } });
        return;
      }
      if (error.status === 401) {
        dispatch({ type: 'resolved', to: { kind: 'expired' } });
        return;
      }
      if (error.status === 409 && error.code === 'document_version_conflict') {
        let server: TailoringRun;
        try {
          server = await queryClient.query({ ...tailoringRunQueryOptions(runId), staleTime: 0 });
        } catch {
          dispatch({ type: 'resolved', to: { kind: 'failed' } });
          return;
        }
        const mine = handle.serialize();
        if (server[textField] === mine) {
          lastSavedRef.current = mine;
          dispatch({ type: 'resolved', to: { kind: 'saved' } });
        } else {
          dispatch({ type: 'resolved', to: { kind: 'conflict' } });
        }
        return;
      }
      if (error.status === 422 && error.code === 'document_invalid') {
        const problem: unknown = error.details['problem'];
        if (isDocumentProblem(problem)) {
          dispatch({ type: 'resolved', to: { kind: 'invalid', problem } });
          return;
        }
      }
      if (error.status === 429) {
        const retryAfterSeconds = error.retryAfterSeconds ?? RATE_LIMIT_FALLBACK_SECONDS;
        dispatch({ type: 'resolved', to: { kind: 'paused', retryAfterSeconds } });
        return;
      }
      dispatch({ type: 'resolved', to: { kind: 'failed' } });
    },
  });

  /** Hand a `PUT` to the mutation, unconditionally. */
  const submit = useCallback(
    (variables: SaveVariables = {}) => {
      dispatch({ type: 'sent' });
      mutate(variables);
    },
    [mutate],
  );

  /** Save if there is something to save and the state allows it. */
  const send = useCallback(() => {
    const current = stateRef.current;
    if (current.kind === 'expired' || current.kind === 'conflict') {
      return;
    }
    if (handle.serialize() === lastSavedRef.current) {
      if (current.kind !== 'saving') {
        dispatch({ type: 'resolved', to: { kind: 'saved' } });
      }
      return;
    }
    submit();
  }, [handle, submit]);

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const arm = useCallback(
    (ms: number) => {
      clearTimer();
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        send();
      }, ms);
    },
    [clearTimer, send],
  );

  /** A pending debounce becomes a save now (AC-31's two flush triggers, and the unmount). */
  const flush = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimer();
      send();
    }
  }, [clearTimer, send]);

  // The editor's `update` event arms the debounce — subscribing to an instance outside React.
  useEffect(() => {
    const editor = handle.editor;
    if (editor === null) {
      return;
    }
    const onUpdate = (): void => {
      const differs = handle.serialize() !== lastSavedRef.current;
      dispatch({ type: 'changed', differs });
      const current = stateRef.current;
      if (current.kind === 'expired' || current.kind === 'conflict' || current.kind === 'paused') {
        return;
      }
      if (differs) {
        arm(AUTOSAVE_DEBOUNCE_MS);
      } else {
        clearTimer();
      }
    };
    editor.on('update', onUpdate);
    return () => {
      editor.off('update', onUpdate);
    };
  }, [handle, arm, clearTimer]);

  // A 429 waits out the server's window, then tries once more.
  useEffect(() => {
    if (state.kind === 'paused') {
      arm(Math.max(state.retryAfterSeconds * 1000, AUTOSAVE_DEBOUNCE_MS));
    }
  }, [state, arm]);

  // The tab going to the background flushes: a hidden tab may be a closing one.
  useEffect(() => {
    const onVisibilityChange = (): void => {
      if (document.visibilityState === 'hidden') {
        flush();
      }
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [flush]);

  // Switching to the other document flushes this one (AC-31): the URL changed under the router,
  // and the pane that just left the screen should not still be waiting out its debounce.
  useEffect(() => {
    if (!options.visible) {
      flush();
    }
  }, [options.visible, flush]);

  // Leaving the run page altogether: the editor is about to be destroyed, so the text is captured
  // now and the `PUT` is handed to the mutation cache, which outlives this component. The timer
  // is cleared either way — a timer that outlives its component is a save into a dead editor.
  useEffect(() => {
    return () => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
        const current = stateRef.current;
        if (current.kind !== 'expired' && current.kind !== 'conflict') {
          const content = handle.serialize();
          if (content !== lastSavedRef.current) {
            mutate({ content });
          }
        }
      }
    };
  }, [handle, mutate]);

  const loadLatest = useCallback(() => {
    const server = queryClient.getQueryData<TailoringRun>(queryKey);
    if (server === undefined) {
      return;
    }
    handle.reseed(server[textField] ?? '');
    lastSavedRef.current = handle.serialize();
    dispatch({ type: 'resolved', to: { kind: 'saved' } });
  }, [handle, queryClient, queryKey, textField]);

  const keepMine = useCallback(() => {
    submit();
  }, [submit]);

  return useMemo<SaveState>(() => {
    switch (state.kind) {
      case 'failed':
        return { kind: 'failed', retry: send };
      case 'conflict':
        return { kind: 'conflict', loadLatest, keepMine };
      case 'saved':
      case 'dirty':
      case 'saving':
      case 'paused':
      case 'invalid':
      case 'expired':
        return state;
    }
  }, [state, send, loadLatest, keepMine]);
}
