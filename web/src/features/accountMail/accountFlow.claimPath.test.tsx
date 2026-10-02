import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CHECK_EMAIL_HEADING, SEND_AGAIN_LABEL } from '@/features/accountMail/accountMailCopy';
import {
  CTA_REGION_LABEL,
  CTA_REGISTER_LABEL,
  KEEP_LABEL,
  OFFER_REGION_LABEL,
} from '@/features/claim/claimCopy';
import { guestCv, guestRun } from '@/features/claim/test/support';
import {
  USER_A,
  callsTo,
  ok,
  signedInRoutes,
  stubAccountFetch,
  tokenFor,
} from '@/test/accountFetch';
import { jsonResponse, makeRun } from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';

import { EMAIL, PASSWORD, accepted, resetAccountMailTest } from './test/support';

/**
 * T37 RED — AC-50, through the REAL route table: 2.4's claim path survives registration no longer
 * signing in. CTA on a succeeded guest run → `/register?next=X` → "Check your email" (the 202) →
 * *Already confirmed? Log in* → `/login?next=X` → logging in lands on `safeNext(X)` → the claim
 * offer. The mail click happens "elsewhere" and is not part of this browser's flow.
 *
 * The server's answers are stateful where it matters: login 200 only after the register 202, and
 * the guest lists still hold the work (nothing consumed it).
 *
 * This **replaces the meaning** of 2.4's "register, then land on `safeNext`" (that path is
 * `crossLinks.test.tsx`'s round trip, amended in this commit).
 */

const RUN_ID = 'run-1';
const NEXT = `/runs/${RUN_ID}/cv`;
const ENCODED_NEXT = '%2Fruns%2Frun-1%2Fcv';

function run() {
  return makeRun({
    id: RUN_ID,
    status: 'succeeded',
    tailored_cv: 'my tailored cv text',
    cover_letter: 'my cover letter text',
    tailored_cv_character_count: 19,
    cover_letter_character_count: 20,
    completed_at: '2026-09-12T10:00:09Z',
  });
}

beforeEach(() => {
  resetAccountMailTest();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetAccountMailTest();
});

describe('The claim path with email confirmation (AC-50)', () => {
  it('CTA → register → check your email → Already confirmed? Log in → login → the claim offer on the same run', async () => {
    const state = { registered: false };
    const fetch = stubAccountFetch({
      ...signedInRoutes(USER_A),
      'GET /api/base-cvs': ok({ items: [guestCv()] }),
      'GET /api/tailoring-runs': ok({ items: [guestRun({ id: RUN_ID })] }),
      'GET /api/tailoring-runs/:id': ok(run()),
      'GET /api/tailoring-runs/:id/exports': ok({ items: [] }),
      'POST /api/auth/register': () => {
        state.registered = true;
        return accepted();
      },
      'POST /api/auth/login': () =>
        state.registered
          ? jsonResponse(200, {
              access_token: tokenFor(USER_A),
              token_type: 'Bearer',
              expires_in: 900,
              user: USER_A,
            })
          : jsonResponse(401, { error: { code: 'invalid_credentials', message: 'x' } }),
    });
    const user = userEvent.setup();
    const { router } = renderWithRouter(NEXT);

    // 1. The CTA on the succeeded guest run.
    const cta = await screen.findByRole('region', { name: CTA_REGION_LABEL });
    await user.click(within(cta).getByRole('link', { name: CTA_REGISTER_LABEL }));
    await screen.findByRole('region', { name: 'Create an account' });
    expect(new URLSearchParams(router.state.location.search).get('next')).toBe(NEXT);

    // 2. Register: a 202 shows Check your email, and the browser is not signed in.
    await user.type(screen.getByLabelText(/email/i), EMAIL);
    await user.type(screen.getByLabelText(/password/i), PASSWORD);
    await user.click(screen.getByRole('button', { name: /^create account$/i }));
    await screen.findByRole('heading', { name: CHECK_EMAIL_HEADING });
    expect(screen.getByRole('button', { name: SEND_AGAIN_LABEL })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/register');
    expect(callsTo(fetch, 'POST', '/api/auth/login')).toHaveLength(0);

    // 3. (The mail is confirmed elsewhere.) Already confirmed? Log in → /login?next=X.
    const main = within(screen.getByRole('main'));
    const login = main.getByRole('link', { name: /log in/i });
    expect(login).toHaveAttribute('href', `/login?next=${ENCODED_NEXT}`);
    await user.click(login);
    await screen.findByRole('region', { name: 'Log in' });
    expect(router.state.location.pathname).toBe('/login');

    // 4. Log in: safeNext(X) lands on the run, and the claim offer is there.
    await user.type(screen.getByLabelText(/email/i), EMAIL);
    await user.type(screen.getByLabelText(/password/i), PASSWORD);
    await user.click(screen.getByRole('button', { name: /^log in$/i }));

    const offer = await screen.findByRole('region', { name: OFFER_REGION_LABEL });
    expect(router.state.location.pathname).toBe(NEXT);
    expect(within(offer).getByRole('button', { name: KEEP_LABEL })).toBeInTheDocument();
  });
});
