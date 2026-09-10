import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithQuery } from '@/test/render';

import { JobPostingPanel } from './JobPostingPanel';

import type { JobPostingSummary } from '../types';

/**
 * T36 RED — the behavioural contract for `JobPostingPanel` (spec's AC-22, AC-23 and the
 * "Loading / error / empty / success states" table in technical-plan.md's Frontend section).
 *
 * Written against the **spec**, not against `JobPostingPanel.tsx`'s T35 stub — the stub
 * deliberately renders placeholder roles/text ("stub input", "stub empty", "stub card",
 * "stub error") and nothing real, so most of what follows is expected to fail until T37. Where a
 * test happens to pass against the stub, that is called out at the point it matters (see the
 * loading-state test below) rather than left to look like an accident.
 *
 * This file fixes the contract T37 must build to, in particular the exact accessible names the
 * component is expected to expose:
 *   - radio "Paste the text" (checked by default) and radio "Link to the posting"
 *   - a labelled textarea "Job posting text"
 *   - a labelled `<input type="url">` "Job posting URL", shown after switching to link mode
 *   - the submit control's text while pending: "Saving…" (paste) / "Reading the job posting…" (fetch)
 *   - the fetch-failure action button "Paste the description instead"
 */

const RETENTION_SENTENCE = 'We delete guest job postings after 24 hours.';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function makeSummary(overrides: Partial<JobPostingSummary> = {}): JobPostingSummary {
  return {
    id: '0192f0a1-0000-7000-8000-000000000001',
    source: 'fetched',
    source_url: 'https://jobs.example.com/postings/1234',
    title: 'Senior Python Engineer',
    character_count: 4321,
    preview: 'We are looking for a senior Python engineer…',
    created_at: '2026-09-09T10:00:00Z',
    expires_at: '2026-09-10T10:00:00Z',
    ...overrides,
  };
}

/**
 * A `fetch` stub routing on method, mirroring `BaseCvUploadPanel.test.tsx`'s helper: POST goes
 * through `handlePost`, anything else (the list GET) goes through `handleGet`.
 */
function stubFetch(
  handleGet: () => Promise<Response>,
  handlePost: () => Promise<Response>,
): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'POST') {
      return handlePost();
    }
    return handleGet();
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/** Stub the list as empty, render the panel, and wait for the input to be ready. */
async function renderEmpty(): Promise<void> {
  stubFetch(
    () => Promise.resolve(jsonResponse(200, { items: [] })),
    () => Promise.reject(new Error('POST should not be called in this test')),
  );
  renderWithQuery(<JobPostingPanel />);
  await screen.findByText(RETENTION_SENTENCE);
}

