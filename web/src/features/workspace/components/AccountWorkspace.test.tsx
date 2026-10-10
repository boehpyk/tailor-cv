import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import {
  USER_A,
  bearerFor,
  callsTo,
  makeHistoryEntry,
  ok,
  signInAs,
  signedInRoutes,
  status,
  stubAccountFetch,
} from '@/test/accountFetch';
import { jsonResponse, makeExtractedCv, makePostingSummary, makeRunSummary } from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';

import type { RouteHandler } from '@/test/accountFetch';
import type { SavedBaseCv } from '@/features/savedCvs/types';

/**
 * T30 RED — the account workspace on `/` (AC-39 base CV, AC-40 posting, AC-41 launch, AC-42 guest
 * work named, AC-49's workspace sentences, H-58, H-60). Mounted through the real route table while
 * signed in, with every request recorded (`stubAccountFetch`) so each test can say *where* a call
 * went and *with which credential*, not only what the page shows.
 *
 * Against T29's skeleton `AccountWorkspace` is one placeholder paragraph: every assertion below is
 * red on the missing behaviour, never on an import.
 */

const ACCOUNT_PROMISE =
  "You're signed in, so what you tailor here is saved to your history — the job posting, the tailored CV and cover letter, and any files you export — until you delete it. The AI provider sees your CV's text and the job posting when you tailor. The person who runs TailorCraft can read what is stored here — CVs, job postings and tailored documents — to operate and support the service.";

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

const ACCOUNT_POSTING = makePostingSummary({
  id: 'account-posting-1',
  title: 'Senior Platform Engineer',
  expires_at: null,
});

