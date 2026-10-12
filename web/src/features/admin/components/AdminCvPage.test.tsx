import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { createAppQueryClient } from '@/queryClient';
import { callsTo, hang, signInAs, status, stubAccountFetch } from '@/test/accountFetch';
import { ADMIN, TARGET_ID, contentRoutes, savedCv, world } from '@/test/adminContentFetch';
import { firewallNotFound, userGone } from '@/test/adminFetch';
import { renderWithRouter } from '@/test/render';

import {
  BACK_TO_ACCOUNT,
  BACK_TO_USERS,
  CV_ERROR,
  CV_GONE,
  USER_GONE,
  USER_LOADING,
  cvExtractionFailed,
} from '../adminCopy';

import type { AdminSavedBaseCv } from '@/api/admin';
import type { RouteHandler } from '@/test/accountFetch';

/**
 * T12 RED — slice 4.3, AC-27 (the CV page) and AC-25 for the CV body (text, never markup).
 * Against T11's skeleton (a fixed "Loading…" status) each fails on an absent element.
 */

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete (window as unknown as Record<string, unknown>).__pwned;
});

const P = '/api/admin/users/:id';
const CV_PATH = `/admin/users/${TARGET_ID}/cvs/cv-1`;

function open(cv: AdminSavedBaseCv = savedCv(), overrides: Record<string, RouteHandler> = {}) {
  const fetch = stubAccountFetch(contentRoutes(world({ cvs: [cv] }), overrides));
  signInAs(ADMIN);
  const view = renderWithRouter(CV_PATH, { queryClient: createAppQueryClient() });
  return { fetch, ...view };
}

describe('the CV page — AC-27', () => {
  it('loading: a status line while the CV is pending', async () => {
    const { fetch } = open(savedCv(), { [`GET ${P}/base-cvs/:cvId`]: hang });

    // Not the lazy chunk's Suspense fallback (also a status): wait until the page asked for its CV.
    await waitFor(() => {
      expect(callsTo(fetch, 'GET', `${P}/base-cvs/cv-1`)).toHaveLength(1);
    });
    expect(within(screen.getByRole('main')).getByText(USER_LOADING)).toHaveAttribute(
      'role',
      'status',
    );
  });

  it('the label is the h1, with the filename, type, size, uploaded and character count', async () => {
    open();

    expect(await screen.findByRole('heading', { level: 1, name: 'Backend roles' })).toBeVisible();
    expect(within(screen.getByRole('main')).getAllByRole('heading', { level: 1 })).toHaveLength(1);
    const field = (term: string): HTMLElement | null =>
      screen.getByText(term, { selector: 'dt' }).nextElementSibling as HTMLElement | null;
    expect(field('Filename')).toHaveTextContent('jane.pdf');
    expect(field('Type')).toBeInTheDocument();
    expect(field('Size')).toBeInTheDocument();
    expect(field('Uploaded')).toBeInTheDocument();
    expect(field('Characters')).toHaveTextContent(/4,?200/);
  });

  it('without a label the filename is the h1', async () => {
    open(savedCv({ label: null, original_filename: 'only-a-file.pdf' }));

    expect(await screen.findByRole('heading', { level: 1, name: 'only-a-file.pdf' })).toBeVisible();
  });

  it('shows the extracted text', async () => {
    open(savedCv({ extracted_text: 'Jane Doe led the platform team.' }));

    expect(await screen.findByText('Jane Doe led the platform team.')).toBeVisible();
  });

  it('a CV whose extraction failed says so with the failure message and shows no text', async () => {
    open(
      savedCv({
        status: 'extraction_failed',
        extracted_text: null,
        character_count: null,
        failure_reason: 'extractor_error',
        failure_message: 'This PDF could not be read.',
      }),
    );

    expect(
      await screen.findByText(cvExtractionFailed('This PDF could not be read.')),
    ).toBeVisible();
  });

  it('AC-25: the CV text is a pre-wrap text node — markup and Markdown stay literal', async () => {
    const body = '<img src=x onerror="window.__pwned=1"> and **bold**';
    const { container } = open(savedCv({ extracted_text: body }));

    const block = await screen.findByText(body);
    expect(block).toHaveClass('whitespace-pre-wrap');
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('strong')).toBeNull();
    expect((window as unknown as Record<string, unknown>).__pwned).toBeUndefined();
  });

  it('base_cv_not_found: "This CV no longer exists." with a link back to the account', async () => {
    open(savedCv(), { [`GET ${P}/base-cvs/:cvId`]: status(404, 'base_cv_not_found') });

    expect(await screen.findByText(CV_GONE)).toBeVisible();
    expect(screen.getByRole('link', { name: BACK_TO_ACCOUNT })).toHaveAttribute(
      'href',
      `/admin/users/${TARGET_ID}`,
    );
  });

  it('user_not_found: "This account no longer exists." with a link to /admin', async () => {
    open(savedCv(), { [`GET ${P}/base-cvs/:cvId`]: userGone });

    expect(await screen.findByText(USER_GONE)).toBeVisible();
    expect(screen.getByRole('link', { name: BACK_TO_USERS })).toHaveAttribute('href', '/admin');
  });

  it("the firewall's code-less 404 is the app's not-found page, never the admin copy", async () => {
    open(savedCv(), { [`GET ${P}/base-cvs/:cvId`]: firewallNotFound });

    expect(await screen.findByText("We couldn't find that page.")).toBeVisible();
    expect(screen.queryByText(CV_GONE)).not.toBeInTheDocument();
  });

  it('a 503 is an alert with Retry, distinct from loading; Retry shows the CV', async () => {
    let calls = 0;
    open(savedCv(), {
      [`GET ${P}/base-cvs/:cvId`]: () => {
        calls += 1;
        return calls <= 2 ? status(503, 'service_unavailable')() : Response.json(savedCv());
      },
    });

    const alert = await screen.findByRole('alert', {}, { timeout: 4000 });
    expect(within(alert).getByText(CV_ERROR)).toBeVisible();
    await userEvent.setup().click(within(alert).getByRole('button', { name: 'Retry' }));

    expect(await screen.findByRole('heading', { level: 1, name: 'Backend roles' })).toBeVisible();
  });
});
