import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import { StrictMode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { tailoringRunQueryKey } from '@/features/tailoring/hooks/useTailoringRun';
import { tailoringRunsQueryKey } from '@/features/tailoring/hooks/useTailoringRuns';
import { countCallsTo, jsonResponse, makeRun } from '@/test/fixtures';

import { useDocumentAutosave } from './useDocumentAutosave';
import { allPutBodies, lastPutBody, stubDocumentFetch } from '../test/fetchStub';
import { AUTOSAVE_DEBOUNCE_MS } from '../saveState';

import type { DocumentEditorHandle } from './useDocumentEditor';
import type { TailoredDocumentKind, TailoringRun } from '@/features/tailoring/types';
import type { ReactNode } from 'react';

/**
 * F9 RED — the autosave hook (feature-spec AC-31, AC-32, AC-33; E-8, E-9, E-15, E-16, E-17;
 * technical-plan "The editor" → `useDocumentAutosave.ts`).
 *
 * Written against the **spec**, not `useDocumentAutosave.ts`'s F8 skeleton, which ignores every
 * parameter and always returns `{ kind: 'saved' }` — no timer, no mutation, no request (its own
 * docstring says so). Every test below drives the hook through a hand-built `DocumentEditorHandle`
 * (the seam `useDocumentEditor`'s own docstring names: "typed explicitly, so the autosave hook can
 * be tested against a hand-built handle without a real editor") and fails on a real mismatch — a PUT
 * count of 0 where 1 was expected, a save state that never leaves `saved` — never on an
 * `ImportError`.
 *
 * AC-34's DOM-visible consequences (read-only markup, the session-expired copy, `beforeunload`) are
 * `DocumentWorkspace.test.tsx`'s job, not this file's: this file asserts the *state* the hook
 * resolves to, not how a component renders it.
 */

const RUN_ID = 'autosave-fixture-run';

function putPath(runId: string, kind: TailoredDocumentKind): string {
  return `/api/tailoring-runs/${runId}/documents/${kind}`;
}

async function flushMicrotasks(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
}

function wrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }): React.JSX.Element {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

interface FakeHandleBundle {
  readonly handle: DocumentEditorHandle;
  /** Change what the next `serialize()` call returns, without firing the editor's `update` event. */
  readonly setText: (text: string) => void;
  /** Simulate the editor's `update` event — what F10c is expected to arm the debounce from. */
  readonly emitUpdate: () => void;
  readonly reseedMock: ReturnType<typeof vi.fn>;
  /** How many `'update'` listeners are currently registered — StrictMode's rehearsal is only
   * harmless if exactly one survives it (a leaked or missing cleanup shows up here as 2 or 0). */
  readonly updateListenerCount: () => number;
}

/**
 * A hand-built `DocumentEditorHandle` — no TipTap, no DOM. `editor` is a minimal stand-in exposing
 * only `on`/`off` for the `'update'` event, which is all `DocumentEditorHandle`'s consumer needs
 * (`@tiptap/core`'s `EventEmitter` is not part of the package's public export surface, so this is
 * hand-rolled rather than imported).
 */
function makeFakeHandle(seed: string): FakeHandleBundle {
  let current = seed;
  let dirtyBaseline = seed;
  const updateListeners = new Set<() => void>();

  const fakeEditor = {
    on: (event: string, fn: () => void) => {
      if (event === 'update') {
        updateListeners.add(fn);
      }
      return fakeEditor;
    },
    off: (event: string, fn: () => void) => {
      if (event === 'update') {
        updateListeners.delete(fn);
      }
      return fakeEditor;
    },
    setEditable: vi.fn(),
  };

  const reseedMock = vi.fn((text: string) => {
    current = text;
    dirtyBaseline = text;
  });

  const handle: DocumentEditorHandle = {
    editor: fakeEditor as unknown as DocumentEditorHandle['editor'],
    serialize: () => current,
    isDirty: () => current !== dirtyBaseline,
    reseed: reseedMock,
  };

  return {
    handle,
    setText: (text: string) => {
      current = text;
    },
    emitUpdate: () => {
      updateListeners.forEach((fn) => {
        fn();
      });
    },
    reseedMock,
    updateListenerCount: () => updateListeners.size,
  };
}

function renderAutosave(
  runId: string,
  kind: TailoredDocumentKind,
  handle: DocumentEditorHandle,
  queryClient: QueryClient,
) {
  return renderHook(() => useDocumentAutosave(runId, kind, handle), {
    wrapper: wrapper(queryClient),
  });
}

describe('AC-31 — debounced autosave, sent with the cache’s current version', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('five keystrokes inside the window produce exactly one PUT, sent AUTOSAVE_DEBOUNCE_MS after the last one', async () => {
    vi.useFakeTimers();
    const queryClient = makeQueryClient();
    const run = makeRun({ id: RUN_ID, version: 3, tailored_cv: 'seed text' });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID), run);
    const { handle, setText, emitUpdate } = makeFakeHandle('seed text');
    const fetchMock = stubDocumentFetch({
      putDocument: {
        cv: () =>
          Promise.resolve(
            jsonResponse(200, { ...run, version: 4, tailored_cv: 'seed text edited 4' }),
          ),
      },
    });

    renderAutosave(RUN_ID, 'cv', handle, queryClient);

    for (let i = 0; i < 5; i += 1) {
      setText(`seed text edited ${String(i)}`);
      act(() => {
        emitUpdate();
      });
      await advance(200);
      expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(0);
    }

    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();

    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);
    const body = lastPutBody(fetchMock, putPath(RUN_ID, 'cv')) as {
      readonly expected_version: number;
      readonly content: string;
    };
    // The cache's version at seed time (3), never a client-side increment.
    expect(body.expected_version).toBe(3);
    expect(body.content).toBe('seed text edited 4');
  });

  it('flushes immediately on visibilitychange → hidden, without waiting for the debounce', async () => {
    vi.useFakeTimers();
    const queryClient = makeQueryClient();
    const run = makeRun({ id: RUN_ID, version: 1, tailored_cv: 'seed text' });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID), run);
    const { handle, setText, emitUpdate } = makeFakeHandle('seed text');
    const fetchMock = stubDocumentFetch({
      putDocument: {
        cv: () =>
          Promise.resolve(jsonResponse(200, { ...run, version: 2, tailored_cv: 'seed edited' })),
      },
    });

    renderAutosave(RUN_ID, 'cv', handle, queryClient);

    setText('seed edited');
    act(() => {
      emitUpdate();
    });
    // Well inside the 1,500 ms window — nothing should have been sent by the timer yet.
    await advance(200);
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(0);

    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      value: 'hidden',
    });
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    // Restore before the assertion (not just in afterEach): TanStack's focusManager reads
    // `document.visibilityState` on every retry-after-backoff and every continuation of a scoped
    // mutation queue, so a `document` left permanently "hidden" pauses *later* tests' mutations
    // forever — they hang rather than fail, and only when run after this one. Deleting the own
    // property restores jsdom's prototype getter (which reports "visible").
    Reflect.deleteProperty(document, 'visibilityState');
    await flushMicrotasks();

    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);
  });
});

