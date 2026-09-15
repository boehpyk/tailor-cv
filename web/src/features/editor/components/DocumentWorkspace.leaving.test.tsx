import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { tailoringRunQueryKey } from '@/features/tailoring/hooks/useTailoringRun';
import { jsonResponse, makeRun } from '@/test/fixtures';

import { DocumentWorkspace, UNSAVED_LEAVE_PROMPT } from './DocumentWorkspace';
import { stubDocumentFetch } from '../test/fetchStub';

import type { TailoringRun } from '@/features/tailoring/types';

/**
 * Pins commit d929cc3's "two exit doors, one lock" (E-32, AC-34's leaving-half): `holdsUnsavedText`
 * covers `dirty | saving | failed | invalid | paused | conflict`, and both the browser's
 * `beforeunload` and the router's `useBlocker` are held while it does.
 *
 * A **separate file from `DocumentWorkspace.test.tsx`**, for the same reason
 * `DocumentWorkspace.bridgeFailure.test.tsx` is separate: this is test-after (the router API these
 * tests drive — `useBlocker`'s exemption shape, `router.navigate`, POP via a numeric delta — was
 * discovered against the shipped code, not designed ahead of it, per CLAUDE.md's tiered-TDD rule),
 * and duplicating the render helpers here (rather than importing them from the sibling `.test.tsx`)
 * avoids re-running that file's own `describe` blocks a second time via the module import.
 *
 * `qa` did not write `DocumentWorkspace.tsx`; every assertion below was checked against the
 * implementation and the file's own docstrings (lines 36-62 and 122-167) before being written, and
 * this docblock records any place code and prose disagreed.
 */

const RUN_ID = 'leaving-fixture-run';

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

/**
 * Same trap as `DocumentWorkspace.test.tsx`'s companion helper: the debounce's `setTimeout` is
 * armed by `user.type` under **real** timers, so once fake timers are switched on there is no timer
 * for `vi.advanceTimersByTimeAsync` to find. `visibilitychange -> hidden` reaches the autosave
 * hook's own separate flush-on-hide listener instead, which was armed after the switch and so is
 * visible to the fake clock. `visibilityState` is restored immediately after, or every later
 * `focusManager`-gated assertion in a shared worker would see a permanently hidden document.
 */
function flushDirtySaveViaVisibilityChange(): void {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
  act(() => {
    document.dispatchEvent(new Event('visibilitychange'));
  });
  Reflect.deleteProperty(document, 'visibilityState');
}

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
}

/**
 * Mounts `DocumentWorkspace` on a real data router with two routes: this run's own
 * `/runs/:runId/:document` (so `useBlocker`'s exemption can compare `pathname`) and a trivial `/`
 * (the workspace, the blocked destination in most cases below). `useBlocker` requires a data
 * router — a bare `MemoryRouter` has no data-router context — which is exactly what
 * `createMemoryRouter` supplies here, matching the sibling file.
 */
function renderDocumentWorkspace(
  run: TailoringRun,
  documentSegment: 'cv' | 'cover_letter' = 'cv',
  initialEntries: readonly string[] = [`/runs/${run.id}/${documentSegment}`],
) {
  const queryClient = makeQueryClient();
  queryClient.setQueryData(tailoringRunQueryKey(run.id), run);
  const router = createMemoryRouter(
    [
      { path: '/', element: <p>the workspace</p> },
      { path: '/runs/:runId/:document', element: <DocumentWorkspace run={run} /> },
    ],
    { initialEntries: initialEntries as string[] },
  );

  const result = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );

  return { ...result, router, queryClient };
}

function paneFor(kind: 'cv' | 'cover_letter'): HTMLElement {
  const pane = document.querySelector<HTMLElement>(`[data-document="${kind}"]`);
  if (pane === null) {
    throw new Error(`no pane rendered for ${kind}`);
  }
  return pane;
}

function editableIn(pane: HTMLElement): HTMLElement {
  const editable = pane.querySelector<HTMLElement>('.ProseMirror');
  if (editable === null) {
    throw new Error('no ProseMirror contenteditable found in pane');
  }
  return editable;
}

/** Same rationale as the sibling file's helper of the same name: assert the two fragments
 * separately rather than their concatenation, because jsdom's caret placement is not stable. */
