import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, renderHook, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import { useDocumentAutosave } from '@/features/editor/hooks/useDocumentAutosave';
import { AUTOSAVE_DEBOUNCE_MS } from '@/features/editor/saveState';
import { makeExportJob } from '@/features/export/test/fixtures';
import { WorkspaceScopeProvider } from '@/features/scope/WorkspaceScope';
import { scopeMap } from '@/features/scope/scopeMap';
import { tailoringRunQueryKey } from '@/features/tailoring/hooks/useTailoringRun';
import {
  USER_A,
  bearerFor,
  callsTo,
  hang,
  makeAccountRun,
  ok,
  signInAs,
  signedInRoutes,
  status,
  stubAccountFetch,
} from '@/test/accountFetch';
import { jsonResponse, makeRun } from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';

import type { DocumentEditorHandle } from '@/features/editor/hooks/useDocumentEditor';
import type { RouteHandler } from '@/test/accountFetch';
import type { ReactNode } from 'react';

/**
 * T30 RED — re-open (AC-46), re-export (AC-47), credential discipline (AC-48) and the run page's
 * half of AC-49, all on `/history/:runId/:document`: 1.4's run page and 1.5's export bar mounted in
 * account scope by the route. Every request is recorded, so "against `/api/me/`, with the bearer" is
 * asserted on the calls themselves.
 *
 * T28 already threaded the scope through the hooks, so some of these are **green on arrival** — the
 * plain re-read of a run, its polling, the editor and the export bar's own paths now follow the
 * route. Those are listed in the commit with that reason; they pin that the route really does put
 * the page in account scope. What is red is what T31 still owes: the account 404 copy and link, *Try
 * again* landing in `/history`, and every account sentence that must not claim 24-hour deletion.
 */

const RUN_ID = 'run-1';
const RUN_URL = `/history/${RUN_ID}/cv`;

function reopen(run: RouteHandler, extra: Record<string, RouteHandler> = {}) {
  return stubAccountFetch({
    ...signedInRoutes(USER_A),
    'GET /api/me/tailoring-runs/:id': run,
    'GET /api/me/tailoring-runs/:id/exports': ok({ items: [] }),
    ...extra,
  });
}

function succeededRun() {
  return makeAccountRun({
    id: RUN_ID,
    status: 'succeeded',
    base_cv_id: 'cv-1',
    job_posting_id: 'posting-1',
    tailored_cv: 'my tailored cv text',
    cover_letter: 'my cover letter text',
    tailored_cv_character_count: 19,
    cover_letter_character_count: 20,
    completed_at: '2026-09-20T10:00:09Z',
  });
}

function stubObjectUrl(): { readonly createObjectURL: ReturnType<typeof vi.fn> } {
  const createObjectURL = vi.fn(() => 'blob:mock-url');
  URL.createObjectURL = createObjectURL;
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
  return { createObjectURL };
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  __resetForTests();
});

// --- AC-46: re-open ---------------------------------------------------------------------------------