describe('AC-32 — saves are serialized per run (mutation scope)', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('with both documents dirty, the second PUT waits for the first to settle and carries the version the first returned', async () => {
    vi.useFakeTimers();
    const queryClient = makeQueryClient();
    const run = makeRun({
      id: RUN_ID,
      version: 1,
      tailored_cv: 'cv seed',
      cover_letter: 'letter seed',
    });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID), run);
    const cv = makeFakeHandle('cv seed');
    const letter = makeFakeHandle('letter seed');

    let resolveCvPut: ((response: Response) => void) | undefined;
    const fetchMock = stubDocumentFetch({
      putDocument: {
        cv: () =>
          new Promise<Response>((resolve) => {
            resolveCvPut = resolve;
          }),
        cover_letter: () =>
          Promise.resolve(
            jsonResponse(200, { ...run, version: 3, cover_letter: 'letter seed edited' }),
          ),
      },
    });

    renderAutosave(RUN_ID, 'cv', cv.handle, queryClient);
    renderAutosave(RUN_ID, 'cover_letter', letter.handle, queryClient);

    cv.setText('cv seed edited');
    act(() => {
      cv.emitUpdate();
    });
    letter.setText('letter seed edited');
    act(() => {
      letter.emitUpdate();
    });

    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();

    // The CV's PUT is in flight (deliberately unresolved); the letter's must not have gone out yet.
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cover_letter'), 'PUT')).toBe(0);

    resolveCvPut?.(jsonResponse(200, { ...run, version: 2, tailored_cv: 'cv seed edited' }));
    await flushMicrotasks();
    await flushMicrotasks();

    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cover_letter'), 'PUT')).toBe(1);
    const cvBody = lastPutBody(fetchMock, putPath(RUN_ID, 'cv')) as {
      readonly expected_version: number;
    };
    const letterBody = lastPutBody(fetchMock, putPath(RUN_ID, 'cover_letter')) as {
      readonly expected_version: number;
    };
    expect(cvBody.expected_version).toBe(1);
    // The version the CV's PUT returned (2), not the version read when the letter was typed (1).
    expect(letterBody.expected_version).toBe(2);
    expect(allPutBodies(fetchMock, putPath(RUN_ID, 'cv'))).toHaveLength(1);
  });
});

