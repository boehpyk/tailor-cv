import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { tailoringRunQueryKey } from '@/features/tailoring/hooks/useTailoringRun';
import { countCallsTo, jsonResponse, makeRun } from '@/test/fixtures';

import { DocumentWorkspace } from './DocumentWorkspace';
import { normalizationFixtureMarkdown } from '../markdown/fixtures';
import { AUTOSAVE_DEBOUNCE_MS } from '../saveState';
import { stubDocumentFetch } from '../test/fetchStub';

import type { TailoringRun } from '@/features/tailoring/types';

/**
 * F9 RED — the editor's container (feature-spec AC-29 [wiring half], AC-30, AC-34, AC-35, AC-36;
 * E-16, E-17, E-29, E-32; technical-plan "The editor" → `DocumentWorkspace.tsx`).
 *
 * Written against the **spec**, not the F8 skeleton, which: hardcodes `hidden={false}` on both
 * panes regardless of the URL (visibility toggling does not exist yet); seeds each editor with the
 * *raw* text via `plainParagraph`, never through the bridge (so a normalized fixture displays
 * unnormalized); never renders `ConflictNotice`, a footer, or an error boundary; and pairs with an
 * autosave hook that always answers `saved` and calls `fetch` for nothing. Every assertion below is
 * therefore expected to fail on a real mismatch — an element still visible that should be `hidden`,
 * text not found, a `PUT` count of 0 where 1 was wanted — never on an `ImportError`.
 *
 * `DocumentWorkspace` is not wired into `RunPage` until F11, so it is mounted here directly as the
 * element of its own `/runs/:runId/:document` route on a `createMemoryRouter` — the same shape
 * `RunPage` will hand it once F11 lands, and the only way to drive `useParams()`-based visibility
 * (technical-plan: "reads `:document` for visibility") at all.
 */

const RUN_ID = 'workspace-fixture-run';

