import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';

import { jsonResponse } from '@/test/fixtures';

import { ApiError } from '@/api/client';
import { __resetForTests, authStore } from '@/features/auth/authStore';

import {
  PICKER_EMPTY_ACTION,
  PICKER_EMPTY_NOTE,
  PICKER_LEGEND,
  PICKER_RETENTION_NOTICE,
  PICKER_UNAVAILABLE_NOTE,
  SAVED_CVS_LOAD_ERROR_NOTE,
  SIGNED_OUT_NOTE,
  USE_THIS_CV_LABEL,
  USE_THIS_CV_PENDING_LABEL,
  copySavedCvErrorCopy,
} from '../savedCvsCopy';
import { SavedBaseCvPicker } from './SavedBaseCvPicker';

import type { SavedBaseCv } from '../types';
import type { User } from '@/features/auth/types';

/**
 * T26 RED — AC-38, AC-39, AC-45, against feature-spec.md and technical-plan.md §7, never against
 * `SavedBaseCvPicker.tsx`'s T25 skeleton, which renders `null` unconditionally. Every assertion
 * fails on "unable to find the expected role/text" (or, for the anonymous/booting cases, on the
 * discriminating *positive* control below proving the harness itself can render something), never
 * on an `ImportError`.
 *
 * **AC-39's "a 401 that survives one refresh" scenario** is built so the auth store stays
 * `authenticated` throughout: the refresh itself succeeds (200, a fresh token), but the *retried*
 * copy request is answered 401 `invalid_access_token` again regardless of which token it carries.
 * That is deliberately different from S-38's "refresh fails" case (which would carry the store to
 * `anonymous` and, per AC-38, unmount the picker before any mutation error could render) — it is
 * the literal reading of AC-39's own words, and it is the one scenario in which the picker is still
 * on screen to show the sentence at all.
 */

const USER: User = { id: 'user-1', email: 'alex@example.com', created_at: '2026-09-25T10:00:00Z' };
const AUTHENTICATED_RESPONSE = {
  access_token: 'token-1',
  token_type: 'Bearer' as const,
  expires_in: 900,
  user: USER,
};

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
}

function makeSavedCv(overrides: Partial<SavedBaseCv> = {}): SavedBaseCv {
  return {
    id: 'saved-cv-1',
    label: null,
    original_filename: 'jane-resume.pdf',
    content_type: 'application/pdf',
    size_bytes: 2048,
    status: 'extracted',
    character_count: 512,
    failure_reason: null,
    failure_message: null,
    uploaded_at: '2026-09-20T10:00:00Z',
    ...overrides,
  };
}

type Handler = (init: RequestInit | undefined, callNumber: number) => Response | Promise<Response>;