describe('AC-33 — resolving a save', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('on 200 the response replaces the run in the cache and the list key is invalidated', async () => {
    vi.useFakeTimers();
    const queryClient = makeQueryClient();
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');
    const run = makeRun({ id: RUN_ID, version: 1, tailored_cv: 'seed' });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID), run);
    const { handle, setText, emitUpdate } = makeFakeHandle('seed');
    const updatedRun = makeRun({
      id: RUN_ID,
      version: 2,
      tailored_cv: 'seed edited',
      tailored_cv_edited_at: '2026-09-14T10:00:00Z',
    });
    stubDocumentFetch({
      putDocument: { cv: () => Promise.resolve(jsonResponse(200, updatedRun)) },
    });

    renderAutosave(RUN_ID, 'cv', handle, queryClient);
    setText('seed edited');
    act(() => {
      emitUpdate();
    });
    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();

    expect(queryClient.getQueryData(tailoringRunQueryKey(RUN_ID))).toEqual(updatedRun);
    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: tailoringRunsQueryKey }) as unknown,
    );
  });

  it('E-15b: a 409 whose server text equals the editor’s serialization resolves to saved, adopts the version, and issues no second PUT', async () => {
    vi.useFakeTimers();
    const queryClient = makeQueryClient();
    const run = makeRun({ id: RUN_ID, version: 1, tailored_cv: 'seed' });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID), run);
    const { handle, setText, emitUpdate } = makeFakeHandle('seed');
    const serverRun = makeRun({ id: RUN_ID, version: 5, tailored_cv: 'seed edited' });
    const fetchMock = stubDocumentFetch({
      putDocument: {
        cv: (callNumber) =>
          callNumber === 1
            ? Promise.resolve(
                jsonResponse(409, {
                  error: {
                    code: 'document_version_conflict',
                    message: 'stale',
                    current_version: 5,
                  },
                }),
              )
            : Promise.reject(new Error('a second PUT was not expected')),
      },
      runDetail: () => Promise.resolve(jsonResponse(200, serverRun)),
    });

    const { result } = renderAutosave(RUN_ID, 'cv', handle, queryClient);
    setText('seed edited');
    act(() => {
      emitUpdate();
    });
    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();
    await flushMicrotasks();

    expect(result.current.kind).toBe('saved');
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);
    expect(queryClient.getQueryData(tailoringRunQueryKey(RUN_ID))).toEqual(serverRun);
  });

  it('E-8/E-17: a 409 whose server text differs resolves to conflict, and loadLatest re-seeds from the server', async () => {
    vi.useFakeTimers();
    const queryClient = makeQueryClient();
    const run = makeRun({ id: RUN_ID, version: 1, tailored_cv: 'seed' });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID), run);
    const { handle, setText, emitUpdate, reseedMock } = makeFakeHandle('seed');
    const serverRun = makeRun({ id: RUN_ID, version: 5, tailored_cv: 'someone else edited this' });
    stubDocumentFetch({
      putDocument: {
        cv: () =>
          Promise.resolve(
            jsonResponse(409, {
              error: { code: 'document_version_conflict', message: 'stale', current_version: 5 },
            }),
          ),
      },
      runDetail: () => Promise.resolve(jsonResponse(200, serverRun)),
    });

    const { result } = renderAutosave(RUN_ID, 'cv', handle, queryClient);
    setText('seed edited');
    act(() => {
      emitUpdate();
    });
    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();
    await flushMicrotasks();

    expect(result.current.kind).toBe('conflict');
    // Nothing happens without a click.
    expect(reseedMock).not.toHaveBeenCalled();

    // Captured into a local: `result.current` is an accessor, so narrowing it in an `if` does not
    // narrow the property access inside — TypeScript cannot know a getter returns the same value
    // twice.
    const conflictState = result.current;
    if (conflictState.kind === 'conflict') {
      act(() => {
        conflictState.loadLatest();
      });
    }

    expect(reseedMock).toHaveBeenCalledWith('someone else edited this');
  });

  it('E-8/E-17: keepMine sends one more PUT, against the fresh version the conflict revealed', async () => {
    vi.useFakeTimers();
    const queryClient = makeQueryClient();
    const run = makeRun({ id: RUN_ID, version: 1, tailored_cv: 'seed' });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID), run);
    const { handle, setText, emitUpdate } = makeFakeHandle('seed');
    const serverRun = makeRun({ id: RUN_ID, version: 5, tailored_cv: 'someone else edited this' });
    const fetchMock = stubDocumentFetch({
      putDocument: {
        cv: (callNumber) =>
          callNumber === 1
            ? Promise.resolve(
                jsonResponse(409, {
                  error: {
                    code: 'document_version_conflict',
                    message: 'stale',
                    current_version: 5,
                  },
                }),
              )
            : Promise.resolve(
                jsonResponse(200, { ...serverRun, version: 6, tailored_cv: 'seed edited' }),
              ),
      },
      runDetail: () => Promise.resolve(jsonResponse(200, serverRun)),
    });

    const { result } = renderAutosave(RUN_ID, 'cv', handle, queryClient);
    setText('seed edited');
    act(() => {
      emitUpdate();
    });
    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();
    await flushMicrotasks();

    expect(result.current.kind).toBe('conflict');

    const conflictState = result.current;
    if (conflictState.kind === 'conflict') {
      act(() => {
        conflictState.keepMine();
      });
    }
    await flushMicrotasks();
    await flushMicrotasks();

    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(2);
    const secondBody = lastPutBody(fetchMock, putPath(RUN_ID, 'cv')) as {
      readonly expected_version: number;
    };
    expect(secondBody.expected_version).toBe(5);
  });
});

