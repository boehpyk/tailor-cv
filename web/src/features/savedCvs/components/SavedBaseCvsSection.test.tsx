import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { jsonResponse } from '@/test/fixtures';

import { __resetForTests, authStore } from '@/features/auth/authStore';

import { SAVED_CV_RETENTION_NOTICE, confirmDeleteMessage } from '../savedCvsCopy';
import { SavedBaseCvsSection } from './SavedBaseCvsSection';

import type { SavedBaseCv } from '../types';
import type { User } from '@/features/auth/types';

/**
 * T26 RED — AC-34, AC-35, AC-36, AC-44, against feature-spec.md and technical-plan.md §7 (the
 * Loading/error/empty/success table), never against `SavedBaseCvsSection.tsx`'s T25 skeleton, which
 * renders `null` unconditionally regardless of auth state or query result. Every assertion below
 * fails on "unable to find the expected role/text" — a legitimate assertion red — never on an
 * `ImportError`: the component, the copy constants and every hook it will call already exist.
 *
 * Mounted authenticated (the section itself does not gate on auth — that is `RequireAuth`'s job,
 * unchanged from 2.1 — so every test here starts from `authStore.setAuthenticated`, as a real mount
 * inside `RequireAuth` would have arrived, exactly `AccountPage.test.tsx`'s pattern).
 */

const USER: User = {
  id: 'user-1',
  email: 'alex@example.com',
  created_at: '2026-09-25T10:00:00Z',
  role: 'user' as const,
};
const AUTHENTICATED_RESPONSE = {
  access_token: 'token-abc',
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

/** A `fetch` stub keyed by `"<METHOD> <path>"`, counting calls per key like 1.1's `stubFetch`. */
function makeFetchMock(handlers: Record<string, Handler>): ReturnType<typeof vi.fn> {
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

function renderSection(queryClient?: QueryClient) {
  const client = queryClient ?? makeQueryClient();
  return {
    ...render(
      <QueryClientProvider client={client}>
        <SavedBaseCvsSection />
      </QueryClientProvider>,
    ),
    queryClient: client,
  };
}

beforeEach(() => {
  __resetForTests();
  authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('SavedBaseCvsSection — AC-34 list states', () => {
  it('loading: role=status, "Loading your saved CVs…"', () => {
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () => new Promise<Response>(() => undefined),
    });

    renderSection();

    expect(screen.getByRole('status')).toHaveTextContent('Loading your saved CVs…');
  });

  it('error: role=alert, "Couldn\'t load your saved CVs" + Retry — never the empty state', async () => {
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () =>
        jsonResponse(503, { error: { code: 'service_unavailable', message: 'down' } }),
    });

    renderSection();

    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't load your saved CVs");
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
    expect(screen.queryByText('No saved CVs yet')).not.toBeInTheDocument();
  });

  it('empty: "No saved CVs yet" + the upload control + AC-44 retention notice', async () => {
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () => jsonResponse(200, { items: [] }),
    });

    renderSection();

    expect(await screen.findByText('No saved CVs yet')).toBeInTheDocument();
    expect(screen.getByText(SAVED_CV_RETENTION_NOTICE)).toBeInTheDocument();
    expect(screen.getByLabelText(/upload a cv to your account/i)).toBeInTheDocument();
  });

  it('success: a labelled CV shows its label AND its filename, its status, its size, Rename and Delete', async () => {
    const cv = makeSavedCv({ label: 'Backend roles', original_filename: 'jane-cv.pdf' });
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () => jsonResponse(200, { items: [cv] }),
    });

    renderSection();

    expect(await screen.findByText('Backend roles')).toBeInTheDocument();
    expect(screen.getByText('jane-cv.pdf')).toBeInTheDocument();
    expect(screen.getByText('Ready to use')).toBeInTheDocument();
    expect(screen.getByText('2 KB')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Rename' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument();
  });

  it('success: an unlabelled CV shows its filename as the display name (no separate label text)', async () => {
    const cv = makeSavedCv({ label: null, original_filename: 'no-label.pdf' });
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () => jsonResponse(200, { items: [cv] }),
    });

    renderSection();

    expect(await screen.findByText('no-label.pdf')).toBeInTheDocument();
  });

  it("an extraction_failed CV shows the server's failure_message on its row", async () => {
    const cv = makeSavedCv({
      status: 'extraction_failed',
      character_count: null,
      failure_reason: 'no_text_layer',
      failure_message: "Couldn't read this scanned CV.",
    });
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () => jsonResponse(200, { items: [cv] }),
    });

    renderSection();

    expect(await screen.findByText("Couldn't read this scanned CV.")).toBeInTheDocument();
  });
});