function makeFetchMock(handlers: Record<string, Handler>) {
  const counts = new Map<string, number>();
  const mock = vi.fn((input: string | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = init?.method ?? 'GET';
    const key = `${method} ${url}`;
    const handler = handlers[key];
    if (handler === undefined) {
      return Promise.reject(new Error(`unhandled fetch: ${key}`));
    }
    const callNumber = (counts.get(key) ?? 0) + 1;
    counts.set(key, callNumber);
    return Promise.resolve(handler(init, callNumber));
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

function renderPicker(queryClient?: QueryClient) {
  const client = queryClient ?? makeQueryClient();
  return {
    ...render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <SavedBaseCvPicker />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
    queryClient: client,
  };
}

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('SavedBaseCvPicker — AC-38 auth gate', () => {
  it('anonymous: renders nothing at all', () => {
    makeFetchMock({});
    authStore.signOut('expired');

    const { container } = renderPicker();

    expect(container).toBeEmptyDOMElement();
  });

  it('booting: renders nothing (no flicker) — a fresh store defaults to booting', () => {
    makeFetchMock({});

    const { container } = renderPicker();

    expect(container).toBeEmptyDOMElement();
  });

  it('unavailable: "Couldn\'t check your account…" + Retry', async () => {
    makeFetchMock({
      'POST /api/auth/refresh': () => Promise.reject(new TypeError('Failed to fetch')),
    });
    await authStore.bootstrap();

    renderPicker();

    expect(await screen.findByText(PICKER_UNAVAILABLE_NOTE)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('authenticated + error: the same load-failure copy as the account section, plus Retry', async () => {
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () =>
        jsonResponse(503, { error: { code: 'service_unavailable', message: 'down' } }),
    });
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);

    renderPicker();

    expect(await screen.findByText(SAVED_CVS_LOAD_ERROR_NOTE)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('authenticated + empty: "You have no saved CVs." + a link to /account', async () => {
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () => jsonResponse(200, { items: [] }),
    });
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);

    renderPicker();

    expect(await screen.findByText(PICKER_EMPTY_NOTE)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: PICKER_EMPTY_ACTION })).toHaveAttribute(
      'href',
      '/account',
    );
  });

  it('authenticated + success: a radiogroup with a legend, one radio per CV, newest first, the retention notice', async () => {
    const older = makeSavedCv({ id: 'saved-cv-older', uploaded_at: '2026-09-01T10:00:00Z' });
    const newer = makeSavedCv({
      id: 'saved-cv-newer',
      original_filename: 'newest.pdf',
      uploaded_at: '2026-09-20T10:00:00Z',
    });
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () => jsonResponse(200, { items: [newer, older] }),
    });
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);

    renderPicker();

    const group = await screen.findByRole('radiogroup', { name: PICKER_LEGEND });
    const radios = within(group).getAllByRole('radio');
    expect(radios).toHaveLength(2);
    expect(screen.getByText(PICKER_RETENTION_NOTICE)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: USE_THIS_CV_LABEL })).toBeInTheDocument();
  });

  it('the only CV is preselected when there is exactly one', async () => {
    const only = makeSavedCv();
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () => jsonResponse(200, { items: [only] }),
    });
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);

    renderPicker();

    const group = await screen.findByRole('radiogroup', { name: PICKER_LEGEND });
    expect(within(group).getByRole('radio')).toBeChecked();
  });

  it('an extraction_failed CV is listed but disabled, with its failure reason shown', async () => {
    const failed = makeSavedCv({
      id: 'saved-cv-failed',
      status: 'extraction_failed',
      character_count: null,
      failure_reason: 'corrupt',
      failure_message: "We couldn't read this file.",
    });
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () => jsonResponse(200, { items: [failed] }),
    });
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);

    renderPicker();

    const group = await screen.findByRole('radiogroup', { name: PICKER_LEGEND });
    expect(within(group).getByRole('radio')).toBeDisabled();
    expect(screen.getByText("We couldn't read this file.")).toBeInTheDocument();
  });
});