describe('/verify slice 1.4 — a second PUT already queued when a 409 arrives must not bypass AC-33’s choice', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  /**
   * The race: the first PUT is held pending; while it is on the wire the person keeps typing, so
   * a second PUT for the same document is queued behind it in the mutation's scope (AC-32). The
   * first then answers 409 `document_version_conflict`, and the handler refetches the run — a
   * fresh `version` lands in the cache. `mutationFn` reads `expected_version` and the document's
   * text **at execution time**, never from a closure — so if the queued PUT is allowed to run
   * next, it carries the fresh version and whatever the editor holds right now, and a server that
   * accepts it overwrites the other writer's text without AC-33's two-choice notice ever
   * appearing. Returns everything a test needs to drive the two keystrokes and then resolve the
   * first PUT; `resolveFirstPut` is a getter because the mock only sets the variable once the
   * first PUT actually goes out.
   */
  function setupQueuedConflict(serverRun: TailoringRun) {
    const queryClient = makeQueryClient();
    const run = makeRun({ id: RUN_ID, version: 1, tailored_cv: 'seed' });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID), run);
    const { handle, setText, emitUpdate, reseedMock } = makeFakeHandle('seed');

    let resolveFirstPut: ((response: Response) => void) | undefined;
    const fetchMock = stubDocumentFetch({
      putDocument: {
        cv: (callNumber) =>
          callNumber === 1
            ? new Promise<Response>((resolve) => {
                resolveFirstPut = resolve;
              })
            : Promise.resolve(jsonResponse(200, { ...serverRun, version: serverRun.version + 1 })),
      },
      runDetail: () => Promise.resolve(jsonResponse(200, serverRun)),
    });

    const { result } = renderAutosave(RUN_ID, 'cv', handle, queryClient);
    return {
      handle,
      setText,
      emitUpdate,
      reseedMock,
      fetchMock,
      result,
      resolveFirstPut: () => resolveFirstPut,
    };
  }

  /** Types once, lets the debounce fire, types again, lets that debounce fire too — leaving the
   * first PUT pending on the wire and a second queued behind it in the scope. */
  async function typeTwiceQueuingASecondSend(
    setText: (text: string) => void,
    emitUpdate: () => void,
    fetchMock: ReturnType<typeof vi.fn>,
  ): Promise<void> {
    setText('seed edited once');
    act(() => {
      emitUpdate();
    });
    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);

    setText('seed edited twice');
    act(() => {
      emitUpdate();
    });
    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();
    // The second send is queued in the scope behind the first, still on the wire — not sent yet.
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);
  }

  it('resolves to conflict and sends no second PUT until a choice is made; Keep my version then sends exactly one more, against the fresh version', async () => {
    vi.useFakeTimers();
    const serverRun = makeRun({
      id: RUN_ID,
      version: 5,
      tailored_cv: 'someone else edited this while I was mid-save',
    });
    const { setText, emitUpdate, fetchMock, result, resolveFirstPut } =
      setupQueuedConflict(serverRun);

    await typeTwiceQueuingASecondSend(setText, emitUpdate, fetchMock);

    resolveFirstPut()?.(
      jsonResponse(409, {
        error: { code: 'document_version_conflict', message: 'stale', current_version: 5 },
      }),
    );
    await flushMicrotasks();
    await flushMicrotasks();
    await flushMicrotasks();

    expect(result.current.kind).toBe('conflict');
    // The queued PUT must not have gone out on its own — the choice has to be shown first, or the
    // other writer's text is silently overwritten (the MAJOR this test exists to catch).
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);

    const conflictState = result.current;
    if (conflictState.kind === 'conflict') {
      act(() => {
        conflictState.keepMine();
      });
    }
    await flushMicrotasks();
    await flushMicrotasks();

    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(2);
    const secondBody = lastPutBody(fetchMock, putPath(RUN_ID, 'cv')) as {
      readonly expected_version: number;
    };
    expect(secondBody.expected_version).toBe(5);
  });

  it('alternatively, Load the latest version re-seeds from the server and sends no further PUT', async () => {
    vi.useFakeTimers();
    const serverRun = makeRun({
      id: RUN_ID,
      version: 5,
      tailored_cv: 'someone else edited this while I was mid-save',
    });
    const { setText, emitUpdate, fetchMock, result, resolveFirstPut, reseedMock } =
      setupQueuedConflict(serverRun);

    await typeTwiceQueuingASecondSend(setText, emitUpdate, fetchMock);

    resolveFirstPut()?.(
      jsonResponse(409, {
        error: { code: 'document_version_conflict', message: 'stale', current_version: 5 },
      }),
    );
    await flushMicrotasks();
    await flushMicrotasks();
    await flushMicrotasks();

    expect(result.current.kind).toBe('conflict');
    expect(reseedMock).not.toHaveBeenCalled();

    const conflictState = result.current;
    if (conflictState.kind === 'conflict') {
      act(() => {
        conflictState.loadLatest();
      });
    }

    expect(reseedMock).toHaveBeenCalledWith('someone else edited this while I was mid-save');
    // Loading the server's copy is a resolution, not a send.
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);
  });

  it('same-content variant: if the server’s text already equals the editor’s, this resolves to saved with no second PUT', async () => {
    vi.useFakeTimers();
    // Equal to what the editor holds after the second keystroke below — the lost-update case
    // (E-15b), not a genuine conflict.
    const serverRun = makeRun({ id: RUN_ID, version: 5, tailored_cv: 'seed edited twice' });
    const { setText, emitUpdate, fetchMock, result, resolveFirstPut } =
      setupQueuedConflict(serverRun);

    await typeTwiceQueuingASecondSend(setText, emitUpdate, fetchMock);

    resolveFirstPut()?.(
      jsonResponse(409, {
        error: { code: 'document_version_conflict', message: 'stale', current_version: 5 },
      }),
    );
    await flushMicrotasks();
    await flushMicrotasks();
    await flushMicrotasks();

    expect(result.current.kind).toBe('saved');
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);
  });
});