describe('SavedBaseCvsSection — AC-35 upload to the account', () => {
  it('uploading: shows "Uploading and reading your CV…" while the request is in flight', async () => {
    const user = userEvent.setup();
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () => jsonResponse(200, { items: [] }),
      'POST /api/me/base-cvs': () => new Promise<Response>(() => undefined),
    });

    renderSection();
    const input = await screen.findByLabelText(/upload a cv to your account/i);
    const file = new File(['%PDF-1.4 body'], 'resume.pdf', { type: 'application/pdf' });
    await user.upload(input, file);

    expect(await screen.findByText('Uploading and reading your CV…')).toBeInTheDocument();
  });

  it('success: the new saved CV appears as a row after a 201', async () => {
    const user = userEvent.setup();
    const uploaded = makeSavedCv({ id: 'saved-cv-new', original_filename: 'resume.pdf' });
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': (_init, callNumber) =>
        jsonResponse(200, { items: callNumber === 1 ? [] : [uploaded] }),
      'POST /api/me/base-cvs': () => jsonResponse(201, uploaded),
    });

    renderSection();
    const input = await screen.findByLabelText(/upload a cv to your account/i);
    const file = new File(['%PDF-1.4 body'], 'resume.pdf', { type: 'application/pdf' });
    await user.upload(input, file);

    expect(await screen.findByText('resume.pdf')).toBeInTheDocument();
  });

  it('an extraction_failed upload result (201) is a success, not an error — the row shows the message', async () => {
    const user = userEvent.setup();
    const failed = makeSavedCv({
      id: 'saved-cv-scan',
      status: 'extraction_failed',
      character_count: null,
      failure_reason: 'no_text_layer',
      failure_message: "We couldn't read any text from this file.",
    });
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': (_init, callNumber) =>
        jsonResponse(200, { items: callNumber === 1 ? [] : [failed] }),
      'POST /api/me/base-cvs': () => jsonResponse(201, failed),
    });

    renderSection();
    const input = await screen.findByLabelText(/upload a cv to your account/i);
    await user.upload(input, new File(['scan'], 'scan.pdf', { type: 'application/pdf' }));

    expect(
      await screen.findByText("We couldn't read any text from this file."),
    ).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each([
    ['file_too_large', 413, 'That file is too large. Try a smaller file, up to 10 MB.'],
    ['unsupported_format', 415, "That file isn't a PDF, DOCX or TXT file. Try one of those."],
    ['empty_file', 422, 'That file is empty. Choose the file with your CV in it.'],
    ['invalid_filename', 422, "That file's name can't be used. Rename the file and try again."],
    [
      'storage_unavailable',
      503,
      "We couldn't store your file right now. Nothing was saved — try again in a moment.",
    ],
    [
      'service_unavailable',
      503,
      'Saving CVs is unavailable right now. Nothing was saved — try again in a moment.',
    ],
  ] as const)(
    'AC-35 refusal %s renders its own distinct copy',
    async (code, status, expectedText) => {
      const user = userEvent.setup();
      makeFetchMock({
        'GET /api/auth/me': () => jsonResponse(200, USER),
        'GET /api/me/base-cvs': () => jsonResponse(200, { items: [] }),
        'POST /api/me/base-cvs': () =>
          jsonResponse(status, { error: { code, message: 'server said so' } }),
      });

      renderSection();
      const input = await screen.findByLabelText(/upload a cv to your account/i);
      await user.upload(input, new File(['x'], 'resume.pdf', { type: 'application/pdf' }));

      expect(await screen.findByText(expectedText)).toBeInTheDocument();
    },
  );

  it("AC-35: 409 too_many_saved_base_cvs shows the server's own message (it names the cap) and the control stays enabled", async () => {
    const user = userEvent.setup();
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () => jsonResponse(200, { items: [] }),
      'POST /api/me/base-cvs': () =>
        jsonResponse(409, {
          error: {
            code: 'too_many_saved_base_cvs',
            message: 'You already have 5 saved CVs, the most this account can hold.',
          },
        }),
    });

    renderSection();
    const input = await screen.findByLabelText(/upload a cv to your account/i);
    await user.upload(input, new File(['x'], 'resume.pdf', { type: 'application/pdf' }));

    expect(
      await screen.findByText('You already have 5 saved CVs, the most this account can hold.'),
    ).toBeInTheDocument();
    expect(input).not.toBeDisabled();
  });

  it('AC-35: 429 rate_limited names the Retry-After seconds', async () => {
    const user = userEvent.setup();
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () => jsonResponse(200, { items: [] }),
      'POST /api/me/base-cvs': () =>
        new Response(JSON.stringify({ error: { code: 'rate_limited', message: 'slow down' } }), {
          status: 429,
          headers: { 'Content-Type': 'application/json', 'Retry-After': '37' },
        }),
    });

    renderSection();
    const input = await screen.findByLabelText(/upload a cv to your account/i);
    await user.upload(input, new File(['x'], 'resume.pdf', { type: 'application/pdf' }));

    expect(await screen.findByText(/try again in 37 seconds/i)).toBeInTheDocument();
  });

  it('AC-48: the upload refusal is role="alert" and linked to the file input by aria-describedby', async () => {
    const user = userEvent.setup();
    makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': () => jsonResponse(200, { items: [] }),
      'POST /api/me/base-cvs': () =>
        jsonResponse(422, { error: { code: 'empty_file', message: 'server said so' } }),
    });

    renderSection();
    const input = await screen.findByLabelText(/upload a cv to your account/i);
    await user.upload(input, new File(['x'], 'resume.pdf', { type: 'application/pdf' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('That file is empty. Choose the file with your CV in it.');
    expect(input.getAttribute('aria-describedby')).toBe(alert.id);
  });
});

