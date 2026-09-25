import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { jsonResponse } from '@/test/fixtures';

import { RenameLabelForm } from './RenameLabelForm';

import type { SavedBaseCv } from '../types';

/**
 * T26 RED — AC-37, against feature-spec.md and technical-plan.md §7, never against
 * `RenameLabelForm.tsx`'s T25 skeleton, which renders `null` regardless of props. Every assertion
 * fails on "unable to find the expected role/text", never on an `ImportError`.
 */

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

type Handler = () => Response | Promise<Response>;

function makeFetchMock(handlers: Record<string, Handler>): ReturnType<typeof vi.fn> {
  const mock = vi.fn((input: string | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = init?.method ?? 'GET';
    const handler = handlers[`${method} ${url}`];
    if (handler === undefined) {
      return Promise.reject(new Error(`unhandled fetch: ${method} ${url}`));
    }
    return Promise.resolve(handler());
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

function renderForm(cv: SavedBaseCv, onDone: () => void = vi.fn()) {
  const queryClient = makeQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <RenameLabelForm cv={cv} onDone={onDone} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('RenameLabelForm (AC-37)', () => {
  it("shows a labelled input seeded from the CV's current label, and Save/Cancel controls", () => {
    makeFetchMock({});
    renderForm(makeSavedCv({ label: 'Backend roles' }));

    const input = screen.getByLabelText('Name for this CV');
    expect(input).toHaveValue('Backend roles');
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
  });

  it('seeds an empty input when the CV has no label yet', () => {
    makeFetchMock({});
    renderForm(makeSavedCv({ label: null }));

    expect(screen.getByLabelText('Name for this CV')).toHaveValue('');
  });

  it('Cancel calls onDone without sending a request', () => {
    const fetchMock = makeFetchMock({});
    const onDone = vi.fn();
    renderForm(makeSavedCv(), onDone);

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(onDone).toHaveBeenCalledTimes(1);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('shows "Saving…" while the PATCH is in flight', async () => {
    const user = userEvent.setup();
    makeFetchMock({
      'PATCH /api/me/base-cvs/saved-cv-1': () => new Promise<Response>(() => undefined),
    });
    renderForm(makeSavedCv());

    await user.clear(screen.getByLabelText('Name for this CV'));
    await user.type(screen.getByLabelText('Name for this CV'), 'New name');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText('Saving…')).toBeInTheDocument();
  });

  it('success calls onDone after the response, and sends the typed label', async () => {
    const user = userEvent.setup();
    const fetchMock = makeFetchMock({
      'PATCH /api/me/base-cvs/saved-cv-1': () =>
        jsonResponse(200, makeSavedCv({ label: 'New name' })),
    });
    const onDone = vi.fn();
    renderForm(makeSavedCv(), onDone);

    await user.clear(screen.getByLabelText('Name for this CV'));
    await user.type(screen.getByLabelText('Name for this CV'), 'New name');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await vi.waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    const patchCall = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === 'PATCH',
    );
    expect(patchCall).toBeDefined();
    const body = JSON.parse((patchCall?.[1] as RequestInit).body as string) as { label: unknown };
    expect(body.label).toBe('New name');
  });

  it('AC-37: invalid_label is shown as role="alert", linked to the input by aria-describedby', async () => {
    const user = userEvent.setup();
    makeFetchMock({
      'PATCH /api/me/base-cvs/saved-cv-1': () =>
        jsonResponse(422, {
          error: { code: 'invalid_label', message: 'A name must be 1 to 80 characters.' },
        }),
    });
    renderForm(makeSavedCv());

    await user.clear(screen.getByLabelText('Name for this CV'));
    await user.type(screen.getByLabelText('Name for this CV'), 'x'.repeat(90));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/1 to 80 characters/i);
    const input = screen.getByLabelText('Name for this CV');
    expect(input.getAttribute('aria-describedby')).toBe(alert.id);
  });
});