describe('the failure contract beyond the happy path', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('E-15a: a network failure retries with backoff before resolving to failed', async () => {
    vi.useFakeTimers();
    const queryClient = makeQueryClient();
    const run = makeRun({ id: RUN_ID, version: 1, tailored_cv: 'seed' });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID), run);
    const { handle, setText, emitUpdate } = makeFakeHandle('seed');
    let attempts = 0;
    stubDocumentFetch({
      putDocument: {
        cv: () => {
          attempts += 1;
          return Promise.reject(new TypeError('Failed to fetch'));
        },
      },
    });

    const { result } = renderAutosave(RUN_ID, 'cv', handle, queryClient);
    setText('seed edited');
    act(() => {
      emitUpdate();
    });
    await advance(AUTOSAVE_DEBOUNCE_MS); // the first attempt
    await advance(1000); // retry #1 backoff
    await advance(2000); // retry #2 backoff
    await advance(4000); // retry #3 backoff — retries spent
    await flushMicrotasks();

    expect(attempts).toBe(4);
    expect(result.current.kind).toBe('failed');
  });

  it('E-16: a 401 resolves to expired', async () => {
    vi.useFakeTimers();
    const queryClient = makeQueryClient();
    const run = makeRun({ id: RUN_ID, version: 1, tailored_cv: 'seed' });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID), run);
    const { handle, setText, emitUpdate } = makeFakeHandle('seed');
    stubDocumentFetch({
      putDocument: {
        cv: () =>
          Promise.resolve(
            jsonResponse(401, {
              error: { code: 'guest_session_expired', message: 'the session expired' },
            }),
          ),
      },
    });

    const { result } = renderAutosave(RUN_ID, 'cv', handle, queryClient);
    setText('seed edited');
    act(() => {
      emitUpdate();
    });
    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();

    expect(result.current.kind).toBe('expired');
  });
});

/**
 * /verify slice 1.4 round 3 pinned three findings against `useDocumentAutosave.ts` before the
 * owner's re-model into one state machine (MAJOR: resend-on-200 was untested; MINOR: the unmount
 * flush compares against the wrong baseline; MINOR: the 409-then-debounce guard's timing). These
 * three `describe` blocks exist to survive that refactor as a spec, independent of the hook's
 * current shape — each one says, in its own docblock, what it protects and whether it is red
 * against the code as it stands today.
 */

