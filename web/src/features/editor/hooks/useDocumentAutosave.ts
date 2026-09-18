import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from 'react';

import { ApiError } from '@/api/client';
import { reviseTailoredDocument } from '@/api/tailoringRuns';
import { exportJobsQueryKey } from '@/features/export/hooks/useExportJobs';
import {
  tailoringRunQueryKey,
  tailoringRunQueryOptions,
} from '@/features/tailoring/hooks/useTailoringRun';
import { tailoringRunsQueryKey } from '@/features/tailoring/hooks/useTailoringRuns';

import { sameView, step, viewOf } from '../autosaveMachine';
import { documentProblemCopy } from '../saveState';

import type {
  AutosaveEffect,
  AutosaveEvent,
  AutosaveMachine,
  AutosaveView,
  SaveFailure,
} from '../autosaveMachine';
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
 * The HTTP answer, translated into the machine's language. This is the boundary: past here
 * nothing knows a status code, and the machine decides what a refusal means for the document.
 */
function failureOf(error: Error): SaveFailure {
  if (!(error instanceof ApiError)) {
    return { kind: 'failed' };
  }
  if (error.status === 401) {
    return { kind: 'expired' };
  }
  if (error.status === 409 && error.code === 'document_version_conflict') {
    return { kind: 'conflict' };
  }
  if (error.status === 422 && error.code === 'document_invalid') {
    const problem: unknown = error.details['problem'];
    if (isDocumentProblem(problem)) {
      return { kind: 'invalid', problem };
    }
  }
  if (error.status === 429) {
    return {
      kind: 'rateLimited',
      retryAfterSeconds: error.retryAfterSeconds ?? RATE_LIMIT_FALLBACK_SECONDS,
    };
  }
  return { kind: 'failed' };
}

/**
 * What performing an effect needs from the world outside the machine: the editor to read, a way
 * to send a `PUT`, and a way to re-read the run. Each comes from a render — the editor handle is
 * a hook's return, the sender wraps `useMutation`'s `mutate` — so the store, which outlives every
 * render, reads them through a ref the hook refreshes after each commit rather than capturing
 * them at construction. A timer firing a minute after a re-render reaches the current ones.
 */
interface AutosaveCollaborators {
  readonly handle: DocumentEditorHandle;
  /** Perform the `send` effect: one `PUT` carrying `content`. */
  readonly send: (content: string) => void;
  /** Perform the `refetch` effect: the server's text for this document, freshly read. */
  readonly refetch: () => Promise<string | null>;
}

/**
 * The machine, held where callbacks can reach it and React can subscribe to it. `dispatch` steps
 * the machine and performs the effects it returns; `view` is the snapshot React renders, replaced
 * only when what it shows would change, so `useSyncExternalStore` sees a stable value between
 * meaningful changes.
 *
 * **`dispatch` is created once, with the store, and its identity is a fact rather than a cache.**
 * The hook's unmount cleanup fires an `unmount` event, which steps a document with a `PUT` out
 * into `leaving` — a state where the editor is assumed gone and later keystrokes are ignored. So
 * that cleanup must run for exactly one reason. An earlier version built `dispatch` with `useMemo`
 * and listed it as that effect's dependency; React documents `useMemo` as a performance hint it
 * may discard, and a recomputation would have unmounted a document that was still on screen.
 * Owning `dispatch` here, in a closure the hook creates once in a ref, is what makes "the same
 * `dispatch` for the life of the component" true by construction.
 *
 * **The one timer lives here too.** The machine is its accountant (see `autosaveMachine.ts`); the
 * store is its holder, because the timer's callback dispatches and the store is what dispatches.
 */
interface AutosaveStore {
  readonly dispatch: (event: AutosaveEvent) => void;
  readonly view: () => AutosaveView;
  readonly subscribe: (listener: () => void) => () => void;
}