describe('Re-open a history run (AC-46)', () => {
  it("loading: 1.4's line, while the account run is read with the bearer", async () => {
    const fetch = reopen(hang);

    renderWithRouter(RUN_URL);

    expect(await screen.findByText('Loading your tailoring run…')).toBeInTheDocument();
    await waitFor(() => {
      expect(callsTo(fetch, 'GET', '/api/me/tailoring-runs/:id')).toHaveLength(1);
    });
    expect(callsTo(fetch, 'GET', '/api/me/tailoring-runs/:id')[0]?.authorization).toBe(
      bearerFor(USER_A),
    );
    expect(callsTo(fetch, 'GET', '/api/tailoring-runs/:id')).toHaveLength(0);
  });

  it('working: a queued run shows 1.3\'s "Waiting for a worker…"', async () => {
    reopen(ok(makeAccountRun({ id: RUN_ID, status: 'queued' })));

    renderWithRouter(RUN_URL);

    expect(await screen.findByText('Waiting for a worker…')).toBeInTheDocument();
  });

  it('succeeded: the documents', async () => {
    reopen(ok(succeededRun()));

    renderWithRouter(RUN_URL);

    expect(await screen.findByText('my tailored cv text')).toBeInTheDocument();
  });

  it('failed: Try again posts the same inputs to /api/me/tailoring-runs and opens /history/{new}', async () => {
    const fetch = reopen(
      ok(
        makeAccountRun({
          id: RUN_ID,
          status: 'failed',
          failure_reason: 'llm_timed_out',
          retryable: true,
          base_cv_id: 'cv-1',
          job_posting_id: 'posting-1',
        }),
      ),
      {
        'POST /api/me/tailoring-runs': () =>
          jsonResponse(202, makeAccountRun({ id: 'run-new', status: 'queued' })),
      },
    );
    const user = userEvent.setup();

    const { router } = renderWithRouter(RUN_URL);
    await user.click(await screen.findByRole('button', { name: /try again/i }));

    await waitFor(() => {
      expect(router.state.location.pathname).toMatch(/^\/history\/run-new(\/cv)?$/);
    });
    const [retried] = callsTo(fetch, 'POST', '/api/me/tailoring-runs');
    expect(retried?.body).toEqual({ base_cv_id: 'cv-1', job_posting_id: 'posting-1' });
    expect(retried?.authorization).toBe(bearerFor(USER_A));
    expect(callsTo(fetch, 'POST', '/api/tailoring-runs')).toHaveLength(0);
  });

  it('a base_cv_deleted failure offers no Try again', async () => {
    reopen(
      ok(
        makeAccountRun({
          id: RUN_ID,
          status: 'failed',
          failure_reason: 'base_cv_deleted',
          retryable: false,
        }),
      ),
    );

    renderWithRouter(RUN_URL);

    expect(
      await screen.findByText('The CV this was using was deleted before tailoring started.'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument();
  });

  it('404: "This isn\'t in your history" and a link to /history', async () => {
    reopen(status(404, 'tailoring_run_not_found', "We couldn't find that run."));

    renderWithRouter(RUN_URL);

    expect(await screen.findByText("This isn't in your history")).toBeInTheDocument();
    expect(
      screen.getAllByRole('link').some((link) => link.getAttribute('href') === '/history'),
    ).toBe(true);
  });

  it("unreadable: 1.3's copy with Check again", async () => {
    vi.useFakeTimers();
    reopen(() => Promise.reject(new TypeError('Failed to fetch')));

    renderWithRouter(RUN_URL);
    for (const ms of [0, 1000, 2000, 4000, 1000]) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ms);
      });
    }

    expect(
      screen.getByText('We lost contact with your tailoring run. It may still be working.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /check again/i })).toBeInTheDocument();
  });
});

// --- AC-46: the autosave in account scope ----------------------------------------------------------

function makeFakeHandle(seed: string) {
  let current = seed;
  let baseline = seed;
  const listeners = new Set<() => void>();
  const editor = {
    on: (event: string, fn: () => void) => {
      if (event === 'update') listeners.add(fn);
      return editor;
    },
    off: (event: string, fn: () => void) => {
      if (event === 'update') listeners.delete(fn);
      return editor;
    },
    setEditable: vi.fn(),
  };
  const handle: DocumentEditorHandle = {
    editor: editor as unknown as DocumentEditorHandle['editor'],
    serialize: () => current,
    isDirty: () => current !== baseline,
    reseed: (text: string) => {
      current = text;
      baseline = text;
    },
  };
  return {
    handle,
    edit: (text: string) => {
      current = text;
      listeners.forEach((fn) => {
        fn();
      });
    },
  };
}