function expectTypedTextKept(pane: HTMLElement, typed: string): void {
  expect(pane).toHaveTextContent('CV seed text');
  expect(pane).toHaveTextContent(typed);
}

/** Types into the visible CV pane under real timers — `userEvent` does not get on with fake ones
 * against ProseMirror, matching every AC-34/AC-31 test in the sibling file. */
async function typeIntoCv(edit: string): Promise<void> {
  const user = userEvent.setup();
  await user.click(editableIn(paneFor('cv')));
  await user.type(editableIn(paneFor('cv')), edit);
}

/** Same as `typeIntoCv`, for the cover-letter pane — real timers, same reason. */
async function typeIntoCoverLetter(edit: string): Promise<void> {
  const user = userEvent.setup();
  await user.click(editableIn(paneFor('cover_letter')));
  await user.type(editableIn(paneFor('cover_letter')), edit);
}

/** Drives the CV into `failed` exactly as the sibling file's AC-34 test does: reject every PUT,
 * flush the dirty save via a visibility change, then exhaust the three retries' backoff
 * (1s, 2s, 4s) under fake timers so no timer is left pending. */
async function driveCvToFailed(): Promise<void> {
  flushDirtySaveViaVisibilityChange(); // the first attempt
  await advance(1000); // retry #1 backoff
  await advance(2000); // retry #2 backoff
  await advance(4000); // retry #3 backoff — retries spent
  await flushMicrotasks();
  expect(screen.getByText(/couldn.t save/i)).toBeInTheDocument();
}

afterEach(() => {
  vi.useRealTimers();
});