function createAutosaveStore(
  initial: AutosaveMachine,
  collaborators: { readonly current: AutosaveCollaborators },
): AutosaveStore {
  let machine = initial;
  let view = viewOf(machine);
  let timer: ReturnType<typeof setTimeout> | null = null;
  const listeners = new Set<() => void>();

  function clearTimer(): void {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  // The editor is read when the event happens, never earlier: a timer or a promise settles long
  // after the render that armed it.
  function textNow(): string {
    return collaborators.current.handle.serialize();
  }

  function perform(effect: AutosaveEffect): void {
    switch (effect.type) {
      case 'send':
        collaborators.current.send(effect.content);
        return;
      case 'refetch':
        void collaborators.current.refetch().then(
          (serverText) => {
            dispatch({ type: 'refetched', serverText, textNow });
          },
          () => {
            dispatch({ type: 'refetchFailed' });
          },
        );
        return;
      case 'armTimer':
        clearTimer();
        timer = setTimeout(() => {
          timer = null;
          dispatch({ type: 'timerDue', text: textNow() });
        }, effect.ms);
        return;
      case 'clearTimer':
        clearTimer();
        return;
    }
  }

  function dispatch(event: AutosaveEvent): void {
    const { next, effects } = step(machine, event);
    machine = next;
    const nextView = viewOf(next);
    if (!sameView(view, nextView)) {
      view = nextView;
      listeners.forEach((listener) => {
        listener();
      });
    }
    for (const effect of effects) {
      perform(effect);
    }
  }

  return {
    dispatch,
    view: () => view,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/**
 * Keep one document saved — the slice's teaching hook (technical plan, "The editor").
 *
 * **The decision lives in `autosaveMachine.ts`; this hook performs it.** Everything that happens
 * to the document — a keystroke, the timer firing, a tab switch, a `PUT` answering, a click on
 * *Retry* or *Keep my version*, the component going away — is an `AutosaveEvent` handed to
 * `step`, which returns the next state and a list of effects. The hook's job is the boring half:
 * read the editor to build the event, perform the effects (`send` becomes `mutate`, `refetch`
 * becomes a query, `armTimer`/`clearTimer` become the one `setTimeout`), and give React something
 * to render. Read the machine's docstring for the states and the rules; read this one for how it
 * is wired.
 *
 * **Where the machine lives, and why React subscribes to it rather than owning it.** The machine
 * is stepped from places React does not schedule — a timer, a promise settling, a DOM event — and
 * read back in the same places, synchronously. That is the definition of state *outside* React,
 * and `useSyncExternalStore` is the hook made for it: the store holds the machine in a closure
 * created once in a ref, and React subscribes to a snapshot of it. The alternative — mirroring the
 * state into `useReducer` and reading a ref that a layout effect copies it into — is what the
 * first version did, and the review found the gap in it: a timer due between the promise settling
 * and React committing read a state the machine had already left. Here there is no copy to lag; a
 * callback reads the machine, and the render reads the last snapshot the machine published.
 *
 * **The store performs the effects too, and reads the world through a ref.** `send` needs
 * `mutate`, `refetch` needs the query client, the timer needs the editor — all things a render
 * hands out. Rather than rebuild the performer whenever one of them might change (which is what a
 * `useMemo` over them would do, and `useMemo` is a cache React may drop at will), the store reads
 * them from a ref the hook refreshes after every commit. `dispatch`'s identity is therefore a
 * fact of construction, which the unmount effect below relies on; the reason is spelled out there.
 *
 * **What TanStack still owns.** The `PUT` is a `useMutation` **scoped per run**
 * (`scope: { id: 'revise:<runId>' }`): TanStack runs mutations in one scope serially, which is
 * AC-32 — with both documents dirty, the letter's `PUT` waits for the CV's and then reads the
 * version *that one returned*, because `expected_version` is read from the query cache **at send
 * time** inside `mutationFn`, never captured in a closure at render. The scope serialises the two
 * documents; it is *not* how a second save of the same document is queued — the machine never
 * sends while one is in flight, for the reason its docstring gives (a queued `PUT` after a 409
 * would silently win). `retry` handles the transient cases with backoff; the machine sees only the
 * final answer.
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
 * document, the pane's visibility changing under a URL the router owns, and the unmount. None
 * fetches, none derives. The unmount cleanup hands the machine an `unmount` event and nothing
 * else: what to do — send the text now, or remember it for when the flight lands — is the
 * machine's decision, and in a quiet state it is nothing, which is what keeps StrictMode's
 * mount-unmount-mount rehearsal in development harmless.
 *
 * **What the text is, and when it is read.** Every event that needs the document carries it,
 * read from the editor at the moment the event happens. Two events arrive from a promise and may
 * arrive after the component is gone — a `PUT` landing, the 409's refetch completing — and carry
 * `textNow` instead, a function the machine calls only in a state where the editor still exists.
 * After an unmount the machine is `leaving`, holding the text it captured while the editor was
 * still there, and reads nothing.
 */
export function useDocumentAutosave(
  runId: string,
  kind: TailoredDocumentKind,
  handle: DocumentEditorHandle,
  options: DocumentAutosaveOptions = { visible: true },
): SaveState {
  const queryClient = useQueryClient();
  const textField = textFieldOf(kind);

  useQuery({ ...tailoringRunQueryOptions(runId), enabled: false });

  const { mutate } = useMutation({
    scope: { id: `revise:${runId}` },
    mutationFn: ({ content }: { readonly content: string }) => {
      const run = queryClient.getQueryData<TailoringRun>(tailoringRunQueryKey(runId));
      if (run === undefined) {
        // Unreachable while this hook's own observer is mounted; a plain failure rather than a
        // request with a guessed version, which the server would rightly refuse.
        return Promise.reject(new Error('useDocumentAutosave: the run is not in the cache.'));
      }
      return reviseTailoredDocument(runId, kind, { content, expected_version: run.version });
    },
    retry: (failureCount, error) => isTransient(error) && failureCount < MAX_TRANSIENT_RETRIES,
    onSuccess: (run) => {
      queryClient.setQueryData(tailoringRunQueryKey(runId), run);
      void queryClient.invalidateQueries({ queryKey: tailoringRunsQueryKey });
      // A save moved the run's `version`, which is what every export job's `current` is compared
      // against on the server (AC-41). Without this line a PDF rendered a minute ago keeps offering
      // *Download PDF* for a document the user has since edited — the file is real, it is simply of
      // the wrong version, and only a fresh list says so. Invalidated rather than patched, for the
      // reason `useCreateJobPosting` gives: `current` is the server's comparison, not ours to
      // recompute here. Harmless when nothing has been exported — an invalidation with no observer
      // fetches nothing.
      void queryClient.invalidateQueries({ queryKey: exportJobsQueryKey(runId) });
      // `store` is the `const` below; this callback runs when an answer arrives, long after the
      // render that declared both — and a `PUT` can only have been sent through the store.
      store.dispatch({ type: 'landed200', textNow: () => handle.serialize() });
    },
    onError: (error) => {
      store.dispatch({ type: 'landedError', failure: failureOf(error) });
    },
  });

  // The latest-render view of what an effect needs. The ref is refreshed after every commit — an
  // effect with no dependency list — so the store, created once below, never holds a stale
  // `handle` or `mutate`, and never has to be recreated because a render produced a new one.
  const latest: AutosaveCollaborators = {
    handle,
    send: (content) => {
      mutate({ content });
    },
    refetch: () =>
      queryClient
        .query({ ...tailoringRunQueryOptions(runId), staleTime: 0 })
        .then((server) => server[textField]),
  };
  const collaborators = useRef(latest);
  useEffect(() => {
    collaborators.current = latest;
  });

  // Created once, seeded with the editor's own serialization so that opening is not dirtying
  // (AC-29) even where the bridge normalised the text on the way in.
  const storeRef = useRef<AutosaveStore | null>(null);
  storeRef.current ??= createAutosaveStore(
    { kind: 'idle', lastSaved: handle.serialize() },
    collaborators,
  );
  const store = storeRef.current;

  const view = useSyncExternalStore(store.subscribe, store.view);

  // The editor's `update` event is a `change` — subscribing to an instance outside React.
  useEffect(() => {
    const editor = handle.editor;
    if (editor === null) {
      return;
    }
    const onUpdate = (): void => {
      store.dispatch({ type: 'change', text: handle.serialize() });
    };
    editor.on('update', onUpdate);
    return () => {
      editor.off('update', onUpdate);
    };
  }, [handle, store]);

  // The tab going to the background flushes: a hidden tab may be a closing one.
  useEffect(() => {
    const onVisibilityChange = (): void => {
      if (document.visibilityState === 'hidden') {
        store.dispatch({ type: 'flush', text: handle.serialize() });
      }
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [handle, store]);

  // Switching to the other document flushes this one (AC-31): the URL changed under the router,
  // and the pane that just left the screen should not still be waiting out its debounce.
  useEffect(() => {
    if (!options.visible) {
      store.dispatch({ type: 'flush', text: handle.serialize() });
    }
  }, [options.visible, handle, store]);

  // Leaving the run page altogether: the editor is about to be destroyed, so the text is read
  // now, and the machine decides whether it is sent at once (a pending debounce), remembered for
  // when the flight lands (one on the wire), or nothing (a quiet document). The `PUT` goes to the
  // mutation cache, which outlives this component.
  //
  // This cleanup can run for exactly one reason: the component is gone. Its only dependency is
  // `store`, created once in a ref and never replaced; the editor handle is read through the
  // collaborators ref, which is not a dependency. Nothing a re-render produces — a new handle, a
  // new `mutate`, a memo React chose to drop — can make it fire, and that matters because the
  // `unmount` event steps a document with a `PUT` out into `leaving`, where keystrokes are ignored.
  useEffect(() => {
    return () => {
      store.dispatch({ type: 'unmount', text: collaborators.current.handle.serialize() });
    };
  }, [store]);

  const retry = useCallback(() => {
    store.dispatch({ type: 'retry', text: handle.serialize() });
  }, [handle, store]);

  const loadLatest = useCallback(() => {
    const server = queryClient.getQueryData<TailoringRun>(tailoringRunQueryKey(runId));
    if (server === undefined) {
      return;
    }
    handle.reseed(server[textField] ?? '');
    store.dispatch({ type: 'loadLatest', text: handle.serialize() });
  }, [handle, queryClient, runId, textField, store]);

  const keepMine = useCallback(() => {
    store.dispatch({ type: 'keepMine', text: handle.serialize() });
  }, [handle, store]);

  return useMemo<SaveState>(() => {
    switch (view.kind) {
      case 'failed':
        return { kind: 'failed', retry };
      case 'conflict':
        return { kind: 'conflict', loadLatest, keepMine };
      case 'saved':
      case 'dirty':
      case 'saving':
      case 'paused':
      case 'invalid':
      case 'expired':
        return view;
    }
  }, [view, retry, loadLatest, keepMine]);
}
