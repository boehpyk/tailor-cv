import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithQuery } from '@/test/render';

import { BaseCvUploadPanel } from './BaseCvUploadPanel';

import type { BaseCv } from '../types';

/** The label text on the dropzone's `<input type="file">` (technical-plan.md's empty-state row). */
const DROPZONE_LABEL = /drop your cv here/i;

/** AC-16 — the retention promise, asserted literally: it is a promise to the user, not decoration. */
const RETENTION_SENTENCE = 'We delete guest CVs after 24 hours.';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function makeExtractedCv(overrides: Partial<BaseCv> = {}): BaseCv {
  return {
    id: '0192f0a1-0000-7000-8000-000000000001',
    original_filename: 'resume.pdf',
    content_type: 'application/pdf',
    size_bytes: 2048,
    status: 'extracted',
    character_count: 512,
    failure_reason: null,
    failure_message: null,
    uploaded_at: '2026-09-07T10:00:00Z',
    expires_at: '2026-09-08T10:00:00Z',
    ...overrides,
  };
}

/**
 * A `fetch` stub that routes on method: POST goes through `handlePost`, anything else (the list
 * GET, called once on mount and again after the upload mutation invalidates the cache) goes
 * through `handleGet`, which sees how many times it has already been called so a test can hand
 * back a different list before and after an upload.
 */
function stubFetch(
  handleGet: (callNumber: number) => Promise<Response>,
  handlePost: () => Promise<Response>,
): void {
  let getCalls = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST') {
        return handlePost();
      }
      getCalls += 1;
      return handleGet(getCalls);
    }),
  );
}