describe('E-32/AC-34 — the router door (useBlocker)', () => {
  it('case 1: saved -> navigating to / proceeds without asking', async () => {
    const run = makeRun({
      id: RUN_ID,
      status: 'succeeded',
      tailored_cv: 'CV seed text',
      cover_letter: 'Letter seed text',
    });
    stubDocumentFetch({});
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { router } = renderDocumentWorkspace(run, 'cv');

    await act(async () => {
      await router.navigate('/');
    });

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(router.state.location.pathname).toBe('/');
  });

  it('case 2: dirty -> tab switch to /cover_letter proceeds with no confirm (the exemption)', async () => {
    const run = makeRun({
      id: RUN_ID,
      status: 'succeeded',
      tailored_cv: 'CV seed text',
      cover_letter: 'Letter seed text',
    });
    stubDocumentFetch({
      putDocument: { cv: () => Promise.reject(new TypeError('Failed to fetch')) },
    });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { router } = renderDocumentWorkspace(run, 'cv');

    await typeIntoCv(' edited');

    await act(async () => {
      await router.navigate(`/runs/${RUN_ID}/cover_letter`);
    });

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(router.state.location.pathname).toBe(`/runs/${RUN_ID}/cover_letter`);
  });

  it('case 3: dirty -> navigating to / asks the exact sentence; Cancel keeps the location and the text', async () => {
    const run = makeRun({
      id: RUN_ID,
      status: 'succeeded',
      tailored_cv: 'CV seed text',
      cover_letter: 'Letter seed text',
    });
    stubDocumentFetch({
      putDocument: { cv: () => Promise.reject(new TypeError('Failed to fetch')) },
    });
    // jsdom's own window.confirm returns undefined — a falsy Cancel — but the test pins the exact
    // return value the implementation branches on, per the task's own note about jsdom's default.
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const { router } = renderDocumentWorkspace(run, 'cv');

    await typeIntoCv(' edited');

    await act(async () => {
      await router.navigate('/');
    });

    expect(confirmSpy).toHaveBeenCalledWith(UNSAVED_LEAVE_PROMPT);
    expect(router.state.location.pathname).toBe(`/runs/${RUN_ID}/cv`);
    expectTypedTextKept(paneFor('cv'), 'edited');

    // A second attempt asks again — the blocker resets to `unblocked` rather than staying blocked.
    confirmSpy.mockClear();
    await act(async () => {
      await router.navigate('/');
    });
    expect(confirmSpy).toHaveBeenCalledWith(UNSAVED_LEAVE_PROMPT);
    expect(router.state.location.pathname).toBe(`/runs/${RUN_ID}/cv`);
  });

  it('case 4: dirty -> navigating to /, OK proceeds and the location becomes /', async () => {
    const run = makeRun({
      id: RUN_ID,
      status: 'succeeded',
      tailored_cv: 'CV seed text',
      cover_letter: 'Letter seed text',
    });
    stubDocumentFetch({
      putDocument: { cv: () => Promise.reject(new TypeError('Failed to fetch')) },
    });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { router } = renderDocumentWorkspace(run, 'cv');

    await typeIntoCv(' edited');

    await act(async () => {
      await router.navigate('/');
    });

    expect(confirmSpy).toHaveBeenCalledWith(UNSAVED_LEAVE_PROMPT);
    expect(router.state.location.pathname).toBe('/');
  });

  it('case 5: failed -> navigating to / asks before leaving', async () => {
    const run = makeRun({
      id: RUN_ID,
      status: 'succeeded',
      tailored_cv: 'CV seed text',
      cover_letter: 'Letter seed text',
    });
    stubDocumentFetch({
      putDocument: { cv: () => Promise.reject(new TypeError('Failed to fetch')) },
    });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { router } = renderDocumentWorkspace(run, 'cv');

    await typeIntoCv(' edited');
    vi.useFakeTimers();
    await driveCvToFailed();

    await act(async () => {
      await router.navigate('/');
    });

    expect(confirmSpy).toHaveBeenCalledWith(UNSAVED_LEAVE_PROMPT);
    expect(router.state.location.pathname).toBe('/');
  });

  it('blocks navigation to a different run entirely, not only "/"', async () => {
    const run = makeRun({
      id: RUN_ID,
      status: 'succeeded',
      tailored_cv: 'CV seed text',
      cover_letter: 'Letter seed text',
    });
    stubDocumentFetch({
      putDocument: { cv: () => Promise.reject(new TypeError('Failed to fetch')) },
    });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const { router } = renderDocumentWorkspace(run, 'cv');

    await typeIntoCv(' edited');

    await act(async () => {
      await router.navigate(`/runs/other-run/cv`);
    });

    expect(confirmSpy).toHaveBeenCalledWith(UNSAVED_LEAVE_PROMPT);
    expect(router.state.location.pathname).toBe(`/runs/${RUN_ID}/cv`);
  });

  it('blocks a POP navigation (the back button), not only a programmatic push', async () => {
    const run = makeRun({
      id: RUN_ID,
      status: 'succeeded',
      tailored_cv: 'CV seed text',
      cover_letter: 'Letter seed text',
    });
    stubDocumentFetch({
      putDocument: { cv: () => Promise.reject(new TypeError('Failed to fetch')) },
    });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const { router } = renderDocumentWorkspace(run, 'cv', ['/', `/runs/${RUN_ID}/cv`]);
    expect(router.state.location.pathname).toBe(`/runs/${RUN_ID}/cv`);

    await typeIntoCv(' edited');

    await act(async () => {
      await router.navigate(-1);
    });

    expect(confirmSpy).toHaveBeenCalledWith(UNSAVED_LEAVE_PROMPT);
    expect(router.state.location.pathname).toBe(`/runs/${RUN_ID}/cv`);

    confirmSpy.mockReturnValue(true);
    await act(async () => {
      await router.navigate(-1);
    });
    expect(router.state.location.pathname).toBe('/');
  });
});

describe('E-32/AC-34 — the browser door (beforeunload) in the failed state', () => {
  it('case 6: beforeunload stays registered once the editor has settled into failed', async () => {
    const addEventListenerSpy = vi.spyOn(window, 'addEventListener');
    const removeEventListenerSpy = vi.spyOn(window, 'removeEventListener');
    const run = makeRun({
      id: RUN_ID,
      status: 'succeeded',
      tailored_cv: 'CV seed text',
      cover_letter: 'Letter seed text',
    });
    stubDocumentFetch({
      putDocument: { cv: () => Promise.reject(new TypeError('Failed to fetch')) },
    });
    renderDocumentWorkspace(run, 'cv');

    await typeIntoCv(' edited');

    // Registered from the very first dirty keystroke (the sibling file's AC-34 test already pins
    // this for `dirty`); this test's job is only the `failed` half.
    expect(addEventListenerSpy).toHaveBeenCalledWith(
      'beforeunload',
      expect.any(Function) as unknown,
    );
    const dirtyRegistrationCalls = addEventListenerSpy.mock.calls.filter(
      (call) => call[0] === 'beforeunload',
    ).length;

    vi.useFakeTimers();
    await driveCvToFailed();

    // The lock never released between `dirty` and `failed` (both are in `holdsUnsavedText`'s held
    // set), so the effect's dependency array never toggled from `true` back to `true` through
    // `false` — no extra add/remove pair, and the listener that is registered right now still
    // answers `true` to `event.preventDefault()` being callable, i.e. it is still attached.
    const registrationCallsAfterFailed = addEventListenerSpy.mock.calls.filter(
      (call) => call[0] === 'beforeunload',
    ).length;
    expect(registrationCallsAfterFailed).toBe(dirtyRegistrationCalls);
    const removalCalls = removeEventListenerSpy.mock.calls.filter(
      (call) => call[0] === 'beforeunload',
    ).length;
    expect(removalCalls).toBe(0);

    const [, handler] = addEventListenerSpy.mock.calls.find(
      (call) => call[0] === 'beforeunload',
    ) as [string, (event: BeforeUnloadEvent) => void];
    const event: BeforeUnloadEvent = new Event('beforeunload');
    const preventDefault = vi.spyOn(event, 'preventDefault');
    handler(event);
    expect(preventDefault).toHaveBeenCalled();
  });
});