describe('/verify slice 1.4 round 3 — MAJOR: resend-on-200 must not silently vanish', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  /**
   * Regression guard for the resend-on-200 rule — `autosaveMachine.ts`'s `stepInFlight`, the
   * `landed200` case's `if (sendWanted) { return send(sent, text, owed); }` branch (around lines
   * 260-263). Both tests in this block **pass today**: the branch exists, so there is nothing to
   * observe failing. Their value is the opposite direction — round 3's actual MAJOR (against the
   * pre-machine hook) was that deleting the equivalent block left every existing test green, i.e.
   * nothing in the suite could tell the branch was gone. `qa` does not edit production code, so
   * the mutation this guard is meant to catch (delete that branch, or hard-code `sendWanted` to
   * `false`) is not performed here; the implementer/reviewer should delete it and confirm both
   * tests below go red before trusting a future refactor kept the behaviour. As a sanity check on
   * the tests themselves (not production code), each was run once against a deliberately wrong
   * fetch stub (the second PUT's response body changed so the assertions could not pass) and
   * failed on the assertion it exists to protect, then restored to the version below.
   */
  it('(a) a later edit sent while PUT #1 is in flight is resent once PUT #1 lands 200, with the latest text and the returned version', async () => {
    vi.useFakeTimers();
    const queryClient = makeQueryClient();
    const run = makeRun({ id: RUN_ID, version: 1, tailored_cv: 'seed' });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID), run);
    const { handle, setText, emitUpdate } = makeFakeHandle('seed');

    let resolveFirstPut: ((response: Response) => void) | undefined;
    const fetchMock = stubDocumentFetch({
      putDocument: {
        cv: (callNumber) =>
          callNumber === 1
            ? new Promise<Response>((resolve) => {
                resolveFirstPut = resolve;
              })
            : Promise.resolve(
                jsonResponse(200, { ...run, version: 3, tailored_cv: 'seed edited twice' }),
              ),
      },
    });

    const { result } = renderAutosave(RUN_ID, 'cv', handle, queryClient);

    setText('seed edited once');
    act(() => {
      emitUpdate();
    });
    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);

    // Type more while PUT #1 is still on the wire — arms a debounce (`inFlight + change` →
    // `debounceArmed`), and its firing below turns into the wish (`inFlight + timerDue` →
    // `sendWanted`), not a second send.
    setText('seed edited twice');
    act(() => {
      emitUpdate();
    });
    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);

    // PUT #1 lands 200, carrying the *first* text — exactly as the server would answer it.
    resolveFirstPut?.(jsonResponse(200, { ...run, version: 2, tailored_cv: 'seed edited once' }));
    await flushMicrotasks();
    await flushMicrotasks();

    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(2);
    const secondBody = lastPutBody(fetchMock, putPath(RUN_ID, 'cv')) as {
      readonly expected_version: number;
      readonly content: string;
    };
    expect(secondBody.content).toBe('seed edited twice');
    // The version PUT #1 returned, not the version read when the first PUT was sent.
    expect(secondBody.expected_version).toBe(2);

    await flushMicrotasks();
    await flushMicrotasks();
    expect(result.current.kind).toBe('saved');
  });

  it('(b) typing away and back to exactly what PUT #1 sent, while it is in flight, issues no second PUT once it lands 200', async () => {
    vi.useFakeTimers();
    const queryClient = makeQueryClient();
    const run = makeRun({ id: RUN_ID, version: 1, tailored_cv: 'seed' });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID), run);
    const { handle, setText, emitUpdate } = makeFakeHandle('seed');

    let resolveFirstPut: ((response: Response) => void) | undefined;
    const fetchMock = stubDocumentFetch({
      putDocument: {
        cv: (callNumber) =>
          callNumber === 1
            ? new Promise<Response>((resolve) => {
                resolveFirstPut = resolve;
              })
            : Promise.reject(new Error('a second PUT was not expected')),
      },
    });

    const { result } = renderAutosave(RUN_ID, 'cv', handle, queryClient);

    setText('seed edited once');
    act(() => {
      emitUpdate();
    });
    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);

    // Type away, then back to exactly what PUT #1 is carrying. `inFlight + change`
    // (`autosaveMachine.ts`'s `stepInFlight`) measures `differs` against `sent` — the in-flight
    // machine's own field — so landing back on exactly what was sent reads as no change at all.
    setText('seed edited once and then some');
    act(() => {
      emitUpdate();
    });
    setText('seed edited once');
    act(() => {
      emitUpdate();
    });
    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);

    resolveFirstPut?.(jsonResponse(200, { ...run, version: 2, tailored_cv: 'seed edited once' }));
    await flushMicrotasks();
    await flushMicrotasks();

    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);
    expect(result.current.kind).toBe('saved');
  });
});

