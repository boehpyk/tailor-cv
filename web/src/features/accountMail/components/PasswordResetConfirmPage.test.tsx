import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AUTH_CHANNEL_NAME, authStore } from '@/features/auth/authStore';
import { PASSWORD_HINT } from '@/features/auth/authCopy';
import { USER_A, callsTo, signInAs, stubAccountFetch, tokenFor } from '@/test/accountFetch';

import {
  LINK_INCOMPLETE,
  LOG_IN_LABEL,
  PASSWORD_CHANGED_NOTE,
  RESET_CONFIRM_HEADING,
  RESET_CONFIRM_PENDING_LABEL,
  RESET_CONFIRM_SUBMIT_LABEL,
  RESET_LINK_INVALID,
  SEND_NEW_LINK_LABEL,
} from '../accountMailCopy';
import {
  PASSWORD,
  TOKEN,
  activateTwiceInOneTick,
  hangForever,
  networkFailure,
  newClient,
  noContent,
  openLink,
  refusal,
  renderPage,
  resetAccountMailTest,
} from '../test/support';
import { PasswordResetConfirmPage } from './PasswordResetConfirmPage';

/**
 * T37 RED — `/reset-password/confirm` (AC-48, V-54, V-65, V-66). The skeleton renders `null`.
 *
 * The signed-in cases drive the **real** store and a **real** `BroadcastChannel` pair ("two stores,
 * one channel", 2.2's pattern): the page's store is connected to one channel, a second channel of
 * the same name stands for another tab and hears what this tab broadcasts.
 */

const PATH = '/reset-password/confirm';
const CONFIRM = 'POST /api/auth/password-reset/confirm';

function mountWithToken(
  options: { readonly strict?: boolean; readonly client?: ReturnType<typeof newClient> } = {},
) {
  openLink(PATH, TOKEN);
  return renderPage(<PasswordResetConfirmPage />, { routePath: PATH, ...options });
}

async function submitPassword(password = PASSWORD): Promise<void> {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText(/new password/i), password);
  await user.click(screen.getByRole('button', { name: RESET_CONFIRM_SUBMIT_LABEL }));
}

beforeEach(() => {
  resetAccountMailTest();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetAccountMailTest();
});

describe('/reset-password/confirm: empty', () => {
  it('no token: the incomplete-link sentence, no form, no request', async () => {
    const fetch = stubAccountFetch({});
    openLink(PATH);

    renderPage(<PasswordResetConfirmPage />, { routePath: PATH });

    expect(await screen.findByText(LINK_INCOMPLETE)).toBeInTheDocument();
    expect(screen.queryByLabelText(/new password/i)).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: RESET_CONFIRM_SUBMIT_LABEL }),
    ).not.toBeInTheDocument();
    expect(fetch.calls).toHaveLength(0);
  });
});

describe('/reset-password/confirm: idle', () => {
  it("a heading, a labelled new-password field with 2.1's hint, and Save new password", async () => {
    stubAccountFetch({});

    mountWithToken();

    expect(await screen.findByRole('heading', { name: RESET_CONFIRM_HEADING })).toBeInTheDocument();
    const field = screen.getByLabelText(/new password/i);
    expect(field).toHaveAttribute('type', 'password');
    expect(field).toHaveAttribute('autocomplete', 'new-password');
    expect(screen.getByText(PASSWORD_HINT)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: RESET_CONFIRM_SUBMIT_LABEL })).toBeEnabled();
  });

  it('strips the fragment on mount; under <StrictMode> once, and the token is still posted', async () => {
    const fetch = stubAccountFetch({ [CONFIRM]: noContent });
    openLink(PATH, TOKEN);
    const replaceState = vi.spyOn(window.history, 'replaceState');

    renderPage(<PasswordResetConfirmPage />, { routePath: PATH, strict: true });
    await submitPassword();

    await screen.findByText(PASSWORD_CHANGED_NOTE);
    expect(replaceState).toHaveBeenCalledTimes(1);
    expect(replaceState).toHaveBeenCalledWith(null, '', PATH);
    expect(window.location.hash).toBe('');
    expect(callsTo(fetch, 'POST', '/api/auth/password-reset/confirm').map((c) => c.body)).toEqual([
      { token: TOKEN, password: PASSWORD },
    ]);
  });
});

