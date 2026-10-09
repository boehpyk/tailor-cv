import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { callsTo, stubAccountFetch } from '@/test/accountFetch';

import {
  MAIL_PROVIDER_SENTENCE,
  RESET_REQUEST_HEADING,
  RESET_REQUEST_SUBMIT_LABEL,
  SENDING_LABEL,
  resetRequestedSentence,
} from '../accountMailCopy';
import {
  EMAIL,
  accepted,
  activateTwiceInOneTick,
  hangForever,
  networkFailure,
  refusal,
  renderPage,
  resetAccountMailTest,
} from '../test/support';
import { PasswordResetRequestPage } from './PasswordResetRequestPage';

/**
 * T37 RED — `/reset-password` (AC-47, V-60, V-66). The skeleton renders `null`, so each test fails
 * on a missing role or text.
 */

const PATH = '/reset-password';
const REQUEST = 'POST /api/auth/password-reset';

function mount() {
  return renderPage(<PasswordResetRequestPage />, { routePath: PATH });
}

async function submitEmail(address = EMAIL): Promise<void> {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText(/email/i), address);
  await user.click(screen.getByRole('button', { name: RESET_REQUEST_SUBMIT_LABEL }));
}

beforeEach(() => {
  resetAccountMailTest();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetAccountMailTest();
});

describe('/reset-password: idle', () => {
  it('shows the heading, a labelled email field and an enabled Send reset link button', async () => {
    const fetch = stubAccountFetch({});

    mount();

    expect(await screen.findByRole('heading', { name: RESET_REQUEST_HEADING })).toBeInTheDocument();
    const email = screen.getByLabelText(/email/i);
    expect(email).toHaveAttribute('type', 'email');
    expect(email).toHaveAttribute('autocomplete', 'email');
    expect(screen.getByRole('button', { name: RESET_REQUEST_SUBMIT_LABEL })).toBeEnabled();
    expect(fetch.calls).toHaveLength(0);
  });
});

describe('/reset-password: pending', () => {
  it('says "Sending…", disables the field and the button, and shows no outcome yet', async () => {
    stubAccountFetch({ [REQUEST]: hangForever });
    mount();

    await submitEmail();

    expect(await screen.findByRole('button', { name: SENDING_LABEL })).toBeDisabled();
    expect(screen.getByLabelText(/email/i)).toBeDisabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('a same-tick double click posts once (V-60, isMutating)', async () => {
    const fetch = stubAccountFetch({ [REQUEST]: hangForever });
    mount();
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/email/i), EMAIL);

    activateTwiceInOneTick(screen.getByRole('button', { name: RESET_REQUEST_SUBMIT_LABEL }));

    await screen.findByRole('button', { name: SENDING_LABEL });
    expect(callsTo(fetch, 'POST', '/api/auth/password-reset')).toHaveLength(1);
  });
});

describe('/reset-password: success', () => {
  it('posts exactly {email} and answers with the neutral sentence and the mail-provider sentence', async () => {
    const fetch = stubAccountFetch({ [REQUEST]: accepted });
    mount();

    await submitEmail();

    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent(
      "If there's an account for alex.marker@example.com, we've sent a link to reset its password. It's valid for 60 minutes.",
    );
    expect(status).toHaveTextContent(MAIL_PROVIDER_SENTENCE);
    expect(callsTo(fetch, 'POST', '/api/auth/password-reset').map((call) => call.body)).toEqual([
      { email: EMAIL },
    ]);
  });

  it('the copy builder carries the address and never says whether an account exists', async () => {
    expect(resetRequestedSentence(EMAIL)).toContain(EMAIL);
    expect(resetRequestedSentence(EMAIL)).toMatch(/^If there's an account for /);
    stubAccountFetch({ [REQUEST]: accepted });
    mount();

    await submitEmail();

    const status = await screen.findByRole('status');
    expect(status.textContent).not.toMatch(/no account|not registered|new account|doesn't exist/i);
  });
});

describe('/reset-password: failures (each distinct)', () => {
  it('422 invalid_email, 429, 503 and a network failure all render a role="alert" with different text', async () => {
    // `held`: AC-15 supersedes "the button is enabled after every outcome" for the 429, which
    // now holds the submit until the time it names (proved with release in
    // `retry/accountHolds.test.tsx`).
    const outcomes: Array<[string, () => Response | Promise<Response>, boolean]> = [
      ['422', refusal(422, 'invalid_email'), false],
      ['429', refusal(429, 'rate_limited', {}, { 'Retry-After': '120' }), true],
      ['503', refusal(503, 'service_unavailable'), false],
      ['network', networkFailure, false],
    ];
    const texts: string[] = [];
    for (const [, handler, held] of outcomes) {
      stubAccountFetch({ [REQUEST]: handler });
      const page = mount();
      await submitEmail('not-an-address');
      const alert = await screen.findByRole('alert');
      texts.push(alert.textContent);
      // Nothing is *said* as a status: a 429's hold mounts its release region empty (so the
      // release is announced as a change), and an empty live region announces nothing.
      expect(screen.queryAllByRole('status').filter((s) => s.textContent !== '')).toHaveLength(0);
      const submit = screen.getByRole('button', { name: RESET_REQUEST_SUBMIT_LABEL });
      if (held) {
        expect(submit).toBeDisabled();
      } else {
        expect(submit).toBeEnabled();
      }
      page.unmount();
    }

    expect(texts.every((text) => text.length > 0)).toBe(true);
    expect(new Set(texts).size).toBe(4);
  });

  it("429 is built from the server's Retry-After, not a fixed sentence", async () => {
    const texts: string[] = [];
    for (const seconds of ['120', '7200']) {
      stubAccountFetch({
        [REQUEST]: refusal(429, 'rate_limited', {}, { 'Retry-After': seconds }),
      });
      const page = mount();
      await submitEmail();
      texts.push((await screen.findByRole('alert')).textContent);
      page.unmount();
    }

    expect(texts[0]).not.toBe('');
    expect(texts[0]).not.toBe(texts[1]);
  });
});
