import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';

import { __resetForTests } from '@/features/auth/authStore';
import { makeExportJob } from '@/features/export/test/fixtures';
import {
  USER_A,
  makeAccountRun,
  ok,
  signInAs,
  signedInRoutes,
  status,
  stubAccountFetch,
} from '@/test/accountFetch';
import { jsonResponse } from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';

import { SavedBaseCvPicker } from './SavedBaseCvPicker';
import { SavedBaseCvsSection } from './SavedBaseCvsSection';

import type { SavedBaseCv } from '../types';

/**
 * T30 — AC-43: over a cap, the client renders what the server holds, honestly. Slice 2.4's claim
 * bounds creation, not transfer (OQ-4), so an account can come to hold more saved CVs than 2.2's
 * cap of 5 — and a run can hold more exports than 2.3's per-run cap of 20. Nothing in the client may
 * assume ≤ 5 or ≤ 20.
 *
 * **Green on arrival, and said so.** The saved-CV list, the picker and the export bar render
 * whatever the server returns; there is no client-side cap to remove. These are regression guards
 * for the contract the spec states (a future "slice the list to the cap" would turn them red), not a
 * red-first test, so they carry no recorded red.
 */

function makeSavedCvs(count: number): SavedBaseCv[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `saved-${String(index + 1)}`,
    label: null,
    original_filename: `cv-number-${String(index + 1)}.pdf`,
    content_type: 'application/pdf',
    size_bytes: 2048,
    status: 'extracted',
    character_count: 512,
    failure_reason: null,
    failure_message: null,
    uploaded_at: `2026-09-${String(10 + index)}T10:00:00Z`,
  }));
}

function client(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  __resetForTests();
});

describe('Over the saved-CV cap (AC-43)', () => {
  it('the account page lists all 7 saved CVs', async () => {
    stubAccountFetch({
      ...signedInRoutes(USER_A),
      'GET /api/me/base-cvs': ok({ items: makeSavedCvs(7) }),
    });

    render(
      <QueryClientProvider client={client()}>
        <SavedBaseCvsSection />
      </QueryClientProvider>,
    );

    for (let n = 1; n <= 7; n += 1) {
      expect(await screen.findByText(`cv-number-${String(n)}.pdf`)).toBeInTheDocument();
    }
    expect(screen.getAllByRole('button', { name: /^delete/i })).toHaveLength(7);
  });

  it('the picker offers all 7 as radios', async () => {
    stubAccountFetch({
      ...signedInRoutes(USER_A),
      'GET /api/me/base-cvs': ok({ items: makeSavedCvs(7) }),
    });

    render(
      <QueryClientProvider client={client()}>
        <MemoryRouter>
          <SavedBaseCvPicker mode="select" chosenId={null} onChoose={() => undefined} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findAllByRole('radio')).toHaveLength(7);
  });

  it('with 7 held, the upload control stays enabled and shows 2.2’s cap reason when the server refuses', async () => {
    const message = 'You already have 5 saved CVs, the most this account can hold.';
    stubAccountFetch({
      ...signedInRoutes(USER_A),
      'GET /api/me/base-cvs': ok({ items: makeSavedCvs(7) }),
      'POST /api/me/base-cvs': status(409, 'too_many_saved_base_cvs', message),
    });
    render(
      <QueryClientProvider client={client()}>
        <SavedBaseCvsSection />
      </QueryClientProvider>,
    );
    await screen.findByText('cv-number-7.pdf');
    const input = screen.getByLabelText(/upload a cv to your account/i);
    expect(input).toBeEnabled();

    await userEvent
      .setup()
      .upload(input, new File(['x'], 'one-more.pdf', { type: 'application/pdf' }));

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(input).toBeEnabled();
  });
});

describe('Over the per-run export cap (AC-43)', () => {
  it('a run holding 25 export jobs answers the next request with 2.3’s too_many_export_jobs copy', async () => {
    const jobs = Array.from({ length: 25 }, (_, index) =>
      makeExportJob({
        id: `job-${String(index + 1)}`,
        tailoring_run_id: 'run-1',
        document: 'cover_letter',
        format: 'docx',
        status: 'ready',
        byte_size: 4096,
      }),
    );
    stubAccountFetch({
      ...signedInRoutes(USER_A),
      'GET /api/me/tailoring-runs/:id': ok(
        makeAccountRun({
          id: 'run-1',
          status: 'succeeded',
          tailored_cv: 'my tailored cv text',
          cover_letter: 'my cover letter text',
          tailored_cv_character_count: 19,
          cover_letter_character_count: 20,
        }),
      ),
      'GET /api/me/tailoring-runs/:id/exports': ok({ items: jobs }),
      'POST /api/me/tailoring-runs/:id/exports': () =>
        jsonResponse(409, {
          error: { code: 'too_many_export_jobs', message: 'server prose' },
        }),
    });

    renderWithRouter('/history/run-1/cv');
    await screen.findByText('my tailored cv text');
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'PDF' })).not.toBeDisabled();
    });
    fireEvent.click(screen.getByRole('button', { name: 'PDF' }));

    expect(
      await screen.findByText("You've reached the download limit for this session."),
    ).toBeInTheDocument();
  });
});