describe('SavedBaseCvsSection — AC-36 delete', () => {
  const CV = makeSavedCv({ id: 'saved-cv-del', label: null, original_filename: 'to-delete.pdf' });

  function stubList(deleteHandler: Handler, listAfterDelete: readonly SavedBaseCv[] = []) {
    return makeFetchMock({
      'GET /api/auth/me': () => jsonResponse(200, USER),
      'GET /api/me/base-cvs': (_init, callNumber) =>
        jsonResponse(200, { items: callNumber === 1 ? [CV] : listAfterDelete }),
      [`DELETE /api/me/base-cvs/${CV.id}`]: deleteHandler,
    });
  }

  it('opens a role="alertdialog" confirmation with the exact AC-36 message around the CV\'s name', async () => {
    stubList(() => new Promise<Response>(() => undefined));
    renderSection();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));

    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(confirmDeleteMessage('to-delete.pdf'))).toBeInTheDocument();
  });

  // AC-48: opening the dialog moves focus into it (onto Cancel, the safe choice), and closing it —
  // by Escape, here — gives focus back to the row's own Delete button that opened it, not to the
  // document body or some other element.
  it("AC-48: opening moves focus to Cancel; Escape closes it and returns focus to the row's Delete button", async () => {
    stubList(() => Promise.reject(new Error('DELETE must not be called after Escape cancels')));
    renderSection();
    const deleteButton = await screen.findByRole('button', { name: 'Delete' });
    deleteButton.focus();

    fireEvent.click(deleteButton);
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus();

    fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' });

    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: 'Delete' })).toHaveFocus();
  });

  it('Escape cancels the dialog without deleting anything', async () => {
    const fetchMock = stubList(() =>
      Promise.reject(new Error('DELETE must not be called after Escape cancels')),
    );
    renderSection();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    const dialog = await screen.findByRole('alertdialog');

    fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' });

    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    });
    expect(
      fetchMock.mock.calls.some(
        ([, init]) => (init as RequestInit | undefined)?.method === 'DELETE',
      ),
    ).toBe(false);
    // No optimistic removal either way: the row is still there.
    expect(screen.getByText('to-delete.pdf')).toBeInTheDocument();
  });

  it('confirming shows "Deleting…" on the row while pending — no optimistic removal', async () => {
    stubList(() => new Promise<Response>(() => undefined));
    renderSection();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    expect(await screen.findByText('Deleting…')).toBeInTheDocument();
    // Still on screen: an irreversible action is shown as done only once the server says so.
    expect(screen.getByText('to-delete.pdf')).toBeInTheDocument();
  });

  it('204: the row is removed', async () => {
    stubList(() => new Response(null, { status: 204 }), []);
    renderSection();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    await waitFor(() => {
      expect(screen.queryByText('to-delete.pdf')).not.toBeInTheDocument();
    });
  });

  it('404: treated as already gone — the row is removed, never shown as an error', async () => {
    stubList(
      () => jsonResponse(404, { error: { code: 'base_cv_not_found', message: 'gone' } }),
      [],
    );
    renderSection();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    await waitFor(() => {
      expect(screen.queryByText('to-delete.pdf')).not.toBeInTheDocument();
    });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('503: the row stays and says it was not deleted', async () => {
    stubList(
      () => jsonResponse(503, { error: { code: 'service_unavailable', message: 'down' } }),
      [CV],
    );
    renderSection();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    expect(await screen.findByText('Not deleted — try again')).toBeInTheDocument();
    expect(screen.getByText('to-delete.pdf')).toBeInTheDocument();
  });
});