describe('/verify slice 1.4 round 3 — MINOR: the unmount flush must compare against what is actually in flight', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  /**
   * The unmount transition (`autosaveMachine.ts`'s `stepInFlight`, the `unmount` case) decides
   * whether to record a wish by comparing the editor's text against `sent` — what *that* flight is
   * carrying — never against `lastSaved`, the last **resolved** save. That is the same baseline
   * `stepInFlight`'s `change` case already compares against while a PUT is in flight. Typing back
   * to the last-resolved text while a *different* text is in flight must still record a wish,
   * because the in-flight PUT is about to overwrite the server with text the person is no longer
   * looking at.
   *
   * GREEN against the machine as it stands: `wish: event.text === sent ? null : event.text` reads
   * `sent`, so the comparison this test drives is correct today. (Against the pre-machine hook,
   * whose unmount cleanup compared against the last-*resolved* save instead, this test recorded
   * the round-3 MINOR: only PUT B was ever sent, and the assertion below — expecting a second PUT
   * carrying 'A' — failed as `expect(received).toBe(expected) // Object.is equality — Expected: 2,
   * Received: 1`.)
   */
  it('typing back to the last-saved text while a newer PUT is in flight still queues a correcting PUT once that flight lands', async () => {
    vi.useFakeTimers();
    const queryClient = makeQueryClient();
    const run = makeRun({ id: RUN_ID, version: 1, tailored_cv: 'A' });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID), run);
    const { handle, setText, emitUpdate } = makeFakeHandle('A');

    let resolveFirstPut: ((response: Response) => void) | undefined;
    const fetchMock = stubDocumentFetch({
      putDocument: {
        cv: (callNumber) =>
          callNumber === 1
            ? new Promise<Response>((resolve) => {
                resolveFirstPut = resolve;
              })
            : Promise.resolve(jsonResponse(200, { ...run, version: 3, tailored_cv: 'A' })),
      },
    });

    const { unmount } = renderAutosave(RUN_ID, 'cv', handle, queryClient);

    // B goes on the wire — held pending.
    setText('B');
    act(() => {
      emitUpdate();
    });
    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);
    expect(lastPutBody(fetchMock, putPath(RUN_ID, 'cv'))).toMatchObject({ content: 'B' });

    // Type to C, then back to A — the text the person leaves on screen — all while B is still on
    // the wire, and before the resulting debounce has fired.
    setText('C');
    act(() => {
      emitUpdate();
    });
    setText('A');
    act(() => {
      emitUpdate();
    });

    // Unmount now, before the debounce fires — the workspace navigating away.
    unmount();

    // B lands 200.
    resolveFirstPut?.(jsonResponse(200, { ...run, version: 2, tailored_cv: 'B' }));
    await flushMicrotasks();
    await flushMicrotasks();

    // The server must end up holding A, the text on screen when the person left — exactly one
    // more PUT, carrying A.
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(2);
    expect(lastPutBody(fetchMock, putPath(RUN_ID, 'cv'))).toMatchObject({ content: 'A' });
  });
});

describe('/verify slice 1.4 round 3 — MINOR: a debounce firing after a 409 resolves to conflict sends nothing', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  /**
   * Pins the rule for a wish recorded during a 409's flight: once the document has settled into
   * `conflict`, a debounce that becomes due afterwards must send nothing. The wish is dropped
   * structurally — `autosaveMachine.ts`'s `resolvingConflict` and `conflict` variants carry no
   * `sendWanted` field at all, so nothing survives the `inFlight + landedError(conflict) →
   * resolvingConflict` transition to resend — and the guard against a *later* `timerDue` is
   * `stepConflict`'s default branch ("a keystroke is not a choice (AC-33), and neither is a timer,
   * a flush or an unmount"): every event but `keepMine`/`loadLatest` is a no-op once `conflict` is
   * reached. This test arms a debounce **before** the 409 resolves and lets it fire strictly
   * **after** `result.current.kind` already reads `'conflict'`, so the send it does not make can
   * only be explained by that default branch still holding once the timer is due.
   *
   * Unlike the pre-machine hook, `step` is a pure, synchronous reducer — there is no window
   * between a promise settling and a `dispatch` for a stray timer to land in; the hook calls
   * `step` once, synchronously, inside the mutation's own callback. What this test still cannot
   * prove is React's *commit* timing — whether `result.current` has visibly updated by the instant
   * the debounce timer callback runs is a jsdom/fake-timers question, not a `step` question, and
   * this suite has no way to freeze that gap. What it does prove: a debounce due after `conflict`
   * has visibly settled is inert, which is the externally observable half of the rule. Passes
   * today.
   */
  it('after resolving to conflict, a debounce that becomes due afterwards sends nothing', async () => {
    vi.useFakeTimers();
    const queryClient = makeQueryClient();
    const run = makeRun({ id: RUN_ID, version: 1, tailored_cv: 'seed' });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID), run);
    const { handle, setText, emitUpdate } = makeFakeHandle('seed');
    const serverRun = makeRun({ id: RUN_ID, version: 5, tailored_cv: 'someone else edited this' });

    let resolveFirstPut: ((response: Response) => void) | undefined;
    const fetchMock = stubDocumentFetch({
      putDocument: {
        cv: (callNumber) =>
          callNumber === 1
            ? new Promise<Response>((resolve) => {
                resolveFirstPut = resolve;
              })
            : Promise.reject(new Error('no PUT should follow the conflict in this test')),
      },
      runDetail: () => Promise.resolve(jsonResponse(200, serverRun)),
    });

    const { result } = renderAutosave(RUN_ID, 'cv', handle, queryClient);

    setText('seed edited');
    act(() => {
      emitUpdate();
    });
    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);

    // Arm a fresh debounce while the first PUT is still in flight — the wish the docstring names.
    setText('seed edited again');
    act(() => {
      emitUpdate();
    });

    // The first PUT lands 409, and the server's text genuinely differs — a real conflict.
    resolveFirstPut?.(
      jsonResponse(409, {
        error: { code: 'document_version_conflict', message: 'stale', current_version: 5 },
      }),
    );
    await flushMicrotasks();
    await flushMicrotasks();
    await flushMicrotasks();

    expect(result.current.kind).toBe('conflict');
    // The wish armed above must not have turned into a send during the resolution.
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);

    // The debounce armed before the resolution is still pending — let it become due now that
    // `conflict` has visibly settled.
    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();

    expect(result.current.kind).toBe('conflict');
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);
  });
});

