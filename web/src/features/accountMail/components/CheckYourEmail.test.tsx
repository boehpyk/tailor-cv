import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { callsTo, stubAccountFetch } from '@/test/accountFetch';

import {
  ALREADY_CONFIRMED_PROMPT,
  CHECK_EMAIL_HEADING,
  GUEST_WORK_DEADLINE_NOTE,
  MAIL_PROVIDER_SENTENCE,
  SENDING_LABEL,
  SEND_AGAIN_FAILED,
  SEND_AGAIN_LABEL,
  SENT_AGAIN_NOTE,
  USE_DIFFERENT_EMAIL_LABEL,
  checkInboxSentence,
} from '../accountMailCopy';
import {
  EMAIL,
  PASSWORD,
  accepted,
  activateTwiceInOneTick,
  hangForever,
  networkFailure,
  refusal,
  renderPage,
  resetAccountMailTest,
} from '../test/support';
import { CheckYourEmail } from './CheckYourEmail';

import type { CheckYourEmailProps } from './CheckYourEmail';

/**
 * T37 RED — what replaces the register form on a 202 (AC-45, V-6, V-61, V-66), rendered on its own
 * with the props `RegisterPage` will hand it. The skeleton renders `null`.
 */

const REGISTER = 'POST /api/auth/register';
const CREDENTIALS = { email: EMAIL, password: PASSWORD } as const;

function mount(overrides: Partial<CheckYourEmailProps> = {}) {
  const props: CheckYourEmailProps = {
    credentials: CREDENTIALS,
    next: null,
    hasGuestWork: false,
    onUseDifferentEmail: () => undefined,
    ...overrides,
  };
  return renderPage(<CheckYourEmail {...props} />, { routePath: '/register' });
}

function statusText(): string {
  return screen
    .queryAllByRole('status')
    .map((element) => element.textContent)
    .join('\n');
}

function loginLink(): HTMLElement {
  return screen.getByRole('link', { name: /log in/i });
}

beforeEach(() => {
  resetAccountMailTest();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetAccountMailTest();
});

describe('Check your email: idle', () => {
  it('a heading, and a status with the inbox sentence for this address, verbatim', async () => {
    const fetch = stubAccountFetch({});

    mount();

    expect(await screen.findByRole('heading', { name: CHECK_EMAIL_HEADING })).toBeInTheDocument();
    expect(statusText()).toContain(
      "Check your inbox at alex.marker@example.com. We've sent a message to that address — open it to finish. It's valid for 24 hours.",
    );
    expect(statusText()).toContain(MAIL_PROVIDER_SENTENCE);
    // Nothing is sent by merely showing it.
    expect(fetch.calls).toHaveLength(0);
  });

  it('the inbox sentence builder carries the address, and the copy never guesses new-or-existing', async () => {
    expect(checkInboxSentence(EMAIL)).toContain(EMAIL);
    stubAccountFetch({});
    mount();

    await screen.findByRole('status');

    expect(statusText()).not.toMatch(/new account|no account|already (have|registered)/i);
  });

  it('offers Send it again, Use a different email and Already confirmed? Log in', async () => {
    stubAccountFetch({});

    mount();

    expect(await screen.findByRole('button', { name: SEND_AGAIN_LABEL })).toBeEnabled();
    expect(screen.getByRole('button', { name: USE_DIFFERENT_EMAIL_LABEL })).toBeEnabled();
    expect(screen.getByText(ALREADY_CONFIRMED_PROMPT)).toBeInTheDocument();
    expect(loginLink()).toBeInTheDocument();
  });

  it('shows the 24-hour guest-work deadline only when this browser has guest work (V-6)', async () => {
    stubAccountFetch({});
    const withWork = mount({ hasGuestWork: true });
    await screen.findByRole('heading', { name: CHECK_EMAIL_HEADING });
    expect(screen.getByText(GUEST_WORK_DEADLINE_NOTE)).toBeInTheDocument();
    withWork.unmount();

    mount({ hasGuestWork: false });
    await screen.findByRole('heading', { name: CHECK_EMAIL_HEADING });
    expect(screen.queryByText(GUEST_WORK_DEADLINE_NOTE)).not.toBeInTheDocument();
  });
});

describe('Check your email: Already confirmed? Log in', () => {
  it('with no next it is exactly /login', async () => {
    stubAccountFetch({});
    mount({ next: null });

    await screen.findByRole('heading', { name: CHECK_EMAIL_HEADING });

    expect(loginLink()).toHaveAttribute('href', '/login');
  });

  it('carries next to /login?next=… and leaves safeNext to judge it there (AC-50)', async () => {
    stubAccountFetch({});
    mount({ next: '/runs/run-1/cv' });

    await screen.findByRole('heading', { name: CHECK_EMAIL_HEADING });

    const href = loginLink().getAttribute('href') ?? '';
    const url = new URL(href, 'http://app.test');
    expect(url.pathname).toBe('/login');
    expect(url.searchParams.get('next')).toBe('/runs/run-1/cv');
  });

  it('an unsafe next is carried verbatim, never rewritten here', async () => {
    stubAccountFetch({});
    mount({ next: '//evil.example' });

    await screen.findByRole('heading', { name: CHECK_EMAIL_HEADING });

    const url = new URL(loginLink().getAttribute('href') ?? '', 'http://app.test');
    expect(url.pathname).toBe('/login');
    expect(url.searchParams.get('next')).toBe('//evil.example');
  });
});

