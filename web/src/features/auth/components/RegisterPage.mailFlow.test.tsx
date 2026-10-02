import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  CHECK_EMAIL_HEADING,
  GUEST_WORK_DEADLINE_NOTE,
  SEND_AGAIN_LABEL,
  SENT_AGAIN_NOTE,
  USE_DIFFERENT_EMAIL_LABEL,
} from '@/features/accountMail/accountMailCopy';
import {
  EMAIL,
  PASSWORD,
  accepted,
  renderPage,
  resetAccountMailTest,
} from '@/features/accountMail/test/support';
import { guestCv, guestRun } from '@/features/claim/test/support';
import { callsTo, ok, stubAccountFetch } from '@/test/accountFetch';

import { authStore } from '../authStore';
import { RegisterPage } from './RegisterPage';

/**
 * T37 RED — `/register` through the 202 (AC-45, AC-50, V-60, V-61), the page's half: the form is
 * *replaced* by "Check your email", **Send it again** re-posts what the page holds, **Use a different
 * email** brings the form back with the address and without the password. The components'
 * own states are `CheckYourEmail.test.tsx`; this file is the wiring between them.
 *
 * Mounted bare (no app layout), so the only "Log in" link on screen is the page's own.
 */

const REGISTER = 'POST /api/auth/register';

function guestRoutes(withWork: boolean) {
  return {
    'GET /api/base-cvs': ok({ items: withWork ? [guestCv()] : [] }),
    'GET /api/tailoring-runs': ok({ items: withWork ? [guestRun()] : [] }),
  };
}

function mount(entry = '/register') {
  return renderPage(<RegisterPage />, { routePath: '/register', entry });
}

async function fillAndSubmit(): Promise<void> {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/email/i), EMAIL);
  await user.type(screen.getByLabelText(/password/i), PASSWORD);
  await user.click(screen.getByRole('button', { name: /^create account$/i }));
}

beforeEach(() => {
  resetAccountMailTest();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetAccountMailTest();
});

describe('/register after a 202', () => {
  it('replaces the form: the heading shows, the fields and the submit button are gone, and the URL does not change', async () => {
    stubAccountFetch({ [REGISTER]: accepted, ...guestRoutes(false) });
    const page = mount('/register?next=%2Fruns%2Frun-1%2Fcv');

    await fillAndSubmit();

    expect(await screen.findByRole('heading', { name: CHECK_EMAIL_HEADING })).toBeInTheDocument();
    expect(screen.queryByLabelText(/password/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^create account$/i })).not.toBeInTheDocument();
    expect(page.location()).toBe('/register?next=%2Fruns%2Frun-1%2Fcv');
  });

  it('signs nobody in and asks the server for no session: only the one POST (plan §0.6)', async () => {
    const fetch = stubAccountFetch({ [REGISTER]: accepted, ...guestRoutes(false) });
    mount();

    await fillAndSubmit();

    await screen.findByRole('heading', { name: CHECK_EMAIL_HEADING });
    expect(authStore.getSnapshot()).toEqual({ status: 'anonymous', reason: 'expired' });
    expect(fetch.calls.filter((call) => call.method === 'POST').map((call) => call.path)).toEqual([
      '/api/auth/register',
    ]);
  });

  it('names the address the visitor typed', async () => {
    stubAccountFetch({ [REGISTER]: accepted, ...guestRoutes(false) });
    mount();

    await fillAndSubmit();

    expect(await screen.findByRole('status')).toHaveTextContent(EMAIL);
  });

  it('Send it again re-posts the typed credentials (and only those), then says "Sent again."', async () => {
    const fetch = stubAccountFetch({ [REGISTER]: accepted, ...guestRoutes(false) });
    mount();
    await fillAndSubmit();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: SEND_AGAIN_LABEL }));

    await screen.findByText(SENT_AGAIN_NOTE);
    expect(callsTo(fetch, 'POST', '/api/auth/register').map((call) => call.body)).toEqual([
      { email: EMAIL, password: PASSWORD },
      { email: EMAIL, password: PASSWORD },
    ]);
  });

  it('Use a different email brings the form back: the email kept, the password cleared, no request', async () => {
    const fetch = stubAccountFetch({ [REGISTER]: accepted, ...guestRoutes(false) });
    mount();
    await fillAndSubmit();

    await userEvent
      .setup()
      .click(await screen.findByRole('button', { name: USE_DIFFERENT_EMAIL_LABEL }));

    const email = await screen.findByLabelText(/email/i);
    expect(email).toHaveValue(EMAIL);
    expect(screen.getByLabelText(/password/i)).toHaveValue('');
    expect(screen.queryByRole('heading', { name: CHECK_EMAIL_HEADING })).not.toBeInTheDocument();
    expect(callsTo(fetch, 'POST', '/api/auth/register')).toHaveLength(1);
  });

  it('the password the visitor typed is nowhere in the document after Use a different email', async () => {
    stubAccountFetch({ [REGISTER]: accepted, ...guestRoutes(false) });
    const page = mount();
    await fillAndSubmit();

    await userEvent
      .setup()
      .click(await screen.findByRole('button', { name: USE_DIFFERENT_EMAIL_LABEL }));

    await screen.findByLabelText(/email/i);
    expect(page.container.innerHTML).not.toContain(PASSWORD);
  });

  it('the guest-work deadline shows when this browser has guest work, and not when it has none (V-6)', async () => {
    stubAccountFetch({ [REGISTER]: accepted, ...guestRoutes(true) });
    const withWork = mount();
    await fillAndSubmit();
    expect(await screen.findByText(GUEST_WORK_DEADLINE_NOTE)).toBeInTheDocument();
    withWork.unmount();

    stubAccountFetch({ [REGISTER]: accepted, ...guestRoutes(false) });
    mount();
    await fillAndSubmit();
    await screen.findByRole('heading', { name: CHECK_EMAIL_HEADING });
    await waitFor(() => {
      expect(screen.queryByText(GUEST_WORK_DEADLINE_NOTE)).not.toBeInTheDocument();
    });
  });
});
