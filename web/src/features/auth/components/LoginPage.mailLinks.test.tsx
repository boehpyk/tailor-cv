import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  CONFIRM_EMAIL_FIRST_NOTE,
  FORGOT_PASSWORD_LABEL,
} from '@/features/accountMail/accountMailCopy';
import {
  EMAIL,
  PASSWORD,
  refusal,
  renderPage,
  resetAccountMailTest,
} from '@/features/accountMail/test/support';
import { stubAccountFetch } from '@/test/accountFetch';

import { LoginPage } from './LoginPage';

/**
 * T37 RED — `/login` gains the way to a reset and a second sentence (AC-49, V-55). Both fail on a
 * missing link / text today.
 */

const LOGIN = 'POST /api/auth/login';

async function submitLogin(): Promise<void> {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/email/i), EMAIL);
  await user.type(screen.getByLabelText(/password/i), PASSWORD);
  await user.click(screen.getByRole('button', { name: /^log in$/i }));
}

beforeEach(() => {
  resetAccountMailTest();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetAccountMailTest();
});

describe('/login: Forgot your password? (AC-49)', () => {
  it('is a link to /reset-password', () => {
    stubAccountFetch({});

    renderPage(<LoginPage />, { routePath: '/login' });

    expect(screen.getByRole('link', { name: FORGOT_PASSWORD_LABEL })).toHaveAttribute(
      'href',
      '/reset-password',
    );
  });

  it('is still /reset-password when the page arrived with a next', () => {
    stubAccountFetch({});

    renderPage(<LoginPage />, { routePath: '/login', entry: '/login?next=%2Fruns%2Frun-1%2Fcv' });

    expect(screen.getByRole('link', { name: FORGOT_PASSWORD_LABEL })).toHaveAttribute(
      'href',
      '/reset-password',
    );
  });
});

describe('/login: invalid_credentials gains a second sentence (AC-49)', () => {
  it('keeps the first sentence and adds "Just created an account? Confirm your email address first — check your inbox."', async () => {
    stubAccountFetch({ [LOGIN]: refusal(401, 'invalid_credentials') });
    renderPage(<LoginPage />, { routePath: '/login' });

    await submitLogin();

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent("That email and password don't match an account.");
    expect(alert).toHaveTextContent(CONFIRM_EMAIL_FIRST_NOTE);
  });

  it('pins the spec sentence itself, not only the constant', () => {
    expect(CONFIRM_EMAIL_FIRST_NOTE).toBe(
      'Just created an account? Confirm your email address first — check your inbox.',
    );
  });

  it('is only for invalid_credentials: a 429 does not say it', async () => {
    stubAccountFetch({ [LOGIN]: refusal(429, 'rate_limited', {}, { 'Retry-After': '120' }) });
    renderPage(<LoginPage />, { routePath: '/login' });

    await submitLogin();

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).not.toBe('');
    expect(alert).not.toHaveTextContent(CONFIRM_EMAIL_FIRST_NOTE);
  });
});