describe('Autosave on a history run (AC-46)', () => {
  it('PUTs /api/me/tailoring-runs/{id}/documents/cv with the bearer', async () => {
    vi.useFakeTimers();
    const account = scopeMap({ kind: 'account', userId: USER_A.id });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
    });
    const run = makeAccountRun({
      id: RUN_ID,
      status: 'succeeded',
      version: 3,
      tailored_cv: 'seed',
    });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID, account), run);
    const fetch = stubAccountFetch({
      'PUT /api/me/tailoring-runs/:id/documents/:kind': () =>
        jsonResponse(200, { ...run, version: 4, tailored_cv: 'seed edited' }),
    });
    const { handle, edit } = makeFakeHandle('seed');
    function Wrapper({ children }: { children: ReactNode }): React.JSX.Element {
      return (
        <QueryClientProvider client={queryClient}>
          <WorkspaceScopeProvider scope={{ kind: 'account', userId: USER_A.id }}>
            {children}
          </WorkspaceScopeProvider>
        </QueryClientProvider>
      );
    }

    renderHook(() => useDocumentAutosave(RUN_ID, 'cv', handle), { wrapper: Wrapper });
    act(() => {
      edit('seed edited');
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(AUTOSAVE_DEBOUNCE_MS + 10);
    });

    const puts = callsTo(fetch, 'PUT', '/api/me/tailoring-runs/:id/documents/:kind');
    expect(puts).toHaveLength(1);
    expect(puts[0]?.path).toBe(`/api/me/tailoring-runs/${RUN_ID}/documents/cv`);
    expect(puts[0]?.authorization).toBe(bearerFor(USER_A));
    expect(puts[0]?.body).toEqual({ content: 'seed edited', expected_version: 3 });
  });
});

// --- AC-47: re-export -------------------------------------------------------------------------------

describe('Re-export a history run (AC-47)', () => {
  it("a PDF export goes to /api/me/…/exports and the run's account export list is polled, with the bearer", async () => {
    // 1.5 polls the run's export *list* (`useExportJobs`' `refetchInterval`), not `/export-jobs/{id}`;
    // in account scope that list is `/api/me/tailoring-runs/{id}/exports`.
    const queued = makeExportJob({
      id: 'job-1',
      tailoring_run_id: RUN_ID,
      format: 'pdf',
      status: 'queued',
    });
    const fetch = reopen(ok(succeededRun()), {
      'POST /api/me/tailoring-runs/:id/exports': () => jsonResponse(202, queued),
      'GET /api/me/tailoring-runs/:id/exports': (_call, n) =>
        jsonResponse(200, { items: n === 1 ? [] : [queued] }),
    });

    renderWithRouter(RUN_URL);
    await screen.findByText('my tailored cv text');
    fireEvent.click(await screen.findByRole('button', { name: 'PDF' }));

    await waitFor(() => {
      expect(callsTo(fetch, 'POST', '/api/me/tailoring-runs/:id/exports')).toHaveLength(1);
    });
    await waitFor(() => {
      expect(callsTo(fetch, 'GET', '/api/me/tailoring-runs/:id/exports').length).toBeGreaterThan(1);
    });
    const exportCalls = fetch.calls.filter((call) => /export/.test(call.path));
    expect(exportCalls.every((call) => call.path.startsWith('/api/me/'))).toBe(true);
    expect(exportCalls.every((call) => call.authorization === bearerFor(USER_A))).toBe(true);
  });

  it('a ready file downloads through the bearer as a blob — never a bare link', async () => {
    const { createObjectURL } = stubObjectUrl();
    const fetch = reopen(ok(succeededRun()), {
      'GET /api/me/tailoring-runs/:id/exports': ok({
        items: [
          makeExportJob({
            id: 'job-ok',
            tailoring_run_id: RUN_ID,
            document: 'cv',
            format: 'pdf',
            status: 'ready',
            current: true,
            byte_size: 86016,
            file_url: '/api/me/export-jobs/job-ok/file',
          }),
        ],
      }),
      'GET /api/me/export-jobs/:id/file': () =>
        new Response(new Blob(['%PDF']), {
          status: 200,
          headers: { 'Content-Type': 'application/pdf' },
        }),
    });

    const { container } = renderWithRouter(RUN_URL);
    await screen.findByText('Download PDF · 84 KB', { exact: false });
    fireEvent.click(screen.getByRole('button', { name: /pdf/i }));

    await waitFor(() => {
      expect(createObjectURL).toHaveBeenCalled();
    });
    const [download] = callsTo(fetch, 'GET', '/api/me/export-jobs/:id/file');
    expect(download?.authorization).toBe(bearerFor(USER_A));
    expect(container.querySelectorAll('a[href^="/api/"]')).toHaveLength(0);
  });

  it('410 export_file_gone says Export again, and the retry is a new /api/me/ export', async () => {
    stubObjectUrl();
    const fetch = reopen(ok(succeededRun()), {
      'POST /api/me/tailoring-runs/:id/exports': () =>
        jsonResponse(
          202,
          makeExportJob({
            id: 'job-410-retry',
            tailoring_run_id: RUN_ID,
            format: 'pdf',
            status: 'queued',
            byte_size: null,
          }),
        ),
      'GET /api/me/tailoring-runs/:id/exports': ok({
        items: [
          makeExportJob({
            id: 'job-410',
            tailoring_run_id: RUN_ID,
            format: 'pdf',
            status: 'ready',
            byte_size: 86016,
          }),
        ],
      }),
      'GET /api/me/export-jobs/:id/file': status(410, 'export_file_gone'),
    });

    renderWithRouter(RUN_URL);
    await screen.findByText('Download PDF · 84 KB', { exact: false });
    fireEvent.click(screen.getByRole('button', { name: /pdf/i }));

    expect(
      await screen.findByText('That file is no longer available — Export again'),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /try again/i }));
    await waitFor(() => {
      expect(callsTo(fetch, 'POST', '/api/me/tailoring-runs/:id/exports')).toHaveLength(1);
    });
    expect(callsTo(fetch, 'POST', '/api/me/tailoring-runs/:id/exports')[0]?.authorization).toBe(
      bearerFor(USER_A),
    );
  });
});