describe('AC-34 — a 401 on one document releases the lock for both', () => {
  it('the CV answering 401 releases the lock even while the letter is still dirty: no confirm, and beforeunload is no longer held', async () => {
    const addEventListenerSpy = vi.spyOn(window, 'addEventListener');
    const removeEventListenerSpy = vi.spyOn(window, 'removeEventListener');
    const run = makeRun({
      id: RUN_ID,
      status: 'succeeded',
      tailored_cv: 'CV seed text',
      cover_letter: 'Letter seed text',
    });
    stubDocumentFetch({
      putDocument: {
        cv: () =>
          Promise.resolve(
            jsonResponse(401, {
              error: { code: 'guest_session_expired', message: 'the session expired' },
            }),
          ),
        // The letter is never flushed in this test (no tab switch away from it, no visibility
        // change), so no PUT for it should ever be issued — leaving no handler here means the stub
        // itself would fail the test loudly if that assumption were wrong.
      },
    });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { router } = renderDocumentWorkspace(run, 'cv');

    await typeIntoCv(' edited'); // CV -> dirty

    // AC-31: leaving the CV pane flushes its pending save at once. This sends the CV's PUT, which
    // the stub above answers 401 — but answering it takes a microtask tick, so the letter is typed
    // into (and left dirty, on purpose) before that 401 has had a chance to land and flip both
    // editors to `editable: false`.
    await act(async () => {
      await router.navigate(`/runs/${RUN_ID}/cover_letter`);
    });
    await typeIntoCoverLetter(' also edited'); // letter -> dirty, and stays that way

    // Wait for the CV's 401 to resolve to `expired`. The sibling file's own 401 test asserts the
    // *SaveIndicator's* "Session expired" copy, but that indicator only ever shows the currently
    // *visible* document's state (`states[visible]`) — and the visible document here is the
    // letter, still `dirty`, never the CV. `DocumentWorkspace`'s `expired` flag instead drives the
    // shared footer (`cvState.kind === 'expired' || coverLetterState.kind === 'expired'`), which is
    // exactly the fact this test needs: it flips the instant *either* document expires, regardless
    // of which pane is on screen. This is a real-timer `waitFor` under the hood, well short of the
    // 1500ms autosave debounce, so the letter's own still-pending debounce never fires and never
    // sends an unstubbed PUT.
    await screen.findByText(/session has ended/i);
    expect(editableIn(paneFor('cover_letter'))).toHaveAttribute('contenteditable', 'false');

    // The lock releases the instant the CV expires — before this navigation, not because of it.
    const beforeunloadAdds = addEventListenerSpy.mock.calls.filter(
      (call) => call[0] === 'beforeunload',
    ).length;
    const beforeunloadRemoves = removeEventListenerSpy.mock.calls.filter(
      (call) => call[0] === 'beforeunload',
    ).length;
    expect(beforeunloadAdds).toBeGreaterThan(0); // it was held while the CV was merely dirty
    expect(beforeunloadRemoves).toBe(beforeunloadAdds); // and fully released once expired arrived

    confirmSpy.mockClear();
    await act(async () => {
      await router.navigate('/');
    });

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(router.state.location.pathname).toBe('/');
  });
});
