import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { authStore } from '@/features/auth/authStore';
import { USER_A, callsTo, signInAs, stubAccountFetch, tokenFor } from '@/test/accountFetch';

import {
  CONFIRMING_LABEL,
  CONFIRM_ALREADY_REGISTERED,
  CONFIRM_EMAIL_HEADING,
  CONFIRM_EMAIL_LABEL,
  CONFIRM_LINK_INVALID,
  CONFIRM_UNAVAILABLE,
  CREATE_ACCOUNT_LABEL,
  EMAIL_CONFIRMED_NOTE,
  LINK_INCOMPLETE,
  LOG_IN_LABEL,
  RESET_PASSWORD_LINK_LABEL,
} from '../accountMailCopy';
import {
  TOKEN,
  activateTwiceInOneTick,
  hangForever,
  networkFailure,
  noContent,
  openLink,
  refusal,
  renderPage,
  resetAccountMailTest,
} from '../test/support';
import { ConfirmEmailPage } from './ConfirmEmailPage';

/**
 * T37 RED — `/confirm-email` (AC-46, V-62, V-63, V-65, V-66), against feature-spec.md and
 * technical-plan.md §7's table — never against the T36 skeleton, which renders `null`. Every
 * assertion below fails on a missing role or text ("Unable to find …"), not on an import.
 *
 * **Absence is paired with a positive** (the skeleton satisfies every absence): "no request on
 * mount" first waits for the page to have rendered the thing it is in the state of.
 */

const PATH = '/confirm-email';
const CONFIRM = 'POST /api/auth/registration/confirm';

function mountWithToken() {
  openLink(PATH, TOKEN);
  return renderPage(<ConfirmEmailPage />, { routePath: PATH, entry: PATH });
}

function confirmButton(): HTMLElement {
  return screen.getByRole('button', { name: CONFIRM_EMAIL_LABEL });
}

beforeEach(() => {
  resetAccountMailTest();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetAccountMailTest();
});

describe('/confirm-email: empty (no token in the fragment)', () => {
  it('says the link is incomplete, offers no button, and sends nothing (V-62)', async () => {
    const fetch = stubAccountFetch({});
    openLink(PATH);

    renderPage(<ConfirmEmailPage />, { routePath: PATH });

    expect(await screen.findByText(LINK_INCOMPLETE)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: CONFIRM_EMAIL_LABEL })).not.toBeInTheDocument();
    expect(fetch.calls).toHaveLength(0);
  });

  it('a reload after the strip is the same empty state (V-63)', async () => {
    stubAccountFetch({});
    const first = mountWithToken();
    await screen.findByRole('button', { name: CONFIRM_EMAIL_LABEL });
    first.unmount();

    renderPage(<ConfirmEmailPage />, { routePath: PATH });

    expect(await screen.findByText(LINK_INCOMPLETE)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: CONFIRM_EMAIL_LABEL })).not.toBeInTheDocument();
  });
});

describe('/confirm-email: idle', () => {
  it('shows the heading and an enabled button, and sends nothing until it is clicked (OQ-4)', async () => {
    const fetch = stubAccountFetch({});

    mountWithToken();

    expect(await screen.findByRole('heading', { name: CONFIRM_EMAIL_HEADING })).toBeInTheDocument();
    expect(confirmButton()).toBeEnabled();
    // A mail scanner that prefetches the link must confirm nothing.
    expect(fetch.calls).toHaveLength(0);
  });

  it('strips the fragment on mount, keeping the path (AC-46)', async () => {
    stubAccountFetch({});
    openLink(PATH, TOKEN);
    const replaceState = vi.spyOn(window.history, 'replaceState');

    renderPage(<ConfirmEmailPage />, { routePath: PATH });

    await screen.findByRole('button', { name: CONFIRM_EMAIL_LABEL });
    expect(replaceState).toHaveBeenCalledWith(null, '', PATH);
    expect(window.location.hash).toBe('');
  });

  it('under <StrictMode> reads once, strips once, and still posts the token on click', async () => {
    const fetch = stubAccountFetch({ [CONFIRM]: noContent });
    openLink(PATH, TOKEN);
    const replaceState = vi.spyOn(window.history, 'replaceState');

    renderPage(<ConfirmEmailPage />, { routePath: PATH, strict: true });
    await userEvent.setup().click(await screen.findByRole('button', { name: CONFIRM_EMAIL_LABEL }));

    await screen.findByText(EMAIL_CONFIRMED_NOTE);
    expect(replaceState).toHaveBeenCalledTimes(1);
    expect(callsTo(fetch, 'POST', '/api/auth/registration/confirm').map((c) => c.body)).toEqual([
      { token: TOKEN },
    ]);
  });
});

describe('/confirm-email: pending', () => {
  it('says "Confirming…", disables the button, and shows no error (still working is not failed)', async () => {
    stubAccountFetch({ [CONFIRM]: hangForever });
    mountWithToken();

    await userEvent.setup().click(await screen.findByRole('button', { name: CONFIRM_EMAIL_LABEL }));

    const pending = await screen.findByRole('button', { name: CONFIRMING_LABEL });
    expect(pending).toBeDisabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByText(EMAIL_CONFIRMED_NOTE)).not.toBeInTheDocument();
  });

  it('a same-tick double click posts once (V-60, isMutating)', async () => {
    const fetch = stubAccountFetch({ [CONFIRM]: hangForever });
    mountWithToken();
    const button = await screen.findByRole('button', { name: CONFIRM_EMAIL_LABEL });

    activateTwiceInOneTick(button);

    await screen.findByRole('button', { name: CONFIRMING_LABEL });
    expect(callsTo(fetch, 'POST', '/api/auth/registration/confirm')).toHaveLength(1);
  });
});

