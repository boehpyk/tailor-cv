import { act, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { callsTo, stubAccountFetch } from '@/test/accountFetch';

import {
  CONFIRM_EMAIL_LABEL,
  CONFIRM_LINK_INVALID,
  EMAIL_CONFIRMED_NOTE,
  RESET_CONFIRM_SUBMIT_LABEL,
  RESET_LINK_INVALID,
} from './accountMailCopy';
import { ConfirmEmailPage } from './components/ConfirmEmailPage';
import { PasswordResetConfirmPage } from './components/PasswordResetConfirmPage';
import {
  PASSWORD,
  TOKEN,
  noContent,
  openLink,
  refusal,
  renderPage,
  resetAccountMailTest,
} from './test/support';

/**
 * /verify r1, found in a real browser: a **same-document** navigation to a second link.
 *
 * Open `/confirm-email#token=A` (a superseded link), confirm, read "expired or already used" and see
 * the fragment stripped. In the **same tab** go to `/confirm-email#token=B`, a valid link. Nothing
 * remounts — only the hash changes — so the page kept the old outcome and no button, and token B
 * stayed in the address bar. AC-46/AC-48/AC-51's "read once and stripped" must hold for every link
 * the page is handed, not only the first one it mounts with.
 *
 * A same-document navigation reaches the page as `hashchange`; jsdom fires the same event when
 * `location.hash` is assigned, which is what the address bar does. Both screens share
 * `useFragmentToken`, so both are exercised.
 *
 * **Absence is paired with a positive** (the app rewrote the address bar for each link), and the
 * `replaceState` spy is cleared after the harness's own staging so it judges only the app.
 */

const SECOND_TOKEN = 'TOKENMARKER-second-91be4d07';

const CONFIRM_PATH = '/confirm-email';
const CONFIRM = 'POST /api/auth/registration/confirm';
const RESET_PATH = '/reset-password/confirm';
const RESET = 'POST /api/auth/password-reset/confirm';

/** The address bar going to a second link in the same document (no reload, no remount). */
async function followLinkInSameDocument(token: string): Promise<void> {
  await act(async () => {
    window.location.hash = `#token=${token}`;
    // `hashchange` is asynchronous in jsdom, as in a browser: let it be delivered inside the act.
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

/** Every URL the app (not the harness) wrote with `replaceState` since the spy was last cleared. */
function appUrls(spy: ReturnType<typeof vi.spyOn>): string[] {
  return spy.mock.calls.map((call) => String(call[2]));
}

beforeEach(() => {
  resetAccountMailTest();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetAccountMailTest();
});

/** `/confirm-email#token=A` where A is superseded: confirmed, refused, outcome on screen. */
async function openSupersededConfirmLink() {
  const fetch = stubAccountFetch({
    [CONFIRM]: (_call, callNumber) =>
      callNumber === 1 ? refusal(400, 'link_invalid')() : noContent(),
  });
  openLink(CONFIRM_PATH, TOKEN);
  renderPage(<ConfirmEmailPage />, { routePath: CONFIRM_PATH });
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: CONFIRM_EMAIL_LABEL }));
  expect(await screen.findByRole('alert')).toHaveTextContent(CONFIRM_LINK_INVALID);
  return { fetch, user };
}

/** The same for `/reset-password/confirm`. */
async function openSupersededResetLink() {
  const fetch = stubAccountFetch({
    [RESET]: (_call, callNumber) =>
      callNumber === 1 ? refusal(400, 'link_invalid')() : noContent(),
  });
  openLink(RESET_PATH, TOKEN);
  renderPage(<PasswordResetConfirmPage />, { routePath: RESET_PATH });
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText(/new password/i), PASSWORD);
  await user.click(screen.getByRole('button', { name: RESET_CONFIRM_SUBMIT_LABEL }));
  expect(await screen.findByRole('alert')).toHaveTextContent(RESET_LINK_INVALID);
  return { fetch, user };
}

describe('/confirm-email: a second link in the same document', () => {
  it('strips the second link from the address bar (AC-46, AC-51)', async () => {
    await openSupersededConfirmLink();
    const replaceState = vi.spyOn(window.history, 'replaceState');

    await followLinkInSameDocument(SECOND_TOKEN);

    expect(window.location.hash).toBe('');
    expect(window.location.href).not.toContain(SECOND_TOKEN);
    // Positive control, and the app's own writes never carry a token.
    expect(appUrls(replaceState).length).toBeGreaterThanOrEqual(1);
    expect(appUrls(replaceState).join('\n')).not.toContain(SECOND_TOKEN);
  });

  it('goes back to the ready state: the old outcome is gone and the button is offered', async () => {
    await openSupersededConfirmLink();

    await followLinkInSameDocument(SECOND_TOKEN);

    expect(await screen.findByRole('button', { name: CONFIRM_EMAIL_LABEL })).toBeEnabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('confirms with the second token, not the first', async () => {
    const { fetch, user } = await openSupersededConfirmLink();

    await followLinkInSameDocument(SECOND_TOKEN);
    await user.click(await screen.findByRole('button', { name: CONFIRM_EMAIL_LABEL }));

    await screen.findByText(EMAIL_CONFIRMED_NOTE);
    expect(callsTo(fetch, 'POST', '/api/auth/registration/confirm').map((c) => c.body)).toEqual([
      { token: TOKEN },
      { token: SECOND_TOKEN },
    ]);
  });
});

describe('/reset-password/confirm: a second link in the same document', () => {
  it('strips the second link from the address bar (AC-48, AC-51)', async () => {
    await openSupersededResetLink();
    const replaceState = vi.spyOn(window.history, 'replaceState');

    await followLinkInSameDocument(SECOND_TOKEN);

    expect(window.location.hash).toBe('');
    expect(window.location.href).not.toContain(SECOND_TOKEN);
    expect(appUrls(replaceState).length).toBeGreaterThanOrEqual(1);
    expect(appUrls(replaceState).join('\n')).not.toContain(SECOND_TOKEN);
  });

  it('goes back to the ready state: the old outcome is gone and the form is offered', async () => {
    await openSupersededResetLink();

    await followLinkInSameDocument(SECOND_TOKEN);

    expect(await screen.findByLabelText(/new password/i)).toBeEnabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('resets with the second token, not the first', async () => {
    const { fetch, user } = await openSupersededResetLink();

    await followLinkInSameDocument(SECOND_TOKEN);
    await user.type(await screen.findByLabelText(/new password/i), PASSWORD);
    await user.click(screen.getByRole('button', { name: RESET_CONFIRM_SUBMIT_LABEL }));

    await screen.findByRole('status');
    expect(callsTo(fetch, 'POST', '/api/auth/password-reset/confirm').map((c) => c.body)).toEqual([
      { token: TOKEN, password: PASSWORD },
      { token: SECOND_TOKEN, password: PASSWORD },
    ]);
  });
});