describe('JobPostingPanel', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows the loading state and no input control before the list resolves', () => {
    // A promise that never resolves — the list query stays `isPending` for the life of the test.
    stubFetch(
      () => new Promise<Response>(() => undefined),
      () => new Promise<Response>(() => undefined),
    );

    renderWithQuery(<JobPostingPanel />);

    // NOTE: this test passes against the T35 stub too — the stub's `isPending` branch already
    // renders only `<p role="status">` with no radio or textbox, which is genuinely the entire
    // loading-state contract (an interactive control about to be replaced would read as a
    // flicker). That is not a weak assertion; there is simply nothing more to build for this
    // state than the stub already has. Every other state below fails against the stub.
    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(screen.queryByRole('radio')).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('AC-23: states the retention promise before the user commits anything', async () => {
    await renderEmpty();

    expect(screen.getByText(RETENTION_SENTENCE)).toBeInTheDocument();
  });

  it('offers both input modes as labelled, keyboard-reachable radios, with paste selected by default', async () => {
    await renderEmpty();

    // A real fieldset (role "group"), not a div-based segmented control.
    expect(screen.getByRole('group')).toBeInTheDocument();

    const pasteRadio = screen.getByRole('radio', { name: /paste the text/i });
    const linkRadio = screen.getByRole('radio', { name: /link to the posting/i });

    expect(pasteRadio).toBeInTheDocument();
    expect(linkRadio).toBeInTheDocument();
    // Paste always works; the link is the optimistic path — so paste is the default.
    expect(pasteRadio).toBeChecked();
    expect(linkRadio).not.toBeChecked();
  });

  it('shows a labelled textarea for paste, and a labelled URL input after switching to link mode', async () => {
    const user = userEvent.setup();
    await renderEmpty();

    expect(screen.getByLabelText(/job posting text/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/job posting url/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole('radio', { name: /link to the posting/i }));

    const urlInput = screen.getByLabelText(/job posting url/i);
    expect(urlInput).toBeInTheDocument();
    expect(urlInput).toHaveAttribute('type', 'url');
  });

  it('disables the submit control and reads "Saving…" while a paste is in flight', async () => {
    const user = userEvent.setup();
    stubFetch(
      () => Promise.resolve(jsonResponse(200, { items: [] })),
      // Never resolves — the mutation stays `isPending` for the life of the test.
      () => new Promise<Response>(() => undefined),
    );

    renderWithQuery(<JobPostingPanel />);
    const textarea = await screen.findByLabelText(/job posting text/i);
    await user.type(
      textarea,
      'A senior Python engineer role at a mid-size logistics company, remote-friendly, ' +
        'requiring five years of backend experience with FastAPI and PostgreSQL.',
    );
    await user.click(screen.getByRole('button'));

    expect(await screen.findByText('Saving…')).toBeInTheDocument();
    expect(screen.queryByText('Reading the job posting…')).not.toBeInTheDocument();
    expect(screen.getByRole('button')).toBeDisabled();
  });

  it('disables the submit control and reads "Reading the job posting…" while a fetch is in flight', async () => {
    const user = userEvent.setup();
    stubFetch(
      () => Promise.resolve(jsonResponse(200, { items: [] })),
      // Never resolves — the mutation stays `isPending` for the life of the test.
      () => new Promise<Response>(() => undefined),
    );

    renderWithQuery(<JobPostingPanel />);
    await screen.findByText(RETENTION_SENTENCE);
    await user.click(screen.getByRole('radio', { name: /link to the posting/i }));
    const urlInput = screen.getByLabelText(/job posting url/i);
    await user.type(urlInput, 'https://jobs.example.com/postings/5678');
    await user.click(screen.getByRole('button'));

    expect(await screen.findByText('Reading the job posting…')).toBeInTheDocument();
    expect(screen.queryByText('Saving…')).not.toBeInTheDocument();
    expect(screen.getByRole('button')).toBeDisabled();
  });

  it('uses different verbs for a fetch and a paste while submitting — a user told "Saving…" for a fetch would conclude we are broken', async () => {
    const user = userEvent.setup();

    stubFetch(
      () => Promise.resolve(jsonResponse(200, { items: [] })),
      () => new Promise<Response>(() => undefined),
    );
    const pasteRender = renderWithQuery(<JobPostingPanel />);
    const textarea = await screen.findByLabelText(/job posting text/i);
    await user.type(
      textarea,
      'A senior Python engineer role at a mid-size logistics company, remote-friendly, ' +
        'requiring five years of backend experience with FastAPI and PostgreSQL.',
    );
    await user.click(screen.getByRole('button'));
    const pasteVerb = (await screen.findByRole('button')).textContent;
    pasteRender.unmount();
    vi.unstubAllGlobals();

    vi.stubGlobal('fetch', vi.fn());
    stubFetch(
      () => Promise.resolve(jsonResponse(200, { items: [] })),
      () => new Promise<Response>(() => undefined),
    );
    const fetchRender = renderWithQuery(<JobPostingPanel />);
    await screen.findByText(RETENTION_SENTENCE);
    await user.click(screen.getByRole('radio', { name: /link to the posting/i }));
    await user.type(
      screen.getByLabelText(/job posting url/i),
      'https://jobs.example.com/postings/999',
    );
    await user.click(screen.getByRole('button'));
    const fetchVerb = (await screen.findByRole('button')).textContent;
    fetchRender.unmount();

    expect(pasteVerb).toBe('Saving…');
    expect(fetchVerb).toBe('Reading the job posting…');
    expect(pasteVerb).not.toBe(fetchVerb);
  });

  it('renders a card with the title, character count and the source host as an external nofollow link', async () => {
    stubFetch(
      () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeSummary({
                title: 'Senior Python Engineer',
                source_url: 'https://jobs.example.com/postings/1234',
                character_count: 4321,
              }),
            ],
          }),
        ),
      () => Promise.reject(new Error('POST should not be called in this test')),
    );

    renderWithQuery(<JobPostingPanel />);

    expect(await screen.findByText('Senior Python Engineer')).toBeInTheDocument();
    expect(screen.getByText(/4321/)).toBeInTheDocument();

    const hostLink = screen.getByRole('link', { name: /jobs\.example\.com/i });
    expect(hostLink).toHaveAttribute('target', '_blank');
    const rel = hostLink.getAttribute('rel') ?? '';
    expect(rel).toContain('noopener');
    expect(rel).toContain('noreferrer');
    expect(rel).toContain('nofollow');
  });

  it('falls back to a generic label and shows no host link for a pasted posting with no title or URL', async () => {
    stubFetch(
      () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeSummary({
                source: 'pasted',
                source_url: null,
                title: null,
                character_count: 150,
                preview: 'We are looking for someone great…',
              }),
            ],
          }),
        ),
      () => Promise.reject(new Error('POST should not be called in this test')),
    );

    renderWithQuery(<JobPostingPanel />);

    expect(await screen.findByText('Job posting')).toBeInTheDocument();
    expect(screen.getByText(/150/)).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('error A: rejects an empty paste before issuing any request', async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch(
      () => Promise.resolve(jsonResponse(200, { items: [] })),
      () => Promise.reject(new Error('POST should not be called in this test')),
    );

    renderWithQuery(<JobPostingPanel />);
    await screen.findByText(RETENTION_SENTENCE);
    const postCallsBeforeSubmit = fetchMock.mock.calls.filter(
      (call) => (call[1] as RequestInit | undefined)?.method === 'POST',
    ).length;

    // Paste mode is the default; the textarea is left empty.
    await user.click(screen.getByRole('button'));

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    const postCallsAfterSubmit = fetchMock.mock.calls.filter(
      (call) => (call[1] as RequestInit | undefined)?.method === 'POST',
    ).length;
    // The entire point of a client-side pre-check is that no request is issued for input it
    // rejects — a test that only checked the alert would also pass a component that submitted
    // anyway and merely happened to also show this text.
    expect(postCallsAfterSubmit).toBe(postCallsBeforeSubmit);
  });

  it('error B: shows the server message for a paste the API rejects as too short', async () => {
    const user = userEvent.setup();
    const message = 'Job postings need at least 100 characters of real content.';
    stubFetch(
      () => Promise.resolve(jsonResponse(200, { items: [] })),
      () =>
        Promise.resolve(
          jsonResponse(422, {
            error: { code: 'posting_text_too_short', message },
          }),
        ),
    );

    renderWithQuery(<JobPostingPanel />);
    const textarea = await screen.findByLabelText(/job posting text/i);
    // Short, but not blank — a client-side pre-check that only rejects an empty textarea (error A
    // covers that case) must let this one through to the API.
    await user.type(textarea, 'Too short.');
    await user.click(screen.getByRole('button'));

    expect(await screen.findByText(message)).toBeInTheDocument();
  });

  it('error C: a fetch failure is textually distinct from error B and offers a working paste-fallback action', async () => {
    const user = userEvent.setup();
    const errorBMessage = 'Job postings need at least 100 characters of real content.';
    const errorCMessage = "We couldn't read that job posting from the link you gave us.";
    const url = 'https://jobs.example.com/postings/blocked';

    // Error B first, to capture its rendered text for comparison.
    stubFetch(
      () => Promise.resolve(jsonResponse(200, { items: [] })),
      () =>
        Promise.resolve(
          jsonResponse(422, { error: { code: 'posting_text_too_short', message: errorBMessage } }),
        ),
    );
    const renderB = renderWithQuery(<JobPostingPanel />);
    const textareaB = await screen.findByLabelText(/job posting text/i);
    await user.type(textareaB, 'Too short.');
    await user.click(screen.getByRole('button'));
    const textB = (await screen.findByText(errorBMessage)).textContent;
    renderB.unmount();
    vi.unstubAllGlobals();

    // Error C — the fetch itself failed (502 source_rejected).
    vi.stubGlobal('fetch', vi.fn());
    stubFetch(
      () => Promise.resolve(jsonResponse(200, { items: [] })),
      () =>
        Promise.resolve(
          jsonResponse(502, { error: { code: 'source_rejected', message: errorCMessage } }),
        ),
    );
    renderWithQuery(<JobPostingPanel />);
    await screen.findByText(RETENTION_SENTENCE);
    await user.click(screen.getByRole('radio', { name: /link to the posting/i }));
    const urlInput = screen.getByLabelText(/job posting url/i);
    await user.type(urlInput, url);
    await user.click(screen.getByRole('button'));
    const textC = (await screen.findByText(errorCMessage)).textContent;

    // Only one of the three error kinds is fixed by retrying the same input — a user who cannot
    // tell error B from error C will retry and get the same answer.
    expect(textC).not.toBe(textB);

    // FR-2/AC-13: the paste fallback is an ACTION, not advice.
    const pasteFallbackButton = screen.getByRole('button', {
      name: /paste the description instead/i,
    });
    await user.click(pasteFallbackButton);

    const pasteTextarea = await screen.findByLabelText(/job posting text/i);
    expect(pasteTextarea).toHaveFocus();
    // With the URL still visible, as a link, so the user can open it in a tab and copy from it.
    const urlLink = screen.getByRole('link', { name: url });
    expect(urlLink).toHaveAttribute('href', url);
  });
});