describe('StrictMode — the mount-unmount-mount rehearsal (per the hook’s own docstring)', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  /**
   * `useDocumentAutosave.ts`'s own docstring claims the unmount cleanup "hands the machine an
   * `unmount` event and nothing else... which is what keeps StrictMode's mount-unmount-mount
   * rehearsal in development harmless." This test drives that claim rather than reading it: it
   * mounts the hook under a real `<StrictMode>` (matching `main.tsx`), which runs the effect
   * lifecycle (setup -> cleanup -> setup) once extra, synchronously, before any test code runs —
   * and then checks the three things a leak in that rehearsal would break.
   *
   * If the `update`-listener effect's cleanup ever stopped calling `editor.off` before the
   * rehearsal's second `editor.on`, `updateListenerCount()` would read 2, not 1, and a single
   * `emitUpdate()` later would silently dispatch the same `change` twice. If the unmount effect's
   * cleanup fired the `unmount` event against a document that was never dirty and the machine
   * mishandled it, a PUT would go out before anyone typed a keystroke — `stepIdle`'s default
   * branch is what makes that impossible, and the assertion below is what makes the claim
   * checkable instead of merely asserted in a docstring.
   */
  it('mounts cleanly under StrictMode, sends exactly one PUT per debounce, and an unmount mid-flight sends the wish exactly once', async () => {
    vi.useFakeTimers();
    const queryClient = makeQueryClient();
    const run = makeRun({ id: RUN_ID, version: 1, tailored_cv: 'seed' });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID), run);
    const { handle, setText, emitUpdate, updateListenerCount } = makeFakeHandle('seed');

    let resolveFirstPut: ((response: Response) => void) | undefined;
    const fetchMock = stubDocumentFetch({
      putDocument: {
        cv: (callNumber) =>
          callNumber === 1
            ? new Promise<Response>((resolve) => {
                resolveFirstPut = resolve;
              })
            : Promise.reject(new Error('a second PUT was not expected')),
      },
    });

    function strictWrapper({ children }: { children: ReactNode }): React.JSX.Element {
      return (
        <StrictMode>
          <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
        </StrictMode>
      );
    }

    const { unmount } = renderHook(() => useDocumentAutosave(RUN_ID, 'cv', handle), {
      wrapper: strictWrapper,
    });

    // The rehearsal has already run, synchronously, inside the `render` above. The document was
    // never dirty during it, so nothing was sent, and exactly one `'update'` listener survives —
    // not two (a leaked first registration) and not zero (a cleanup that outran the replayed setup).
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(0);
    expect(updateListenerCount()).toBe(1);

    // One keystroke, one debounce, one PUT — the rehearsal left the machine in a single, ordinary
    // `idle` state, not doubled.
    setText('seed edited once');
    act(() => {
      emitUpdate();
    });
    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);
    const firstBody = lastPutBody(fetchMock, putPath(RUN_ID, 'cv')) as {
      readonly expected_version: number;
    };
    expect(firstBody.expected_version).toBe(1);

    // Type again while PUT #1 is on the wire, then unmount before it lands: `stepInFlight`'s
    // `unmount` case captures the wish, read now, while the editor still exists.
    setText('seed edited twice');
    act(() => {
      emitUpdate();
    });
    unmount();

    // PUT #1 lands 200, carrying the version this run started at.
    resolveFirstPut?.(jsonResponse(200, { ...run, version: 2, tailored_cv: 'seed edited once' }));
    await flushMicrotasks();
    await flushMicrotasks();

    // The wish is sent exactly once, against the version PUT #1 returned.
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(2);
    const secondBody = lastPutBody(fetchMock, putPath(RUN_ID, 'cv')) as {
      readonly expected_version: number;
      readonly content: string;
    };
    expect(secondBody.expected_version).toBe(2);
    expect(secondBody.content).toBe('seed edited twice');

    // Nothing after: no component is left to arm a debounce, and a further tick sends nothing.
    await advance(AUTOSAVE_DEBOUNCE_MS);
    await flushMicrotasks();
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(2);
  });
});