describe('SavedBaseCvPicker — AC-39 Use this CV', () => {
  const CV = makeSavedCv();

  function baseHandlers(copy: Handler): Record<string, Handler> {
    return {
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () => jsonResponse(200, { items: [CV] }),
      'POST /api/base-cvs/copies': copy,
    };
  }

  function selectAndClickUse() {
    const radio = screen.getByRole('radio');
    fireEvent.click(radio);
    fireEvent.click(screen.getByRole('button', { name: USE_THIS_CV_LABEL }));
  }

  it('pending: the button is disabled and reads the pending label', async () => {
    makeFetchMock(baseHandlers(() => new Promise<Response>(() => undefined)));
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    renderPicker();
    await screen.findByRole('radiogroup', { name: PICKER_LEGEND });

    selectAndClickUse();

    expect(await screen.findByText(USE_THIS_CV_PENDING_LABEL)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: USE_THIS_CV_PENDING_LABEL })).toBeDisabled();
  });

  it('a same-tick double click sends exactly one POST /api/base-cvs/copies', async () => {
    const fetchMock = makeFetchMock(baseHandlers(() => new Promise<Response>(() => undefined)));
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    renderPicker();
    await screen.findByRole('radiogroup', { name: PICKER_LEGEND });

    fireEvent.click(screen.getByRole('radio'));
    const button = screen.getByRole('button', { name: USE_THIS_CV_LABEL });
    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => {
      expect(screen.getByText(USE_THIS_CV_PENDING_LABEL)).toBeInTheDocument();
    });
    const copyPosts = fetchMock.mock.calls.filter(
      ([input, init]) =>
        (typeof input === 'string' ? input : input.toString()) === '/api/base-cvs/copies' &&
        init?.method === 'POST',
    );
    expect(copyPosts).toHaveLength(1);
  });

  it("success invalidates ['intake', 'baseCvs']", async () => {
    const handlers = baseHandlers(() =>
      jsonResponse(201, { id: 'guest-cv-new', origin: 'copied_from_saved' }),
    );
    makeFetchMock(handlers);
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    const queryClient = makeQueryClient();
    // `setQueryData` alone creates a cache entry with no observer — nothing in this render tree
    // calls `useQuery(['intake','baseCvs'])`, so a real refetch would never happen no matter what
    // the mutation does. Reading the query's own `isInvalidated` flag is what actually tests "was
    // `invalidateQueries` called with this key", independent of whether anything is watching it.
    queryClient.setQueryData(['intake', 'baseCvs'], { items: [] });
    renderPicker(queryClient);
    await screen.findByRole('radiogroup', { name: PICKER_LEGEND });

    selectAndClickUse();

    await waitFor(() => {
      expect(queryClient.getQueryState(['intake', 'baseCvs'])?.isInvalidated).toBe(true);
    });
  });

  it.each([
    ['base_cv_not_extracted', 409],
    ['too_many_base_cvs', 409],
    ['saved_base_cv_file_gone', 410],
    ['validation_error', 422],
    ['storage_unavailable', 503],
    ['service_unavailable', 503],
  ] as const)('AC-39 refusal %s renders its own distinct copy', async (code, status) => {
    makeFetchMock(
      baseHandlers(() => jsonResponse(status, { error: { code, message: 'server said so' } })),
    );
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    renderPicker();
    await screen.findByRole('radiogroup', { name: PICKER_LEGEND });

    selectAndClickUse();

    // `copySavedCvErrorCopy` (real, already-implemented code — `savedCvsCopy.ts`) is the single
    // source of truth for what each `code` says; asking it directly, rather than guessing the
    // sentence here, is what keeps this test from drifting from that module's own wording.
    const expectedText = copySavedCvErrorCopy(new ApiError(status, 'server said so', code));
    expect(await screen.findByText(expectedText)).toBeInTheDocument();
  });

  it('a 404 refetches the saved list and says the CV was deleted', async () => {
    const handlers = baseHandlers(() =>
      jsonResponse(404, { error: { code: 'base_cv_not_found', message: 'gone' } }),
    );
    let listCalls = 0;
    handlers['GET /api/me/base-cvs'] = () => {
      listCalls += 1;
      return jsonResponse(200, { items: listCalls === 1 ? [CV] : [] });
    };
    makeFetchMock(handlers);
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    renderPicker();
    await screen.findByRole('radiogroup', { name: PICKER_LEGEND });

    fireEvent.click(screen.getByRole('radio'));
    fireEvent.click(screen.getByRole('button', { name: USE_THIS_CV_LABEL }));

    await waitFor(() => {
      expect(listCalls).toBeGreaterThanOrEqual(2);
    });
    expect(
      screen.getByText('That saved CV was deleted, so there is nothing to copy.'),
    ).toBeInTheDocument();
  });

  it(
    'a 401 that survives one refresh (the refresh itself succeeds, the retry is refused again) ' +
      'shows the signed-out copy',
    async () => {
      const handlers: Record<string, Handler> = {
        'GET /api/auth/me': () => jsonResponse(200, USER),
        'GET /api/me/base-cvs': () => jsonResponse(200, { items: [CV] }),
        'POST /api/base-cvs/copies': () =>
          jsonResponse(401, { error: { code: 'invalid_access_token', message: 'bad token' } }),
        'POST /api/auth/refresh': () =>
          jsonResponse(200, {
            access_token: 'token-2',
            token_type: 'Bearer',
            expires_in: 900,
            user: USER,
          }),
      };
      makeFetchMock(handlers);
      authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
      renderPicker();
      await screen.findByRole('radiogroup', { name: PICKER_LEGEND });

      selectAndClickUse();

      expect(await screen.findByText(SIGNED_OUT_NOTE)).toBeInTheDocument();
      // The store itself is still authenticated: the refresh succeeded, only the retried request
      // was refused — so the picker is still on screen to show this sentence at all (AC-38).
      expect(authStore.getSnapshot().status).toBe('authenticated');
    },
  );
});