function workspace(overrides: Record<string, RouteHandler> = {}) {
  return stubAccountFetch({
    ...signedInRoutes(USER_A),
    'GET /api/me/base-cvs': ok({ items: [makeSavedCv()] }),
    'GET /api/me/job-postings': ok({ items: [ACCOUNT_POSTING] }),
    'GET /api/me/tailoring-runs': ok({ items: [], next_cursor: null }),
    // Slice 2.4: the offer also reads the guest CV list (without the bearer), beside the run list.
    'GET /api/base-cvs': ok({ items: [] }),
    ...overrides,
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

async function ready(): Promise<void> {
  expect(await screen.findByText(ACCOUNT_PROMISE)).toBeInTheDocument();
}

// --- AC-39: the saved CV is the run's base CV -----------------------------------------------------

describe('AccountWorkspace — base CV (AC-39)', () => {
  it('preselects the saved CV when it is the only one and it is extracted', async () => {
    workspace();

    renderWithRouter('/');

    expect(await screen.findByRole('radio', { name: /jane-resume\.pdf/ })).toBeChecked();
  });

  it('preselects nothing when two saved CVs are extracted', async () => {
    workspace({
      'GET /api/me/base-cvs': ok({
        items: [
          makeSavedCv({ id: 'cv-a', original_filename: 'a.pdf' }),
          makeSavedCv({ id: 'cv-b', original_filename: 'b.pdf' }),
        ],
      }),
    });

    renderWithRouter('/');

    expect(await screen.findByRole('radio', { name: /a\.pdf/ })).not.toBeChecked();
    expect(screen.getByRole('radio', { name: /b\.pdf/ })).not.toBeChecked();
  });

  it('lists a CV whose extraction failed, disabled, with its reason', async () => {
    workspace({
      'GET /api/me/base-cvs': ok({
        items: [
          makeSavedCv({ id: 'cv-ok', original_filename: 'good.pdf' }),
          makeSavedCv({
            id: 'cv-bad',
            original_filename: 'scanned.pdf',
            status: 'extraction_failed',
            character_count: null,
            failure_reason: 'no_text_layer',
            failure_message: 'This PDF has no text layer.',
          }),
        ],
      }),
    });

    renderWithRouter('/');

    expect(await screen.findByRole('radio', { name: /scanned\.pdf/ })).toBeDisabled();
    expect(screen.getByText('This PDF has no text layer.')).toBeInTheDocument();
  });

  it('with no saved CV: "Upload your CV to get started" and the account upload control', async () => {
    workspace({ 'GET /api/me/base-cvs': ok({ items: [] }) });

    renderWithRouter('/');

    expect(await screen.findByText('Upload your CV to get started')).toBeInTheDocument();
    expect(screen.getByLabelText(/upload a cv to your account/i)).toBeInTheDocument();
  });
});

// --- AC-40: the posting ----------------------------------------------------------------------------

describe('AccountWorkspace — posting (AC-40)', () => {
  it('reads the latest account posting with limit=1 and the bearer, and says it is saved with the history', async () => {
    const fetch = workspace();

    renderWithRouter('/');

    expect(await screen.findByText('Saved with your history')).toBeInTheDocument();
    expect(screen.queryByText(/Stored until/)).not.toBeInTheDocument();
    const [read] = callsTo(fetch, 'GET', '/api/me/job-postings');
    expect(read?.query.get('limit')).toBe('1');
    expect(read?.authorization).toBe(bearerFor(USER_A));
  });

  it('a pasted posting goes to /api/me/job-postings with the bearer', async () => {
    const fetch = workspace({
      'GET /api/me/job-postings': ok({ items: [] }),
      'POST /api/me/job-postings': () =>
        jsonResponse(201, { ...ACCOUNT_POSTING, text: 'We are hiring.', expires_at: null }),
    });
    const user = userEvent.setup();

    renderWithRouter('/');
    await ready();
    await user.type(
      await screen.findByLabelText('Job posting text'),
      'We are hiring a platform engineer, remote, fully async.',
    );
    await user.click(screen.getByRole('button', { name: 'Add job posting' }));

    await waitFor(() => {
      expect(callsTo(fetch, 'POST', '/api/me/job-postings')).toHaveLength(1);
    });
    const [created] = callsTo(fetch, 'POST', '/api/me/job-postings');
    expect(created?.authorization).toBe(bearerFor(USER_A));
    expect(created?.body).toMatchObject({ source: 'pasted' });
    expect(callsTo(fetch, 'POST', '/api/job-postings')).toHaveLength(0);
  });

  it("a failed fetch shows 1.2's paste fallback", async () => {
    workspace({
      'GET /api/me/job-postings': ok({ items: [] }),
      'POST /api/me/job-postings': status(
        502,
        'source_unreachable',
        "We couldn't reach that page.",
      ),
    });
    const user = userEvent.setup();

    renderWithRouter('/');
    await ready();
    await user.click(await screen.findByRole('radio', { name: /link to the posting/i }));
    await user.type(screen.getByLabelText('Job posting URL'), 'https://jobs.example.com/1');
    await user.click(screen.getByRole('button', { name: 'Add job posting' }));

    expect(
      await screen.findByRole('button', { name: /paste the description instead/i }),
    ).toBeInTheDocument();
  });
});

// --- AC-41: launch -----------------------------------------------------------------------------------

describe('AccountWorkspace — launch (AC-41)', () => {
  it('Tailor posts the saved CV and the posting to /api/me/tailoring-runs and opens /history/{id} — never a copy', async () => {
    const fetch = workspace({
      'POST /api/me/tailoring-runs': () =>
        jsonResponse(202, {
          ...makeRunSummary({ id: 'new-run', status: 'queued' }),
          expires_at: null,
        }),
      'GET /api/me/tailoring-runs/:id': () =>
        jsonResponse(200, {
          ...makeRunSummary({ id: 'new-run', status: 'queued' }),
          expires_at: null,
        }),
    });
    const user = userEvent.setup();

    const { router } = renderWithRouter('/');
    await ready();
    await user.click(await screen.findByRole('button', { name: /tailor/i }));

    await waitFor(() => {
      expect(router.state.location.pathname).toMatch(/^\/history\/new-run(\/cv)?$/);
    });
    const [launched] = callsTo(fetch, 'POST', '/api/me/tailoring-runs');
    expect(launched?.authorization).toBe(bearerFor(USER_A));
    expect(launched?.body).toEqual({
      base_cv_id: 'saved-cv-1',
      job_posting_id: 'account-posting-1',
    });
    expect(fetch.calls.filter((call) => call.path === '/api/base-cvs/copies')).toHaveLength(0);
  });

  it('H-58: a same-tick double click sends one request', async () => {
    const fetch = workspace({
      'POST /api/me/tailoring-runs': () => new Promise<Response>(() => undefined),
    });

    renderWithRouter('/');
    await ready();
    const tailor = await screen.findByRole('button', { name: /tailor/i });
    fireEvent.click(tailor);
    fireEvent.click(tailor);

    await waitFor(() => {
      expect(callsTo(fetch, 'POST', '/api/me/tailoring-runs')).toHaveLength(1);
    });
    expect(callsTo(fetch, 'POST', '/api/me/tailoring-runs')).toHaveLength(1);
  });

  it('409 tailoring_already_running links to the active run in the history', async () => {
    workspace({
      'POST /api/me/tailoring-runs': status(409, 'tailoring_already_running', 'Already running.', {
        active_tailoring_run_id: 'active-1',
      }),
    });
    const user = userEvent.setup();

    renderWithRouter('/');
    await ready();
    await user.click(await screen.findByRole('button', { name: /tailor/i }));

    const links = await screen.findAllByRole('link');
    expect(links.some((link) => link.getAttribute('href')?.startsWith('/history/active-1'))).toBe(
      true,
    );
  });

  it('every rejection code has its own copy', async () => {
    const codes = ['too_many_tailoring_runs', 'base_cv_not_extracted', 'tailoring_already_running'];
    const texts: string[] = [];
    for (const code of codes) {
      workspace({
        'POST /api/me/tailoring-runs': status(409, code, 'server words', {
          active_tailoring_run_id: 'active-1',
        }),
      });
      const user = userEvent.setup();
      const { unmount } = renderWithRouter('/');
      await ready();
      await user.click(await screen.findByRole('button', { name: /tailor/i }));
      const alert = await screen.findByRole('alert');
      texts.push(alert.textContent);
      unmount();
    }

    expect(texts.every((text) => text.trim().length > 0)).toBe(true);
    expect(new Set(texts).size).toBe(codes.length);
  });

  it('the latest-run card reads /api/me/tailoring-runs?limit=1 and links into the history', async () => {
    const fetch = workspace({
      'GET /api/me/tailoring-runs': ok({
        items: [makeHistoryEntry({ id: 'latest-1', status: 'succeeded' })],
        next_cursor: null,
      }),
    });

    renderWithRouter('/');

    const card = await screen.findByRole('region', { name: 'Your latest run' });
    const link = within(card).getByRole('link', { name: 'Open your tailored documents' });
    expect(link.getAttribute('href')).toMatch(/^\/history\/latest-1/);
    const [read] = callsTo(fetch, 'GET', '/api/me/tailoring-runs');
    expect(read?.query.get('limit')).toBe('1');
    expect(read?.authorization).toBe(bearerFor(USER_A));
  });
});

// --- AC-42 / H-60, amended by slice 2.4 (AC-37): guest work is OFFERED, not just named ------------------
//
// 2.3's `GuestWorkNotice` said "This browser has N run(s)… deleted within 24 hours" and linked to the
// newest guest run, because signing in moved nothing. Slice 2.4 makes it an offer: a `role="region"`
// naming the CVs and runs, with **Keep them in my account**. The three tests below are 2.3's three
// notice tests, amended in T30's commit (not in T31's) to say the same things about the offer — the
// guest list is still read WITHOUT the bearer (AC-42), an empty browser still says nothing, and a 401
// from the guest list is still no offer and no refresh call (H-60). The "N run(s)" sentence and the
// "Open the most recent one" link are gone with the notice (AC-37 specifies no link).

describe('AccountWorkspace — guest work in this browser (AC-42, amended: the offer, AC-37)', () => {
  it("offers the browser's guest work, fetched without the bearer", async () => {
    const fetch = workspace({
      'GET /api/tailoring-runs': ok({
        items: [
          makeRunSummary({ id: 'guest-new', requested_at: '2026-09-28T10:00:00Z' }),
          makeRunSummary({ id: 'guest-old', requested_at: '2026-09-27T10:00:00Z' }),
        ],
      }),
    });

    renderWithRouter('/');

    const offer = await screen.findByRole('region', { name: 'Keep your work' });
    expect(offer).toHaveTextContent('2 tailored applications');
    expect(
      within(offer).getByRole('button', { name: 'Keep them in my account' }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/from before you signed in/)).not.toBeInTheDocument();
    const [guestList] = callsTo(fetch, 'GET', '/api/tailoring-runs');
    expect(guestList).toBeDefined();
    expect(guestList?.authorization).toBeNull();
  });

  it('offers a guest CV even when the browser holds no guest run', async () => {
    workspace({
      'GET /api/base-cvs': ok({
        items: [makeExtractedCv({ original_filename: 'guest-only.pdf' })],
      }),
    });

    renderWithRouter('/');

    expect(await screen.findByRole('region', { name: 'Keep your work' })).toHaveTextContent(
      'guest-only.pdf',
    );
  });

  it('says nothing when the browser holds no guest work', async () => {
    const fetch = workspace();

    renderWithRouter('/');
    await ready();
    await waitFor(() => {
      expect(callsTo(fetch, 'GET', '/api/tailoring-runs').length).toBeGreaterThan(0);
      expect(callsTo(fetch, 'GET', '/api/base-cvs').length).toBeGreaterThan(0);
    });

    expect(screen.queryByRole('region', { name: 'Keep your work' })).not.toBeInTheDocument();
    expect(screen.queryByText(/from before you signed in/)).not.toBeInTheDocument();
  });

  it('H-60: a 401 from the guest list is no offer and no refresh call', async () => {
    const fetch = workspace({
      'GET /api/tailoring-runs': status(401, 'guest_session_expired'),
    });

    renderWithRouter('/');
    await ready();
    await waitFor(() => {
      expect(callsTo(fetch, 'GET', '/api/tailoring-runs').length).toBeGreaterThan(0);
    });

    expect(screen.queryByRole('region', { name: 'Keep your work' })).not.toBeInTheDocument();
    expect(callsTo(fetch, 'POST', '/api/auth/refresh')).toHaveLength(0);
  });
});

// --- AC-49: the workspace's own sentences ---------------------------------------------------------

describe('AccountWorkspace — what it promises (AC-49)', () => {
  it('states that tailoring is saved to the history, and never that it is not saved or deleted in 24 hours', async () => {
    workspace();

    renderWithRouter('/');
    await ready();

    expect(document.body.textContent).not.toContain("aren't saved to your account yet");
    expect(document.body.textContent).not.toContain('24 hours');
  });

  it('the guest workspace still says 24 hours (the other half of the same rule)', async () => {
    __resetForTests();
    stubAccountFetch({
      'GET /api/base-cvs': ok({ items: [] }),
      'GET /api/job-postings': ok({ items: [] }),
      'GET /api/tailoring-runs': ok({ items: [] }),
    });
    authStore.signOut('expired');

    renderWithRouter('/');

    await screen.findByRole('tab', { name: 'Base CV' });
    expect(document.body.textContent).toContain('24 hours');
  });
});
