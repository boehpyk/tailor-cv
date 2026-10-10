import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import { SavedBaseCvsSection } from '@/features/savedCvs/components/SavedBaseCvsSection';
import { USER_A, ok, signInAs, signedInRoutes, stubAccountFetch } from '@/test/accountFetch';
import { renderWithRouter } from '@/test/render';

/**
 * T20 RED — slice 4.1, AC-37's behaviour half (OQ-14, approved; Constitution §8 quotes it): the one
 * operator-access sentence is on the guest workspace, the account workspace's promise and the
 * saved-CV notice on `/account`. The sentence is spelled out here, not imported, so a rename of the
 * constant cannot make this pass. (The verbatim-from-one-constant pin is T22's.)
 */

const OPERATOR_SENTENCE =
  'The person who runs TailorCraft can read what is stored here — CVs, job postings and tailored documents — to operate and support the service.';

/** Matches an element whose own text contains the sentence, wherever the implementer puts it. */
const containsSentence = (content: string): boolean => content.includes(OPERATOR_SENTENCE);

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('the operator-access disclosure (AC-37)', () => {
  it('the guest workspace states it', async () => {
    stubAccountFetch({
      'GET /api/base-cvs': ok({ items: [] }),
      'GET /api/job-postings': ok({ items: [] }),
      'GET /api/tailoring-runs': ok({ items: [] }),
    });
    authStore.signOut('expired');

    renderWithRouter('/');

    expect(await screen.findByRole('tab', { name: 'Base CV' })).toBeInTheDocument();
    expect(screen.getAllByText(containsSentence).length).toBeGreaterThan(0);
  });

  it('the account workspace promise states it', async () => {
    stubAccountFetch({
      ...signedInRoutes(USER_A),
      'GET /api/me/base-cvs': ok({ items: [] }),
      'GET /api/me/job-postings': ok({ items: [] }),
      'GET /api/me/tailoring-runs': ok({ items: [], next_cursor: null }),
    });
    signInAs(USER_A);

    renderWithRouter('/');

    expect(
      await screen.findByText(/You're signed in, so what you tailor here/),
    ).toBeInTheDocument();
    expect(screen.getAllByText(containsSentence).length).toBeGreaterThan(0);
  });

  it('the saved-CV notice on /account states it', async () => {
    stubAccountFetch({ ...signedInRoutes(USER_A), 'GET /api/me/base-cvs': ok({ items: [] }) });
    signInAs(USER_A);

    render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
        }
      >
        <SavedBaseCvsSection />
      </QueryClientProvider>,
    );

    expect(await screen.findByText('No saved CVs yet')).toBeInTheDocument();
    expect(screen.getAllByText(containsSentence).length).toBeGreaterThan(0);
  });
});