function putPath(runId: string, kind: 'cv' | 'cover_letter'): string {
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

/**
 * Forces the autosave hook to flush a pending save immediately, instead of advancing fake timers
 * past its debounce. In these AC-34 tests the debounce's `setTimeout` is armed by `user.type` under
 * **real** timers (`userEvent` does not get on with fake ones against ProseMirror — see the AC-31
 * companion test's comment), so a `vi.advanceTimersByTimeAsync` issued after `vi.useFakeTimers()`
 * cannot see or convert it: a timer created before the fake clock exists is neither tracked nor
 * advanced by it. `visibilitychange → hidden` reaches the hook's own, separate flush-on-hide
 * listener instead, which does not care which clock armed the debounce. `visibilityState` is
 * restored immediately after — left "hidden", TanStack's `focusManager` pauses every later
 * retry-after-backoff and scoped-mutation continuation in this file, the same cross-test leak
 * `useDocumentAutosave.test.tsx` hit.
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
 * Mounts `DocumentWorkspace` as the element of a real `/runs/:runId/:document` route, with a fresh
 * `QueryClient` pre-seeded with `run` under `tailoringRunQueryKey(run.id)` — the state the poller
 * (`useTailoringRun`, read by `RunPage`) would already hold by the time a succeeded run reaches this
 * component, and the same key `useDocumentAutosave` reads/writes.
 */
function renderDocumentWorkspace(run: TailoringRun, documentSegment: 'cv' | 'cover_letter' = 'cv') {
  const queryClient = makeQueryClient();
  queryClient.setQueryData(tailoringRunQueryKey(run.id), run);
  const router = createMemoryRouter(
    [{ path: '/runs/:runId/:document', element: <DocumentWorkspace run={run} /> }],
    { initialEntries: [`/runs/${run.id}/${documentSegment}`] },
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

afterEach(() => {
  vi.useRealTimers();
});

describe('AC-29 — opening a document does not dirty it, even when the bridge normalizes it', () => {
  it('shows the normalized text (proof the bridge, not a plain-text passthrough, seeded it) and issues no PUT', async () => {
    vi.useFakeTimers();
    const run = makeRun({
      id: RUN_ID,
      status: 'succeeded',
      tailored_cv: normalizationFixtureMarkdown,
      cover_letter: 'a cover letter',
    });
    const fetchMock = stubDocumentFetch({});

    renderDocumentWorkspace(run, 'cv');

    // The fixtures module's hand-transcribed expected Markdown ("**bold**") never appears verbatim
    // in the rendered DOM, because the bridge turns `**bold**` into a real `<strong>` element with no
    // asterisks anywhere in its text. Assert what the DOM actually carries: a STRONG-tagged "bold"
    // and the plain rendered sentence, plus proof the un-normalized source spelling is gone.
    const pane = paneFor('cv');
    expect(within(pane).getByText('bold').tagName).toBe('STRONG');
    expect(pane).toHaveTextContent('A paragraph with bold text that should normalize.');
    expect(pane).not.toHaveTextContent('__bold__');

    await advance(AUTOSAVE_DEBOUNCE_MS);

    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(0);
  });
});

describe('AC-30 — both editors stay mounted; the URL only toggles visibility', () => {
  it('switching from /cv to /cover_letter and back hides/shows panes and keeps typed text, with no PUT needed', async () => {
    const run = makeRun({
      id: RUN_ID,
      status: 'succeeded',
      tailored_cv: 'CV seed text',
      cover_letter: 'Letter seed text',
    });
    // "No PUT needed" (the spec's own wording) is not "no PUT happened" — the AC-31 companion test
    // below proves the tab switch *does* flush a real PUT for a dirty document. What AC-30 actually
    // promises is that the typed text survives the switch on its own, independent of whether that
    // save succeeds. So the stub here makes the PUT fail outright, and the proof is that the text is
    // still there and the cache is untouched — never a call count, which would just contradict AC-31.
    stubDocumentFetch({
      putDocument: { cv: () => Promise.reject(new TypeError('Failed to fetch')) },
    });
    const { router, queryClient } = renderDocumentWorkspace(run, 'cv');

    expect(paneFor('cv')).toBeVisible();

    const user = userEvent.setup();
    await user.click(editableIn(paneFor('cv')));
    await user.type(editableIn(paneFor('cv')), ' plus typed text');

    await act(async () => {
      await router.navigate(`/runs/${RUN_ID}/cover_letter`);
    });

    expect(paneFor('cv')).not.toBeVisible();
    expect(paneFor('cover_letter')).toBeVisible();

    await act(async () => {
      await router.navigate(`/runs/${RUN_ID}/cv`);
    });

    expect(paneFor('cv')).toBeVisible();
    expect(paneFor('cv')).toHaveTextContent('CV seed text plus typed text');
    expect(queryClient.getQueryData(tailoringRunQueryKey(RUN_ID))).toEqual(run);
  });

  it('an unsaved document shows a marker on its own tab while the other document is the visible one', async () => {
    const run = makeRun({
      id: RUN_ID,
      status: 'succeeded',
      tailored_cv: 'CV seed text',
      cover_letter: 'Letter seed text',
    });
    stubDocumentFetch({});
    const { router } = renderDocumentWorkspace(run, 'cv');

    const user = userEvent.setup();
    await user.click(editableIn(paneFor('cv')));
    await user.type(editableIn(paneFor('cv')), ' edited');

    await act(async () => {
      await router.navigate(`/runs/${RUN_ID}/cover_letter`);
    });

    const cvTab = Array.from(document.querySelectorAll('[role="tab"]')).find((tab) =>
      (tab.getAttribute('href') ?? '').endsWith('/cv'),
    );
    if (cvTab === undefined) {
      throw new Error('no cv tab rendered');
    }
    expect(cvTab).toHaveTextContent(/unsaved/i);
  });

  it('AC-31 companion: switching away from a dirty document flushes the save immediately, without waiting for the debounce', async () => {
    const run = makeRun({
      id: RUN_ID,
      status: 'succeeded',
      tailored_cv: 'CV seed text',
      cover_letter: 'Letter seed text',
    });
    const fetchMock = stubDocumentFetch({
      putDocument: {
        cv: () =>
          Promise.resolve(jsonResponse(200, { ...run, version: 2, tailored_cv: 'CV edited' })),
      },
    });
    const { router } = renderDocumentWorkspace(run, 'cv');

    // Typing goes through real timers — `userEvent`'s internals do not get on with fake ones even
    // with `delay: null` — and fake timers are switched on only once there is nothing left to type,
    // for the debounce/backoff math below.
    const user = userEvent.setup();
    await user.click(editableIn(paneFor('cv')));
    await user.type(editableIn(paneFor('cv')), ' edited');

    vi.useFakeTimers();
    // Well inside the debounce window — nothing should be in flight from the timer alone.
    await advance(200);
    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(0);

    await act(async () => {
      await router.navigate(`/runs/${RUN_ID}/cover_letter`);
    });
    await flushMicrotasks();

    expect(countCallsTo(fetchMock, putPath(RUN_ID, 'cv'), 'PUT')).toBe(1);
  });
});

describe('AC-34 — session expiry and unrecoverable saves', () => {
  it('a 401 makes the editor read-only, names the reason, offers a link home, keeps the text on screen, and does not navigate', async () => {
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
      },
    });
    const { router } = renderDocumentWorkspace(run, 'cv');

    // Typing under real timers — see the AC-31 companion test's comment above.
    const user = userEvent.setup();
    await user.click(editableIn(paneFor('cv')));
    await user.type(editableIn(paneFor('cv')), ' edited');

    vi.useFakeTimers();
    flushDirtySaveViaVisibilityChange();
    await flushMicrotasks();
    await flushMicrotasks();

    expect(editableIn(paneFor('cv'))).toHaveAttribute('contenteditable', 'false');
    expect(screen.getByText(/session expired/i)).toBeInTheDocument();
    expect(screen.getByText(/24 hours/i)).toBeInTheDocument();
    const homeLink = screen.getByRole('link', { name: /home|workspace/i });
    expect(homeLink).toHaveAttribute('href', '/');
    expect(paneFor('cv')).toHaveTextContent('CV seed text edited');
    expect(router.state.location.pathname).toBe(`/runs/${RUN_ID}/cv`);
  });

  it("a network failure retries and then shows Couldn't save with a working Retry, never losing the text; beforeunload is registered while dirty", async () => {
    const addEventListenerSpy = vi.spyOn(window, 'addEventListener');
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

    // Typing under real timers — see the AC-31 companion test's comment above.
    const user = userEvent.setup();
    await user.click(editableIn(paneFor('cv')));
    await user.type(editableIn(paneFor('cv')), ' edited');

    expect(addEventListenerSpy).toHaveBeenCalledWith(
      'beforeunload',
      expect.any(Function) as unknown,
    );

    vi.useFakeTimers();
    flushDirtySaveViaVisibilityChange(); // the first attempt
    await advance(1000); // retry #1 backoff
    await advance(2000); // retry #2 backoff
    await advance(4000); // retry #3 backoff — retries spent
    await flushMicrotasks();

    expect(screen.getByText(/couldn.t save/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
    expect(paneFor('cv')).toHaveTextContent('CV seed text edited');
  });
});

describe('AC-36 — the footer states where the CV now lives', () => {
  it('names the exact 24-hour, not-kept-in-the-browser sentence the implementer must match', () => {
    const run = makeRun({
      id: RUN_ID,
      status: 'succeeded',
      tailored_cv: 'CV seed text',
      cover_letter: 'Letter seed text',
    });
    stubDocumentFetch({});

    renderDocumentWorkspace(run, 'cv');

    expect(
      screen.getByText(
        'These documents are stored for 24 hours and are never kept in your browser.',
      ),
    ).toBeInTheDocument();
  });
});