describe('/reset-password/confirm: pending', () => {
  it('says "Saving your new password…", disables the field and button, no error', async () => {
    stubAccountFetch({ [CONFIRM]: hangForever });
    mountWithToken();

    await submitPassword();

    expect(await screen.findByRole('button', { name: RESET_CONFIRM_PENDING_LABEL })).toBeDisabled();
    expect(screen.getByLabelText(/new password/i)).toBeDisabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('a same-tick double click posts once (V-60, isMutating)', async () => {
    const fetch = stubAccountFetch({ [CONFIRM]: hangForever });
    mountWithToken();
    await userEvent.setup().type(await screen.findByLabelText(/new password/i), PASSWORD);

    activateTwiceInOneTick(screen.getByRole('button', { name: RESET_CONFIRM_SUBMIT_LABEL }));

    await screen.findByRole('button', { name: RESET_CONFIRM_PENDING_LABEL });
    expect(callsTo(fetch, 'POST', '/api/auth/password-reset/confirm')).toHaveLength(1);
  });
});

describe('/reset-password/confirm: success', () => {
  it('204 in an anonymous tab: the status sentence and a Log in link to /login', async () => {
    stubAccountFetch({ [CONFIRM]: noContent });
    mountWithToken();

    await submitPassword();

    expect(await screen.findByRole('status')).toHaveTextContent(PASSWORD_CHANGED_NOTE);
    expect(screen.getByRole('link', { name: LOG_IN_LABEL })).toHaveAttribute('href', '/login');
  });

  it("204 in a signed-in tab: this tab becomes anonymous 'password_changed', with no refresh or logout call", async () => {
    const fetch = stubAccountFetch({ [CONFIRM]: noContent });
    signInAs(USER_A);
    const page = mountWithToken();

    await submitPassword();

    await screen.findByText(PASSWORD_CHANGED_NOTE);
    expect(authStore.getSnapshot()).toEqual({ status: 'anonymous', reason: 'password_changed' });
    expect(fetch.calls.map((call) => `${call.method} ${call.path}`)).toEqual([
      'POST /api/auth/password-reset/confirm',
    ]);
    // The page stays: a reset is not a navigation.
    expect(page.location()).toBe(PATH);
    // No bearer on the confirm: it proves nothing and must not leak (V-65).
    expect(callsTo(fetch, 'POST', '/api/auth/password-reset/confirm')[0]?.authorization).toBeNull();
    expect(JSON.stringify(fetch.calls)).not.toContain(tokenFor(USER_A));
  });

  it('204 in a signed-in tab broadcasts a signed-out message so other tabs follow (2.2)', async () => {
    stubAccountFetch({ [CONFIRM]: noContent });
    signInAs(USER_A);
    const otherTab = new BroadcastChannel(AUTH_CHANNEL_NAME);
    const received: unknown[] = [];
    otherTab.onmessage = (event: MessageEvent) => received.push(event.data);
    const thisTab = new BroadcastChannel(AUTH_CHANNEL_NAME);
    authStore.connectChannel(thisTab, () => undefined);
    mountWithToken();

    await submitPassword();

    await waitFor(() => {
      expect(received).toContainEqual({ type: 'signed-out' });
    });
    expect(
      received.filter((message) => (message as { type: string }).type === 'signed-out'),
    ).toHaveLength(1);
    otherTab.close();
    thisTab.close();
  });

  it("204 in a signed-in tab drops the account's ['auth', …] queries and keeps the guest's", async () => {
    stubAccountFetch({ [CONFIRM]: noContent });
    signInAs(USER_A);
    const client = newClient();
    client.setQueryData(['auth', 'me'], USER_A);
    client.setQueryData(['base-cvs'], [{ id: 'guest-cv-1' }]);
    mountWithToken({ client });

    await submitPassword();

    await screen.findByText(PASSWORD_CHANGED_NOTE);
    expect(client.getQueryData(['auth', 'me'])).toBeUndefined();
    expect(client.getQueryData(['base-cvs'])).toEqual([{ id: 'guest-cv-1' }]);
  });
});

describe('/reset-password/confirm: failures', () => {
  it('400 link_invalid: the expired-or-used sentence and Send a new link → /reset-password; a signed-in tab stays signed in', async () => {
    stubAccountFetch({ [CONFIRM]: refusal(400, 'link_invalid') });
    signInAs(USER_A);
    mountWithToken();

    await submitPassword();

    expect(await screen.findByRole('alert')).toHaveTextContent(RESET_LINK_INVALID);
    expect(screen.getByRole('link', { name: SEND_NEW_LINK_LABEL })).toHaveAttribute(
      'href',
      '/reset-password',
    );
    expect(authStore.getSnapshot()).toEqual({ status: 'authenticated' });
  });

  it("422 password_too_short: the server's bound, the form stays and the same token is re-sent", async () => {
    const fetch = stubAccountFetch({
      [CONFIRM]: refusal(422, 'password_too_short', { min_length: 13 }),
    });
    mountWithToken();
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/new password/i), 'short');
    await user.click(screen.getByRole('button', { name: RESET_CONFIRM_SUBMIT_LABEL }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Use at least 13 characters.');
    expect(screen.getByLabelText(/new password/i)).toBeEnabled();
    await user.click(screen.getByRole('button', { name: RESET_CONFIRM_SUBMIT_LABEL }));
    await waitFor(() => {
      expect(callsTo(fetch, 'POST', '/api/auth/password-reset/confirm')).toHaveLength(2);
    });
    expect(
      callsTo(fetch, 'POST', '/api/auth/password-reset/confirm').map(
        (call) => (call.body as { token: string }).token,
      ),
    ).toEqual([TOKEN, TOKEN]);
    expect(screen.queryByText(LINK_INCOMPLETE)).not.toBeInTheDocument();
  });

  it.each([
    ['503 service_unavailable', refusal(503, 'service_unavailable')],
    ['a network failure', networkFailure],
  ])(
    '%s: an alert distinct from pending, and the button returns with the token kept',
    async (_name, handler) => {
      const fetch = stubAccountFetch({ [CONFIRM]: handler });
      mountWithToken();
      const user = userEvent.setup();

      await submitPassword();

      const alert = await screen.findByRole('alert');
      expect(alert.textContent).not.toBe('');
      expect(alert).not.toHaveTextContent(RESET_LINK_INVALID);
      expect(
        screen.queryByRole('button', { name: RESET_CONFIRM_PENDING_LABEL }),
      ).not.toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: RESET_CONFIRM_SUBMIT_LABEL }));
      await waitFor(() => {
        expect(callsTo(fetch, 'POST', '/api/auth/password-reset/confirm')).toHaveLength(2);
      });
    },
  );
});
