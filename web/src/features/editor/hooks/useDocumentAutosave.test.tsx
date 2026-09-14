import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { tailoringRunQueryKey } from '@/features/tailoring/hooks/useTailoringRun';
import { tailoringRunsQueryKey } from '@/features/tailoring/hooks/useTailoringRuns';
import { countCallsTo, jsonResponse, makeRun } from '@/test/fixtures';

import { useDocumentAutosave } from './useDocumentAutosave';
import { allPutBodies, lastPutBody, stubDocumentFetch } from '../test/fetchStub';
import { AUTOSAVE_DEBOUNCE_MS } from '../saveState';

import type { DocumentEditorHandle } from './useDocumentEditor';
import type { TailoredDocumentKind } from '@/features/tailoring/types';
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
