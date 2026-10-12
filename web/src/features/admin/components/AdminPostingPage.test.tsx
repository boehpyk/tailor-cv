import { screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { createAppQueryClient } from '@/queryClient';
import { hang, signInAs, status, stubAccountFetch } from '@/test/accountFetch';
import { ADMIN, TARGET_ID, contentRoutes, posting, world } from '@/test/adminContentFetch';
import { renderWithRouter } from '@/test/render';

import { BACK_TO_ACCOUNT, POSTING_GONE } from '../adminCopy';

import type { JobPosting } from '@/features/posting/types';
import type { RouteHandler } from '@/test/accountFetch';

/**
 * T12 RED — slice 4.3, AC-25 for a posting (its text, and its source URL as **text, not a link**,
 * OQ-13) and the page's states. Against T11's skeleton each fails on an absent element.
 */

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete (window as unknown as Record<string, unknown>).__pwned;
});

const P = '/api/admin/users/:id';

function open(p: JobPosting = posting(), overrides: Record<string, RouteHandler> = {}) {
  stubAccountFetch(contentRoutes(world({ postings: [p] }), overrides));
  signInAs(ADMIN);
  return renderWithRouter(`/admin/users/${TARGET_ID}/postings/posting-1`, {
    queryClient: createAppQueryClient(),
  });
}

describe('the posting page — AC-25', () => {
  it('loading: a status line', async () => {
    open(posting(), { [`GET ${P}/job-postings/:pid`]: hang });

    expect(await screen.findByText(/^Loading/)).toHaveAttribute('role', 'status');
  });

  it('the title is the h1 and the text is shown', async () => {
    open(posting({ text: 'Lead the platform team.' }));

    expect(
      await screen.findByRole('heading', { level: 1, name: 'Senior Platform Engineer' }),
    ).toBeVisible();
    expect(screen.getByText('Lead the platform team.')).toBeVisible();
  });

  it('the posting text is a pre-wrap text node — markup and Markdown stay literal', async () => {
    const body = '<img src=x onerror="window.__pwned=1"> and **bold**';
    const { container } = open(posting({ text: body }));

    const block = await screen.findByText(body);
    expect(block).toHaveClass('whitespace-pre-wrap');
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('strong')).toBeNull();
    expect((window as unknown as Record<string, unknown>).__pwned).toBeUndefined();
  });

  it('a fetched posting shows its source URL as text, never as a link (OQ-13)', async () => {
    const url = 'https://jobs.example.com/apply/42';
    open(posting({ source: 'fetched', source_url: url }));

    expect(await screen.findByText(url)).toBeVisible();
    expect(screen.queryByRole('link', { name: url })).not.toBeInTheDocument();
    expect(document.querySelector(`a[href="${url}"]`)).toBeNull();
  });

  it('job_posting_not_found: "This job posting no longer exists." and a way back', async () => {
    open(posting(), { [`GET ${P}/job-postings/:pid`]: status(404, 'job_posting_not_found') });

    expect(await screen.findByText(POSTING_GONE)).toBeVisible();
    expect(screen.getByRole('link', { name: BACK_TO_ACCOUNT })).toHaveAttribute(
      'href',
      `/admin/users/${TARGET_ID}`,
    );
  });

  it('a 503 is an alert with Retry', async () => {
    open(posting(), { [`GET ${P}/job-postings/:pid`]: status(503, 'service_unavailable') });

    const alert = await screen.findByRole('alert', {}, { timeout: 4000 });
    expect(alert).toBeVisible();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeVisible();
  });
});