describe('/confirm-email: success', () => {
  it('204: a status "Your email address is confirmed." and a Log in link to /login — and nobody is signed in', async () => {
    const fetch = stubAccountFetch({ [CONFIRM]: noContent });
    mountWithToken();

    await userEvent.setup().click(await screen.findByRole('button', { name: CONFIRM_EMAIL_LABEL }));

    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent(EMAIL_CONFIRMED_NOTE);
    expect(screen.getByRole('link', { name: LOG_IN_LABEL })).toHaveAttribute('href', '/login');
    // Confirming does not sign in (plan §0.6): the store is untouched and nothing was refreshed.
    expect(authStore.getSnapshot()).toEqual({ status: 'anonymous', reason: 'expired' });
    expect(fetch.calls.map((call) => `${call.method} ${call.path}`)).toEqual([
      `POST /api/auth/registration/confirm`,
    ]);
  });

  it('while signed in as someone else, confirming leaves that session untouched and carries no bearer (V-65)', async () => {
    const fetch = stubAccountFetch({ [CONFIRM]: noContent });
    signInAs(USER_A);
    mountWithToken();

    await userEvent.setup().click(await screen.findByRole('button', { name: CONFIRM_EMAIL_LABEL }));

    await screen.findByText(EMAIL_CONFIRMED_NOTE);
    expect(authStore.getSnapshot()).toEqual({ status: 'authenticated' });
    const [post] = callsTo(fetch, 'POST', '/api/auth/registration/confirm');
    expect(post?.authorization).toBeNull();
    expect(JSON.stringify(post)).not.toContain(tokenFor(USER_A));
  });
});

describe('/confirm-email: failures (each distinct from pending and from each other)', () => {
  it('400 link_invalid: the expired-or-used sentence with both ways forward', async () => {
    stubAccountFetch({ [CONFIRM]: refusal(400, 'link_invalid') });
    mountWithToken();

    await userEvent.setup().click(await screen.findByRole('button', { name: CONFIRM_EMAIL_LABEL }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(CONFIRM_LINK_INVALID);
    expect(screen.getByRole('link', { name: LOG_IN_LABEL })).toHaveAttribute('href', '/login');
    expect(screen.getByRole('link', { name: CREATE_ACCOUNT_LABEL })).toHaveAttribute(
      'href',
      '/register',
    );
    expect(screen.queryByRole('button', { name: CONFIRMING_LABEL })).not.toBeInTheDocument();
  });

  it('409 email_already_registered: the already-an-account sentence with Log in and Reset your password', async () => {
    stubAccountFetch({ [CONFIRM]: refusal(409, 'email_already_registered') });
    mountWithToken();

    await userEvent.setup().click(await screen.findByRole('button', { name: CONFIRM_EMAIL_LABEL }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(CONFIRM_ALREADY_REGISTERED);
    expect(screen.getByRole('link', { name: LOG_IN_LABEL })).toHaveAttribute('href', '/login');
    expect(screen.getByRole('link', { name: RESET_PASSWORD_LINK_LABEL })).toHaveAttribute(
      'href',
      '/reset-password',
    );
  });

  it.each([
    ['503 service_unavailable', refusal(503, 'service_unavailable')],
    ['a network failure', networkFailure],
  ])(
    '%s: "Couldn\'t confirm just now. Nothing changed", the button returns and re-sends the same token',
    async (_name, handler) => {
      const fetch = stubAccountFetch({ [CONFIRM]: handler });
      mountWithToken();
      const user = userEvent.setup();

      await user.click(await screen.findByRole('button', { name: CONFIRM_EMAIL_LABEL }));

      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent(CONFIRM_UNAVAILABLE);
      // The token is still in memory although the address bar was stripped on mount.
      expect(window.location.hash).toBe('');
      await user.click(screen.getByRole('button', { name: CONFIRM_EMAIL_LABEL }));
      await waitFor(() => {
        expect(callsTo(fetch, 'POST', '/api/auth/registration/confirm')).toHaveLength(2);
      });
      expect(
        callsTo(fetch, 'POST', '/api/auth/registration/confirm').map((call) => call.body),
      ).toEqual([{ token: TOKEN }, { token: TOKEN }]);
    },
  );

  it('the three outcomes read differently from one another', async () => {
    const texts: string[] = [];
    for (const handler of [
      refusal(400, 'link_invalid'),
      refusal(409, 'email_already_registered'),
      refusal(503, 'service_unavailable'),
    ]) {
      stubAccountFetch({ [CONFIRM]: handler });
      const page = mountWithToken();
      await userEvent
        .setup()
        .click(await screen.findByRole('button', { name: CONFIRM_EMAIL_LABEL }));
      texts.push((await screen.findByRole('alert')).textContent);
      page.unmount();
    }

    expect(texts.every((text) => text.length > 0)).toBe(true);
    expect(new Set(texts).size).toBe(3);
  });
});
