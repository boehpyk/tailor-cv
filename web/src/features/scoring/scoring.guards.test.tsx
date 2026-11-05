import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { WorkspaceScopeProvider } from '@/features/scope/WorkspaceScope';
import { createAppQueryClient } from '@/queryClient';
import { USER_A, signInAs, stubAccountFetch } from '@/test/accountFetch';
import { jsonResponse } from '@/test/fixtures';

import { CHECK_BUTTON, FIT_SCORE_NOTICE, NOT_READY } from './fitScoreCopy';
import { FitScorePanel } from './components/FitScorePanel';
import { FitScoreResult } from './components/FitScoreResult';

import type { FitScore } from './types';

/**
 * T42 (`qa`, test-after): the panel's accessibility, the markup at 360 px, and gap text as text.
 * The source-wide ban on raw-HTML rendering is `security.grep.test.ts` (AC-27), which scans all of
 * `web/src` including this feature; a second scan here would only trip it.
 */

const PAIR = {
  base_cv_id: '0192f0a1-aaaa-7000-8000-00000000aaaa',
  job_posting_id: '0192f0a1-bbbb-7000-8000-00000000bbbb',
};
const SCORE: FitScore = {
  id: '0190a000-0000-7000-8000-000000000001',
  score_percent: 62,
  gaps: ['Kubernetes'],
  scored_at: '2026-10-31T10:00:00Z',
  expires_at: null,
};

function renderPanel(pair: typeof PAIR | null): void {
  signInAs(USER_A);
  stubAccountFetch({
    'GET /api/me/fit-scores': () => jsonResponse(200, { fit_score: null }),
  });
  render(
    <QueryClientProvider client={createAppQueryClient()}>
      <MemoryRouter>
        <WorkspaceScopeProvider scope={{ kind: 'account', userId: USER_A.id }}>
          <FitScorePanel pair={pair} />
        </WorkspaceScopeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const unprefixed = (el: Element): string[] =>
  el.className.split(/\s+/).filter((t) => t !== '' && !t.includes(':'));

beforeEach(() => {
  __resetForTests();
});
afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

describe('gap text', () => {
  it('a gap containing markup renders as text, not as an element', () => {
    const { container } = render(
      <FitScoreResult score={{ ...SCORE, gaps: ['<img src=x onerror=alert(1)>'] }} scope="guest" />,
    );
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
  });
});

describe('FitScorePanel accessibility', () => {
  it('the button is described by the notice, which is visible text', async () => {
    renderPanel(PAIR);
    const button = await screen.findByRole('button', { name: CHECK_BUTTON });
    expect(button).toBeEnabled();
    expect(button).toHaveAccessibleDescription(FIT_SCORE_NOTICE);
    expect(screen.getByText(FIT_SCORE_NOTICE)).toBeVisible();
  });

  it('a disabled button is described by its reason', async () => {
    renderPanel(null);
    const button = await screen.findByRole('button', { name: CHECK_BUTTON });
    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription(NOT_READY);
  });

  it('the status region exists, empty, before anything is working', async () => {
    renderPanel(PAIR);
    await screen.findByRole('button', { name: CHECK_BUTTON });
    for (const region of screen.getAllByRole('status')) {
      expect(region).toBeEmptyDOMElement();
    }
  });

  it('the result is a heading, a list and plain text, with no landmark noise', () => {
    render(<FitScoreResult score={SCORE} scope="account" />);
    expect(screen.getByRole('heading', { level: 3 })).toBeInTheDocument();
    expect(screen.getByRole('list')).toBeInTheDocument();
    expect(screen.getAllByRole('listitem')).toHaveLength(1);
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });
});

describe('FitScorePanel at 360 px (the classes that produce it)', () => {
  it('the button is full width below sm and at least 44 px tall', async () => {
    renderPanel(PAIR);
    const button = await screen.findByRole('button', { name: CHECK_BUTTON });
    // jsdom computes no layout, so this pins the Tailwind structure.
    expect(button.className).toContain('max-sm:w-full');
    expect(button.className).toContain('min-h-11');
  });

  it('nothing in the result has an unprefixed fixed width or sideways overflow', () => {
    const { container } = render(<FitScoreResult score={SCORE} scope="guest" />);
    for (const el of container.querySelectorAll('*')) {
      expect(
        unprefixed(el).filter((t) => /^(?:min-|max-)?w-|^overflow-x-|^whitespace-nowrap$/.test(t)),
      ).toEqual([]);
    }
  });
});