describe('BaseCvUploadPanel', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows the loading state and no file input before the list resolves', () => {
    // A promise that never resolves — the list query stays `isPending` for the life of the test.
    stubFetch(
      () => new Promise<Response>(() => undefined),
      () => new Promise<Response>(() => undefined),
    );

    renderWithQuery(<BaseCvUploadPanel />);

    expect(screen.getByText('Loading your CV…')).toBeInTheDocument();
    expect(screen.queryByLabelText(DROPZONE_LABEL)).not.toBeInTheDocument();
  });

  it('shows the dropzone and the retention promise for an empty list', async () => {
    stubFetch(
      () => Promise.resolve(jsonResponse(200, { items: [] })),
      () => Promise.resolve(jsonResponse(200, {})),
    );

    renderWithQuery(<BaseCvUploadPanel />);

    expect(await screen.findByLabelText(DROPZONE_LABEL)).toBeInTheDocument();
    expect(screen.getByText(RETENTION_SENTENCE)).toBeInTheDocument();
  });

  it('shows a distinct error state — not the empty state — when the list request fails for a reason other than session expiry', async () => {
    // A 500 (or a network failure) is a genuine "this failed" — unlike F-19's 401, `useBaseCvs`
    // does not fold this one away, so it must surface as `listIsError` (BaseCvUploadPanel.tsx:105).
    stubFetch(
      () =>
        Promise.resolve(
          jsonResponse(500, { error: { code: 'internal_error', message: 'Server error' } }),
        ),
      () => Promise.reject(new Error('POST should not be called in this test')),
    );

    renderWithQuery(<BaseCvUploadPanel />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not load your CVs: Server error',
    );
    // Textually AND visibly distinct from the empty state (CLAUDE.md: a user must be able to tell
    // "still working"/"no CVs yet" from "this failed") — neither the dropzone nor the retention
    // sentence a first-time visitor sees should be present behind an error box.
    expect(screen.queryByLabelText(DROPZONE_LABEL)).not.toBeInTheDocument();
    expect(screen.queryByText(RETENTION_SENTENCE)).not.toBeInTheDocument();
  });

  it('folds a 401 guest_session_expired list response into the empty dropzone state, not an error', async () => {
    // F-19: "The UI clears local state and shows the fresh dropzone." A cleared/expired guest
    // session must render identically to a first-time visitor with zero CVs — not the error box
    // from useBaseCvs.ts:40-46's fold.
    stubFetch(
      () =>
        Promise.resolve(
          jsonResponse(401, {
            error: { code: 'guest_session_expired', message: 'Session expired' },
          }),
        ),
      () => Promise.reject(new Error('POST should not be called in this test')),
    );

    renderWithQuery(<BaseCvUploadPanel />);

    expect(await screen.findByLabelText(DROPZONE_LABEL)).toBeInTheDocument();
    expect(screen.getByText(RETENTION_SENTENCE)).toBeInTheDocument();
    // Distinct from the error state above: no alert, and none of its text.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByText(/could not load your cvs/i)).not.toBeInTheDocument();
  });

  it('disables the control and shows the chosen filename while the upload is pending', async () => {
    const user = userEvent.setup();
    stubFetch(
      () => Promise.resolve(jsonResponse(200, { items: [] })),
      // Never resolves — the mutation stays `isPending` for the life of the test, which is exactly
      // the window F-21's disabled control is meant to bound.
      () => new Promise<Response>(() => undefined),
    );

    renderWithQuery(<BaseCvUploadPanel />);
    const input = await screen.findByLabelText(DROPZONE_LABEL);
    const file = new File(['%PDF-1.4 body'], 'resume.pdf', { type: 'application/pdf' });

    await user.upload(input, file);

    expect(await screen.findByText('resume.pdf')).toBeInTheDocument();
    expect(input).toBeDisabled();
  });

  it('renders filename, size and character count for an extracted CV', async () => {
    const cv = makeExtractedCv({
      original_filename: 'jane-cv.pdf',
      size_bytes: 2048,
      character_count: 512,
    });
    stubFetch(
      () => Promise.resolve(jsonResponse(200, { items: [cv] })),
      () => Promise.resolve(jsonResponse(200, {})),
    );

    renderWithQuery(<BaseCvUploadPanel />);

    expect(await screen.findByText('jane-cv.pdf')).toBeInTheDocument();
    expect(screen.getByText('2 KB')).toBeInTheDocument();
    expect(screen.getByText('512 characters extracted')).toBeInTheDocument();
  });

  it('rejects an oversized file before issuing any request — error A', async () => {
    // Not a bad-extension file: `<input accept=".pdf,.docx,.txt">` makes user-event silently
    // discard a file whose type doesn't match `accept` (that filtering is testing-library's own
    // behaviour, standing in for the OS file-picker filter — a real browser lets a user override
    // it). An oversized file with an accepted extension reaches the same client-side pre-check
    // (`preValidate`'s size branch) without hitting that filter.
    const user = userEvent.setup();
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(200, { items: [] })));
    vi.stubGlobal('fetch', fetchMock);

    renderWithQuery(<BaseCvUploadPanel />);
    const input = await screen.findByLabelText(DROPZONE_LABEL);
    const callsBeforeChoosingFile = fetchMock.mock.calls.length;

    const oversizedFile = new File([new Uint8Array(10 * 1024 * 1024 + 1)], 'resume.pdf', {
      type: 'application/pdf',
    });
    await user.upload(input, oversizedFile);

    expect(
      await screen.findByText('That file is larger than 10 MB. Please choose a smaller file.'),
    ).toBeInTheDocument();
    // The whole point of a client-side pre-check is that no request is issued for a file it
    // rejects — a test that only checked the message would also pass a component that uploaded
    // anyway and merely happened to also show this text.
    expect(fetchMock.mock.calls.length).toBe(callsBeforeChoosingFile);
  });

  it('shows the server message when the API rejects the upload — error B', async () => {
    const user = userEvent.setup();
    stubFetch(
      () => Promise.resolve(jsonResponse(200, { items: [] })),
      () =>
        Promise.resolve(
          jsonResponse(415, {
            error: {
              code: 'unsupported_format',
              message: 'We only accept PDF, DOCX or TXT files.',
            },
          }),
        ),
    );

    renderWithQuery(<BaseCvUploadPanel />);
    const input = await screen.findByLabelText(DROPZONE_LABEL);
    const file = new File(['%PDF-1.4 body'], 'resume.pdf', { type: 'application/pdf' });

    await user.upload(input, file);

    expect(await screen.findByText('We only accept PDF, DOCX or TXT files.')).toBeInTheDocument();
  });

  it('shows a distinct notice when the file was stored but could not be read — error C', async () => {
    const user = userEvent.setup();
    const failureMessage =
      "We saved your file but couldn't read any text from it — it looks like a scan. Try a " +
      'text-based PDF, or paste your CV as a .txt file.';
    const extractionFailedCv = makeExtractedCv({
      status: 'extraction_failed',
      character_count: null,
      failure_reason: 'no_text_layer',
      failure_message: failureMessage,
    });

    stubFetch(
      (callNumber) =>
        Promise.resolve(jsonResponse(200, { items: callNumber === 1 ? [] : [extractionFailedCv] })),
      () => Promise.resolve(jsonResponse(201, extractionFailedCv)),
    );

    renderWithQuery(<BaseCvUploadPanel />);
    const input = await screen.findByLabelText(DROPZONE_LABEL);
    const file = new File(['%PDF-1.4 scan'], 'scan.pdf', { type: 'application/pdf' });

    await user.upload(input, file);

    expect(await screen.findByText(failureMessage)).toBeInTheDocument();
  });

  it('AC-15: the three error kinds render textually distinct messages', async () => {
    const user = userEvent.setup();

    // Error A — rejected before upload.
    stubFetch(
      () => Promise.resolve(jsonResponse(200, { items: [] })),
      () => Promise.resolve(jsonResponse(200, {})),
    );
    const renderA = renderWithQuery(<BaseCvUploadPanel />);
    const inputA = await screen.findByLabelText(DROPZONE_LABEL);
    const oversizedFile = new File([new Uint8Array(10 * 1024 * 1024 + 1)], 'resume.pdf', {
      type: 'application/pdf',
    });
    await user.upload(inputA, oversizedFile);
    const textA = (await screen.findByRole('alert')).textContent;
    renderA.unmount();
    vi.unstubAllGlobals();

    // Error B — rejected by the API.
    stubFetch(
      () => Promise.resolve(jsonResponse(200, { items: [] })),
      () =>
        Promise.resolve(
          jsonResponse(415, {
            error: {
              code: 'unsupported_format',
              message: 'We only accept PDF, DOCX or TXT files.',
            },
          }),
        ),
    );
    const renderB = renderWithQuery(<BaseCvUploadPanel />);
    const inputB = await screen.findByLabelText(DROPZONE_LABEL);
    await user.upload(inputB, new File(['%PDF-1.4'], 'resume.pdf', { type: 'application/pdf' }));
    const textB = (await screen.findByRole('alert')).textContent;
    renderB.unmount();
    vi.unstubAllGlobals();

    // Error C — stored but unreadable.
    const failureMessage =
      "We saved your file but couldn't read any text from it — it looks like a scan. Try a " +
      'text-based PDF, or paste your CV as a .txt file.';
    const extractionFailedCv = makeExtractedCv({
      status: 'extraction_failed',
      character_count: null,
      failure_reason: 'no_text_layer',
      failure_message: failureMessage,
    });
    stubFetch(
      (callNumber) =>
        Promise.resolve(jsonResponse(200, { items: callNumber === 1 ? [] : [extractionFailedCv] })),
      () => Promise.resolve(jsonResponse(201, extractionFailedCv)),
    );
    const renderC = renderWithQuery(<BaseCvUploadPanel />);
    const inputC = await screen.findByLabelText(DROPZONE_LABEL);
    await user.upload(inputC, new File(['%PDF-1.4'], 'scan.pdf', { type: 'application/pdf' }));
    const textC = (await screen.findByText(failureMessage)).textContent;
    renderC.unmount();

    // The direct comparison AC-15 exists for: error C must not read as either of the other two.
    expect(textC).not.toBe(textA);
    expect(textC).not.toBe(textB);
    expect(textA).not.toBe(textB);
  });
});