describe('Check your email: Use a different email', () => {
  it('calls onUseDifferentEmail once and sends nothing', async () => {
    const fetch = stubAccountFetch({});
    const onUseDifferentEmail = vi.fn();
    mount({ onUseDifferentEmail });

    await userEvent
      .setup()
      .click(await screen.findByRole('button', { name: USE_DIFFERENT_EMAIL_LABEL }));

    expect(onUseDifferentEmail).toHaveBeenCalledTimes(1);
    expect(fetch.calls).toHaveLength(0);
  });
});

describe('Check your email: Send it again', () => {
  it('re-posts exactly the held credentials', async () => {
    const fetch = stubAccountFetch({ [REGISTER]: accepted });
    mount();

    await userEvent.setup().click(await screen.findByRole('button', { name: SEND_AGAIN_LABEL }));

    await screen.findByText(SENT_AGAIN_NOTE);
    expect(callsTo(fetch, 'POST', '/api/auth/register').map((call) => call.body)).toEqual([
      CREDENTIALS,
    ]);
  });

  it('pending: "Sending…", disabled, and no outcome yet', async () => {
    stubAccountFetch({ [REGISTER]: hangForever });
    mount();

    await userEvent.setup().click(await screen.findByRole('button', { name: SEND_AGAIN_LABEL }));

    expect(await screen.findByRole('button', { name: SENDING_LABEL })).toBeDisabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByText(SENT_AGAIN_NOTE)).not.toBeInTheDocument();
  });

  it('a same-tick double click posts once (V-60, isMutating)', async () => {
    const fetch = stubAccountFetch({ [REGISTER]: hangForever });
    mount();
    const button = await screen.findByRole('button', { name: SEND_AGAIN_LABEL });

    activateTwiceInOneTick(button);

    await screen.findByRole('button', { name: SENDING_LABEL });
    expect(callsTo(fetch, 'POST', '/api/auth/register')).toHaveLength(1);
  });

  it('success: "Sent again." as a status, and the button is available again', async () => {
    stubAccountFetch({ [REGISTER]: accepted });
    mount();

    await userEvent.setup().click(await screen.findByRole('button', { name: SEND_AGAIN_LABEL }));

    const sent = await screen.findByText(SENT_AGAIN_NOTE);
    expect(sent.closest('[role="status"]')).not.toBeNull();
    expect(screen.getByRole('button', { name: SEND_AGAIN_LABEL })).toBeEnabled();
  });

  it("429: an alert built from the server's Retry-After, and the button is held (V-61, AC-15)", async () => {
    stubAccountFetch({ [REGISTER]: refusal(429, 'rate_limited', {}, { 'Retry-After': '120' }) });
    mount();

    await userEvent.setup().click(await screen.findByRole('button', { name: SEND_AGAIN_LABEL }));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).not.toBe('');
    expect(alert).not.toHaveTextContent(SEND_AGAIN_FAILED);
    expect(screen.queryByText(SENT_AGAIN_NOTE)).not.toBeInTheDocument();
    // AC-15 supersedes "then the button is re-enabled": a 429 now holds Send it again until the
    // time it names (proved, with release, in `retry/accountHolds.test.tsx`).
    expect(screen.getByRole('button', { name: SEND_AGAIN_LABEL })).toBeDisabled();
  });

  it.each([
    ['503 service_unavailable', refusal(503, 'service_unavailable')],
    ['a network failure', networkFailure],
  ])(
    '%s: "Couldn\'t send just now — try again." and the button returns (V-66)',
    async (_name, handler) => {
      stubAccountFetch({ [REGISTER]: handler });
      mount();

      await userEvent.setup().click(await screen.findByRole('button', { name: SEND_AGAIN_LABEL }));

      expect(await screen.findByRole('alert')).toHaveTextContent(SEND_AGAIN_FAILED);
      expect(screen.queryByText(SENT_AGAIN_NOTE)).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: SEND_AGAIN_LABEL })).toBeEnabled();
    },
  );

  it("a second Send it again after a failure posts the credentials again (supersede is the server's)", async () => {
    const fetch = stubAccountFetch({ [REGISTER]: refusal(503, 'service_unavailable') });
    mount();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: SEND_AGAIN_LABEL }));
    await screen.findByRole('alert');
    await user.click(screen.getByRole('button', { name: SEND_AGAIN_LABEL }));

    await vi.waitFor(() => {
      expect(callsTo(fetch, 'POST', '/api/auth/register')).toHaveLength(2);
    });
    expect(within(document.body).queryAllByRole('alert').length).toBeLessThanOrEqual(1);
  });
});