// --- AC-48: credential discipline -------------------------------------------------------------------

describe('Credentials follow the route, not the sign-in (AC-48)', () => {
  it('a guest run opened while signed in is read from the guest route, without the bearer', async () => {
    const fetch = stubAccountFetch({
      ...signedInRoutes(USER_A),
      'GET /api/tailoring-runs/:id': ok(makeRun({ id: 'guest-run', status: 'queued' })),
    });

    renderWithRouter('/runs/guest-run/cv');

    expect(await screen.findByText('Waiting for a worker…')).toBeInTheDocument();
    const guestReads = callsTo(fetch, 'GET', '/api/tailoring-runs/:id');
    expect(guestReads.length).toBeGreaterThan(0);
    expect(guestReads.every((call) => call.authorization === null)).toBe(true);
    expect(fetch.calls.some((call) => call.path.startsWith('/api/me/tailoring-runs'))).toBe(false);
  });

  it('every /api/me/ call a history run makes carries the bearer', async () => {
    const fetch = reopen(ok(succeededRun()));

    renderWithRouter(RUN_URL);
    await screen.findByText('my tailored cv text');

    const meCalls = fetch.calls.filter((call) => call.path.startsWith('/api/me/'));
    expect(meCalls.length).toBeGreaterThan(0);
    expect(meCalls.every((call) => call.authorization === bearerFor(USER_A))).toBe(true);
  });
});

// --- AC-49: no 24-hour claim in account scope, still there in guest scope ---------------------------

describe('The retention sentence follows the scope (AC-49)', () => {
  it('a history run with its export bar never says "24 hours"', async () => {
    reopen(ok(succeededRun()));

    renderWithRouter(RUN_URL);
    await screen.findByText('my tailored cv text');
    await screen.findByRole('button', { name: 'PDF' });

    expect(document.body.textContent).not.toContain('24 hours');
  });

  it('the same page in guest scope still says it', async () => {
    __resetForTests();
    authStore.signOut('expired');
    stubAccountFetch({
      'GET /api/tailoring-runs/:id': ok(
        makeRun({
          id: 'guest-run',
          status: 'succeeded',
          tailored_cv: 'guest tailored cv text',
          cover_letter: 'guest letter',
          tailored_cv_character_count: 22,
          cover_letter_character_count: 12,
        }),
      ),
      'GET /api/tailoring-runs/:id/exports': ok({ items: [] }),
    });

    renderWithRouter('/runs/guest-run/cv');
    await screen.findByText('guest tailored cv text');
    await screen.findByRole('button', { name: 'PDF' });

    expect(document.body.textContent).toContain('24 hours');
  });
});
